"""Confirmation anchors survive later alert fallback updates and OCR events."""
import json
from datetime import datetime

import pytest

from app.matching.transaction_matcher import TransactionMatcher
from app.parsers.container_logs_parser import ContainerLogsParser

UUID = 'ce1cfcd7-47d6-4fcd-bba9-b719b774c357'
CONFIRMATION = datetime.fromisoformat('2026-10-06T12:39:59+00:00')
OCR_TIME = datetime.fromisoformat('2026-10-06T12:40:08+00:00')


def parse(tmp_path, events):
    path = tmp_path / 'container_log.txt'
    path.write_text('\n'.join(f'{stamp} {json.dumps(payload)}' for stamp, payload in events))
    return ContainerLogsParser(str(path)).parse()


def confirmation(**changes):
    payload = dict(event='ALERT', message='Sending Confirmation Alert', track_uuid=UUID,
                   gate_id='Incoming gate', orientation='IN', direction='IN')
    payload.update(changes)
    return payload


def ocr(code_type='truck_number', prediction=1691):
    return dict(event='OCR_GEMINI_PREDICTION', track_uuid=UUID, status='ok',
                code_type=code_type, prediction=prediction)


def test_remco_confirmation_survives_scene_and_extended_data_alerts(tmp_path):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation()),
        ('2026-10-06T12:39:59', dict(event='PERCEPTION', track_uuid=UUID,
            classification_distribution={'straight truck': 1.0})),
        ('2026-10-06T12:40:07', confirmation(message='scene_update',
            gate_id='Scene gate', orientation='OUT', direction='OUT')),
        ('2026-10-06T12:40:08', ocr()),
        ('2026-10-06T12:40:09', confirmation(message='extended_data_update',
            gate_id='MAINENTRANCE', orientation='OUT', direction='OUT')),
        ('2026-10-06T12:40:10', ocr('trailer_number', 'BTIU533072')),
        ('2026-10-06T12:40:11', confirmation(message='extended_data_update',
            gate_id='Later gate', orientation='OUT', direction='OUT')),
    ])[UUID]
    assert record.first_timestamp_utc == CONFIRMATION
    assert record.last_timestamp_utc == datetime.fromisoformat('2026-10-06T12:40:10+00:00')
    assert CONFIRMATION in record.timestamps_utc
    assert record.timestamps_utc.count(CONFIRMATION) == 1
    assert record.metadata['confirmation_alert_timestamp_utc'] == CONFIRMATION
    assert record.metadata['confirmation_alert_orientation'] == 'IN'
    assert record.metadata['confirmation_alert_gate_id'] == 'Incoming gate'
    assert record.metadata['confirmation_alert_direction'] == 'IN'
    assert 'IN' in record.orientations and 'IN' in record.directions
    assert 'Incoming gate' in record.gate_ids
    assert record.truck_numbers == ['1691']
    assert record.trailer_numbers == ['BTIU533072']
    assert record.vehicle_types == ['straight truck']
    # Ordinary alert fallback is still applied to subsequent OCR.
    assert 'Scene gate' in record.gate_ids and 'MAINENTRANCE' in record.gate_ids
    assert TransactionMatcher._get_container_reference_timestamp(record) == CONFIRMATION


@pytest.mark.parametrize('event,message', [
    ('ALERT', 'Sending Confirmation Alert'),
    (' alert ', '  sending confirmation alert  '),
    ('Alert', 'SENDING CONFIRMATION ALERT'),
])
def test_confirmation_creates_record_immediately_without_ocr(tmp_path, event, message):
    record = parse(tmp_path, [('2026-10-06T12:39:59Z',
        confirmation(event=event, message=message))])[UUID]
    assert record.first_timestamp_utc == CONFIRMATION
    assert record.last_timestamp_utc == CONFIRMATION
    assert record.metadata['confirmation_alert_timestamp_utc'] == CONFIRMATION
    assert record.gate_ids == ['Incoming gate']
    assert record.orientations == ['IN'] and record.directions == ['IN']


@pytest.mark.parametrize('message', ['scene_update', 'extended_data_update', None])
def test_ordinary_alert_remains_fallback_without_canonical_timestamp(tmp_path, message):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation(message=message)),
        ('2026-10-06T12:40:08', ocr()),
    ])[UUID]
    assert record.first_timestamp_utc == OCR_TIME
    assert record.metadata == {}
    assert record.gate_ids == ['Incoming gate']
    assert record.orientations == ['IN']
    assert TransactionMatcher._get_container_reference_timestamp(record) == OCR_TIME


def test_confirmation_requires_alert_event_and_uuid(tmp_path):
    assert parse(tmp_path, [('2026-10-06T12:39:59', confirmation(track_uuid=None))]) == {}
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation(event='OTHER')),
        ('2026-10-06T12:40:08', ocr()),
    ])[UUID]
    assert record.first_timestamp_utc == OCR_TIME
    assert record.metadata == {}


def test_confirmation_can_update_an_existing_ocr_record(tmp_path):
    record = parse(tmp_path, [
        ('2026-10-06T12:40:08', ocr()),
        ('2026-10-06T12:39:59', confirmation(direction=None)),
    ])[UUID]
    assert record.first_timestamp_utc == CONFIRMATION
    assert record.last_timestamp_utc == OCR_TIME
    assert record.metadata['confirmation_alert_timestamp_utc'] == CONFIRMATION
    assert record.metadata['confirmation_alert_direction'] is None
    assert record.orientations == ['IN']
    assert record.truck_numbers == ['1691']


def test_repeated_confirmation_preserves_first_snapshot_and_unique_timestamps(tmp_path):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation()),
        ('2026-10-06T12:39:59', confirmation()),
        ('2026-10-06T12:40:08', confirmation(gate_id='Other gate', orientation='OUT')),
    ])[UUID]
    assert record.metadata['confirmation_alert_timestamp_utc'] == CONFIRMATION
    assert record.metadata['confirmation_alert_gate_id'] == 'Incoming gate'
    assert record.metadata['confirmation_alert_orientation'] == 'IN'
    assert record.timestamps_utc == [CONFIRMATION, OCR_TIME]


def test_explicit_gate_identity_is_collected_from_uuid_events_in_first_seen_order(tmp_path):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', dict(event='TRANSACTION', uuid=UUID,
            gate_name='Main Gate', fusion_center_id='Gate 3 Entrance', gate_id='Main Gate')),
        ('2026-10-06T12:39:59', dict(event='PERCEPTION', uuid=UUID,
            gate_name='Main Gate', fusion_center_id='Gate 3 Entrance')),
        ('2026-10-06T12:40:00', dict(event='ALERT_DISPATCH', uuid=UUID,
            dispatch_succeeded=True, response_status=200,
            response_text=json.dumps({'gate_id': 109246188}))),
        ('2026-10-06T12:40:01', dict(event='ALERT_DISPATCH', uuid=UUID,
            dispatch_succeeded=True, response_status='201',
            response_text=json.dumps({'gate_ids': [109246188, '0109246370', 109246370]}))),
        ('2026-10-06T12:40:02', dict(event='PERCEPTION', uuid=UUID,
            gate_name='Long Term', fusion_center_id='Long Term Exit')),
    ])[UUID]
    assert record.gate_names == ['Main Gate', 'Long Term']
    assert record.fusion_center_ids == ['Gate 3 Entrance', 'Long Term Exit']
    assert record.remote_gate_ids == ['109246188', '109246370']
    serialized = record.to_dict()
    for name in ('gate_names', 'remote_gate_ids', 'fusion_center_ids'):
        assert serialized[name] == getattr(record, name)


@pytest.mark.parametrize('response', ['not-json', '{', '[]', 'null', '"text"',
    '{"gate_ids":[true,1.5,"Main Gate",null]}'])
def test_bad_response_json_or_non_numeric_ids_are_ignored(tmp_path, response):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation()),
        ('2026-10-06T12:40:00', dict(event='ALERT_DISPATCH', uuid=UUID,
            dispatch_succeeded=True, response_status=200, response_text=response)),
    ])[UUID]
    assert record.remote_gate_ids == []
    assert record.metadata['confirmation_alert_timestamp_utc'] == CONFIRMATION


@pytest.mark.parametrize('dispatch,status', [(False, 200), (True, 400), (True, 500),
    (None, 200), (True, None), ('true', 200)])
def test_failed_or_unverified_dispatch_does_not_supply_remote_gate_ids(tmp_path, dispatch, status):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:59', confirmation()),
        ('2026-10-06T12:40:00', dict(event='ALERT_DISPATCH', uuid=UUID,
            dispatch_succeeded=dispatch, response_status=status,
            response_text='{"gate_ids":[109246188]}')),
    ])[UUID]
    assert record.remote_gate_ids == []


def test_response_without_uuid_cannot_be_associated_with_next_record(tmp_path):
    record = parse(tmp_path, [
        ('2026-10-06T12:39:58', dict(event='ALERT_DISPATCH', dispatch_succeeded=True,
            response_status=200, response_text='{"gate_id":109246188}')),
        ('2026-10-06T12:39:59', confirmation(gate_id='Main Gate')),
        ('2026-10-06T12:40:00', ocr()),
    ])[UUID]
    assert record.remote_gate_ids == []
    assert record.gate_ids == ['Main Gate']


def test_local_numeric_gate_id_does_not_become_remote_id(tmp_path):
    record = parse(tmp_path, [('2026-10-06T12:39:59', confirmation(gate_id=109246188))])[UUID]
    assert record.remote_gate_ids == []
