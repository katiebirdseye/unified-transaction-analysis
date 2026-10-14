from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class GroundTruthSource(str, Enum):
    AIQ = "AIQ"
    ARCHIVE = "ARCHIVE"
    NONE = "NONE"


class TransactionMatchStatus(str, Enum):
    MATCHED = "MATCHED"
    GROUND_TRUTH_ONLY = "GROUND_TRUTH_ONLY"
    CONTAINER_LOGS_ONLY = "CONTAINER_LOGS_ONLY"
    AMBIGUOUS = "AMBIGUOUS"


class MatchMethod(str, Enum):
    UUID = "UUID"
    TIMESTAMP_DIRECTION = "TIMESTAMP_DIRECTION"
    TIMESTAMP_DIRECTION_GATE = "TIMESTAMP_DIRECTION_GATE"
    NONE = "NONE"


@dataclass
class UnifiedTransaction:
    """
    One instance represents one complete transaction.

    Ground truth:
      - AIQ when available
      - Archive as fallback

    Predictions:
      - Alisa prediction from AIQ
      - All OCR values observed in container logs
    """

    # Identity
    yard_id: str
    # Container-log identity only; never an AIQ external_uuid.
    uuid: Optional[str] = None
    ground_truth_transaction_id: Optional[str] = None
    ground_truth_lookup_yard_id: Optional[str] = None

    # Time
    timestamp_local: Optional[datetime] = None
    timestamp_utc: Optional[datetime] = None

    container_logs_first_timestamp_utc: Optional[datetime] = None
    container_logs_last_timestamp_utc: Optional[datetime] = None
    container_logs_timestamps_utc: list[datetime] = field(
        default_factory=list
    )

    # Location
    gate_id: Optional[str] = None
    gate_name: Optional[str] = None
    direction: Optional[str] = None

    # Matching metadata
    ground_truth_source: GroundTruthSource = GroundTruthSource.NONE
    transaction_match_status: Optional[TransactionMatchStatus] = None
    match_method: MatchMethod = MatchMethod.NONE
    match_time_difference_seconds: Optional[float] = None

    # Ground truth
    ground_truth_truck_number: Optional[str] = None
    ground_truth_trailer_number_1: Optional[str] = None
    ground_truth_trailer_number_2: Optional[str] = None
    ground_truth_vehicle_type: Optional[str] = None

    # Alisa predictions
    alisa_prediction_truck_number: Optional[str] = None
    alisa_prediction_trailer_number_1: Optional[str] = None
    alisa_prediction_trailer_number_2: Optional[str] = None
    alisa_prediction_vehicle_type: Optional[str] = None

    # All distinct container-log values for the UUID
    container_logs_truck_numbers: list[str] = field(
        default_factory=list
    )
    container_logs_trailer_numbers: list[str] = field(
        default_factory=list
    )
    container_logs_vehicle_types: list[str] = field(
        default_factory=list
    )

    container_logs_gate_names: list[str] = field(default_factory=list)
    container_logs_remote_gate_ids: list[str] = field(default_factory=list)
    container_logs_fusion_center_ids: list[str] = field(default_factory=list)

    container_logs_event_types: list[str] = field(
        default_factory=list
    )

    flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Collector-owned observation, excluded from CSV/legacy JSON and equality.
    _retained_aiq_fields: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    _decision_raw_fields: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def add_flag(self, flag: str) -> None:
        cleaned = flag.strip()

        if cleaned and cleaned not in self.flags:
            self.flags.append(cleaned)

    def to_dict(self) -> dict[str, Any]:
        def serialize(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()

            if isinstance(value, Enum):
                return value.value

            if isinstance(value, list):
                return [serialize(item) for item in value]

            if isinstance(value, dict):
                return {
                    key: serialize(item)
                    for key, item in value.items()
                }

            return value

        return {
            key: serialize(value)
            for key, value in asdict(self).items()
            if key not in ("_retained_aiq_fields", "_decision_raw_fields")
        }