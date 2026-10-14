"""Offline API/model regression tests; no startup, credentials or real providers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_application_services
from app.api.routes import router
from app.exporters.dataframe_exporter import DataFrameExporter
from app.models.proof import ProofFact
from app.models.unified_transaction import GroundTruthSource, MatchMethod, TransactionMatchStatus, UnifiedTransaction
from app.services.analysis_service import AnalysisResult, AnalysisService
from app.services.proof_serializer import serialize_candidate

UTC = timezone.utc
STAMP = datetime(2026, 10, 2, 12, tzinfo=UTC)
PAYLOAD = dict(yard_id='test', start_local='2026-10-02T07:50:00', end_local='2026-10-02T08:10:00')
UID = '12345678-1234-1234-1234-123456789abc'


def row(status=TransactionMatchStatus.MATCHED, **changes):
    values = dict(yard_id='test', uuid=UID, ground_truth_transaction_id='42',
        ground_truth_source=GroundTruthSource.AIQ, timestamp_utc=STAMP,
        timestamp_local=STAMP.replace(tzinfo=None)-timedelta(hours=4),
        gate_id='1', gate_name='Main', direction='IN', transaction_match_status=status,
        match_method=MatchMethod.UUID, ground_truth_truck_number='123',
        alisa_prediction_truck_number='128', container_logs_event_types=['ALERT'],
        metadata=dict(secret='must-not-be-exported'))
    values.update(changes)
    return UnifiedTransaction(**values)


def result(rows, **changes):
    values = dict(yard_id='test', dates=['2026-10-02'], ground_truth_source='AIQ',
        ground_truth_reason='fake sources', fallback_used=False, transactions=rows)
    values.update(changes)
    return AnalysisResult(**values)


@pytest.fixture
def api(monkeypatch):
    # Mount the production router without production main's startup acquisition.
    # Reject provider construction, sockets and subprocesses if fake wiring fails.
    import socket
    import subprocess
    import app.api.dependencies as deps
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail('Offline proof tests attempted production acquisition or export.')
    monkeypatch.setattr(deps, 'get_application_services', forbidden)
    for name in ('AIQDatabaseProvider', 'GDriveArchiveParser', 'TimezoneService', 'GoogleSheetsExporter'):
        monkeypatch.setattr(deps, name, forbidden)
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    yard = SimpleNamespace(yard_id='test', yard_name='Test', observes_dst=True, timezone_name='Eastern (GMT-5)',
                           standard_utc_offset_hours=-5, dst_utc_offset_hours=-4)
    class FakeTimezone:
        def refresh(self):
            return [yard]
        def resolve_by_id(self, value, refresh=False):
            assert value == 'test'
            return yard
        def local_to_utc(self, value, _yard):
            return (value+timedelta(hours=4)).replace(tzinfo=UTC)
    state = SimpleNamespace(result=result([row()]), calls=[])
    def analyze(request):
        state.calls.append(request)
        if isinstance(state.result, Exception):
            raise state.result
        return state.result
    services = SimpleNamespace(timezone_service=FakeTimezone(),
        analysis_service=SimpleNamespace(analyze=analyze),
        google_sheets_exporter=SimpleNamespace(with_worksheet=forbidden))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_application_services] = lambda: services
    with TestClient(app) as client:
        yield client, state, services


def get_proof(api, rows=None, **changes):
    client, state, _ = api
    if rows is not None:
        state.result = result(rows, **changes)
    response = client.post('/api/analyze/proof', json=PAYLOAD)
    assert response.status_code == 200, response.text
    return response.json()


def assert_unknown(fact):
    assert fact['value'] is None and fact['status'] == 'unknown'
    assert fact['reason_codes']


def test_successful_one_candidate_reuses_exact_csv_scope(api):
    proof = get_proof(api)
    assert proof['schema_version'] == 'unified-proof.v1'
    assert proof['request']['start_utc'] == '2026-10-02T11:50:00+00:00'
    assert proof['request']['end_utc'] == '2026-10-02T12:10:00+00:00'
    assert proof['request']['interval'] == '[start,end)'
    assert proof['request']['iana_timezone'] is None
    assert proof['acquisition']['pipeline_succeeded']['value'] is True
    assert proof['acquisition']['status'] == 'partial'
    assert len(api[1].calls) == 1 and len(proof['candidates']) == 1
    assert api[1].calls[0].dates == [datetime(2026, 10, 2).date()]
    candidate = proof['candidates'][0]
    assert candidate['ground_truth_record_observed']['value'] is True
    assert candidate['ground_truth_record_observed']['evidence']['record_id'] == '42'
    assert candidate['timestamps']['utc']['kind'] == 'candidate_reference'
    assert candidate['identity']['prediction']['truck_number']['value'] == '128'
    assert 'metadata' not in candidate['raw_candidate']
    assert 'must-not-be-exported' not in str(proof)
    assert not any(name in proof for name in ('decision', 'classification', 'resolved', 'confirmed_miss'))


@pytest.mark.parametrize('status', list(TransactionMatchStatus))
def test_match_status_never_establishes_proof(api, status):
    proof = get_proof(api, [row(status)])
    candidate = proof['candidates'][0]
    assert candidate['transaction_match_status'] == status.value
    for name in ('automatic_origin', 'generation', 'receipt', 'save', 'physical_incident_verified'):
        assert_unknown(candidate[name])
    assert_unknown(candidate['gate']['verified'])
    assert 'MATCH_STATUS_INFORMATIONAL_ONLY' in proof['reason_codes']


def test_no_candidate_is_not_global_absence(api):
    proof = get_proof(api, [])
    assert proof['candidates'] == [] and 'NO_CANDIDATE' in proof['reason_codes']
    for absence in proof['absence']:
        assert_unknown(absence['absent'])
        assert absence['subject'] is None and absence['matching_rule_version'] is None
    assert_unknown(proof['facts']['physical_incident_verified'])


def test_multiple_ambiguous_candidates_preserved(api):
    proof = get_proof(api, [row(flags=['AMBIGUOUS_ARCHIVE_CONTAINER_LOG_MATCH']), row(uuid='other')])
    assert len(proof['candidates']) == 2
    assert 'MULTIPLE_CANDIDATES' in proof['reason_codes']
    assert 'AMBIGUOUS_MATCH' in proof['reason_codes']
    assert len({c['candidate_id'] for c in proof['candidates']}) == 2


def test_archive_fallback_and_source_failure_remain_partial(api):
    proof = get_proof(api, [row(ground_truth_source=GroundTruthSource.ARCHIVE)],
        fallback_used=True, aiq_error='password=private')
    assert proof['acquisition']['sources']['aiq']['status'] == 'failed'
    assert proof['acquisition']['sources']['archive']['status'] == 'succeeded'
    assert 'SOURCE_FALLBACK_ARCHIVE' in proof['reason_codes']
    assert 'password' not in str(proof)
    assert proof['candidates'][0]['ground_truth_record_observed']['source'] == 'ARCHIVE'


@pytest.mark.parametrize('field', ['gate_id', 'uuid'])
def test_missing_gate_or_uuid_is_explicit(api, field):
    proof = get_proof(api, [row(**{field: None})])
    candidate = proof['candidates'][0]
    code = 'GATE_FIELDS_MISSING' if field == 'gate_id' else 'UUID_MISSING'
    assert code in proof['reason_codes']
    if field == 'gate_id':
        assert candidate['gate']['id']['value'] is None
    else:
        assert candidate['uuid'] is None
    assert_unknown(candidate['gate']['verified'])


@pytest.mark.parametrize('hint', ['automatic', 'manual', 'simulated'])
def test_unvalidated_origin_hints_are_not_certificates(api, hint):
    proof = get_proof(api, [row(metadata=dict(source=hint))])
    assert_unknown(proof['candidates'][0]['automatic_origin'])


def test_unsupported_coverage_and_save_never_become_absence(api):
    proof = get_proof(api)
    assert_unknown(proof['candidates'][0]['save'])
    for name, coverage in proof['coverage'].items():
        assert_unknown(coverage['complete'])
        assert coverage['requested_start_utc'] == proof['request']['start_utc']
        assert coverage['covered_start_utc'] is coverage['covered_end_utc'] is coverage['gaps'] is None
        absence = next(a for a in proof['absence'] if a['stage'] == name)
        assert_unknown(absence['absent'])
        assert_unknown(absence['coverage_complete'])
        assert absence['scope']['yard_id'] == 'test'


def test_container_only_does_not_prove_ground_truth_absence(api):
    proof = get_proof(api, [row(ground_truth_source=GroundTruthSource.NONE, ground_truth_transaction_id=None)])
    assert_unknown(proof['candidates'][0]['ground_truth_record_observed'])


def test_failure_has_versioned_body_without_provider_secrets(api):
    api[1].result = RuntimeError('private-database-password')
    response = api[0].post('/api/analyze/proof', json=PAYLOAD)
    assert response.status_code == 502
    proof = response.json()
    assert proof['schema_version'] == 'unified-proof.v1'
    assert proof['acquisition']['status'] == 'failed'
    assert proof['acquisition']['pipeline_succeeded']['value'] is False
    assert 'NO_CANDIDATE' not in proof['reason_codes']
    assert 'private-database-password' not in response.text
    for absence in proof['absence']:
        assert_unknown(absence['absent'])


@pytest.mark.parametrize('changes', [dict(gate='ticket fallback'), dict(start_local='2026-10-02T07:50:00Z'),
    dict(end_local=PAYLOAD['start_local']), dict(end_local='2027-10-02T08:10:00')])
def test_invalid_scope_rejected_before_analysis(api, changes):
    response = api[0].post('/api/analyze/proof', json=dict(PAYLOAD, **changes))
    assert response.status_code in (400, 422)
    assert api[1].calls == []


def test_exact_interval_is_half_open(api):
    proof = get_proof(api, [row(timestamp_utc=STAMP-timedelta(minutes=10)),
        row(timestamp_utc=STAMP+timedelta(minutes=10)), row(timestamp_utc=STAMP-timedelta(hours=1))])
    assert len(proof['candidates']) == 1
    assert proof['candidates'][0]['timestamps']['utc']['value'] == proof['request']['start_utc']


def test_naive_candidate_timestamp_is_not_certified(api):
    proof = get_proof(api, [row(timestamp_utc=STAMP.replace(tzinfo=None))])
    assert proof['candidates'][0]['timestamps']['utc']['value'] is None
    assert 'TIMESTAMP_TIMEZONE_UNKNOWN' in proof['reason_codes']


def test_projection_does_not_mutate_and_ids_bind_changes():
    transaction = row()
    before = deepcopy(transaction)
    one = serialize_candidate(transaction)
    assert transaction == before
    assert one.candidate_id == serialize_candidate(transaction).candidate_id
    transaction.alisa_prediction_truck_number = '999'
    assert one.candidate_id != serialize_candidate(transaction).candidate_id


@pytest.mark.parametrize('state,value', [('known', True), ('known', False), ('unknown', None), ('not_applicable', None)])
def test_fact_states_are_distinct(state, value):
    fact = ProofFact(status=state, value=value, source='test', evidence={'record_id': '1'}, reason_codes=['TEST'])
    assert fact.status == state and fact.value is value


@pytest.mark.parametrize('values', [dict(status='known'), dict(status='unknown', value=False),
    dict(status='not_applicable', value=False), dict(status='known', value=1, source='test', evidence={'id': '1'}),
    dict(status='known', value=True, source='test'), dict(status='unknown')])
def test_invalid_fact_states_rejected(values):
    with pytest.raises(ValidationError):
        ProofFact(**values)


def test_csv_endpoint_content_headers_and_existing_analyze_unchanged(api):
    # An independent expected export plus the original filename/headers contract.
    response = api[0].post('/api/export/csv', json=PAYLOAD)
    expected = DataFrameExporter().build_dataframe(api[1].result.transactions).to_csv(index=False)
    assert response.status_code == 200 and response.text == expected
    assert response.headers['x-exported-row-count'] == '1'
    assert response.headers['x-utc-start'] == '2026-10-02T11:50:00+00:00'
    assert response.headers['content-disposition'] == 'attachment; filename="test_20261002T075000_20261002T081000.csv"'
    legacy = api[0].post('/api/analyze', json=dict(yard_id='test', dates=['2026-10-02']))
    assert legacy.status_code == 200
    assert 'schema_version' not in legacy.json()
    assert legacy.json()['transactions'] == [row().to_dict()]


def test_existing_pipeline_missing_log_file_still_cannot_certify_absence(api, tmp_path):
    from app.services.ground_truth_selector import GroundTruthSelection
    from app.matching.transaction_matcher import TransactionMatcher
    services = api[2]
    selection = GroundTruthSelection(yard_id='test', source='AIQ', transactions=[row()], aiq_transaction_count=1)
    service = AnalysisService(ground_truth_selector=SimpleNamespace(select=lambda **kw: selection),
        transaction_matcher=TransactionMatcher(), container_logs_dir=str(tmp_path),
        local_to_utc=lambda value, yard: (value+timedelta(hours=4)).replace(tzinfo=UTC),
        utc_to_local=lambda value, yard: value.replace(tzinfo=None)-timedelta(hours=4))
    services.analysis_service = service
    proof = get_proof(api)
    assert proof['candidates'][0]['transaction_match_status'] == 'GROUND_TRUTH_ONLY'
    assert proof['acquisition']['sources']['container_logs']['status'] == 'unknown'
    assert_unknown(proof['coverage']['generation']['complete'])
    assert_unknown(proof['absence'][1]['absent'])
