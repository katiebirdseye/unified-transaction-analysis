"""Source-boundary tests: only real collector-shaped SQL rows attest fields."""
from copy import deepcopy
from datetime import timedelta

import pytest
from pydantic import ValidationError

from test_proof_api import api, PAYLOAD, STAMP, UID, row, result, assert_unknown
from app.models.system_output import OutputOrigin, OutputProofResponse
from app.models.unified_transaction import GroundTruthSource
from app.providers.aiq_database_provider import AIQDatabaseProvider
from app.services.proof_serializer import serialize_candidate
from app.services.system_output_serializer import outputs_for


def collected(monkeypatch, **changes):
    provider = AIQDatabaseProvider(host='offline', port=5432, database='offline', user='fake',
        password='not-a-credential', connect_timeout_seconds=1,
        utc_to_local=lambda stamp, yard: stamp.replace(tzinfo=None)-timedelta(hours=4))
    source = dict(transaction_id='42', uuid=UID, yard_id='test', timestamp_utc=STAMP,
        truck_alisa=' 075046 ', truck_output_source='alisa', truck_entered='75046')
    source.update(changes)
    def query(sql, params):
        assert 'tta.vehicle_unit_number_source AS truck_output_source' in sql
        assert "WHEN tta.vehicle_unit_number_source = 'alisa'" in sql
        assert 'ON tt.id = tta.id' in sql
        return [source]
    monkeypatch.setattr(provider, '_execute_query', query)
    return provider.get_transactions(yard_ids=['test'], start_utc=STAMP-timedelta(minutes=10),
                                     end_utc=STAMP+timedelta(minutes=10))[0]


def output_api(api, transaction):
    api[1].result = result([transaction])
    response = api[0].post('/api/analyze/proof/v2', json=PAYLOAD)
    assert response.status_code == 200, response.text
    body = response.json()
    OutputProofResponse.model_validate(body)
    return body


def test_exact_source_value_and_native_record_link(api, monkeypatch):
    transaction = collected(monkeypatch)
    body = output_api(api, transaction)
    assert body['schema_version'] == 'unified-proof.v2'
    assert body['capabilities'] == ['retained-aiq-field-observation.v1']
    output = body['candidates'][0]['system_outputs'][0]
    assert transaction.alisa_prediction_truck_number == '075046'
    assert output['field']['raw_value']['value'] == ' 075046 '
    assert output['field']['raw_value']['source'] == 'AIQ_POSTGRESQL'
    assert output['field']['raw_value']['evidence']['source_record_id'] == '42'
    assert output['event']['uuid']['value'] == UID
    assert output['event']['candidate_link_verified']['value'] is True
    assert output['provenance']['field_attribution'] == 'alisa'
    assert_unknown(output['field']['normalized_value'])
    assert output['field']['normalization_version'] is None
    assert_unknown(output['event']['origin'])
    assert_unknown(body['candidates'][0]['automatic_origin'])
    assert len(api[1].calls) == 1
    assert 'OUTPUT_EVENT_LINK_UNKNOWN' not in body['reason_codes']
    for coverage in body['coverage'].values():
        assert_unknown(coverage['complete'])
    for absence in body['absence']:
        assert_unknown(absence['absent'])
    for field in ('generation', 'receipt', 'save', 'physical_incident_verified'):
        assert_unknown(body['candidates'][0][field])
    assert_unknown(body['candidates'][0]['gate']['verified'])


@pytest.mark.parametrize('origin', ['automatic', 'manual', 'simulated', None])
def test_unacquired_event_origin_cannot_be_invented(monkeypatch, origin):
    transaction = collected(monkeypatch, origin=origin)
    transaction.metadata['origin'] = origin
    transaction.metadata['events'] = [dict(origin=origin, uuid=UID, image='automatic.jpg')]
    assert_unknown(outputs_for(transaction)[0]['event']['origin'])


@pytest.mark.parametrize('origin', ['manual', 'simulated'])
def test_origin_vocabulary_keeps_nonautomatic_distinct(origin):
    # Model vocabulary only; no current provider emits a known event origin.
    fact = OutputOrigin(status='known', value=origin, source='test-authoritative-record',
                        evidence={'record_id': 'synthetic'})
    assert fact.value != 'automatic'


@pytest.mark.parametrize('change', [dict(truck_alisa=None), dict(truck_alisa=42),
    dict(truck_output_source='manual'), dict(truck_output_source='simulated'), dict(truck_output_source=None)])
def test_missing_or_unattributed_field_remains_unknown(monkeypatch, change):
    output = outputs_for(collected(monkeypatch, **change))[0]
    assert_unknown(output['field']['raw_value'])
    assert 'OUTPUT_FIELD_UNKNOWN' in output['reason_codes']


@pytest.mark.parametrize('key,value', [('transaction_id', None), ('transaction_id', 'other'),
    ('yard_id', 'other'), ('collector', 'spreadsheet')])
def test_invalid_candidate_record_link_cannot_attest_output(monkeypatch, key, value):
    transaction = collected(monkeypatch)
    transaction._retained_aiq_fields[key] = value
    output = outputs_for(transaction)[0]
    assert_unknown(output['event']['candidate_link_verified'])
    assert_unknown(output['field']['raw_value'])


@pytest.mark.parametrize('source', [GroundTruthSource.ARCHIVE, GroundTruthSource.NONE])
def test_archive_and_container_candidates_do_not_inherit_aiq_authority(monkeypatch, source):
    transaction = collected(monkeypatch)
    transaction.ground_truth_source = source
    assert_unknown(outputs_for(transaction)[0]['event']['candidate_link_verified'])


def test_raw_container_metadata_and_prediction_columns_are_not_output_proof():
    transaction = row(metadata={'container_logs': {'metadata': {
        'retained_aiq_fields': {'collector': 'aiq-postgresql-fields.v1'},
        'image': '/automatic/75046.png', 'content_digest': 'a'*64,
        'generated_at': STAMP.isoformat(), 'received': True, 'saved': True}}})
    output = outputs_for(transaction)[0]
    assert_unknown(output['field']['raw_value'])
    assert_unknown(output['artifact']['content_digest'])
    assert_unknown(output['event']['candidate_link_verified'])


@pytest.mark.parametrize('artifact', [None, {'path': '/automatic/75046.jpg'},
    {'artifact_id': '42', 'uri': 'https://example.invalid/image', 'sha256': 'f'*64},
    {'bytes': b'trust not established', 'event_uuid': UID}])
def test_unacquired_artifact_and_digest_are_never_authenticated(monkeypatch, artifact):
    transaction = collected(monkeypatch)
    transaction.metadata['artifact'] = artifact
    output = outputs_for(transaction)[0]
    for name in ('artifact_id', 'immutable_reference', 'content_digest', 'event_link_verified', 'camera'):
        assert_unknown(output['artifact'][name])
    assert output['artifact']['digest_algorithm'] is None
    assert output['output_id'] is None


def test_timestamps_and_stages_are_independent(monkeypatch):
    output = outputs_for(collected(monkeypatch))[0]
    assert set(output['state']) == {'generated', 'received', 'saved'}
    assert len({fact['reason_codes'][0] for fact in output['state'].values()}) == 3
    for kind, fact in output['timestamps'].items():
        assert fact['kind'] == kind
        assert_unknown(fact)
    for fact in output['state'].values():
        assert_unknown(fact)
    assert_unknown(output['artifact']['capture_time'])


def test_v1_and_candidate_columns_unchanged_by_provenance_collection(api, monkeypatch):
    transaction = collected(monkeypatch)
    baseline = deepcopy(transaction)
    baseline._retained_aiq_fields = {}
    assert serialize_candidate(transaction) == serialize_candidate(baseline)
    api[1].result = result([transaction])
    first = api[0].post('/api/analyze/proof', json=PAYLOAD).json()
    api[1].result = result([baseline])
    second = api[0].post('/api/analyze/proof', json=PAYLOAD).json()
    assert first == second
    assert 'system_outputs' not in first['candidates'][0]
    assert first['schema_version'] == 'unified-proof.v1'


def test_v2_no_candidate_and_failed_acquisition(api):
    api[1].result = result([])
    body = api[0].post('/api/analyze/proof/v2', json=PAYLOAD).json()
    assert body['candidates'] == [] and 'NO_CANDIDATE' in body['reason_codes']
    api[1].result = RuntimeError('do not disclose')
    response = api[0].post('/api/analyze/proof/v2', json=PAYLOAD)
    assert response.status_code == 502
    assert response.json()['schema_version'] == 'unified-proof.v2'
    assert 'do not disclose' not in response.text


def test_invalid_tri_state_output_values_rejected():
    with pytest.raises(ValidationError):
        OutputOrigin(status='unknown', value='automatic', reason_codes=['OUTPUT_ORIGIN_UNKNOWN'])
    with pytest.raises(ValidationError):
        OutputOrigin(status='known', value='automatic')


def test_collector_observation_does_not_change_legacy_json_or_csv(api, monkeypatch):
    transaction = collected(monkeypatch)
    baseline = deepcopy(transaction)
    baseline._retained_aiq_fields = {}
    assert transaction.to_dict() == baseline.to_dict()
    for endpoint, payload in (('/api/export/csv', PAYLOAD),
                              ('/api/analyze', dict(yard_id='test', dates=['2026-10-02']))):
        api[1].result = result([transaction])
        observed = api[0].post(endpoint, json=payload)
        api[1].result = result([baseline])
        expected = api[0].post(endpoint, json=payload)
        assert observed.status_code == expected.status_code == 200
        assert observed.content == expected.content
        assert observed.headers == expected.headers


def test_collector_private_fields_survive_matching_without_container_authority(monkeypatch):
    from app.matching.transaction_matcher import TransactionMatcher
    transaction = collected(monkeypatch)
    matched = TransactionMatcher().match(ground_truth_transactions=[transaction], container_logs_by_uuid={})
    assert outputs_for(matched[0])[0]['field']['raw_value']['value'] == ' 075046 '


@pytest.mark.parametrize('value,status', [(False, 'known'), (None, 'unknown'), (None, 'not_applicable')])
def test_event_artifact_link_states_remain_distinct(value, status):
    from app.models.proof import ProofFact
    fact = ProofFact(value=value, status=status, source='synthetic-source',
                     evidence={'relation_id': 'test'}, reason_codes=['TEST'])
    assert fact.value is value and fact.status == status


@pytest.mark.parametrize('container_uuid', [None, 'different-container-uuid'])
def test_retained_aiq_identity_is_independent_of_container_uuid(monkeypatch, container_uuid):
    transaction = collected(monkeypatch)
    assert transaction.uuid is None
    transaction.uuid = container_uuid
    output = outputs_for(transaction)[0]
    assert output['field']['raw_value']['value'] == ' 075046 '
    assert output['event']['uuid']['value'] == UID
