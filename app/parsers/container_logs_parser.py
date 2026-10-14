from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.matching.field_comparator import FieldComparator


@dataclass
class ContainerLogValues:
    """
    All relevant values found in container logs for one UUID.

    Final truck_numbers, trailer_numbers, and vehicle_types preserve
    chronological observations when raw prediction events are available.

    Existing OCR hypotheses and corrected OCR results remain available as
    fallback values for older log formats.
    """

    uuid: str

    truck_numbers: list[str] = field(default_factory=list)
    trailer_numbers: list[str] = field(default_factory=list)
    vehicle_types: list[str] = field(default_factory=list)

    # Raw chronological observations.
    gemini_truck_observations: list[str] = field(
        default_factory=list
    )
    gemini_trailer_observations: list[str] = field(
        default_factory=list
    )
    classification_observations: list[str] = field(
        default_factory=list
    )

    event_types: list[str] = field(default_factory=list)
    timestamps_utc: list[datetime] = field(default_factory=list)

    directions: list[str] = field(default_factory=list)
    gate_ids: list[str] = field(default_factory=list)
    orientations: list[str] = field(default_factory=list)
    # Separate transaction names and remote API IDs from legacy gate_ids.
    gate_names: list[str] = field(default_factory=list)
    remote_gate_ids: list[str] = field(default_factory=list)
    fusion_center_ids: list[str] = field(default_factory=list)

    first_timestamp_utc: Optional[datetime] = None
    last_timestamp_utc: Optional[datetime] = None

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the record to a JSON-friendly dictionary.
        """

        return {
            "uuid": self.uuid,
            "truck_numbers": self.truck_numbers,
            "trailer_numbers": self.trailer_numbers,
            "vehicle_types": self.vehicle_types,
            "gemini_truck_observations": (
                self.gemini_truck_observations
            ),
            "gemini_trailer_observations": (
                self.gemini_trailer_observations
            ),
            "classification_observations": (
                self.classification_observations
            ),
            "event_types": self.event_types,
            "timestamps_utc": [
                timestamp.isoformat()
                for timestamp in self.timestamps_utc
            ],
            "directions": self.directions,
            "gate_ids": self.gate_ids,
            "orientations": self.orientations,
            "gate_names": self.gate_names,
            "remote_gate_ids": self.remote_gate_ids,
            "fusion_center_ids": self.fusion_center_ids,
            "first_timestamp_utc": (
                self.first_timestamp_utc.isoformat()
                if self.first_timestamp_utc
                else None
            ),
            "last_timestamp_utc": (
                self.last_timestamp_utc.isoformat()
                if self.last_timestamp_utc
                else None
            ),
            "metadata": self.metadata,
        }


class ContainerLogsParser:
    """
    Extract OCR and classification values from a container log file.

    One ContainerLogValues object is returned per UUID.

    Preferred chronological sources:

    - Truck and trailer:
      OCR_GEMINI_PREDICTION events

    - Vehicle type:
      PERCEPTION classification_distribution events

    Existing OCR and OCR_AFTER_ERROR_CORRECTION extraction is retained as
    fallback behavior when the preferred events are unavailable.
    """

    SUPPORTED_EVENT_TYPES = {
        "OCR",
        "OCR_AFTER_ERROR_CORRECTION",
    }

    GEMINI_EVENT_TYPE = "OCR_GEMINI_PREDICTION"

    UUID_KEYS = (
        "track_uuid",
        "UUID",
        "uuid",
    )

    VEHICLE_TYPE_KEYS = (
        "vehicle_type",
        "vehicle_transport_type",
        "transport_type",
        "classification",
        "track_type",
        "vehicle_class",
    )

    TRAILER_CODE_TYPES = {
        "trailer_number",
        "second_trailer_number",
        "trailer_number_2",
        "trailer_2_number",
    }

    LINE_PATTERN = re.compile(
        r"^(?P<timestamp>"
        r"\d{4}-\d{2}-\d{2}"
        r"T"
        r"\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?"
        r"(?:Z|[+-]\d{2}:\d{2})?"
        r").*?(?P<payload>\{.*\})\s*$"
    )

    def __init__(
        self,
        log_file_path: str,
        *,
        normalized_deduplication: bool = False,
    ) -> None:
        self.log_file_path = log_file_path
        self.normalized_deduplication = normalized_deduplication
        self.value_helper = FieldComparator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self) -> dict[str, ContainerLogValues]:
        """
        Parse the configured container-log file.

        Returns:
            Dictionary keyed by UUID.
        """

        self.read_succeeded = False
        if not os.path.exists(self.log_file_path):
            logging.warning(
                "Container log file does not exist: %s",
                self.log_file_path,
            )
            return {}

        grouped: dict[str, ContainerLogValues] = {}

        alert_metadata_by_uuid: dict[str, dict[str, Any]] = {}

        with open(
            self.log_file_path,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as file_handle:
            for line_number, line in enumerate(
                file_handle,
                start=1,
            ):
                parsed_line = self._parse_line(line)

                if parsed_line is None:
                    continue

                timestamp_utc, payload = parsed_line

                uuid = self._extract_uuid(payload)
                if uuid:
                    gate_identity = self._extract_gate_identity(payload)
                    if any(gate_identity.values()):
                        record = grouped.setdefault(uuid, ContainerLogValues(uuid=uuid))
                        for name, values in gate_identity.items():
                            for value in values:
                                self.value_helper.add_unique_raw_value(getattr(record, name), value)

                # Ordinary alerts remain fallback metadata for later OCR.
                # Canonical confirmation metadata is retained independently.
                if self._is_alert_event(payload):
                    if uuid:
                        alert_metadata = self._extract_alert_metadata(
                            payload=payload,
                            timestamp_utc=timestamp_utc,
                        )
                        alert_metadata_by_uuid[uuid] = alert_metadata
                        if self._is_confirmation_alert(payload):
                            record = grouped.setdefault(uuid, ContainerLogValues(uuid=uuid))
                            self._apply_confirmation_alert(record, alert_metadata)

                    continue

                event = self._extract_event(payload)
                event_type = self._extract_event_type(payload)

                # ------------------------------------------------------
                # Vehicle classification
                # ------------------------------------------------------

                if event == "PERCEPTION":
                    if not uuid:
                        continue

                    if not self._contains_classification_distribution(
                        payload
                    ):
                        continue

                    record = grouped.setdefault(
                        uuid,
                        ContainerLogValues(uuid=uuid),
                    )

                    self._extract_perception_classification(
                        record=record,
                        payload=payload,
                    )

                    continue

                # ------------------------------------------------------
                # Raw Gemini OCR prediction
                # ------------------------------------------------------

                if event_type == self.GEMINI_EVENT_TYPE:
                    if not uuid:
                        logging.debug(
                            "Skipping Gemini OCR event without UUID "
                            "at line %d",
                            line_number,
                        )
                        continue

                    if not self._has_valid_gemini_prediction(
                        payload
                    ):
                        continue

                    record = grouped.setdefault(
                        uuid,
                        ContainerLogValues(uuid=uuid),
                    )

                    self._add_timestamp(
                        record,
                        timestamp_utc,
                    )

                    self.value_helper.add_unique_raw_value(
                        record.event_types,
                        event_type,
                    )

                    self._add_common_metadata(
                        record=record,
                        payload=payload,
                    )

                    alert_metadata = alert_metadata_by_uuid.get(
                        uuid
                    )

                    if alert_metadata:
                        self._apply_alert_metadata(
                            record=record,
                            metadata=alert_metadata,
                        )

                    self._extract_gemini_prediction(
                        record=record,
                        payload=payload,
                    )

                    continue

                # ------------------------------------------------------
                # Existing aggregated/corrected OCR
                # ------------------------------------------------------

                if event_type not in self.SUPPORTED_EVENT_TYPES:
                    continue

                if not uuid:
                    logging.debug(
                        "Skipping OCR event without UUID at line %d",
                        line_number,
                    )
                    continue

                record = grouped.setdefault(
                    uuid,
                    ContainerLogValues(uuid=uuid),
                )

                self._add_timestamp(
                    record,
                    timestamp_utc,
                )

                self.value_helper.add_unique_raw_value(
                    record.event_types,
                    event_type,
                )

                self._add_common_metadata(
                    record=record,
                    payload=payload,
                )

                alert_metadata = alert_metadata_by_uuid.get(
                    uuid
                )

                if alert_metadata:
                    self._apply_alert_metadata(
                        record=record,
                        metadata=alert_metadata,
                    )

                if event_type == "OCR":
                    self._extract_standard_ocr_values(
                        record=record,
                        payload=payload,
                    )

                elif (
                    event_type
                    == "OCR_AFTER_ERROR_CORRECTION"
                ):
                    self._extract_corrected_ocr_values(
                        record=record,
                        payload=payload,
                    )

        self._finalize_chronological_values(
            grouped
        )

        self.read_succeeded = True
        return grouped

    def parse_as_list(self) -> list[ContainerLogValues]:
        """
        Parse the file and return records sorted by first timestamp.
        """

        records = list(
            self.parse().values()
        )

        return sorted(
            records,
            key=lambda record: (
                record.first_timestamp_utc
                or datetime.max.replace(
                    tzinfo=timezone.utc
                )
            ),
        )

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    @staticmethod
    def _finalize_chronological_values(
        grouped: dict[str, ContainerLogValues],
    ) -> None:
        """
        Promote preferred chronological observations to the public lists.

        When no preferred observations exist, values collected from the
        existing OCR hypotheses/corrected OCR logic remain unchanged.
        """

        for record in grouped.values():
            if record.gemini_truck_observations:
                record.truck_numbers = list(
                    record.gemini_truck_observations
                )

            if record.gemini_trailer_observations:
                record.trailer_numbers = list(
                    record.gemini_trailer_observations
                )

            if record.classification_observations:
                record.vehicle_types = list(
                    record.classification_observations
                )

    # ------------------------------------------------------------------
    # Line parsing
    # ------------------------------------------------------------------

    def _parse_line(
        self,
        line: str,
    ) -> Optional[
        tuple[
            datetime,
            dict[str, Any],
        ]
    ]:
        match = self.LINE_PATTERN.match(
            line
        )

        if not match:
            return None

        timestamp_text = match.group(
            "timestamp"
        )

        payload_text = match.group(
            "payload"
        )

        timestamp_utc = self._parse_timestamp(
            timestamp_text
        )

        if timestamp_utc is None:
            return None

        try:
            payload = json.loads(
                payload_text
            )
        except json.JSONDecodeError:
            return None

        if not isinstance(
            payload,
            dict,
        ):
            return None

        return (
            timestamp_utc,
            payload,
        )

    @staticmethod
    def _parse_timestamp(
        timestamp_text: str,
    ) -> Optional[datetime]:
        """
        Parse a container-log timestamp and return a timezone-aware UTC
        datetime.

        Naive timestamps are assumed to be UTC because the existing
        container logs use UTC timestamps.
        """

        cleaned = timestamp_text.strip()

        if cleaned.endswith("Z"):
            cleaned = (
                cleaned[:-1]
                + "+00:00"
            )

        try:
            parsed = datetime.fromisoformat(
                cleaned
            )
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )
        else:
            parsed = parsed.astimezone(
                timezone.utc
            )

        return parsed

    # ------------------------------------------------------------------
    # Event and UUID extraction
    # ------------------------------------------------------------------

    def _extract_uuid(
        self,
        payload: dict[str, Any],
    ) -> str:
        for key in self.UUID_KEYS:
            value = payload.get(key)

            if value is None:
                continue

            cleaned = str(
                value
            ).strip()

            if cleaned:
                return cleaned

        return ""

    @staticmethod
    def _extract_event(
        payload: dict[str, Any],
    ) -> str:
        return str(
            payload.get("event")
            or ""
        ).strip().upper()

    @staticmethod
    def _extract_event_type(
        payload: dict[str, Any],
    ) -> str:
        value = (
            payload.get("event_type")
            or payload.get("event")
            or ""
        )

        return str(
            value
        ).strip().upper()

    @staticmethod
    def _is_alert_event(
        payload: dict[str, Any],
    ) -> bool:
        event = str(
            payload.get("event")
            or ""
        ).strip().upper()

        return event == "ALERT"

    @staticmethod
    def _is_confirmation_alert(payload: dict[str, Any]) -> bool:
        return (
            ContainerLogsParser._is_alert_event(payload)
            and str(payload.get("message") or "").strip().casefold()
            == "sending confirmation alert"
        )

    # ------------------------------------------------------------------
    # Gemini OCR
    # ------------------------------------------------------------------

    @staticmethod
    def _has_valid_gemini_prediction(
        payload: dict[str, Any],
    ) -> bool:
        status = str(
            payload.get("status")
            or ""
        ).strip().lower()

        if status != "ok":
            return False

        prediction = payload.get(
            "prediction"
        )

        if prediction is None:
            return False

        return bool(
            str(prediction).strip()
        )

    def _extract_gemini_prediction(
        self,
        *,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        """
        Append one raw Gemini prediction in chronological file order.

        Repeated values are intentionally preserved.
        """

        prediction = payload.get(
            "prediction"
        )

        code_type = str(
            payload.get("code_type")
            or ""
        ).strip().lower()

        if code_type == "truck_number":
            self._append_raw_observation(
                record.gemini_truck_observations,
                prediction,
            )

        elif code_type in self.TRAILER_CODE_TYPES:
            self._append_raw_observation(
                record.gemini_trailer_observations,
                prediction,
            )

    # ------------------------------------------------------------------
    # Perception classification
    # ------------------------------------------------------------------

    @staticmethod
    def _contains_classification_distribution(
        payload: dict[str, Any],
    ) -> bool:
        distribution = payload.get(
            "classification_distribution"
        )

        return isinstance(
            distribution,
            dict,
        ) and bool(distribution)

    def _extract_perception_classification(
        self,
        *,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        """
        Select the highest-scoring classification label.

        Each classification event is appended in chronological file order.
        Repeated labels are intentionally preserved.
        """

        distribution = payload.get(
            "classification_distribution"
        )

        if not isinstance(
            distribution,
            dict,
        ):
            return

        candidates: list[
            tuple[
                float,
                str,
            ]
        ] = []

        for label, score in distribution.items():
            cleaned_label = str(
                label
            ).strip()

            if not cleaned_label:
                continue

            try:
                numeric_score = float(
                    score
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            candidates.append(
                (
                    numeric_score,
                    cleaned_label,
                )
            )

        if not candidates:
            return

        candidates.sort(
            key=lambda item: (
                -item[0],
                item[1],
            )
        )

        selected_label = candidates[0][1]

        self._append_raw_observation(
            record.classification_observations,
            selected_label,
        )

    # ------------------------------------------------------------------
    # Standard OCR
    # ------------------------------------------------------------------

    def _extract_standard_ocr_values(
        self,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        hypotheses = (
            payload.get("hypotheses")
            or {}
        )

        if not isinstance(
            hypotheses,
            dict,
        ):
            return

        self._add_hypothesis_items(
            target=record.truck_numbers,
            hypotheses=hypotheses,
            key="truck_number",
        )

        self._add_hypothesis_items(
            target=record.trailer_numbers,
            hypotheses=hypotheses,
            key="trailer_number",
        )

        self._add_vehicle_type_hypotheses(
            record=record,
            hypotheses=hypotheses,
        )

        self._add_vehicle_type_from_payload(
            record=record,
            payload=payload,
        )

    def _add_hypothesis_items(
        self,
        target: list[str],
        hypotheses: dict[str, Any],
        key: str,
    ) -> None:
        block = (
            hypotheses.get(key)
            or {}
        )

        if not isinstance(
            block,
            dict,
        ):
            return

        items = (
            block.get("items")
            or []
        )

        if not isinstance(
            items,
            list,
        ):
            return

        for item in items:
            if isinstance(
                item,
                dict,
            ):
                value = item.get(
                    "value"
                )
            else:
                value = item

            self.value_helper.add_unique_raw_value(
                target,
                value,
                normalized_deduplication=(
                    self.normalized_deduplication
                ),
            )

    def _add_vehicle_type_hypotheses(
        self,
        record: ContainerLogValues,
        hypotheses: dict[str, Any],
    ) -> None:
        for key in self.VEHICLE_TYPE_KEYS:
            if key not in hypotheses:
                continue

            block = hypotheses.get(
                key
            )

            if isinstance(
                block,
                dict,
            ):
                items = (
                    block.get("items")
                    or []
                )

                if isinstance(
                    items,
                    list,
                ):
                    for item in items:
                        value = (
                            item.get("value")
                            if isinstance(
                                item,
                                dict,
                            )
                            else item
                        )

                        self.value_helper.add_unique_raw_value(
                            record.vehicle_types,
                            value,
                        )

                self.value_helper.add_unique_raw_value(
                    record.vehicle_types,
                    block.get("value"),
                )

            else:
                self.value_helper.add_unique_raw_value(
                    record.vehicle_types,
                    block,
                )

    # ------------------------------------------------------------------
    # Corrected OCR
    # ------------------------------------------------------------------

    def _extract_corrected_ocr_values(
        self,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        results = (
            payload.get("results")
            or {}
        )

        if not isinstance(
            results,
            dict,
        ):
            return

        self._add_result_value(
            target=record.truck_numbers,
            results=results,
            key="truck_number",
        )

        self._add_result_value(
            target=record.trailer_numbers,
            results=results,
            key="trailer_number",
        )

        for key in self.VEHICLE_TYPE_KEYS:
            self._add_result_value(
                target=record.vehicle_types,
                results=results,
                key=key,
            )

        self._add_vehicle_type_from_payload(
            record=record,
            payload=payload,
        )

    def _add_result_value(
        self,
        target: list[str],
        results: dict[str, Any],
        key: str,
    ) -> None:
        block = results.get(
            key
        )

        if block is None:
            return

        if isinstance(
            block,
            dict,
        ):
            value = block.get(
                "value"
            )
        else:
            value = block

        self.value_helper.add_unique_raw_value(
            target,
            value,
            normalized_deduplication=(
                self.normalized_deduplication
            ),
        )

    # ------------------------------------------------------------------
    # Vehicle-type extraction
    # ------------------------------------------------------------------

    def _add_vehicle_type_from_payload(
        self,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        for key in self.VEHICLE_TYPE_KEYS:
            value = payload.get(
                key
            )

            if isinstance(
                value,
                dict,
            ):
                value = value.get(
                    "value"
                )

            self.value_helper.add_unique_raw_value(
                record.vehicle_types,
                value,
            )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_gate_identity(payload: dict[str, Any]) -> dict[str, list[str]]:
        result = {"gate_names": [], "remote_gate_ids": [], "fusion_center_ids": []}
        for key, target in (("gate_name", "gate_names"), ("fusion_center_id", "fusion_center_ids")):
            value = str(payload.get(key) or "").strip()
            if value:
                result[target].append(value)

        # Only a UUID-associated caller can attach these response IDs. Local
        # gate_id values never enter this namespace, even if they are numeric.
        status = str(payload.get("response_status") or "").strip()
        if (payload.get("dispatch_succeeded") is not True
                or not status.isdigit() or not 200 <= int(status) < 300):
            return result
        response = payload.get("response_text")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (ValueError, TypeError):
                return result
        if not isinstance(response, dict):
            return result
        values = [response.get("gate_id")]
        if isinstance(response.get("gate_ids"), list):
            values.extend(response["gate_ids"])
        for value in values:
            # Numeric identifiers are normalized strings; bools, floats and
            # name-like IDs are not authoritative numeric gate IDs.
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                cleaned = str(value).strip()
                if cleaned.isascii() and cleaned.isdigit():
                    normalized = str(int(cleaned))
                    if normalized not in result["remote_gate_ids"]:
                        result["remote_gate_ids"].append(normalized)
        return result

    def _add_common_metadata(
        self,
        record: ContainerLogValues,
        payload: dict[str, Any],
    ) -> None:
        direction = (
            payload.get("direction")
            or payload.get("ori_inout")
            or ""
        )

        gate_id = (
            payload.get("gate_id")
            or payload.get("fusion_center_id")
            or ""
        )

        orientation = (
            payload.get("orientation")
            or ""
        )

        self.value_helper.add_unique_raw_value(
            record.directions,
            (
                str(direction).upper()
                if direction
                else None
            ),
        )

        self.value_helper.add_unique_raw_value(
            record.gate_ids,
            gate_id,
        )

        self.value_helper.add_unique_raw_value(
            record.orientations,
            (
                str(orientation).upper()
                if orientation
                else None
            ),
        )

    def _extract_alert_metadata(
        self,
        payload: dict[str, Any],
        timestamp_utc: datetime,
    ) -> dict[str, Any]:
        return {
            "timestamp_utc": timestamp_utc,
            "orientation": payload.get(
                "orientation"
            ),
            "gate_id": (
                payload.get("gate_id")
                or payload.get(
                    "fusion_center_id"
                )
            ),
            "direction": (
                payload.get("direction")
                or payload.get(
                    "ori_inout"
                )
            ),
        }

    def _apply_confirmation_alert(
        self,
        record: ContainerLogValues,
        metadata: dict[str, Any],
    ) -> None:
        # Keep the first confirmation snapshot intact, independent of later
        # generic alerts. All confirmation times still enter the collection.
        if "confirmation_alert_timestamp_utc" not in record.metadata:
            record.metadata.update({
                "confirmation_alert_timestamp_utc": metadata["timestamp_utc"],
                "confirmation_alert_orientation": metadata.get("orientation"),
                "confirmation_alert_gate_id": metadata.get("gate_id"),
                "confirmation_alert_direction": metadata.get("direction"),
            })
        self._add_timestamp(record, metadata["timestamp_utc"])
        self._apply_alert_metadata(record, metadata)

    def _apply_alert_metadata(
        self,
        record: ContainerLogValues,
        metadata: dict[str, Any],
    ) -> None:
        self.value_helper.add_unique_raw_value(
            record.orientations,
            metadata.get(
                "orientation"
            ),
        )

        self.value_helper.add_unique_raw_value(
            record.gate_ids,
            metadata.get(
                "gate_id"
            ),
        )

        direction = metadata.get(
            "direction"
        )

        self.value_helper.add_unique_raw_value(
            record.directions,
            (
                str(direction).upper()
                if direction
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Observation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _append_raw_observation(
        target: list[str],
        value: object,
    ) -> None:
        """
        Append a raw observation without deduplicating it.

        This intentionally preserves chronological sequences such as:

            1234 -> 12345 -> 1234
        """

        if value is None:
            return

        cleaned = str(
            value
        ).strip()

        if cleaned:
            target.append(
                cleaned
            )

    # ------------------------------------------------------------------
    # Timestamp handling
    # ------------------------------------------------------------------

    @staticmethod
    def _add_timestamp(
        record: ContainerLogValues,
        timestamp_utc: datetime,
    ) -> None:
        if (
            timestamp_utc
            not in record.timestamps_utc
        ):
            record.timestamps_utc.append(
                timestamp_utc
            )

        if (
            record.first_timestamp_utc is None
            or timestamp_utc
            < record.first_timestamp_utc
        ):
            record.first_timestamp_utc = (
                timestamp_utc
            )

        if (
            record.last_timestamp_utc is None
            or timestamp_utc
            > record.last_timestamp_utc
        ):
            record.last_timestamp_utc = (
                timestamp_utc
            )
