"""Execute bulk SQL against offline relational fixtures, then check API output."""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_proof_api import api
from app.matching.transaction_matcher import TransactionMatcher
from app.models.unified_transaction import MatchMethod, TransactionMatchStatus
from app.providers.aiq_database_provider import AIQDatabaseProvider
from app.services.analysis_service import AnalysisService
from app.services.ground_truth_selector import GroundTruthSelector

STAMP = datetime.fromisoformat('2026-10-14T02:41:59.757030+00:00')
PREDICTIONS = ('truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type')


def db_time(stamp):
    return stamp.strftime('%Y-%m-%d %H:%M:%S.%f')


@pytest.fixture
def bulk_database(monkeypatch):
    provider = object.__new__(AIQDatabaseProvider)
    provider.utc_to_local = lambda stamp, yard: stamp
    connection = sqlite3.connect(':memory:', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE truck_transaction (
            id TEXT, status TEXT, started TEXT, gate_id TEXT, gate_direction TEXT,
            external_uuid TEXT, vehicle_transport_type TEXT, vehicle_unit_number TEXT,
            trailer_trailer_number TEXT, trailer_trailer_number_two TEXT);
        CREATE TABLE truck_transaction_automation (
            id TEXT, vehicle_unit_number_source TEXT, vehicle_unit_number_value TEXT,
            vehicle_transport_type_source TEXT, vehicle_transport_type_value TEXT,
            vehicle_license_plate_number_source TEXT, trailer_trailer_number_source TEXT,
            trailer_trailer_number_value TEXT, trailer_trailer_number_two_source TEXT,
            trailer_trailer_number_two_value TEXT);
        CREATE TABLE gate (id TEXT, name TEXT, location_id TEXT);
        CREATE TABLE location (id TEXT, name TEXT, full_name TEXT);
        INSERT INTO location VALUES ('1', 'cre_slc', 'Salt Lake City');
        INSERT INTO location VALUES ('2', 'other', 'Other yard');
        INSERT INTO gate VALUES ('10', 'Long Term', '1');
        INSERT INTO gate VALUES ('20', 'Other Gate', '2');
    ''')
    def add(identity, *, status='FINISHED', stamp=STAMP, gate='10', automation='absent'):
        connection.execute('INSERT INTO truck_transaction VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (identity, status, db_time(stamp), gate, 'IN', 'external-'+identity,
             'STRAIGHT_TRUCK', '1691', 'BTIU533072', None))
        if automation != 'absent':
            source = 'alisa' if automation == 'alisa' else None
            # Values exist even with NULL sources: CASE must keep them unexposed.
            connection.execute('INSERT INTO truck_transaction_automation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (identity, source, '1691', source, 'STRAIGHT_TRUCK', source,
                 source, 'BTIU533072', source, 'SECOND'))
        connection.commit()
    captured = []
    def query(sql, params):
        captured.append((sql, params))
        # SQLite adapter changes only schema/parameter syntax, not joins,
        # CASE expressions, status, yard or time predicates.
        translated = sql.replace('public.', '').replace('= ANY(%s)', 'IN (?)').replace('%s', '?')
        assert len(params[0]) == 1
        adapted = (params[0][0], db_time(params[1]), db_time(params[2]))
        return [dict(row) for row in connection.execute(translated, adapted).fetchall()]
    monkeypatch.setattr(provider, '_execute_query', query)
    yield SimpleNamespace(provider=provider, add=add, captured=captured)
    connection.close()


def read(state, start=STAMP-timedelta(seconds=1), end=STAMP+timedelta(seconds=1)):
    return state.provider.get_transactions(yard_ids=[' CRE_SLC '], start_utc=start, end_utc=end)


@pytest.mark.parametrize('automation', ['alisa', 'null_sources', 'absent'])
def test_bulk_returns_finished_transaction_with_optional_automation(bulk_database, automation):
    bulk_database.add('7101518', automation=automation)
    records = read(bulk_database)
    assert len(records) == 1
    row = records[0]
    assert row.ground_truth_transaction_id == '7101518'
    assert row.yard_id == 'cre_slc' and row.timestamp_utc == STAMP
    assert row.gate_name == 'Long Term' and row.direction == 'IN'
    predictions = [getattr(row, 'alisa_prediction_'+name) for name in PREDICTIONS]
    assert predictions == (['1691', 'BTIU533072', 'SECOND', 'STRAIGHT_TRUCK']
                           if automation == 'alisa' else [None]*4)
    assert row.ground_truth_truck_number == '1691'
    assert row.uuid is None
    sql, params = bulk_database.captured[0]
    assert 'LEFT JOIN public.truck_transaction_automation tta' in sql
    assert bulk_database.provider.ALISA_SOURCE_FILTER not in sql
    assert "tt.status = 'FINISHED'" in sql
    assert params == (['cre_slc'], (STAMP-timedelta(seconds=1)).replace(tzinfo=None),
                      (STAMP+timedelta(seconds=1)).replace(tzinfo=None))


@pytest.mark.parametrize('status', ['STARTED', 'IN_PROGRESS', 'CANCELLED'])
def test_non_finished_transactions_remain_excluded(bulk_database, status):
    bulk_database.add('7101518', status=status, automation='alisa')
    assert read(bulk_database) == []


def test_bulk_yard_and_half_open_utc_range_filtering_unchanged(bulk_database):
    start, end = STAMP, STAMP+timedelta(seconds=60)
    bulk_database.add('start', stamp=start)
    bulk_database.add('inside', stamp=start+timedelta(seconds=1))
    bulk_database.add('end', stamp=end)
    bulk_database.add('before', stamp=start-timedelta(microseconds=1))
    bulk_database.add('wrong-yard', gate='20')
    offset = timezone(timedelta(hours=-4))
    records = read(bulk_database, start.astimezone(offset), end.astimezone(offset))
    assert [row.ground_truth_transaction_id for row in records] == ['start', 'inside']


def test_row_conversion_without_automation_keeps_null_predictions(bulk_database):
    row = bulk_database.provider._row_to_transaction(row=dict(transaction_id='7101518',
        yard_id='cre_slc', timestamp_utc=STAMP, gate_name='Long Term', direction='IN'), source_index=1)
    assert all(getattr(row, 'alisa_prediction_'+name) is None for name in PREDICTIONS)


def test_uuid_lookup_keeps_existing_inner_join_alisa_and_finished_filters(monkeypatch):
    provider = object.__new__(AIQDatabaseProvider)
    captured = []
    monkeypatch.setattr(provider, '_execute_query', lambda sql, params: captured.append((sql, params)) or [])
    assert provider.get_transactions_by_uuid(uuids=[' EXTERNAL-UUID ']) == []
    sql, params = captured[0]
    assert 'JOIN public.truck_transaction_automation tta' in sql
    assert 'LEFT JOIN public.truck_transaction_automation tta' not in sql
    assert provider.ALISA_SOURCE_FILTER in sql and "tt.status = 'FINISHED'" in sql
    assert 'LOWER(tt.external_uuid::text) = ANY(%s)' in sql
    assert params == (['external-uuid'],)


def test_bulk_api_retains_production_transaction_without_predictions(api, bulk_database, tmp_path):
    bulk_database.add('7101518', automation='null_sources')
    directory = tmp_path / 'cre_slc'
    directory.mkdir()
    (directory / 'container_log.txt').write_text('')
    selector = GroundTruthSelector(aiq_provider=bulk_database.provider,
        archive_provider=SimpleNamespace(get_transactions=lambda **kwargs: []))
    api[2].analysis_service = AnalysisService(ground_truth_selector=selector,
        transaction_matcher=TransactionMatcher(), container_logs_dir=str(tmp_path),
        utc_to_local=lambda stamp, yard: stamp,
        local_to_utc=lambda stamp, yard: stamp.replace(tzinfo=timezone.utc))
    api[2].timezone_service.resolve_by_id = lambda *args, **kwargs: SimpleNamespace(yard_id='cre_slc', yard_name='Salt Lake City',
        timezone_name='UTC', observes_dst=False, standard_utc_offset_hours=0, dst_utc_offset_hours=0)
    response = api[0].post('/api/analyze', json=dict(yard_id='cre_slc', dates=['2026-10-14']))
    assert response.status_code == 200, response.text
    records = response.json()['transactions']
    assert len(records) == 1
    row = records[0]
    assert row['ground_truth_transaction_id'] == '7101518'
    assert row['ground_truth_source'] == 'AIQ' and row['uuid'] is None
    assert row['transaction_match_status'] == TransactionMatchStatus.GROUND_TRUTH_ONLY.value
    assert row['match_method'] == MatchMethod.NONE.value
    assert 'NO_CONTAINER_LOGS_MATCH_FOR_AIQ_TRANSACTION' in row['flags']
    assert all(row['alisa_prediction_'+name] is None for name in PREDICTIONS)


@pytest.mark.parametrize('automation', ['null_sources', 'absent'])
def test_import_without_predictions_can_still_match_container(bulk_database, automation):
    from app.parsers.container_logs_parser import ContainerLogValues
    bulk_database.add('7101518', automation=automation)
    rows = TransactionMatcher().match(ground_truth_transactions=read(bulk_database),
        container_logs_by_uuid={'container': ContainerLogValues(uuid='container',
            first_timestamp_utc=STAMP, directions=['IN'])}, yard_id='cre_slc')
    row = rows[0]
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION
    assert row.uuid == 'container'
    assert all(getattr(row, 'alisa_prediction_'+name) is None for name in PREDICTIONS)
