"""Identity-oriented endpoint tests: fake providers, no clocks from reports."""
import csv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from test_proof_api import api, row, STAMP, UID
from app.matching.transaction_matcher import TransactionMatcher
from app.models.unified_transaction import GroundTruthSource
from app.parsers.container_logs_parser import ContainerLogValues
from app.providers.aiq_database_provider import AIQDatabaseProvider, AIQDatabaseProviderError
from app.services.analysis_service import AnalysisService
from app.services.ground_truth_selector import GroundTruthSelector

PAYLOAD = dict(transaction_id='42', yard_id='test', gate_name='Main', direction='IN')


@pytest.fixture
def ticket_api(api, monkeypatch):
    client, _, services = api
    state = SimpleNamespace(records=[row()], id_calls=[], archive_calls=[], parser_calls=[],
        archive=[], lookup_error=False, archive_error=False, read_complete=True, logs=True,
        neighbors=[], neighbor_calls=[], neighbor_error=False)
    def forbidden(*args, **kwargs):
        pytest.fail('Ticket identity lookup attempted regular analysis or report-clock fallback')
    def lookup(**kwargs):
        state.id_calls.append(kwargs)
        if state.lookup_error:
            raise AIQDatabaseProviderError('fake failure')
        return deepcopy(state.records)
    def neighbors(**kwargs):
        state.neighbor_calls.append(kwargs)
        if state.neighbor_error:
            raise AIQDatabaseProviderError('fake competition failure')
        return deepcopy(state.neighbors)
    def archive(**kwargs):
        state.archive_calls.append(kwargs)
        if state.archive_error:
            raise RuntimeError('fake failure')
        return deepcopy(state.archive)
    selector = GroundTruthSelector(aiq_provider=SimpleNamespace(
        get_transactions_by_id=lookup, get_transactions=forbidden,
        get_transactions_for_matching=neighbors),
        archive_provider=SimpleNamespace(get_transactions=archive))
    monkeypatch.setattr(selector, 'select', forbidden)
    def parser(path):
        state.parser_calls.append(path)
        values = ContainerLogValues(uuid=UID, timestamps_utc=[STAMP], first_timestamp_utc=STAMP,
            last_timestamp_utc=STAMP, truck_numbers=['00042'], event_types=['OCR'])
        return SimpleNamespace(read_succeeded=state.read_complete,
            parse=lambda: (deepcopy(state.logs) if isinstance(state.logs, dict) else
                           {UID: values} if state.logs else {}))
    service = AnalysisService(ground_truth_selector=selector, transaction_matcher=TransactionMatcher(),
        container_logs_dir='/fake/logs',
        utc_to_local=lambda stamp, yard: stamp.astimezone(timezone(timedelta(hours=-4))),
        local_to_utc=lambda stamp, yard: (stamp+timedelta(hours=4)).replace(tzinfo=timezone.utc),
        parser_factory=parser)
    monkeypatch.setattr(service, 'analyze', forbidden)
    services.analysis_service = service
    return client, state, service


def post(ticket_api, **changes):
    client, state, _ = ticket_api
    response = client.post('/api/analyze/ticket', json={**PAYLOAD, **changes})
    assert response.status_code == 200, response.text
    return response.json(), state


def test_exact_id_yard_gate_returns_canonical_raw_row(ticket_api):
    body, state = post(ticket_api, transaction_id=' 42 ')
    assert body['schema_version'] == 'unified-ticket.v1'
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    assert body['ticket_match']['transaction_id'] == '42'
    assert body['ticket_match']['row_index'] == 0
    assert body['ticket_match']['direction_validation'] == 'MATCH'
    assert body['authoritative_transaction']['uuid'] == UID
    assert body['decision']['detection_result'] == 'DETECTED'
    parsed = list(csv.reader(StringIO(body['csv_text'])))
    assert parsed == [body['headers'], *body['rows']]
    assert len(body['headers']) == 29 and len(body['rows']) == 1
    record = dict(zip(body['headers'], body['rows'][0]))
    assert record['Ground Truth Transaction ID'] == '42'
    assert record['Container Logs Truck Numbers'] == '00042'
    assert state.id_calls == [dict(transaction_id='42')]


@pytest.mark.parametrize('transaction_id', [None, '', '  '])
def test_missing_id_does_not_acquire(ticket_api, transaction_id):
    body, state = post(ticket_api, transaction_id=transaction_id)
    assert body['ticket_match']['status'] == 'MISSING_TRANSACTION_ID'
    assert not state.id_calls and not state.archive_calls and not state.parser_calls
    assert body['rows'] == []


def test_not_found_has_no_time_fallback(ticket_api):
    ticket_api[1].records = []
    body, state = post(ticket_api)
    assert body['ticket_match']['status'] == 'TRANSACTION_NOT_FOUND'
    assert not state.archive_calls and not state.parser_calls
    assert body['decision']['detection_result'] == 'INSUFFICIENT_DATA'


@pytest.mark.parametrize('changes,status', [
    (dict(yard_id='wrong'), 'YARD_MISMATCH'),
    (dict(gate_name='Other'), 'GATE_MISMATCH'),
    (dict(direction='OUT'), 'DIRECTION_MISMATCH')])
def test_mismatches_do_not_search_for_another_transaction(ticket_api, changes, status):
    body, state = post(ticket_api, **changes)
    assert body['ticket_match']['status'] == status
    assert body['ticket_match']['ground_truth_transaction_id'] == '42'
    assert len(state.id_calls) == 1
    assert not state.archive_calls and not state.parser_calls
    assert body['rows'] == [] and body['decision']['detection_result'] == 'INSUFFICIENT_DATA'


def test_existing_gate_normalization_is_deterministic(ticket_api):
    ticket_api[1].records[0].gate_name = 'Incoming gate'
    body, _ = post(ticket_api, gate_name=' incoming-gate ')
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    body, _ = post(ticket_api, gate_name='Incoming gates')
    assert body['ticket_match']['status'] == 'GATE_MISMATCH'


def test_multiple_exact_records_are_ambiguous(ticket_api):
    ticket_api[1].records = [row(), row(yard_id='another-yard')]
    body, state = post(ticket_api)
    assert body['ticket_match']['status'] == 'AMBIGUOUS'
    assert body['decision']['detection_result'] == 'AMBIGUOUS'
    assert not state.archive_calls and not state.parser_calls


def test_leading_zero_identity_remains_a_string(ticket_api):
    ticket_api[1].records = [row(ground_truth_transaction_id='0042')]
    body, state = post(ticket_api, transaction_id=' 0042 ')
    assert state.id_calls == [dict(transaction_id='0042')]
    assert body['ticket_match']['ground_truth_transaction_id'] == '0042'
    assert body['rows'][0][body['headers'].index('Ground Truth Transaction ID')] == '0042'
    # A provider returning 42 for 0042 is contradictory, not an exact match.
    ticket_api[1].records = [row(ground_truth_transaction_id='42')]
    body, _ = post(ticket_api, transaction_id='0042')
    assert body['ticket_match']['status'] == 'INSUFFICIENT_DATA'


def test_correlation_uses_transaction_day_not_aiq_uuid_or_report_clock(ticket_api):
    # The fixture transaction is Oct 2; no report time is supplied or accepted.
    body, state = post(ticket_api)
    assert state.archive_calls == [dict(yard_id='test', dates=[STAMP.date()])]
    assert state.parser_calls == ['/fake/logs/test/container_log.txt']
    assert body['correlation']['start_utc'] == '2026-10-02T04:00:00+00:00'
    assert body['correlation']['end_utc'] == '2026-10-03T04:00:00+00:00'
    assert body['correlation']['uuid'] == UID
    assert body['correlation']['anchor_source'] == 'AIQ_TRANSACTION'
    # AIQ identity remains exact while the log UUID supplies container identity.
    ticket_api[1].records[0].uuid = '87654321-1234-1234-1234-123456789abc'
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'DETECTED'
    assert body['ticket_match']['uuid'] == UID
    response = ticket_api[0].post('/api/analyze/ticket', json={**PAYLOAD,
        'reported_at_utc': '2026-10-08T00:00:00Z'})
    assert response.status_code == 422


@pytest.mark.parametrize('missing', ['gate_name', 'timestamp_utc'])
def test_missing_source_context_is_insufficient(ticket_api, missing):
    setattr(ticket_api[1].records[0], missing, None)
    body, state = post(ticket_api)
    assert body['ticket_match']['status'] == 'INSUFFICIENT_DATA'
    assert not state.archive_calls and not state.parser_calls


def test_direction_unavailable_is_explicit_nonblocking_validation(ticket_api):
    ticket_api[1].records[0].direction = None
    body, _ = post(ticket_api)
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    assert body['ticket_match']['direction_validation'] == 'UNKNOWN'
    assert 'TRANSACTION_DIRECTION_UNAVAILABLE' in body['reason_codes']


@pytest.mark.parametrize('failed', ['lookup_error', 'archive_error', 'read_complete'])
def test_acquisition_failures_remain_uncertain(ticket_api, failed):
    setattr(ticket_api[1], failed, False if failed == 'read_complete' else True)
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'INSUFFICIENT_DATA'
    assert body['reason_codes']
    assert body['ticket_match']['status'] == ('INSUFFICIENT_DATA' if failed == 'lookup_error' else 'EXACT_MATCH')


def test_missing_aiq_uuid_does_not_prevent_container_match(ticket_api):
    ticket_api[1].records[0].uuid = None
    body, _ = post(ticket_api)
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    assert body['rows'] and body['decision']['detection_result'] == 'DETECTED'
    assert body['ticket_match']['uuid'] == UID


def test_numeric_request_id_is_rejected(ticket_api):
    response = ticket_api[0].post('/api/analyze/ticket', json={**PAYLOAD, 'transaction_id': 42})
    assert response.status_code == 422
    assert not ticket_api[1].id_calls


def test_database_lookup_is_parameterized_text_identity_without_old_filters(monkeypatch):
    provider = object.__new__(AIQDatabaseProvider)
    captured = []
    def query(sql, params):
        captured.append((sql, params))
        return [dict(transaction_id='0042', yard_id='test', timestamp_utc=STAMP, gate_name='Main')]
    monkeypatch.setattr(provider, '_execute_query', query)
    provider.utc_to_local = lambda stamp, yard: stamp
    records = provider.get_transactions_by_id(transaction_id=' 0042 ')
    sql, params = captured[0]
    assert params == ('0042',)
    assert 'TRIM(tt.id::text) = %s' in sql
    assert 'LEFT JOIN public.truck_transaction_automation' in sql
    assert 'FINISHED' not in sql and 'tt.started >=' not in sql and 'ANY(%s)' not in sql
    assert records[0].ground_truth_transaction_id == '0042'
    # The query is not restricted to Alisa origin; manual records remain visible.
    assert records[0].alisa_prediction_truck_number is None


def test_found_unusable_database_row_is_not_treated_as_absence(monkeypatch):
    provider = object.__new__(AIQDatabaseProvider)
    monkeypatch.setattr(provider, '_execute_query', lambda *args: [dict(transaction_id='42')])
    with pytest.raises(AIQDatabaseProviderError):
        provider.get_transactions_by_id(transaction_id='42')


@pytest.mark.parametrize('candidate_count,outcome,status', [
    (0, 'MISSED', 'GROUND_TRUTH_ONLY'),
    (1, 'DETECTED', 'MATCHED'),
    (2, 'DETECTED', 'MATCHED'),
])
def test_remco_ticket_matches_nearest_by_time_direction_not_gate_or_external_uuid(
        ticket_api, candidate_count, outcome, status):
    state = ticket_api[1]
    state.records = [row(ground_truth_transaction_id='6840696', yard_id='rmc_msg',
        gate_id=None, gate_name='Incoming gate', direction='IN', uuid='aiq-external-uuid')]
    state.logs = {
        f'container-{index}': ContainerLogValues(uuid=f'container-{index}',
            first_timestamp_utc=STAMP+timedelta(seconds=10+index), directions=['IN'],
            gate_ids=['MAINENTRANCE'], metadata={'yard_id': 'rmc_msg'})
        for index in range(candidate_count)
    }
    body, state = post(ticket_api, transaction_id='6840696', yard_id='rmc_msg',
                       gate_name='Incoming gate', direction='IN')
    assert state.id_calls == [dict(transaction_id='6840696')]
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    assert body['decision']['detection_result'] == outcome
    exported = dict(zip(body['headers'], body['rows'][0]))
    assert exported['Transaction Match Status'] == status
    expected_uuid = 'container-0' if candidate_count else None
    for section in ('ticket_match', 'authoritative_transaction', 'correlation'):
        assert body[section]['uuid'] == expected_uuid
    assert exported['UUID'] == (expected_uuid or '')
    assert 'aiq-external-uuid' not in body['csv_text']


def test_ticket_equally_nearest_container_candidates_are_ambiguous(ticket_api):
    state = ticket_api[1]
    state.logs = {uuid: ContainerLogValues(uuid=uuid, first_timestamp_utc=STAMP,
        directions=['IN'], gate_ids=[gate], metadata={'yard_id': 'test'})
        for uuid, gate in [('first', 'Main'), ('second', 'Different gate')]}
    body, _ = post(ticket_api)
    assert body['ticket_match']['status'] == 'EXACT_MATCH'
    assert body['ticket_match']['uuid'] is None
    assert body['decision']['detection_result'] == 'AMBIGUOUS'


@pytest.mark.parametrize('requested,expected_uuid,outcome,status', [
    ('6840696', None, 'MISSED', 'GROUND_TRUTH_ONLY'),
    ('6840825', 'ce1cfcd7-47d6-4fcd-bba9-b719b774c357', 'DETECTED', 'MATCHED'),
])
def test_remco_competition_returns_only_requested_ticket(
        ticket_api, requested, expected_uuid, outcome, status):
    state = ticket_api[1]
    older = row(ground_truth_transaction_id='6840696', yard_id='rmc_msg',
        timestamp_utc=datetime.fromisoformat('2026-10-06T12:36:05.076522+00:00'),
        gate_name='Incoming gate', ground_truth_vehicle_type='MOTORCYCLE', uuid='external-old')
    closer = row(ground_truth_transaction_id='6840825', yard_id='rmc_msg',
        timestamp_utc=datetime.fromisoformat('2026-10-06T12:39:59.377958+00:00'),
        gate_name='Incoming gate', ground_truth_vehicle_type='STRAIGHT_TRUCK', uuid='external-new',
        alisa_prediction_truck_number='1691', alisa_prediction_trailer_number_1='BTIU533072')
    state.records = [older if requested == '6840696' else closer]
    state.neighbors = [older, closer]
    uuid = 'ce1cfcd7-47d6-4fcd-bba9-b719b774c357'
    canonical = datetime.fromisoformat('2026-10-06T12:39:59+00:00')
    state.logs = {uuid: ContainerLogValues(uuid=uuid, first_timestamp_utc=canonical,
        metadata={'yard_id': 'rmc_msg', 'confirmation_alert_timestamp_utc': canonical},
        directions=['IN'], gate_ids=['MAINENTRANCE'], truck_numbers=['1691'],
        trailer_numbers=['BTIU533072'], vehicle_types=['straight truck'])}
    body, _ = post(ticket_api, transaction_id=requested, yard_id='rmc_msg', gate_name='Incoming gate')
    assert body['ticket_match']['uuid'] == expected_uuid
    assert body['decision']['detection_result'] == outcome
    assert len(body['rows']) == 1
    exported = dict(zip(body['headers'], body['rows'][0]))
    assert exported['Ground Truth Transaction ID'] == requested
    assert exported['Transaction Match Status'] == status
    assert exported['Match Method'] == ('CONTAINER_LOGS -> AIQ' if expected_uuid else 'NONE')
    assert body['correlation']['aiq']['competing_transaction_count'] == 2
    assert state.id_calls == [dict(transaction_id=requested)]
    assert state.neighbor_calls == [dict(yard_id='rmc_msg', start_utc=canonical-timedelta(seconds=300),
        end_utc=canonical+timedelta(seconds=300, microseconds=1))]
    if expected_uuid:
        assert float(exported['Match Time Difference Seconds']) == pytest.approx(0.378)


def test_ticket_tied_aiq_competition_is_ambiguous(ticket_api):
    state = ticket_api[1]
    state.records = [row(timestamp_utc=STAMP-timedelta(seconds=1))]
    state.neighbors = [row(ground_truth_transaction_id='43', timestamp_utc=STAMP+timedelta(seconds=1),
                           gate_name='Other gate')]
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'AMBIGUOUS'
    assert body['ticket_match']['uuid'] is None
    assert len(body['rows']) == 1


def test_competition_failure_cannot_certify_missed_or_detected(ticket_api):
    ticket_api[1].neighbor_error = True
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'INSUFFICIENT_DATA'
    assert body['ticket_match']['uuid'] is None
    assert body['reason_codes']


def test_competitor_duplicates_fail_closed(ticket_api):
    ticket_api[1].neighbors = [row(ground_truth_transaction_id='43'), row(ground_truth_transaction_id='43')]
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'INSUFFICIENT_DATA'
    assert body['ticket_match']['uuid'] is None


def test_competition_scope_crosses_day_boundary_and_requested_threshold(ticket_api):
    state = ticket_api[1]
    # Requested row at local 23:56. Container at next local day 00:00,
    # and a closer contender at 00:01, beyond request +/- 300 seconds.
    stamp = datetime.fromisoformat('2026-10-03T03:56:00+00:00')
    container_time = stamp+timedelta(seconds=240)
    state.records = [row(timestamp_utc=stamp)]
    state.neighbors = [row(ground_truth_transaction_id='43',
        timestamp_utc=container_time+timedelta(seconds=61))]
    state.logs = {UID: ContainerLogValues(uuid=UID, first_timestamp_utc=container_time)}
    body, _ = post(ticket_api)
    assert body['decision']['detection_result'] == 'MISSED'
    assert body['ticket_match']['uuid'] is None
    assert body['correlation']['aiq']['competing_transaction_count'] == 2


def test_competition_expands_to_contender_alternatives(ticket_api):
    state = ticket_api[1]
    state.records = [row(timestamp_utc=STAMP)]
    state.neighbors = [row(ground_truth_transaction_id='43', timestamp_utc=STAMP+timedelta(seconds=400))]
    state.logs = {
        'shared': ContainerLogValues(uuid='shared', first_timestamp_utc=STAMP+timedelta(seconds=290)),
        'closer-alternative': ContainerLogValues(uuid='closer-alternative',
            first_timestamp_utc=STAMP+timedelta(seconds=401)),
    }
    body, _ = post(ticket_api)
    assert body['ticket_match']['uuid'] == 'shared'
    assert body['decision']['detection_result'] == 'DETECTED'
    assert len(state.neighbor_calls) == 2


def test_competition_query_is_exact_yard_time_and_includes_non_alisa_records(monkeypatch):
    provider = object.__new__(AIQDatabaseProvider)
    provider.utc_to_local = lambda stamp, yard: stamp
    captured = []
    def query(sql, params):
        captured.append((sql, params))
        return [dict(transaction_id='43', yard_id='test', timestamp_utc=STAMP)]
    monkeypatch.setattr(provider, '_execute_query', query)
    start, end = STAMP-timedelta(seconds=300), STAMP+timedelta(seconds=300)
    records = provider.get_transactions_for_matching(yard_id='test', start_utc=start, end_utc=end)
    sql, params = captured[0]
    assert params == ('test', start.replace(tzinfo=None), end.replace(tzinfo=None))
    assert 'l.name = %s' in sql and 'tt.started >= %s' in sql and 'tt.started < %s' in sql
    assert 'LEFT JOIN public.truck_transaction_automation' in sql
    assert 'FINISHED' not in sql and provider.ALISA_SOURCE_FILTER not in sql
    assert records[0].ground_truth_transaction_id == '43' and records[0].uuid is None
