"""AIQ/container identity separation and shared Archive matching regressions."""
from datetime import datetime, timedelta, timezone

import pytest

from app.matching.transaction_matcher import TransactionMatcher, TransactionMatcherConfig
from app.models.unified_transaction import (
    GroundTruthSource, MatchMethod, TransactionMatchStatus, UnifiedTransaction,
)
from app.parsers.container_logs_parser import ContainerLogValues

STAMP = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
EXTERNAL_UUID = 'aiq-external-uuid-absent-from-logs'


def aiq(**changes):
    values = dict(yard_id='rmc_msg', ground_truth_transaction_id='6840696',
        ground_truth_source=GroundTruthSource.AIQ, uuid=EXTERNAL_UUID,
        timestamp_utc=STAMP, gate_name='Incoming gate', direction='IN',
        ground_truth_truck_number='AIQ truck', ground_truth_trailer_number_1='AIQ trailer',
        ground_truth_vehicle_type='AIQ type')
    values.update(changes)
    return UnifiedTransaction(**values)


def log(uuid='container-uuid', seconds=10, **changes):
    values = dict(uuid=uuid, first_timestamp_utc=STAMP+timedelta(seconds=seconds),
        gate_ids=['incoming-gate'], directions=['IN'], metadata={'yard_id': 'rmc_msg'},
        truck_numbers=['different truck'], trailer_numbers=['different trailer'],
        vehicle_types=['different type'])
    values.update(changes)
    return ContainerLogValues(**values)


def match(source, *logs, yard_id='rmc_msg', **config):
    return TransactionMatcher(TransactionMatcherConfig(include_container_logs_only=False, **config)).match(
        ground_truth_transactions=[source], container_logs_by_uuid={item.uuid: item for item in logs},
        yard_id=yard_id)[0]


@pytest.mark.parametrize('source_uuid', [EXTERNAL_UUID, None, 'container-uuid'])
def test_remco_unique_nearby_log_ignores_aiq_uuid_and_ocr(source_uuid):
    source = aiq(uuid=source_uuid)
    row = match(source, log())
    assert row.ground_truth_transaction_id == '6840696'
    assert row.uuid == 'container-uuid'
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION
    assert row.match_time_difference_seconds == 10
    assert row.container_logs_truck_numbers == ['different truck']
    assert source.uuid == source_uuid  # input is never mutated


@pytest.mark.parametrize('changes', [dict(seconds=301), dict(directions=['OUT']),
    dict(metadata={'yard_id': 'other'}),
    dict(first_timestamp_utc=None)])
def test_same_external_uuid_does_not_bypass_candidate_constraints(changes):
    row = match(aiq(), log(uuid=EXTERNAL_UUID, **changes))
    assert row.uuid is None
    assert row.transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY
    assert row.match_method == MatchMethod.NONE


def test_remco_no_logs_is_ground_truth_only_with_no_uuid():
    row = match(aiq())
    assert row.uuid is None
    assert row.transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY
    assert row.match_method == MatchMethod.NONE


@pytest.mark.parametrize('second_offset', [10, -10, 10.000001])
def test_remco_nearest_time_ties_are_ambiguous(second_offset):
    row = match(aiq(), log(), log(uuid=EXTERNAL_UUID, seconds=second_offset))
    assert row.uuid is None
    assert row.transaction_match_status == TransactionMatchStatus.AMBIGUOUS
    assert row.match_method == MatchMethod.NONE
    assert row.container_logs_truck_numbers == []


@pytest.mark.parametrize('reverse', [False, True])
def test_unique_nearest_wins_regardless_of_gate_uuid_or_input_order(reverse):
    logs = [log(uuid=EXTERNAL_UUID, seconds=299),
            log(uuid='nearest', seconds=-20, gate_ids=['MAINENTRANCE']),
            log(uuid='middle', seconds=30)]
    row = match(aiq(gate_id='108213552'), *(reversed(logs) if reverse else logs))
    assert row.uuid == 'nearest'
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_time_difference_seconds == 20
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION


@pytest.mark.parametrize('changes', [dict(gate_ids=[]), dict(directions=[]),
    dict(directions=[], orientations=['entry'])])
def test_optional_direction_and_gate_are_supported(changes):
    row = match(aiq(), log(**changes))
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_method != MatchMethod.UUID


def test_missing_gate_uses_timestamp_direction_method():
    assert match(aiq(), log(gate_ids=[])).match_method == MatchMethod.TIMESTAMP_DIRECTION


def test_matching_numeric_gate_id_does_not_change_aiq_match_method():
    assert match(aiq(gate_id='123'), log(gate_ids=['123'])).match_method == MatchMethod.TIMESTAMP_DIRECTION


def test_confirmation_timestamp_is_preferred_and_threshold_is_inclusive():
    values = log(seconds=900, metadata={'yard_id': 'rmc_msg',
        'confirmation_alert_timestamp_utc': STAMP+timedelta(seconds=300)})
    assert match(aiq(), values).match_time_difference_seconds == 300


def test_aiq_direction_constraint_is_independent_of_archive_configuration():
    assert match(aiq(), log(directions=['OUT']), require_direction_for_archive=False).uuid is None


def test_yard_scope_required_when_records_have_no_yard_metadata():
    values = log(metadata={})
    assert match(aiq(), values).uuid == values.uuid
    assert match(aiq(), values, yard_id='other').uuid is None
    assert match(aiq(), values, yard_id=None).uuid is None


def test_missing_aiq_timestamp_cannot_match_even_by_uuid():
    row = match(aiq(timestamp_utc=None), log(uuid=EXTERNAL_UUID))
    assert row.uuid is None
    assert row.transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY


def test_archive_still_selects_nearest_with_gate_preference():
    source = aiq(ground_truth_source=GroundTruthSource.ARCHIVE, uuid=None)
    row = match(source, log(uuid='closer', seconds=1, gate_ids=['Outgoing gate']),
                log(uuid='gate-match', seconds=20))
    assert row.uuid == 'gate-match'
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED


def test_archive_still_flags_ties_and_selects_deterministically():
    row = match(aiq(ground_truth_source=GroundTruthSource.ARCHIVE, uuid=None),
                log(uuid='b'), log(uuid='a'))
    assert row.uuid == 'a'
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert 'AMBIGUOUS_ARCHIVE_CONTAINER_LOG_MATCH' in row.flags


def test_archive_still_allows_gate_mismatch_and_optional_direction_constraint():
    row = match(aiq(ground_truth_source=GroundTruthSource.ARCHIVE, uuid=None),
                log(gate_ids=['Outgoing gate'], directions=['OUT']), require_direction_for_archive=False)
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION


def test_aiq_without_external_uuid_remains_eligible_for_regular_analysis():
    from app.services.ground_truth_selector import GroundTruthSelector
    source = aiq(uuid=None)
    selector = GroundTruthSelector(aiq_provider=None, archive_provider=None)
    eligible = selector._filter_eligible_aiq_transactions(transactions=[source],
        yard_id='rmc_msg', start_utc=STAMP-timedelta(minutes=1), end_utc=STAMP+timedelta(minutes=1))
    assert eligible == [source]
    assert match(eligible[0], log()).transaction_match_status == TransactionMatchStatus.MATCHED


@pytest.mark.parametrize('missing', ['aiq', 'container'])
def test_missing_direction_on_either_side_does_not_reject(missing):
    source = aiq(direction=None) if missing == 'aiq' else aiq()
    candidate = log(directions=[]) if missing == 'container' else log()
    assert match(source, candidate).transaction_match_status == TransactionMatchStatus.MATCHED


def test_remco_real_timestamps_and_different_gate_names_match():
    source = aiq(timestamp_utc=datetime.fromisoformat('2026-10-06T12:36:05.076522+00:00'),
                 gate_id='108213552')
    candidate = log(uuid='ce1cfcd7-47d6-4fcd-bba9-b719b774c357',
        first_timestamp_utc=datetime.fromisoformat('2026-10-06T12:40:08+00:00'),
        gate_ids=['MAINENTRANCE'])
    row = match(source, candidate)
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.uuid == candidate.uuid
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION
    assert row.match_time_difference_seconds == pytest.approx(242.923478)
    assert row.gate_name == 'Incoming gate'
    assert row.metadata['container_logs']['gate_ids'] == ['MAINENTRANCE']


def test_custom_timestamp_threshold_applies_to_aiq():
    assert match(aiq(), log(seconds=61), archive_time_threshold_seconds=60).uuid is None


def test_remco_confirmation_reference_wins_over_ocr_first_timestamp():
    confirmation_time = datetime.fromisoformat('2026-10-06T12:39:59+00:00')
    values = log(uuid='ce1cfcd7-47d6-4fcd-bba9-b719b774c357',
        first_timestamp_utc=datetime.fromisoformat('2026-10-06T12:40:08+00:00'),
        metadata={'confirmation_alert_timestamp_utc': confirmation_time})
    assert TransactionMatcher._get_container_reference_timestamp(values) == confirmation_time


def competitive(rows, *logs):
    matched = TransactionMatcher(TransactionMatcherConfig(include_container_logs_only=False)).match(
        ground_truth_transactions=rows, container_logs_by_uuid={item.uuid: item for item in logs},
        yard_id='rmc_msg', competitive_aiq=True)
    return {row.ground_truth_transaction_id: row for row in matched}


@pytest.mark.parametrize('reverse', [False, True])
def test_competitive_remco_nearest_aiq_owns_uuid_once(reverse):
    older = aiq(timestamp_utc=datetime.fromisoformat('2026-10-06T12:36:05.076522+00:00'),
                ground_truth_vehicle_type='MOTORCYCLE')
    closer = aiq(ground_truth_transaction_id='6840825',
        timestamp_utc=datetime.fromisoformat('2026-10-06T12:39:59.377958+00:00'),
        ground_truth_vehicle_type='STRAIGHT_TRUCK', alisa_prediction_truck_number='1691',
        alisa_prediction_trailer_number_1='BTIU533072')
    candidate = log(uuid='ce1cfcd7-47d6-4fcd-bba9-b719b774c357',
        first_timestamp_utc=datetime.fromisoformat('2026-10-06T12:39:59+00:00'),
        gate_ids=['MAINENTRANCE'])
    rows = competitive([closer, older] if reverse else [older, closer], candidate)
    assert rows['6840825'].uuid == candidate.uuid
    assert rows['6840825'].match_time_difference_seconds == pytest.approx(0.377958)
    assert rows['6840825'].match_method == MatchMethod.TIMESTAMP_DIRECTION
    assert rows['6840696'].uuid is None
    assert rows['6840696'].transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY
    assert rows['6840696'].match_method == MatchMethod.NONE
    assert len({row.uuid for row in rows.values() if row.uuid}) == 1


@pytest.mark.parametrize('reverse', [False, True])
def test_two_aiq_two_logs_get_nearest_one_to_one_assignment(reverse):
    rows = [aiq(ground_truth_transaction_id='far', timestamp_utc=STAMP+timedelta(seconds=20)),
            aiq(ground_truth_transaction_id='near', timestamp_utc=STAMP+timedelta(seconds=1))]
    logs = [log(uuid='first', seconds=0), log(uuid='second', seconds=25)]
    result = competitive(list(reversed(rows)) if reverse else rows,
                         *(reversed(logs) if reverse else logs))
    assert result['near'].uuid == 'first'
    assert result['far'].uuid == 'second'


@pytest.mark.parametrize('changes', [dict(direction='OUT'), dict(timestamp_utc=STAMP+timedelta(seconds=301)),
    dict(yard_id='another-yard')])
def test_competitive_invalid_close_pairs_do_not_steal_container(changes):
    result = competitive([aiq(ground_truth_transaction_id='invalid', **changes),
                          aiq(ground_truth_transaction_id='valid', timestamp_utc=STAMP+timedelta(seconds=10))],
                         log(seconds=0))
    assert result['invalid'].uuid is None
    assert result['invalid'].transaction_match_status == TransactionMatchStatus.GROUND_TRUTH_ONLY
    assert result['valid'].uuid == 'container-uuid'


@pytest.mark.parametrize('second_offset', [1, -1, 1.000001])
def test_competitive_equal_aiq_costs_do_not_pick_identity_order(second_offset):
    result = competitive([aiq(ground_truth_transaction_id='a', timestamp_utc=STAMP+timedelta(seconds=1)),
        aiq(ground_truth_transaction_id='z', timestamp_utc=STAMP+timedelta(seconds=second_offset))],
        log(seconds=0))
    assert all(row.uuid is None and row.transaction_match_status == TransactionMatchStatus.AMBIGUOUS
               for row in result.values())


def test_disjoint_equal_cost_pairs_are_not_ambiguous():
    result = competitive([aiq(ground_truth_transaction_id='a', timestamp_utc=STAMP),
                          aiq(ground_truth_transaction_id='b', timestamp_utc=STAMP+timedelta(seconds=100))],
                         log(uuid='a-log', seconds=1), log(uuid='b-log', seconds=101))
    assert result['a'].uuid == 'a-log' and result['b'].uuid == 'b-log'


def test_competition_tied_containers_and_farther_contender_are_not_arbitrarily_assigned():
    result = competitive([aiq(ground_truth_transaction_id='tie'),
        aiq(ground_truth_transaction_id='far', timestamp_utc=STAMP+timedelta(seconds=100))],
        log(uuid='left', seconds=-10), log(uuid='right', seconds=10))
    assert result['tie'].transaction_match_status == TransactionMatchStatus.AMBIGUOUS
    assert result['far'].uuid is None
    assert result['far'].transaction_match_status == TransactionMatchStatus.AMBIGUOUS


def test_losing_contender_can_take_next_available_candidate():
    result = competitive([aiq(ground_truth_transaction_id='near'),
        aiq(ground_truth_transaction_id='far', timestamp_utc=STAMP+timedelta(seconds=20))],
        log(uuid='shared', seconds=1), log(uuid='alternative', seconds=40))
    assert result['near'].uuid == 'shared'
    assert result['far'].uuid == 'alternative'


def test_competitive_direction_missing_on_one_side_remains_valid():
    result = competitive([aiq(ground_truth_transaction_id='near', direction=None),
        aiq(ground_truth_transaction_id='far', timestamp_utc=STAMP+timedelta(seconds=30))],
        log(seconds=1))
    assert result['near'].uuid == 'container-uuid'
    assert result['far'].uuid is None


def test_competitive_mode_leaves_archive_gate_preference_unchanged():
    rows = TransactionMatcher(TransactionMatcherConfig(include_container_logs_only=False)).match(
        ground_truth_transactions=[aiq(ground_truth_transaction_id='aiq'),
            aiq(ground_truth_transaction_id='archive', ground_truth_source=GroundTruthSource.ARCHIVE)],
        container_logs_by_uuid={item.uuid: item for item in [
            log(uuid='aiq-nearest', seconds=0, gate_ids=['Different gate']),
            log(uuid='archive-closer', seconds=5, gate_ids=['Different gate']),
            log(uuid='archive-gate', seconds=20)]}, yard_id='rmc_msg', competitive_aiq=True)
    indexed = {row.ground_truth_transaction_id: row for row in rows}
    assert indexed['aiq'].uuid == 'aiq-nearest'
    assert indexed['archive'].uuid == 'archive-gate'
    assert indexed['archive'].match_method == MatchMethod.TIMESTAMP_DIRECTION_GATE


@pytest.mark.parametrize('competitive_mode', [False, True])
@pytest.mark.parametrize('source_changes,log_changes,accepted', [
    ({}, {}, True),
    ({}, {'gate_names': ['Long Term'], 'remote_gate_ids': []}, False),
    ({}, {'gate_names': [], 'remote_gate_ids': ['109246369']}, False),
    ({}, {'remote_gate_ids': ['109246369']}, False),
    ({}, {'gate_names': ['Long Term']}, False),
    ({}, {'gate_names': [], 'remote_gate_ids': []}, True),
    ({'gate_name': None, 'gate_id': None}, {}, True),
    ({}, {'gate_names': [' main-gate '], 'remote_gate_ids': ['0109246188']}, True),
    ({}, {'fusion_center_ids': ['Completely different center'], 'gate_ids': ['Wrong legacy gate']}, True),
    ({}, {'gate_names': ['Main Gate'], 'remote_gate_ids': []}, True),
    ({}, {'gate_names': [], 'remote_gate_ids': ['109246188']}, True),
])
def test_explicit_aiq_gate_comparisons_must_all_agree(
        competitive_mode, source_changes, log_changes, accepted):
    source_values = dict(gate_name='Main Gate', gate_id='109246188')
    source_values.update(source_changes)
    log_values = dict(gate_names=['Main Gate'], remote_gate_ids=['109246188'])
    log_values.update(log_changes)
    row = TransactionMatcher(TransactionMatcherConfig(include_container_logs_only=False)).match(
        ground_truth_transactions=[aiq(**source_values)],
        container_logs_by_uuid={'container-uuid': log(**log_values)}, yard_id='rmc_msg',
        competitive_aiq=competitive_mode)[0]
    assert row.transaction_match_status == (TransactionMatchStatus.MATCHED if accepted
        else TransactionMatchStatus.GROUND_TRUTH_ONLY)
    assert row.uuid == ('container-uuid' if accepted else None)
    if accepted:
        assert row.container_logs_gate_names == log_values['gate_names']
        assert row.container_logs_remote_gate_ids == log_values['remote_gate_ids']
        assert row.metadata['container_logs']['gate_names'] == log_values['gate_names']


def test_production_conflicting_gate_rejected_even_without_nearer_contender():
    source = aiq(ground_truth_transaction_id='7101518', yard_id='cre_slc',
        timestamp_utc=datetime.fromisoformat('2026-10-14T02:41:59.757030+00:00'),
        gate_name='Long Term', gate_id='109246369')
    candidate = log(uuid='5e6d043e-9bce-41ea-a1c2-6492ed40b8eb',
        first_timestamp_utc=datetime.fromisoformat('2026-10-14T02:42:30+00:00'),
        gate_names=['Main Gate'], remote_gate_ids=['109246188'],
        fusion_center_ids=['Gate 3 Entrance'], metadata={'yard_id': 'cre_slc'})
    assert match(source, candidate, yard_id='cre_slc').uuid is None
    winner = aiq(ground_truth_transaction_id='7101531', yard_id='cre_slc',
        timestamp_utc=datetime.fromisoformat('2026-10-14T02:42:31.279612+00:00'),
        gate_name='Main Gate', gate_id='109246188')
    matched = TransactionMatcher(TransactionMatcherConfig(include_container_logs_only=False)).match(
        ground_truth_transactions=[source, winner], container_logs_by_uuid={candidate.uuid: candidate},
        yard_id='cre_slc', competitive_aiq=True)
    records = {row.ground_truth_transaction_id: row for row in matched}
    assert records['7101518'].uuid is None
    assert records['7101531'].uuid == candidate.uuid
    assert records['7101531'].container_logs_fusion_center_ids == ['Gate 3 Entrance']


def test_archive_ignores_new_explicit_gate_identity_validation():
    source = aiq(ground_truth_source=GroundTruthSource.ARCHIVE, uuid=None,
                 gate_name='Main Gate', gate_id='109246188')
    row = match(source, log(gate_ids=['Main Gate'], gate_names=['Other'], remote_gate_ids=['109246369']))
    assert row.transaction_match_status == TransactionMatchStatus.MATCHED
    assert row.match_method == MatchMethod.TIMESTAMP_DIRECTION_GATE
