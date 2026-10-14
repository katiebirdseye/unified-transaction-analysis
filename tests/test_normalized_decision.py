from datetime import timedelta

import pytest
from test_proof_api import api, row, result, PAYLOAD, STAMP
from app.models.unified_transaction import TransactionMatchStatus
from app.services.decision_serializer import normalized_analysis, compare_field
from app.services.analysis_service import AnalysisService, AnalysisRequest


@pytest.mark.parametrize('status,expected', [('MATCHED','DETECTED'), ('GROUND_TRUTH_ONLY','MISSED'),
    ('AMBIGUOUS','AMBIGUOUS'), ('CONTAINER_LOGS_ONLY','INSUFFICIENT_DATA')])
def test_detection_mapping(status, expected):
    body = normalized_analysis([row(TransactionMatchStatus(status))], analysis_complete=True)
    assert body['decision']['detection_result'] == expected


@pytest.mark.parametrize('rows,complete,expected', [([],True,'INSUFFICIENT_DATA'),
    ([row()],False,'INSUFFICIENT_DATA'), ([row(),row()],True,'AMBIGUOUS'),
    ([row(timestamp_utc=None)],True,'INSUFFICIENT_DATA')])
def test_uncertain_detection(rows, complete, expected):
    assert normalized_analysis(rows, analysis_complete=complete)['decision']['detection_result'] == expected


@pytest.mark.parametrize('field,truth,prediction,expected', [
    ('truck_number','T5031',None,'MISSING_PREDICTION'),
    ('truck_number','001','001','MATCH'), ('truck_number','001','1','MISMATCH'),
    ('truck_number','A-1','A1','MISMATCH'), ('truck_number','abc','ABC','MISMATCH'),
    ('truck_number',None,'123','MISSING_GROUND_TRUTH'),
    ('truck_number','/',' ','NOT_APPLICABLE'), ('truck_number','  ','/','NOT_APPLICABLE'),
    ('truck_number',' 075046 ','075046','MATCH'),
    ('trailer_number_1','TX53153','TX53153','MATCH'),
    ('trailer_number_2','009','008','MISMATCH'),
    ('vehicle_type','TRUCK','truck','MATCH'), ('vehicle_type','WALK','WALK','MATCH'),
    ('vehicle_type','TRUCK','CAR','MISMATCH')])
def test_field_comparisons(field, truth, prediction, expected):
    tx=row(**{'ground_truth_'+field:truth,'alisa_prediction_'+field:prediction})
    comparison=compare_field(tx,field)
    assert comparison['result']==expected
    assert comparison['raw_ground_truth']==truth and comparison['raw_prediction']==prediction
    if truth and truth.strip() not in ('','/'):
        assert comparison['ground_truth']==truth.strip()


def test_real_examples():
    matched=row(ground_truth_truck_number='T5031', alisa_prediction_truck_number=None,
        ground_truth_trailer_number_1='TX53153', alisa_prediction_trailer_number_1='TX53153',
        ground_truth_vehicle_type='TRUCK', alisa_prediction_vehicle_type='TRUCK')
    first=normalized_analysis([matched],analysis_complete=True)
    assert first['decision']['detection_result']=='DETECTED'
    assert [x['result'] for x in first['field_comparisons']]==['MISSING_PREDICTION','MATCH','NOT_APPLICABLE','MATCH']
    missed=row(TransactionMatchStatus.GROUND_TRUTH_ONLY, ground_truth_truck_number='/',
        alisa_prediction_truck_number=None, ground_truth_vehicle_type='WALK', alisa_prediction_vehicle_type='WALK')
    second=normalized_analysis([missed],analysis_complete=True)
    assert second['decision']['detection_result']=='MISSED'
    assert second['field_comparisons'][0]['result']=='NOT_APPLICABLE'
    assert second['field_comparisons'][3]['result']=='MATCH'


def test_v2_normalized_layer_does_not_change_existing_facts_or_exports(api):
    tx=row()
    api[1].result=result([tx],decision_analysis_complete=True)
    v2=api[0].post('/api/analyze/proof/v2',json=PAYLOAD).json()
    assert v2['decision']['detection_result']=='DETECTED'
    assert len(v2['field_comparisons'])==4
    v1=api[0].post('/api/analyze/proof',json=PAYLOAD).json()
    assert 'decision' not in v1
    assert v1['coverage']==v2['coverage'] and v1['absence']==v2['absence']
    for candidate in v2['candidates']:
        assert len(candidate['system_outputs'])==4
        assert candidate['automatic_origin']['status']=='unknown'
    assert 'decision_analysis_complete' not in api[0].post('/api/analyze',json=dict(yard_id='test',dates=['2026-10-02'])).json()


def test_missing_container_file_is_incomplete_analysis(api,tmp_path):
    from app.services.ground_truth_selector import GroundTruthSelection
    from types import SimpleNamespace
    analysis=AnalysisService(ground_truth_selector=SimpleNamespace(select=lambda **kw:
        GroundTruthSelection(yard_id='test', transactions=[row()], source='AIQ', reason='test')),
        transaction_matcher=__import__('app.matching.transaction_matcher', fromlist=['TransactionMatcher']).TransactionMatcher(),
        utc_to_local=lambda dt,yard:dt.replace(tzinfo=None)-timedelta(hours=4),
        container_logs_dir=str(tmp_path), local_to_utc=lambda dt,yard:(dt+timedelta(hours=4)).replace(tzinfo=STAMP.tzinfo))
    api[2].analysis_service=analysis
    response=api[0].post('/api/analyze/proof/v2',json=PAYLOAD)
    assert response.status_code==200
    assert response.json()['decision']['detection_result']=='INSUFFICIENT_DATA'


def test_postgresql_raw_comparison_values_survive_legacy_cleaning(monkeypatch):
    from test_system_output_proof import collected
    tx=collected(monkeypatch,truck_entered=' 00075046 ',truck_alisa='00075046')
    item=compare_field(tx,'truck_number')
    assert item['raw_ground_truth']==' 00075046 '
    assert item['ground_truth']=='00075046' and item['result']=='MATCH'
    assert tx.ground_truth_truck_number=='00075046'
    assert '_decision_raw_fields' not in tx.to_dict()


def test_successful_empty_source_read_can_return_product_miss_without_coverage_certificate(api,tmp_path):
    from app.matching.transaction_matcher import TransactionMatcher
    from app.services.ground_truth_selector import GroundTruthSelection
    from types import SimpleNamespace
    directory=tmp_path/'test'
    directory.mkdir()
    (directory/'container_log.txt').write_text('')
    service=AnalysisService(ground_truth_selector=SimpleNamespace(select=lambda **kw:
        GroundTruthSelection(yard_id='test',transactions=[row()],source='AIQ')),
        transaction_matcher=TransactionMatcher(),container_logs_dir=str(tmp_path),
        local_to_utc=lambda dt,yard:(dt+timedelta(hours=4)).replace(tzinfo=STAMP.tzinfo),
        utc_to_local=lambda dt,yard:dt.replace(tzinfo=None)-timedelta(hours=4))
    api[2].analysis_service=service
    body=api[0].post('/api/analyze/proof/v2',json=PAYLOAD).json()
    assert body['decision']['detection_result']=='MISSED'
    assert body['coverage']['generation']['complete']['value'] is None
    assert body['absence'][1]['absent']['value'] is None
