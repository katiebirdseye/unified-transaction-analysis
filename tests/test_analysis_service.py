"""Bulk analysis uses competitive ownership without changing other sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_proof_api import api
from app.matching.transaction_matcher import TransactionMatcher
from app.models.unified_transaction import (
    GroundTruthSource, MatchMethod, TransactionMatchStatus, UnifiedTransaction,
)
from app.parsers.container_logs_parser import ContainerLogValues
from app.services.analysis_service import AnalysisRequest, AnalysisService
from app.services.ground_truth_selector import GroundTruthSelection

STAMP = datetime.fromisoformat('2026-10-14T02:42:30+00:00')
UUID = '5e6d043e-9bce-41ea-a1c2-6492ed40b8eb'


def aiq(identity, stamp):
    return UnifiedTransaction(yard_id='cre_slc', ground_truth_transaction_id=identity,
        ground_truth_source=GroundTruthSource.AIQ, timestamp_utc=stamp,
        direction='IN', gate_name='Long Term', uuid='external-'+identity)


def container(uuid=UUID, stamp=STAMP):
    return ContainerLogValues(uuid=uuid, first_timestamp_utc=stamp,
        timestamps_utc=[stamp], directions=['IN'], gate_ids=['Different log gate'],
        metadata={'confirmation_alert_timestamp_utc': stamp})


def service(rows, logs):
    selection = GroundTruthSelection(yard_id='cre_slc', transactions=rows, source='AIQ',
        aiq_transaction_count=sum(row.ground_truth_source == GroundTruthSource.AIQ for row in rows),
        archive_transaction_count=sum(row.ground_truth_source == GroundTruthSource.ARCHIVE for row in rows))
    return AnalysisService(
        ground_truth_selector=SimpleNamespace(select=lambda **kwargs: deepcopy(selection)),
        transaction_matcher=TransactionMatcher(), container_logs_dir='/offline',
        utc_to_local=lambda stamp, yard: stamp,
        local_to_utc=lambda stamp, yard: stamp.replace(tzinfo=timezone.utc),
        parser_factory=lambda path: SimpleNamespace(read_succeeded=True,
            parse=lambda: {record.uuid: deepcopy(record) for record in logs}))


def analyze(rows, logs, include_container_logs_only=False):
    return service(rows, logs).analyze(AnalysisRequest(yard_id='cre_slc', dates=['2026-10-14'],
        include_container_logs_only=include_container_logs_only))


def indexed(result):
    matched_uuids = [row.uuid for row in result.transactions
        if row.ground_truth_source == GroundTruthSource.AIQ
        and row.transaction_match_status == TransactionMatchStatus.MATCHED]
    assert len(matched_uuids) == len(set(matched_uuids))
    return {row.ground_truth_transaction_id: row for row in result.transactions
            if row.ground_truth_transaction_id}


def production_rows():
    return [aiq('7101518', datetime.fromisoformat('2026-10-14T02:41:59.757030+00:00')),
            aiq('7101531', datetime.fromisoformat('2026-10-14T02:42:31.279612+00:00'))]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('include_logs_only', [False, True])
def test_bulk_production_nearest_row_owns_uuid_once(reverse, include_logs_only):
    rows = production_rows()
    result = analyze(list(reversed(rows)) if reverse else rows, [container()], include_logs_only)
    records = indexed(result)
    winner, loser = records['7101531'], records['7101518']
    assert winner.uuid == UUID
    assert winner.transaction_match_status == TransactionMatchStatus.MATCHED
    assert winner.match_method == MatchMethod.TIMESTAMP_DIRECTION
    assert winner.match_time_difference_seconds == pytest.approx(1.279612)
    assert loser.uuid is None
    assert loser.transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY
    assert loser.match_method == MatchMethod.NONE
    assert loser.match_time_difference_seconds is None
    assert 'NO_CONTAINER_LOGS_MATCH_FOR_AIQ_TRANSACTION' in loser.flags
    assert result.matched_transaction_count == 1
    assert result.ground_truth_only_count == 1
    assert result.container_logs_only_count == 0
    assert len(result.transactions) == 2


@pytest.mark.parametrize('reverse', [False, True])
def test_bulk_closer_pair_wins_regardless_of_source_order(reverse):
    rows = [aiq('first-farther', STAMP-timedelta(seconds=30)),
            aiq('second-closer', STAMP+timedelta(seconds=1))]
    records = indexed(analyze(list(reversed(rows)) if reverse else rows, [container()]))
    assert records['second-closer'].uuid == UUID
    assert records['first-farther'].uuid is None


@pytest.mark.parametrize('reverse', [False, True])
def test_bulk_equal_cost_competition_remains_ambiguous(reverse):
    rows = [aiq('a', STAMP-timedelta(seconds=1)), aiq('b', STAMP+timedelta(seconds=1))]
    result = analyze(list(reversed(rows)) if reverse else rows, [container()])
    assert result.ambiguous_count == 2
    for record in indexed(result).values():
        assert record.transaction_match_status == TransactionMatchStatus.AMBIGUOUS
        assert record.uuid is None and record.match_method == MatchMethod.NONE


@pytest.mark.parametrize('free_uuid', [False, True])
def test_archive_only_receives_logs_not_consumed_by_aiq(free_uuid):
    aiq_row = aiq('aiq', STAMP)
    archive = aiq('archive', STAMP+timedelta(seconds=1))
    archive.ground_truth_source = GroundTruthSource.ARCHIVE
    archive.uuid = None
    logs = [container()]
    if free_uuid:
        leftover = container('archive-log', STAMP+timedelta(seconds=20))
        leftover.gate_ids = ['Long Term']
        logs.append(leftover)
    records = indexed(analyze([archive, aiq_row], logs))
    assert records['aiq'].uuid == UUID
    assert records['archive'].uuid == ('archive-log' if free_uuid else None)
    assert records['archive'].transaction_match_status == (
        TransactionMatchStatus.MATCHED if free_uuid else TransactionMatchStatus.GROUND_TRUTH_ONLY)
    assert records['archive'].match_method == (
        MatchMethod.TIMESTAMP_DIRECTION_GATE if free_uuid else MatchMethod.NONE)


@pytest.mark.parametrize('include_logs_only', [False, True])
def test_unused_container_rows_follow_request_option(include_logs_only):
    unused = container('unused', STAMP+timedelta(seconds=900))
    result = analyze(production_rows(), [container(), unused], include_logs_only)
    indexed(result)
    extra = [row for row in result.transactions
             if row.transaction_match_status == TransactionMatchStatus.CONTAINER_LOGS_ONLY]
    assert [row.uuid for row in extra] == (['unused'] if include_logs_only else [])
    assert result.container_logs_only_count == int(include_logs_only)


def test_bulk_two_pairs_are_unique_and_nearest():
    rows = production_rows() + [aiq('other', STAMP+timedelta(seconds=600))]
    records = indexed(analyze(rows, [container(), container('other-log', STAMP+timedelta(seconds=601))]))
    assert records['7101518'].uuid is None
    assert records['7101531'].uuid == UUID
    assert records['other'].uuid == 'other-log'


def test_bulk_api_uses_competitive_assignment(api):
    yard = api[2].timezone_service.resolve_by_id('test')
    yard.yard_id = 'cre_slc'
    api[2].timezone_service.resolve_by_id = lambda *args, **kwargs: yard
    api[2].analysis_service = service(production_rows(), [container()])
    response = api[0].post('/api/analyze', json=dict(yard_id='cre_slc', dates=['2026-10-14']))
    assert response.status_code == 200, response.text
    records = {row['ground_truth_transaction_id']: row for row in response.json()['transactions']}
    assert records['7101518']['uuid'] is None
    assert records['7101518']['transaction_match_status'] == 'GROUND_TRUTH_ONLY'
    assert records['7101531']['uuid'] == UUID
    assert records['7101531']['transaction_match_status'] == 'MATCHED'
