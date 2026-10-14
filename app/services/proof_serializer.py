"""Conservative projection of the existing analysis; no new acquisitions."""
import hashlib
import json
from datetime import timezone

from app.models.proof import (
    CandidateClaim, ProofCandidate, ProofFact, ProofResponse, ScopedAbsence, StageCoverage,
)

STAGES = ('ground_truth', 'generation', 'receipt', 'save')
LIMITATIONS = {
    'MATCH_STATUS_INFORMATIONAL_ONLY': 'Matching correlates records, not physical incident identity or automatic receipt.',
    'COVERAGE_UNKNOWN': 'Providers supply no completeness, retention, truncation or gap certificate for any proof stage.',
    'AUTOMATIC_ORIGIN_UNKNOWN': 'No validated event-level automatic/manual/simulated origin is retained.',
    'GENERATION_STATE_UNKNOWN': 'Aggregated container event types are not validated generation certificates.',
    'RECEIPT_STATE_UNKNOWN': 'No explicit business-receiver event and UUID linkage is retained.',
    'SAVE_STATE_UNKNOWN': 'Ground-truth rows do not establish automatic event save state or a save failure.',
    'GATE_UNVERIFIED': 'Candidate gate fields may be selected from several sources; exact ownership is not certified.',
    'PHYSICAL_INCIDENT_UNVERIFIED': 'Returned records and operator-entered values do not verify a physical incident.',
    'ABSENCE_UNCERTIFIED': 'No certified stage coverage and subject-specific matching rule are available.',
    'AUTOMATIC_EXPECTATION_UNKNOWN': 'No versioned gate eligibility/response policy is supplied.',
    'CONTAINER_ACQUISITION_UNKNOWN': 'The parser does not report missing files, rejected records or read completeness in AnalysisResult.',
    'TIMEZONE_RULES_UNCERTIFIED': 'The service uses configured offsets and North American DST rules, not an IANA-zone certificate.',
}


def unknown(code, source='unified_analysis'):
    return ProofFact(source=source, reason_codes=[code])


def utc_string(value):
    # Never manufacture timezone awareness for proof timestamps.
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def serialize_candidate(transaction):
    raw = transaction.to_dict()
    raw.pop('metadata', None)  # Do not publish raw container payloads or source secrets.
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    candidate_id = 'candidate-' + digest
    evidence = dict(candidate_id=candidate_id, normalized_record_sha256=digest)
    source = raw['ground_truth_source']

    def claim(field):
        # Matcher can fill gate/direction from container metadata; do not attribute
        # mixed fields to AIQ or certify them merely because they are populated.
        return CandidateClaim(value=raw.get(field), source='analysis_pipeline',
                              evidence=dict(evidence, field=field))

    observed = unknown('GROUND_TRUTH_RECORD_UNESTABLISHED')
    if source in ('AIQ', 'ARCHIVE') and raw.get('ground_truth_transaction_id'):
        observed = ProofFact(value=True, status='known', source=source,
            evidence=dict(evidence, record_id=raw['ground_truth_transaction_id'],
                          meaning='Source record observed, not physical incident verification.'))
    reasons = list(LIMITATIONS)
    if not raw.get('uuid'):
        reasons.append('UUID_MISSING')
    if not raw.get('gate_id') or not raw.get('direction'):
        reasons.append('GATE_FIELDS_MISSING')
    if any('AMBIGUOUS' in flag for flag in raw['flags']):
        reasons.append('AMBIGUOUS_MATCH')
    if source == 'ARCHIVE':
        reasons.append('SOURCE_FALLBACK_ARCHIVE')
    utc = utc_string(transaction.timestamp_utc)
    if transaction.timestamp_utc is not None and utc is None:
        reasons.append('TIMESTAMP_TIMEZONE_UNKNOWN')
    identity = {
        'ground_truth': {name: claim('ground_truth_' + name).model_dump()
                        for name in ('truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type')},
        'prediction': {name: claim('alisa_prediction_' + name).model_dump()
                       for name in ('truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type')},
        'verified': unknown('PHYSICAL_INCIDENT_UNVERIFIED').model_dump(),
    }
    return ProofCandidate(candidate_id=candidate_id, uuid=raw.get('uuid'),
        ground_truth_transaction_id=raw.get('ground_truth_transaction_id'), ground_truth_source=source,
        transaction_match_status=raw.get('transaction_match_status'), match_method=raw['match_method'],
        match_time_difference_seconds=raw.get('match_time_difference_seconds'),
        timestamps=dict(utc=dict(value=utc, kind='candidate_reference', source='analysis_pipeline',
                                evidence=dict(evidence, field='timestamp_utc')),
            local=dict(value=raw.get('timestamp_local'), source='analysis_pipeline',
                       evidence=dict(evidence, field='timestamp_local')),
            container_first_utc=claim('container_logs_first_timestamp_utc'),
            container_last_utc=claim('container_logs_last_timestamp_utc')),
        gate=dict(id=claim('gate_id'), name=claim('gate_name'), direction=claim('direction'),
                  verified=unknown('GATE_UNVERIFIED')),
        identity=identity, ground_truth_record_observed=observed,
        physical_incident_verified=unknown('PHYSICAL_INCIDENT_UNVERIFIED'),
        automatic_origin=unknown('AUTOMATIC_ORIGIN_UNKNOWN'), generation=unknown('GENERATION_STATE_UNKNOWN'),
        receipt=unknown('RECEIPT_STATE_UNKNOWN'), save=unknown('SAVE_STATE_UNKNOWN'),
        diagnostics=dict(container_event_types=raw['container_logs_event_types'], flags=raw['flags'],
                         reason_codes=sorted(set(reasons))),
        provenance=dict(evidence, ground_truth_source=source,
                        field_sources='Mixed normalized pipeline values; no field-level source certificate.'),
        raw_candidate=raw)


def build_proof_response(*, request_scope, result=None, transactions=(), failed=False):
    candidates = [serialize_candidate(transaction) for transaction in transactions]
    reasons = set(LIMITATIONS)
    sources = {}
    for name in ('aiq', 'archive'):
        error = getattr(result, name + '_error', None) if result is not None else None
        sources[name] = dict(status='unknown' if result is None else 'failed' if error else 'succeeded',
            evidence=dict(analysis_dates=list(result.dates)) if result is not None else {},
            reason_codes=[name.upper() + '_ACQUISITION_FAILED'] if error else
                         ['SOURCE_ACQUISITION_UNKNOWN'] if result is None else [])
        if error:
            reasons.add(name.upper() + '_ACQUISITION_FAILED')
    sources['container_logs'] = dict(status='unknown', evidence={},
                                    reason_codes=['CONTAINER_ACQUISITION_UNKNOWN'])
    if failed:
        reasons.add('ANALYSIS_FAILED')
    elif not candidates:
        reasons.add('NO_CANDIDATE')
    if len(candidates) > 1:
        reasons.add('MULTIPLE_CANDIDATES')
    if getattr(result, 'fallback_used', False):
        reasons.add('SOURCE_FALLBACK_ARCHIVE')
    for candidate in candidates:
        reasons.update(candidate.diagnostics['reason_codes'])
    coverage, absence = {}, []
    for stage in STAGES:
        coverage[stage] = StageCoverage(requested_start_utc=request_scope['start_utc'],
            requested_end_utc=request_scope['end_utc'], complete=unknown('COVERAGE_UNKNOWN'),
            source='analysis_pipeline', limitations=[LIMITATIONS['COVERAGE_UNKNOWN']])
        absence.append(ScopedAbsence(stage=stage,
            scope=dict(yard_id=request_scope['yard_id'], start_utc=request_scope['start_utc'],
                       end_utc=request_scope['end_utc'], interval='[start,end)',
                       gate_id=None, direction=None), coverage_complete=unknown('COVERAGE_UNKNOWN'),
            absent=unknown('ABSENCE_UNCERTIFIED'), source='unified_analysis'))
    # Returned means the analysis ran; source telemetry is incomplete even if
    # the providers succeeded. This is acquisition partial, not coverage proof.
    return ProofResponse(request=request_scope,
        acquisition=dict(status='failed' if failed else 'partial',
            pipeline_succeeded=ProofFact(value=not failed, status='known', source='analysis_pipeline',
                evidence=dict(outcome='failed' if failed else 'returned')),
            sources=sources, limitations=[LIMITATIONS['CONTAINER_ACQUISITION_UNKNOWN']]),
        coverage=coverage, candidates=candidates, absence=absence,
        facts=dict(automatic_response_expected=unknown('AUTOMATIC_EXPECTATION_UNKNOWN'),
                   physical_incident_verified=unknown('PHYSICAL_INCIDENT_UNVERIFIED')),
        reason_codes=sorted(reasons), limitations=[LIMITATIONS[code] for code in sorted(LIMITATIONS)])
