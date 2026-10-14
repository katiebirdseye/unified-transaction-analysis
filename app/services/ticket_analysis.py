"""Ticket identity lookup; existing matching/export logic remains authoritative."""
import csv
import logging
from copy import deepcopy
from datetime import timezone
from io import StringIO

from app.exporters.dataframe_exporter import DataFrameExporter
from app.matching.transaction_matcher import TransactionMatcher
from app.models.unified_transaction import GroundTruthSource
from app.services.decision_serializer import normalized_analysis
from app.services.ground_truth_selector import GroundTruthSelection
from app.services.proof_serializer import utc_string

SCHEMA_VERSION = "unified-ticket.v1"
GATE_MATCHING_RULE = "unified-gate-alphanumeric-uppercase.v1"


def text(value):
    return str(value).strip() if value is not None else ""


def build_ticket_analysis(service, request):
    identity = text(request.transaction_id)
    yard = text(request.yard_id)
    gate = text(request.gate_name)
    direction = text(request.direction).upper()
    match = dict(status="INSUFFICIENT_DATA", transaction_id=identity or None,
        yard_id=yard, requested_gate_name=gate, requested_direction=direction or None,
        matched_gate_name=None, matched_yard_id=None, direction=None,
        ground_truth_source=None, ground_truth_transaction_id=None, uuid=None,
        transaction_timestamp_utc=None, row_index=None,
        gate_matching_rule_version=GATE_MATCHING_RULE,
        direction_validation="NOT_REQUESTED" if not direction else "UNKNOWN")
    response = dict(schema_version=SCHEMA_VERSION, ticket_match=match,
        authoritative_transaction=None, correlation={}, reason_codes=[])

    def finish(status, rows=(), complete=False):
        match["status"] = status
        response.update(normalized_analysis(rows, analysis_complete=complete))
        if status != "EXACT_MATCH":
            response["decision"] = dict(detection_result=(
                "AMBIGUOUS" if status == "AMBIGUOUS" else "INSUFFICIENT_DATA"),
                detection_reason=status)
            response["reason_codes"].append(status)
        # Derive arrays from the canonical export itself: exact headers, blanks,
        # value spelling and row order are identical to the CSV representation.
        frame = DataFrameExporter().build_dataframe(list(rows))
        buffer = StringIO()
        frame.to_csv(buffer, index=False)
        response["csv_text"] = buffer.getvalue()
        parsed = list(csv.reader(StringIO(response["csv_text"])))
        response.update(headers=parsed[0], rows=parsed[1:],
            raw_format="csv", raw_source="DataFrameExporter")
        return response

    if not identity:
        return finish("MISSING_TRANSACTION_ID")
    if not yard or not gate:
        return finish("INSUFFICIENT_DATA")
    selector = service.ground_truth_selector
    lookup = getattr(selector.aiq_provider, "get_transactions_by_id", None)
    if lookup is None:
        response["reason_codes"].append("EXACT_ID_LOOKUP_UNAVAILABLE")
        return finish("INSUFFICIENT_DATA")
    try:
        records = lookup(transaction_id=identity)
    except Exception:
        logging.exception("Exact AIQ ticket lookup failed")
        response["reason_codes"].append("AIQ_LOOKUP_FAILED")
        return finish("INSUFFICIENT_DATA")
    if not records:
        return finish("TRANSACTION_NOT_FOUND")
    # Do not hide contradictions or select a different nearby record.
    if any(text(row.ground_truth_transaction_id) != identity or
           row.ground_truth_source != GroundTruthSource.AIQ for row in records):
        response["reason_codes"].append("AIQ_IDENTITY_CONTRADICTION")
        return finish("INSUFFICIENT_DATA")
    if len(records) != 1:
        return finish("AMBIGUOUS")
    source = deepcopy(records[0])
    source.uuid = None
    match.update(matched_yard_id=source.yard_id, matched_gate_name=source.gate_name,
        direction=source.direction, ground_truth_source="AIQ",
        ground_truth_transaction_id=source.ground_truth_transaction_id, uuid=None,
        transaction_timestamp_utc=utc_string(source.timestamp_utc))
    if not text(source.yard_id):
        return finish("INSUFFICIENT_DATA")
    if text(source.yard_id).casefold() != yard.casefold():
        return finish("YARD_MISMATCH")
    source_gate = TransactionMatcher._normalize_gate(source.gate_name)
    requested_gate = TransactionMatcher._normalize_gate(gate)
    if not source_gate or not requested_gate:
        response["reason_codes"].append("TRANSACTION_GATE_UNAVAILABLE")
        return finish("INSUFFICIENT_DATA")
    if source_gate != requested_gate:
        return finish("GATE_MISMATCH")
    if direction and text(source.direction):
        if direction != text(source.direction).upper():
            match["direction_validation"] = "MISMATCH"
            return finish("DIRECTION_MISMATCH")
        match["direction_validation"] = "MATCH"
    if direction and not text(source.direction):
        response["reason_codes"].append("TRANSACTION_DIRECTION_UNAVAILABLE")
    if source.timestamp_utc is None or source.timestamp_utc.tzinfo is None:
        response["reason_codes"].append("TRANSACTION_TIME_UNAVAILABLE")
        return finish("INSUFFICIENT_DATA")
    # All subsequent acquisition is anchored to the identified AIQ transaction.
    stamp = source.timestamp_utc.astimezone(timezone.utc)
    try:
        local = service.utc_to_local(stamp, source.yard_id)
        dates = [local.date()]
        start, end = service._build_utc_range(yard_id=source.yard_id, dates=dates)
    except Exception:
        logging.exception("Could not resolve exact transaction local date")
        response["reason_codes"].append("TRANSACTION_TIMEZONE_UNAVAILABLE")
        return finish("INSUFFICIENT_DATA")
    response["authoritative_transaction"] = dict(yard_id=source.yard_id,
        ground_truth_transaction_id=source.ground_truth_transaction_id,
        ground_truth_source="AIQ", uuid=None,
        timestamp_utc=utc_string(stamp), timestamp_local=local.isoformat(),
        gate_id=source.gate_id, gate_name=source.gate_name, direction=source.direction,
        ground_truth_truck_number=source.ground_truth_truck_number,
        ground_truth_trailer_number_1=source.ground_truth_trailer_number_1,
        ground_truth_trailer_number_2=source.ground_truth_trailer_number_2,
        ground_truth_vehicle_type=source.ground_truth_vehicle_type)
    correlation = response["correlation"]
    correlation.update(anchor_source="AIQ_TRANSACTION", uuid=None,
        transaction_timestamp_utc=utc_string(stamp), local_dates=[local.date().isoformat()],
        start_utc=utc_string(start), end_utc=utc_string(end),
        aiq=dict(status="complete", lookup="EXACT_TRANSACTION_ID"))
    archive = []
    archive_error = None
    try:
        archive = selector.archive_provider.get_transactions(yard_id=source.yard_id, dates=dates)
        archive = selector._filter_eligible_archive_transactions(
            transactions=archive, yard_id=source.yard_id, start_utc=start, end_utc=end)
        archive = selector._remove_archive_duplicates_of_aiq(
            aiq_transactions=[source], archive_transactions=archive)
        correlation["archive"] = dict(status="complete", retained_record_count=len(archive))
    except Exception:
        logging.exception("Archive correlation failed for exact ticket transaction")
        archive_error = "ARCHIVE_ACQUISITION_FAILED"
        response["reason_codes"].append(archive_error)
        correlation["archive"] = dict(status="failed")
    try:
        competitors, logs, read_complete = service._load_ticket_competition(source)
        correlation["aiq"].update(competition="CONNECTED_TIME_DIRECTION_CANDIDATES",
            competing_transaction_count=len(competitors))
        correlation["container_logs"] = dict(status="complete" if read_complete else "partial",
            uuid_count=len(logs))
        if not read_complete:
            response["reason_codes"].append("CONTAINER_LOG_ACQUISITION_INCOMPLETE")
        selection = GroundTruthSelection(yard_id=source.yard_id,
            transactions=[*competitors, *archive], source="AIQ", aiq_transaction_count=len(competitors),
            archive_transaction_count=len(archive), archive_error=archive_error)
        matched = service._match_transactions(yard_id=source.yard_id,
            ground_truth_selection=selection, container_logs_by_uuid=logs,
            include_container_logs_only=False, competitive_aiq=True)
        rows = [row for row in matched if row.ground_truth_source == GroundTruthSource.AIQ
                and text(row.ground_truth_transaction_id) == identity]
        if len(rows) != 1:
            response["reason_codes"].append("CORRELATION_IDENTITY_CONTRADICTION")
            return finish("INSUFFICIENT_DATA")
    except Exception:
        logging.exception("Container correlation failed for exact ticket transaction")
        correlation["container_logs"] = dict(status="failed")
        correlation["aiq"]["competition"] = "failed"
        response["reason_codes"].append("CONTAINER_CORRELATION_FAILED")
        rows, read_complete = [source], False
    match["row_index"] = 0
    matched_uuid = rows[0].uuid
    match["uuid"] = matched_uuid
    response["authoritative_transaction"]["uuid"] = matched_uuid
    correlation["uuid"] = matched_uuid
    # Identity can be exact even when correlation is incomplete; only the latter
    # controls normalized detection. No absence or coverage is certified here.
    return finish("EXACT_MATCH", rows, complete=read_complete and archive_error is None)
