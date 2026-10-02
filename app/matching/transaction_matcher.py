from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from app.models.unified_transaction import (
    GroundTruthSource,
    MatchMethod,
    TransactionMatchStatus,
    UnifiedTransaction,
)
from app.parsers.container_logs_parser import ContainerLogValues


@dataclass(frozen=True)
class TransactionMatcherConfig:
    """
    Configuration for transaction matching.

    archive_time_threshold_seconds:
        Maximum timestamp difference allowed when matching Archive ground
        truth to a container-log UUID.

    require_direction_for_archive:
        When True, Archive and container-log directions must match whenever
        both are available.

    prefer_gate_match:
        Prefer candidates with a matching gate when gate data exists.

    include_container_logs_only:
        Add rows for container-log UUIDs that did not match any selected
        ground-truth transaction.
    """

    archive_time_threshold_seconds: int = 300
    require_direction_for_archive: bool = True
    prefer_gate_match: bool = True
    include_container_logs_only: bool = True


class TransactionMatcher:
    """
    Attach container-log values to selected ground-truth transactions.

    Matching rules:

    AIQ:
        Match directly by UUID.

    Archive:
        Match by nearest timestamp within the configured threshold.
        Direction is used as a constraint when available.
        Gate is used as a preference when available.

    OCR truck, trailer, and vehicle-type values are never used to establish
    the transaction match.
    """

    def __init__(
        self,
        config: Optional[TransactionMatcherConfig] = None,
    ) -> None:
        self.config = config or TransactionMatcherConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(
        self,
        *,
        ground_truth_transactions: Sequence[UnifiedTransaction],
        container_logs_by_uuid: dict[str, ContainerLogValues],
        yard_id: Optional[str] = None,
    ) -> list[UnifiedTransaction]:
        """
        Match selected ground truth against grouped container-log records.

        Returns a new list of UnifiedTransaction objects. The input ground
        truth objects are copied before container-log data is attached.
        """

        transactions = [
            deepcopy(transaction)
            for transaction in ground_truth_transactions
        ]

        normalized_logs = self._filter_container_logs_for_yard(
            container_logs_by_uuid=container_logs_by_uuid,
            yard_id=yard_id,
        )

        used_container_log_uuids: set[str] = set()

        aiq_transactions = [
            transaction
            for transaction in transactions
            if transaction.ground_truth_source
            == GroundTruthSource.AIQ
        ]

        archive_transactions = [
            transaction
            for transaction in transactions
            if transaction.ground_truth_source
            == GroundTruthSource.ARCHIVE
        ]

        other_transactions = [
            transaction
            for transaction in transactions
            if transaction.ground_truth_source
            not in {
                GroundTruthSource.AIQ,
                GroundTruthSource.ARCHIVE,
            }
        ]

        matched_rows: list[UnifiedTransaction] = []

        matched_rows.extend(
            self._match_aiq_transactions(
                transactions=aiq_transactions,
                container_logs_by_uuid=normalized_logs,
                used_container_log_uuids=used_container_log_uuids,
            )
        )

        matched_rows.extend(
            self._match_archive_transactions(
                transactions=archive_transactions,
                container_logs_by_uuid=normalized_logs,
                used_container_log_uuids=used_container_log_uuids,
            )
        )

        for transaction in other_transactions:
            transaction.transaction_match_status = (
                TransactionMatchStatus.GROUND_TRUTH_ONLY
            )
            transaction.match_method = MatchMethod.NONE
            transaction.add_flag("UNKNOWN_GROUND_TRUTH_SOURCE")
            matched_rows.append(transaction)

        if self.config.include_container_logs_only:
            matched_rows.extend(
                self._build_container_logs_only_rows(
                    container_logs_by_uuid=normalized_logs,
                    used_container_log_uuids=used_container_log_uuids,
                    yard_id=yard_id,
                )
            )

        return self._sort_output(matched_rows)

    # ------------------------------------------------------------------
    # AIQ matching
    # ------------------------------------------------------------------

    def _match_aiq_transactions(
        self,
        *,
        transactions: Sequence[UnifiedTransaction],
        container_logs_by_uuid: dict[str, ContainerLogValues],
        used_container_log_uuids: set[str],
    ) -> list[UnifiedTransaction]:
        rows: list[UnifiedTransaction] = []

        for transaction in transactions:
            uuid = self._clean_string(transaction.uuid)

            if not uuid:
                transaction.transaction_match_status = (
                    TransactionMatchStatus.GROUND_TRUTH_ONLY
                )
                transaction.match_method = MatchMethod.NONE
                transaction.add_flag("AIQ_UUID_MISSING")
                rows.append(transaction)
                continue

            log_values = container_logs_by_uuid.get(uuid)

            if log_values is None:
                transaction.transaction_match_status = (
                    TransactionMatchStatus.GROUND_TRUTH_ONLY
                )
                transaction.match_method = MatchMethod.UUID
                transaction.add_flag(
                    "NO_CONTAINER_LOGS_FOUND_FOR_AIQ_UUID"
                )
                rows.append(transaction)
                continue

            self._attach_container_log_values(
                transaction=transaction,
                log_values=log_values,
            )

            transaction.transaction_match_status = (
                TransactionMatchStatus.MATCHED
            )
            transaction.match_method = MatchMethod.UUID

            transaction.match_time_difference_seconds = (
                self._calculate_time_difference_seconds(
                    transaction.timestamp_utc,
                    self._get_container_reference_timestamp(
                        log_values
                    ),
                )
            )

            used_container_log_uuids.add(uuid)
            rows.append(transaction)

        return rows

    # ------------------------------------------------------------------
    # Archive matching
    # ------------------------------------------------------------------

    def _match_archive_transactions(
        self,
        *,
        transactions: Sequence[UnifiedTransaction],
        container_logs_by_uuid: dict[str, ContainerLogValues],
        used_container_log_uuids: set[str],
    ) -> list[UnifiedTransaction]:
        rows: list[UnifiedTransaction] = []

        available_logs = {
            uuid: values
            for uuid, values in container_logs_by_uuid.items()
            if uuid not in used_container_log_uuids
        }

        ordered_transactions = sorted(
            transactions,
            key=lambda transaction: (
                self._ensure_utc(transaction.timestamp_utc)
                if transaction.timestamp_utc
                else datetime.max.replace(tzinfo=timezone.utc)
            ),
        )

        for transaction in ordered_transactions:
            best_match = self._find_best_archive_match(
                transaction=transaction,
                container_logs_by_uuid=available_logs,
            )

            if best_match is None:
                transaction.transaction_match_status = (
                    TransactionMatchStatus.GROUND_TRUTH_ONLY
                )
                transaction.match_method = MatchMethod.NONE
                transaction.add_flag(
                    "NO_CONTAINER_LOGS_MATCH_FOR_ARCHIVE_TRANSACTION"
                )
                rows.append(transaction)
                continue

            matched_uuid, log_values, match_method, difference = (
                best_match
            )

            transaction.uuid = matched_uuid

            self._attach_container_log_values(
                transaction=transaction,
                log_values=log_values,
            )

            transaction.transaction_match_status = (
                TransactionMatchStatus.MATCHED
            )
            transaction.match_method = match_method
            transaction.match_time_difference_seconds = difference

            used_container_log_uuids.add(matched_uuid)
            available_logs.pop(matched_uuid, None)

            rows.append(transaction)

        return rows

    def _find_best_archive_match(
        self,
        *,
        transaction: UnifiedTransaction,
        container_logs_by_uuid: dict[str, ContainerLogValues],
    ) -> Optional[
        tuple[
            str,
            ContainerLogValues,
            MatchMethod,
            float,
        ]
    ]:
        if transaction.timestamp_utc is None:
            transaction.add_flag(
                "ARCHIVE_TIMESTAMP_MISSING_FOR_MATCHING"
            )
            return None

        ground_truth_timestamp = self._ensure_utc(
            transaction.timestamp_utc
        )

        ground_truth_direction = self._normalize_direction(
            transaction.direction
        )

        ground_truth_gate_values = {
            value
            for value in (
                self._normalize_gate(transaction.gate_id),
                self._normalize_gate(transaction.gate_name),
            )
            if value
        }

        candidates: list[
            tuple[
                int,
                float,
                str,
                ContainerLogValues,
                MatchMethod,
            ]
        ] = []

        for uuid, log_values in container_logs_by_uuid.items():
            reference_timestamp = (
                self._get_container_reference_timestamp(
                    log_values
                )
            )

            if reference_timestamp is None:
                continue

            difference = abs(
                (
                    ground_truth_timestamp
                    - self._ensure_utc(reference_timestamp)
                ).total_seconds()
            )

            if (
                difference
                > self.config.archive_time_threshold_seconds
            ):
                continue

            container_directions = {
                normalized
                for value in (
                    list(log_values.directions)
                    + list(log_values.orientations)
                )
                if (
                    normalized
                    := self._normalize_direction(value)
                )
            }

            direction_matches = (
                not ground_truth_direction
                or not container_directions
                or ground_truth_direction in container_directions
            )

            if (
                self.config.require_direction_for_archive
                and not direction_matches
            ):
                continue

            container_gate_values = {
                normalized
                for value in log_values.gate_ids
                if (
                    normalized
                    := self._normalize_gate(value)
                )
            }

            gate_matches = bool(
                ground_truth_gate_values
                and container_gate_values
                and ground_truth_gate_values.intersection(
                    container_gate_values
                )
            )

            if gate_matches:
                match_method = (
                    MatchMethod.TIMESTAMP_DIRECTION_GATE
                )
                gate_rank = 0
            else:
                match_method = MatchMethod.TIMESTAMP_DIRECTION
                gate_rank = 1

            if (
                self.config.prefer_gate_match
                and ground_truth_gate_values
                and container_gate_values
            ):
                candidate_rank = gate_rank
            else:
                candidate_rank = 0

            candidates.append(
                (
                    candidate_rank,
                    difference,
                    uuid,
                    log_values,
                    match_method,
                )
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2],
            )
        )

        best_rank = candidates[0][0]
        best_difference = candidates[0][1]

        equally_good = [
            candidate
            for candidate in candidates
            if (
                candidate[0] == best_rank
                and candidate[1] == best_difference
            )
        ]

        if len(equally_good) > 1:
            transaction.add_flag(
                "AMBIGUOUS_ARCHIVE_CONTAINER_LOG_MATCH"
            )

        _, difference, uuid, values, method = candidates[0]

        return uuid, values, method, difference

    # ------------------------------------------------------------------
    # Container-log-only rows
    # ------------------------------------------------------------------

    def _build_container_logs_only_rows(
        self,
        *,
        container_logs_by_uuid: dict[str, ContainerLogValues],
        used_container_log_uuids: set[str],
        yard_id: Optional[str],
    ) -> list[UnifiedTransaction]:
        rows: list[UnifiedTransaction] = []

        for uuid, log_values in container_logs_by_uuid.items():
            if uuid in used_container_log_uuids:
                continue

            reference_timestamp = (
                self._get_container_reference_timestamp(
                    log_values
                )
            )

            direction = self._choose_preferred_direction(
                log_values
            )

            gate_id = self._choose_preferred_gate(
                log_values
            )

            transaction = UnifiedTransaction(
                yard_id=(
                    self._clean_string(yard_id)
                    or self._extract_yard_id_from_metadata(
                        log_values
                    )
                    or "UNKNOWN"
                ),
                uuid=uuid,
                ground_truth_source=GroundTruthSource.NONE,
                timestamp_utc=reference_timestamp,
                container_logs_first_timestamp_utc=(
                    log_values.first_timestamp_utc
                ),
                container_logs_last_timestamp_utc=(
                    log_values.last_timestamp_utc
                ),
                container_logs_timestamps_utc=list(
                    log_values.timestamps_utc
                ),
                gate_id=gate_id,
                direction=direction,
                transaction_match_status=(
                    TransactionMatchStatus.CONTAINER_LOGS_ONLY
                ),
                match_method=MatchMethod.NONE,
            )

            self._attach_container_log_values(
                transaction=transaction,
                log_values=log_values,
            )

            transaction.add_flag(
                "NO_GROUND_TRUTH_TRANSACTION"
            )

            rows.append(transaction)

        return rows

    # ------------------------------------------------------------------
    # Container value attachment
    # ------------------------------------------------------------------

    def _attach_container_log_values(
        self,
        *,
        transaction: UnifiedTransaction,
        log_values: ContainerLogValues,
    ) -> None:
        transaction.container_logs_truck_numbers = list(
            log_values.truck_numbers
        )
        transaction.container_logs_trailer_numbers = list(
            log_values.trailer_numbers
        )
        transaction.container_logs_vehicle_types = list(
            log_values.vehicle_types
        )
        transaction.container_logs_event_types = list(
            log_values.event_types
        )
        transaction.container_logs_timestamps_utc = list(
            log_values.timestamps_utc
        )
        transaction.container_logs_first_timestamp_utc = (
            log_values.first_timestamp_utc
        )
        transaction.container_logs_last_timestamp_utc = (
            log_values.last_timestamp_utc
        )

        if not transaction.uuid:
            transaction.uuid = log_values.uuid

        if not transaction.direction:
            transaction.direction = (
                self._choose_preferred_direction(
                    log_values
                )
            )

        if not transaction.gate_id:
            transaction.gate_id = (
                self._choose_preferred_gate(
                    log_values
                )
            )

        transaction.metadata.setdefault(
            "container_logs",
            {},
        ).update(
            {
                "directions": list(
                    log_values.directions
                ),
                "gate_ids": list(
                    log_values.gate_ids
                ),
                "orientations": list(
                    log_values.orientations
                ),
                "metadata": deepcopy(
                    log_values.metadata
                ),
            }
        )

    # ------------------------------------------------------------------
    # Reference-value selection
    # ------------------------------------------------------------------

    @staticmethod
    def _get_container_reference_timestamp(
        log_values: ContainerLogValues,
    ) -> Optional[datetime]:
        """
        Prefer the confirmation-alert timestamp as the transaction time.

        Fall back to the first UUID-associated event timestamp.
        """

        confirmation_timestamp = (
            log_values.metadata.get(
                "confirmation_alert_timestamp_utc"
            )
        )

        if isinstance(
            confirmation_timestamp,
            datetime,
        ):
            return confirmation_timestamp

        return log_values.first_timestamp_utc

    def _choose_preferred_direction(
        self,
        log_values: ContainerLogValues,
    ) -> Optional[str]:
        confirmation_orientation = (
            log_values.metadata.get(
                "confirmation_alert_orientation"
            )
        )

        normalized = self._normalize_direction(
            confirmation_orientation
        )

        if normalized:
            return normalized

        for value in (
            list(log_values.directions)
            + list(log_values.orientations)
        ):
            normalized = self._normalize_direction(value)

            if normalized:
                return normalized

        return None

    @staticmethod
    def _choose_preferred_gate(
        log_values: ContainerLogValues,
    ) -> Optional[str]:
        confirmation_gate = log_values.metadata.get(
            "confirmation_alert_gate_id"
        )

        cleaned_confirmation_gate = (
            TransactionMatcher._clean_string(
                confirmation_gate
            )
        )

        if cleaned_confirmation_gate:
            return cleaned_confirmation_gate

        for value in reversed(log_values.gate_ids):
            cleaned = TransactionMatcher._clean_string(
                value
            )

            if cleaned:
                return cleaned

        return None

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _filter_container_logs_for_yard(
        self,
        *,
        container_logs_by_uuid: dict[
            str,
            ContainerLogValues,
        ],
        yard_id: Optional[str],
    ) -> dict[str, ContainerLogValues]:
        """
        The parser normally reads one yard file, so every record already
        belongs to the requested yard.

        If future metadata includes yard IDs, mismatched records can be
        filtered here.
        """

        cleaned_yard_id = self._clean_string(yard_id)

        if not cleaned_yard_id:
            return dict(container_logs_by_uuid)

        filtered: dict[str, ContainerLogValues] = {}

        for uuid, values in container_logs_by_uuid.items():
            metadata_yard_id = (
                self._extract_yard_id_from_metadata(
                    values
                )
            )

            if (
                metadata_yard_id
                and metadata_yard_id != cleaned_yard_id
            ):
                logging.warning(
                    "Skipping UUID %s because its metadata yard %s "
                    "does not match requested yard %s",
                    uuid,
                    metadata_yard_id,
                    cleaned_yard_id,
                )
                continue

            filtered[uuid] = values

        return filtered

    @staticmethod
    def _extract_yard_id_from_metadata(
        log_values: ContainerLogValues,
    ) -> Optional[str]:
        possible_values = [
            log_values.metadata.get("yard_id"),
            log_values.metadata.get(
                "container_logs_yard_id"
            ),
        ]

        events = log_values.metadata.get("events") or []

        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue

                payload = event.get("payload") or {}

                if isinstance(payload, dict):
                    possible_values.append(
                        payload.get("yard_id")
                    )

        for value in possible_values:
            cleaned = TransactionMatcher._clean_string(
                value
            )

            if cleaned:
                return cleaned

        return None

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_time_difference_seconds(
        first: Optional[datetime],
        second: Optional[datetime],
    ) -> Optional[float]:
        if first is None or second is None:
            return None

        return abs(
            (
                TransactionMatcher._ensure_utc(first)
                - TransactionMatcher._ensure_utc(second)
            ).total_seconds()
        )

    @staticmethod
    def _normalize_direction(
        value: object,
    ) -> Optional[str]:
        cleaned = TransactionMatcher._clean_string(
            value
        )

        if not cleaned:
            return None

        upper = cleaned.upper()

        if upper in {
            "IN",
            "ENTER",
            "ENTERS",
            "ENTRY",
        }:
            return "IN"

        if upper in {
            "OUT",
            "EXIT",
            "EXITS",
        }:
            return "OUT"

        return upper

    @staticmethod
    def _normalize_gate(
        value: object,
    ) -> Optional[str]:
        cleaned = TransactionMatcher._clean_string(
            value
        )

        if not cleaned:
            return None

        return "".join(
            character
            for character in cleaned.upper()
            if character.isalnum()
        )

    @staticmethod
    def _clean_string(
        value: object,
    ) -> Optional[str]:
        if value is None:
            return None

        cleaned = str(value).strip()

        return cleaned or None

    @staticmethod
    def _ensure_utc(
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    @staticmethod
    def _sort_output(
        transactions: Sequence[UnifiedTransaction],
    ) -> list[UnifiedTransaction]:
        return sorted(
            transactions,
            key=lambda transaction: (
                transaction.timestamp_utc
                or transaction.container_logs_first_timestamp_utc
                or datetime.max.replace(tzinfo=timezone.utc),
                transaction.uuid or "",
            ),
        )