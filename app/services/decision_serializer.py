"""Unified owns detection mapping and field comparison; clients display results."""
from app.services.proof_serializer import utc_string

FIELDS = ('truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type')
VERSION = 'unified-decisions.v1'


def available(value):
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ('', '/') else text


def compare_field(transaction, name):
    retained = transaction._decision_raw_fields.get(name, {})
    raw_truth = retained.get('ground_truth', getattr(transaction, 'ground_truth_' + name))
    raw_prediction = retained.get('prediction', getattr(transaction, 'alisa_prediction_' + name))
    truth, prediction = available(raw_truth), available(raw_prediction)
    if truth is None and prediction is None:
        result = 'NOT_APPLICABLE'
    elif prediction is None:
        result = 'MISSING_PREDICTION'
    elif truth is None:
        result = 'MISSING_GROUND_TRUTH'
    else:
        same = truth.casefold() == prediction.casefold() if name == 'vehicle_type' else truth == prediction
        result = 'MATCH' if same else 'MISMATCH'
    return dict(field=name, ground_truth=truth, prediction=prediction,
        raw_ground_truth=raw_truth, raw_prediction=raw_prediction, result=result)


def normalized_analysis(transactions, *, analysis_complete):
    usable = [row for row in transactions if utc_string(row.timestamp_utc) and
              (row.uuid or row.ground_truth_transaction_id)]
    chosen = None
    if not analysis_complete:
        outcome, reason = 'INSUFFICIENT_DATA', 'ANALYSIS_INCOMPLETE'
    elif not usable:
        outcome, reason = 'INSUFFICIENT_DATA', 'NO_USABLE_CANDIDATE'
    elif len(usable) > 1 or any(row.transaction_match_status == 'AMBIGUOUS' or
            any('AMBIGUOUS' in flag for flag in row.flags) for row in usable):
        outcome, reason = 'AMBIGUOUS', 'MULTIPLE_PLAUSIBLE_CANDIDATES' if len(usable) > 1 else 'AMBIGUOUS'
    else:
        chosen = usable[0]
        status = chosen.transaction_match_status
        outcome = {'MATCHED': 'DETECTED', 'GROUND_TRUTH_ONLY': 'MISSED'}.get(status, 'INSUFFICIENT_DATA')
        reason = status.value if hasattr(status, 'value') else status or 'UNKNOWN_MATCH_STATUS'
    transaction = None
    if chosen is not None:
        transaction = dict(yard_id=chosen.yard_id, uuid=chosen.uuid,
            transaction_id=chosen.ground_truth_transaction_id, ground_truth_source=chosen.ground_truth_source.value,
            timestamp_utc=utc_string(chosen.timestamp_utc),
            timestamp_local=chosen.timestamp_local.isoformat() if chosen.timestamp_local else None,
            gate_id=chosen.gate_id, gate_name=chosen.gate_name, direction=chosen.direction)
    return dict(decision_contract_version=VERSION,
        decision=dict(detection_result=outcome, detection_reason=reason), transaction=transaction,
        field_comparisons=[compare_field(chosen, name) for name in FIELDS] if chosen else [],
        flags=sorted({flag for row in transactions for flag in row.flags}))
