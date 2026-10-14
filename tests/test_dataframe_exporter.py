"""Report provenance follows ground-truth source, independent of match algorithm."""
from copy import deepcopy

import pytest

from app.exporters.dataframe_exporter import DataFrameExporter
from app.models.unified_transaction import (
    GroundTruthSource, MatchMethod, TransactionMatchStatus, UnifiedTransaction,
)


@pytest.mark.parametrize('source,method,expected', [
    (GroundTruthSource.AIQ, MatchMethod.TIMESTAMP_DIRECTION, 'CONTAINER_LOGS -> AIQ'),
    (GroundTruthSource.AIQ, MatchMethod.TIMESTAMP_DIRECTION_GATE, 'CONTAINER_LOGS -> AIQ'),
    (GroundTruthSource.AIQ, MatchMethod.UUID, 'CONTAINER_LOGS -> AIQ'),
    (GroundTruthSource.ARCHIVE, MatchMethod.TIMESTAMP_DIRECTION, 'CONTAINER_LOGS -> ARCHIVE'),
    (GroundTruthSource.ARCHIVE, MatchMethod.TIMESTAMP_DIRECTION_GATE, 'CONTAINER_LOGS -> ARCHIVE'),
    (GroundTruthSource.NONE, MatchMethod.NONE, 'NONE'),
    (GroundTruthSource.AIQ, MatchMethod.NONE, 'NONE'),
    (GroundTruthSource.ARCHIVE, MatchMethod.NONE, 'NONE'),
])
@pytest.mark.parametrize('as_strings', [False, True])
def test_exported_match_method_uses_source_and_method(source, method, expected, as_strings):
    transaction = UnifiedTransaction(yard_id='test',
        ground_truth_source=source.value if as_strings else source,
        match_method=method.value if as_strings else method)
    frame = DataFrameExporter().build_dataframe([transaction])
    assert frame.iloc[0]['Ground Truth Source'] == source.value
    assert frame.iloc[0]['Match Method'] == expected


def test_remco_aiq_timestamp_match_exports_aiq_provenance_without_mutating_identity():
    transaction = UnifiedTransaction(yard_id='rmc_msg', ground_truth_transaction_id='6840696',
        ground_truth_source=GroundTruthSource.AIQ,
        uuid='ce1cfcd7-47d6-4fcd-bba9-b719b774c357',
        transaction_match_status=TransactionMatchStatus.MATCHED,
        match_method=MatchMethod.TIMESTAMP_DIRECTION)
    before = deepcopy(transaction)
    frame = DataFrameExporter().build_dataframe([transaction])
    row = frame.iloc[0]
    assert row['Ground Truth Source'] == 'AIQ'
    assert row['Match Method'] == 'CONTAINER_LOGS -> AIQ'
    assert row['UUID'] == transaction.uuid
    assert row['Ground Truth Transaction ID'] == '6840696'
    assert row['Transaction Match Status'] == 'MATCHED'
    assert transaction == before
    assert list(frame.columns) == DataFrameExporter.COLUMNS


@pytest.mark.parametrize('method', [MatchMethod.UUID, MatchMethod.TIMESTAMP_DIRECTION])
def test_missing_ground_truth_source_does_not_infer_provenance_from_method(method):
    row = DataFrameExporter().transaction_to_row(
        UnifiedTransaction(yard_id='test', ground_truth_source=GroundTruthSource.NONE,
                           match_method=method))
    assert row['Match Method'] == method.value


GATE_COLUMNS = ['Container Logs Gate Names', 'Container Logs Remote Gate IDs',
                'Container Logs Fusion Center IDs']


def gate_transaction():
    return UnifiedTransaction(yard_id='cre_slc', gate_id='109246188', gate_name='Main Gate',
        direction='IN', container_logs_gate_names=['Main Gate', 'Long Term', 'Main Gate'],
        container_logs_remote_gate_ids=['109246188', '109246370'],
        container_logs_fusion_center_ids=['Gate 3 Entrance', 'Long Term Exit'])


def test_gate_columns_keep_ground_truth_fields_and_format_lists_consistently():
    transaction = gate_transaction()
    frame = DataFrameExporter().build_dataframe([transaction])
    row = frame.iloc[0]
    assert row['Gate ID'] == '109246188'
    assert row['Gate Name'] == 'Main Gate' and row['Direction'] == 'IN'
    assert row['Container Logs Gate Names'] == 'Main Gate, Long Term'
    assert row['Container Logs Remote Gate IDs'] == '109246188, 109246370'
    assert row['Container Logs Fusion Center IDs'] == 'Gate 3 Entrance, Long Term Exit'
    assert 'Metadata' not in frame.columns
    assert transaction.to_dict()['container_logs_remote_gate_ids'] == ['109246188', '109246370']


def test_empty_gate_collections_export_blank_cells():
    frame = DataFrameExporter().build_dataframe([UnifiedTransaction(yard_id='test')])
    assert all(frame.iloc[0][name] == '' for name in GATE_COLUMNS)


def test_google_sheets_uses_dataframe_gate_columns_without_extra_builder(monkeypatch):
    from app.exporters.google_sheets_exporter import GoogleSheetsExporter
    sheets = object.__new__(GoogleSheetsExporter)
    sheets.dataframe_exporter = DataFrameExporter()
    captured = []
    def export(frame):
        captured.extend(sheets._dataframe_to_values(frame))
        return {'column_count': len(frame.columns)}
    monkeypatch.setattr(sheets, 'export_dataframe', export)
    result = sheets.export_transactions([gate_transaction()])
    assert result['column_count'] == len(DataFrameExporter.COLUMNS)
    values = dict(zip(captured[0], captured[1]))
    assert values['Container Logs Gate Names'] == 'Main Gate, Long Term'
    assert values['Container Logs Remote Gate IDs'] == '109246188, 109246370'
    assert values['Container Logs Fusion Center IDs'] == 'Gate 3 Entrance, Long Term Exit'


def test_csv_includes_explicit_gate_columns(tmp_path):
    import csv
    path = tmp_path / 'transactions.csv'
    DataFrameExporter().export_csv([gate_transaction()], str(path))
    with path.open() as handle:
        row = next(csv.DictReader(handle))
    assert row['Container Logs Gate Names'] == 'Main Gate, Long Term'
    assert row['Container Logs Remote Gate IDs'] == '109246188, 109246370'
    assert row['Container Logs Fusion Center IDs'] == 'Gate 3 Entrance, Long Term Exit'


def test_excel_path_receives_same_gate_columns(monkeypatch, tmp_path):
    import pandas as pd
    captured = []
    monkeypatch.setattr(pd.DataFrame, 'to_excel', lambda frame, *args, **kwargs: captured.append(frame))
    DataFrameExporter().export_excel([gate_transaction()], str(tmp_path / 'transactions.xlsx'))
    assert captured[0].iloc[0]['Container Logs Remote Gate IDs'] == '109246188, 109246370'
    assert all(name in captured[0].columns for name in GATE_COLUMNS)
