"""Only the direct AIQ SQL collector supplies retained field observations.

Container metadata, candidate columns and spreadsheet rows are not trusted output
records. No current collector supplies origin, stage or image certificates.
"""
from app.models.system_output import OutputProofResponse, OutputValue
from app.services.proof_serializer import unknown
from app.services.decision_serializer import normalized_analysis

FIELDS = ('truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type')
COLUMNS = {
    'truck_number': ('vehicle_unit_number_value', 'vehicle_unit_number_source'),
    'trailer_number_1': ('trailer_trailer_number_value', 'trailer_trailer_number_source'),
    'trailer_number_2': ('trailer_trailer_number_two_value', 'trailer_trailer_number_two_source'),
    'vehicle_type': ('vehicle_transport_type_value', 'vehicle_transport_type_source'),
}
LIMITATIONS = {
    'OUTPUT_RECORD_IMMUTABILITY_UNKNOWN': 'AIQ field rows may be updated; no immutable output revision is acquired.',
    'OUTPUT_ORIGIN_UNKNOWN': 'Field attribution to Alisa does not establish automatic event origin.',
    'OUTPUT_EVENT_LINK_UNKNOWN': 'No validated native AIQ transaction/UUID association is available.',
    'OUTPUT_ARTIFACT_UNKNOWN': 'No authenticated retained output artifact is acquired.',
    'OUTPUT_ARTIFACT_LINK_UNKNOWN': 'No source event-to-artifact relation is acquired.',
    'OUTPUT_DIGEST_UNAVAILABLE': 'No trusted artifact bytes or authoritative digest are acquired.',
    'OUTPUT_FIELD_UNKNOWN': 'No exact source-attributed field value is acquired.',
    'OUTPUT_NORMALIZATION_UNAVAILABLE': 'No versioned output normalization is applied.',
    'OUTPUT_GENERATION_STATE_UNKNOWN': 'A persisted field does not certify event generation.',
    'OUTPUT_RECEIPT_STATE_UNKNOWN': 'A persisted field does not certify business receipt.',
    'OUTPUT_SAVE_STATE_UNKNOWN': 'A persisted field does not certify automatic event persistence.',
    'OUTPUT_TIMESTAMP_UNKNOWN': 'No authoritative stage or artifact capture timestamp is acquired.',
}


def uncertain(code, **extra):
    return dict(OutputValue(reason_codes=[code]).model_dump(), **extra)


def outputs_for(transaction):
    # This collector-owned namespace is separate from raw container payloads.
    record = transaction._retained_aiq_fields
    native = record.get('transaction_id')
    uuid = record.get('uuid')
    linked = (record.get('collector') == 'aiq-postgresql-fields.v1'
        and transaction.ground_truth_source.value == 'AIQ'
        and isinstance(native, str) and bool(native)
        and native == transaction.ground_truth_transaction_id
        and record.get('yard_id') == transaction.yard_id)
    outputs = []
    for name in FIELDS:
        reasons = set(LIMITATIONS)
        evidence = dict(table='public.truck_transaction_automation', source_record_id=native,
                        field=name, value_column=COLUMNS[name][0], attribution_column=COLUMNS[name][1],
                        join='truck_transaction.id = truck_transaction_automation.id',
                        collector='aiq-postgresql-fields.v1') if linked else {}
        source_field = record.get('fields', {}).get(name, {}) if linked else {}
        raw = source_field.get('raw_value')
        available = source_field.get('attribution') == 'alisa' and isinstance(raw, str)
        if available:
            reasons.remove('OUTPUT_FIELD_UNKNOWN')
        if linked:
            reasons.remove('OUTPUT_EVENT_LINK_UNKNOWN')
        def known(value):
            return OutputValue(value=value, status='known', source='AIQ_POSTGRESQL', evidence=evidence).model_dump()
        timestamps = {kind: uncertain('OUTPUT_TIMESTAMP_UNKNOWN', kind=kind)
            for kind in ('generated_at', 'received_at', 'saved_at', 'artifact_capture_at')}
        artifact = {key: uncertain('OUTPUT_ARTIFACT_UNKNOWN') for key in
            ('artifact_id', 'source_system', 'source_record_id', 'event_uuid',
             'immutable_reference', 'camera', 'acquisition_provenance')}
        artifact.update(content_digest=uncertain('OUTPUT_DIGEST_UNAVAILABLE'), digest_algorithm=None,
            capture_time=timestamps['artifact_capture_at'],
            event_link_verified=unknown('OUTPUT_ARTIFACT_LINK_UNKNOWN').model_dump())
        outputs.append(dict(output_id=None, event=dict(
            # This UUID describes the retained AIQ source, not container identity.
            uuid=known(uuid) if linked and uuid else uncertain('OUTPUT_EVENT_LINK_UNKNOWN'),
            transaction_id=known(native) if linked else uncertain('OUTPUT_EVENT_LINK_UNKNOWN'),
            origin=uncertain('OUTPUT_ORIGIN_UNKNOWN'),
            candidate_link_verified=(dict(value=True, status='known', source='AIQ_POSTGRESQL',
                evidence=evidence, reason_codes=[]) if linked else unknown('OUTPUT_EVENT_LINK_UNKNOWN').model_dump())),
            field=dict(name=name, raw_value=known(raw) if available else uncertain('OUTPUT_FIELD_UNKNOWN'),
                normalized_value=uncertain('OUTPUT_NORMALIZATION_UNAVAILABLE'), normalization_version=None),
            state={stage: unknown(code).model_dump() for stage, code in
                (('generated', 'OUTPUT_GENERATION_STATE_UNKNOWN'), ('received', 'OUTPUT_RECEIPT_STATE_UNKNOWN'),
                 ('saved', 'OUTPUT_SAVE_STATE_UNKNOWN'))}, timestamps=timestamps, artifact=artifact,
            provenance=dict(collector='aiq-postgresql-fields.v1' if linked else None,
                acquisition='read-only SQL over TLS' if linked else None,
                field_attribution='alisa' if available else None,
                meaning='Retained source-row field observation; no automatic event or image certification.'),
            reason_codes=sorted(reasons)))
    return outputs


def extend_output_proof(proof, transactions, *, analysis_complete=False):
    transactions = list(transactions)
    data = proof.model_dump(mode='json')
    data.update(schema_version='unified-proof.v2', capabilities=['retained-aiq-field-observation.v1'])
    reasons = set(data['reason_codes'])
    if not data['candidates']:
        reasons.update(LIMITATIONS)
    data['limitations'].extend(LIMITATIONS[code] for code in sorted(LIMITATIONS))
    for candidate, transaction in zip(data['candidates'], transactions, strict=True):
        candidate['system_outputs'] = outputs_for(transaction)
        for output in candidate['system_outputs']:
            reasons.update(output['reason_codes'])
    data['reason_codes'] = sorted(reasons)
    data.update(normalized_analysis(transactions, analysis_complete=analysis_complete))
    return OutputProofResponse.model_validate(data)
