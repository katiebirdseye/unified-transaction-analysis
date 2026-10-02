from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Protocol, Sequence

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class AIQProviderProtocol(Protocol):
    def get_transactions(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[UnifiedTransaction]:
        ...


class ArchiveProviderProtocol(Protocol):
    def get_transactions(
        self,
        *,
        yard_id: str,
        dates: Sequence[date | datetime | str],
    ) -> list[UnifiedTransaction]:
        ...


class GroundTruthSelectionError(RuntimeError):
    """Raised when no ground-truth provider can be queried successfully."""


@dataclass
class GroundTruthSelection:
    """
    Ground-truth records loaded for one yard and analysis period.

    The transaction list may contain both AIQ and Archive transactions.

    AIQ remains the preferred source because TransactionMatcher processes
    AIQ transactions first by UUID. Archive transactions are then matched
    only against container-log UUIDs that AIQ did not consume.
    """

    yard_id: str
    transactions: list[UnifiedTransaction]
    source: str

    aiq_transaction_count: int = 0
    archive_transaction_count: int = 0

    fallback_used: bool = False
    reason: str = ""

    aiq_error: Optional[str] = None
    archive_error: Optional[str] = None


class GroundTruthSelector:
    """
    Load AIQ and Archive ground truth for a yard and date range.

    Selection strategy:

    1. Query AIQ for the requested yard and UTC period.
    2. Query Archive for the requested yard and local dates.
    3. Keep all eligible AIQ transactions.
    4. Keep eligible Archive transactions that are not clear duplicates
       of an AIQ transaction.
    5. Return a combined transaction list.

    TransactionMatcher applies the source priority:

    - AIQ first, matched directly by UUID.
    - Archive second, matched by timestamp, direction, and gate against
      container-log UUIDs not already used by AIQ.
    """

    def __init__(
        self,
        *,
        aiq_provider: AIQProviderProtocol,
        archive_provider: ArchiveProviderProtocol,
        duplicate_time_threshold_seconds: int = 120,
    ) -> None:
        self.aiq_provider = aiq_provider
        self.archive_provider = archive_provider
        self.duplicate_time_threshold_seconds = max(
            int(duplicate_time_threshold_seconds),
            0,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        *,
        yard_id: str,
        dates: Sequence[date | datetime | str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> GroundTruthSelection:
        """
        Load eligible AIQ and Archive ground truth for one yard.

        AIQ and Archive are both queried independently. A failure in one
        source does not prevent the other source from being used.
        """

        cleaned_yard_id = str(yard_id).strip()

        if not cleaned_yard_id:
            raise ValueError("yard_id cannot be empty")

        normalized_start = self._ensure_utc(
            start_utc
        )
        normalized_end = self._ensure_utc(
            end_utc
        )

        if normalized_start >= normalized_end:
            raise ValueError(
                "start_utc must be earlier than end_utc"
            )

        normalized_dates = self._normalize_dates(
            dates
        )

        if not normalized_dates:
            raise ValueError(
                "At least one date is required"
            )

        logging.info(
            "Loading AIQ and Archive ground truth for yard %s",
            cleaned_yard_id,
        )

        aiq_transactions: list[
            UnifiedTransaction
        ] = []

        archive_transactions: list[
            UnifiedTransaction
        ] = []

        aiq_error: Optional[str] = None
        archive_error: Optional[str] = None

        # --------------------------------------------------------------
        # AIQ
        # --------------------------------------------------------------

        try:
            loaded_aiq_transactions = (
                self.aiq_provider.get_transactions(
                    yard_ids=[
                        cleaned_yard_id
                    ],
                    start_utc=normalized_start,
                    end_utc=normalized_end,
                )
            )

            aiq_transactions = (
                self._filter_eligible_aiq_transactions(
                    transactions=(
                        loaded_aiq_transactions
                    ),
                    yard_id=cleaned_yard_id,
                    start_utc=normalized_start,
                    end_utc=normalized_end,
                )
            )

        except Exception as exc:
            aiq_error = str(exc)

            logging.exception(
                "AIQ lookup failed for yard %s",
                cleaned_yard_id,
            )

        # --------------------------------------------------------------
        # Archive
        # --------------------------------------------------------------

        try:
            loaded_archive_transactions = (
                self.archive_provider.get_transactions(
                    yard_id=cleaned_yard_id,
                    dates=normalized_dates,
                )
            )

            archive_transactions = (
                self._filter_eligible_archive_transactions(
                    transactions=(
                        loaded_archive_transactions
                    ),
                    yard_id=cleaned_yard_id,
                    start_utc=normalized_start,
                    end_utc=normalized_end,
                )
            )

        except Exception as exc:
            archive_error = str(exc)

            logging.exception(
                "Archive lookup failed for yard %s",
                cleaned_yard_id,
            )

        if aiq_error and archive_error:
            raise GroundTruthSelectionError(
                "Both AIQ and Archive ground-truth lookups failed for "
                f"yard {cleaned_yard_id}. "
                f"AIQ error: {aiq_error}. "
                f"Archive error: {archive_error}."
            )

        # Remove Archive rows that clearly represent a transaction already
        # present in AIQ. AIQ is preferred when both sources contain the
        # same real-world transaction.
        deduplicated_archive_transactions = (
            self._remove_archive_duplicates_of_aiq(
                aiq_transactions=(
                    aiq_transactions
                ),
                archive_transactions=(
                    archive_transactions
                ),
            )
        )

        combined_transactions = [
            *aiq_transactions,
            *deduplicated_archive_transactions,
        ]

        combined_transactions.sort(
            key=self._transaction_sort_key
        )

        source = self._determine_source(
            aiq_count=len(
                aiq_transactions
            ),
            archive_count=len(
                deduplicated_archive_transactions
            ),
        )

        fallback_used = bool(
            deduplicated_archive_transactions
        )

        reason = self._build_reason(
            aiq_count=len(
                aiq_transactions
            ),
            archive_count=len(
                deduplicated_archive_transactions
            ),
            raw_archive_count=len(
                archive_transactions
            ),
            aiq_error=aiq_error,
            archive_error=archive_error,
        )

        logging.info(
            "Loaded ground truth for yard %s: "
            "%d AIQ transactions, "
            "%d eligible Archive transactions, "
            "%d Archive transactions after AIQ deduplication",
            cleaned_yard_id,
            len(aiq_transactions),
            len(archive_transactions),
            len(
                deduplicated_archive_transactions
            ),
        )

        return GroundTruthSelection(
            yard_id=cleaned_yard_id,
            transactions=combined_transactions,
            source=source,
            aiq_transaction_count=len(
                aiq_transactions
            ),
            archive_transaction_count=len(
                deduplicated_archive_transactions
            ),
            fallback_used=fallback_used,
            reason=reason,
            aiq_error=aiq_error,
            archive_error=archive_error,
        )

    # ------------------------------------------------------------------
    # Eligibility rules
    # ------------------------------------------------------------------

    def _filter_eligible_aiq_transactions(
        self,
        *,
        transactions: Sequence[
            UnifiedTransaction
        ],
        yard_id: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[UnifiedTransaction]:
        """
        Keep AIQ records that can participate in UUID matching.

        Requirements:

        - matching yard ID;
        - timestamp inside the requested UTC interval;
        - non-empty UUID.
        """

        eligible: list[
            UnifiedTransaction
        ] = []

        for transaction in transactions:
            if (
                self._normalize_yard_id(
                    transaction.yard_id
                )
                != self._normalize_yard_id(
                    yard_id
                )
            ):
                continue

            if transaction.timestamp_utc is None:
                transaction.add_flag(
                    "AIQ_TRANSACTION_TIMESTAMP_MISSING"
                )
                continue

            transaction_timestamp = (
                self._ensure_utc(
                    transaction.timestamp_utc
                )
            )

            if not (
                start_utc
                <= transaction_timestamp
                < end_utc
            ):
                continue

            if not self._clean_string(
                transaction.uuid
            ):
                transaction.add_flag(
                    "AIQ_TRANSACTION_NOT_ELIGIBLE_FOR_UUID_MATCH"
                )
                continue

            transaction.ground_truth_source = (
                GroundTruthSource.AIQ
            )

            eligible.append(
                transaction
            )

        return eligible

    def _filter_eligible_archive_transactions(
        self,
        *,
        transactions: Sequence[
            UnifiedTransaction
        ],
        yard_id: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[UnifiedTransaction]:
        """
        Keep Archive records with a valid timestamp in the requested period.

        Archive rows do not require UUIDs because they are matched to
        container logs by timestamp, direction, and gate.
        """

        eligible: list[
            UnifiedTransaction
        ] = []

        normalized_requested_yard = (
            self._normalize_yard_id(
                yard_id
            )
        )

        for transaction in transactions:
            transaction_yard = (
                self._normalize_yard_id(
                    transaction.yard_id
                )
            )

            lookup_yard = (
                self._normalize_yard_id(
                    transaction.ground_truth_lookup_yard_id
                )
            )

            if (
                transaction_yard
                != normalized_requested_yard
                and lookup_yard
                != normalized_requested_yard
            ):
                continue

            if transaction.timestamp_utc is None:
                transaction.add_flag(
                    "ARCHIVE_TRANSACTION_TIMESTAMP_MISSING"
                )
                continue

            transaction_timestamp = (
                self._ensure_utc(
                    transaction.timestamp_utc
                )
            )

            if not (
                start_utc
                <= transaction_timestamp
                < end_utc
            ):
                continue

            transaction.ground_truth_source = (
                GroundTruthSource.ARCHIVE
            )

            eligible.append(
                transaction
            )

        return eligible

    # ------------------------------------------------------------------
    # AIQ / Archive duplicate suppression
    # ------------------------------------------------------------------

    def _remove_archive_duplicates_of_aiq(
        self,
        *,
        aiq_transactions: Sequence[
            UnifiedTransaction
        ],
        archive_transactions: Sequence[
            UnifiedTransaction
        ],
    ) -> list[UnifiedTransaction]:
        """
        Remove Archive rows that clearly represent an AIQ transaction.

        AIQ is treated as the preferred ground truth.

        An Archive record is considered an AIQ duplicate when:

        - timestamps are close;
        - directions are compatible; and
        - at least one truck or trailer identifier overlaps.

        A very close timestamp match may also be treated as a duplicate
        when both records have no usable identifiers.
        """

        if not aiq_transactions:
            return list(
                archive_transactions
            )

        retained: list[
            UnifiedTransaction
        ] = []

        removed_count = 0

        for archive_transaction in (
            archive_transactions
        ):
            duplicate = any(
                self._transactions_likely_duplicate(
                    aiq_transaction=(
                        aiq_transaction
                    ),
                    archive_transaction=(
                        archive_transaction
                    ),
                )
                for aiq_transaction in (
                    aiq_transactions
                )
            )

            if duplicate:
                removed_count += 1

                logging.debug(
                    "Removed Archive transaction %s because a "
                    "matching AIQ transaction was found",
                    (
                        archive_transaction
                        .ground_truth_transaction_id
                    ),
                )

                continue

            retained.append(
                archive_transaction
            )

        logging.info(
            "Removed %d Archive transactions duplicated by AIQ",
            removed_count,
        )

        return retained

    def _transactions_likely_duplicate(
        self,
        *,
        aiq_transaction: UnifiedTransaction,
        archive_transaction: UnifiedTransaction,
    ) -> bool:
        if (
            aiq_transaction.timestamp_utc is None
            or archive_transaction.timestamp_utc
            is None
        ):
            return False

        aiq_timestamp = self._ensure_utc(
            aiq_transaction.timestamp_utc
        )

        archive_timestamp = self._ensure_utc(
            archive_transaction.timestamp_utc
        )

        difference = abs(
            (
                aiq_timestamp
                - archive_timestamp
            ).total_seconds()
        )

        if (
            difference
            > self.duplicate_time_threshold_seconds
        ):
            return False

        aiq_direction = (
            self._normalize_direction(
                aiq_transaction.direction
            )
        )

        archive_direction = (
            self._normalize_direction(
                archive_transaction.direction
            )
        )

        if (
            aiq_direction
            and archive_direction
            and aiq_direction
            != archive_direction
        ):
            return False

        aiq_identifiers = (
            self._transaction_identifiers(
                aiq_transaction
            )
        )

        archive_identifiers = (
            self._transaction_identifiers(
                archive_transaction
            )
        )

        if (
            aiq_identifiers
            and archive_identifiers
        ):
            return bool(
                aiq_identifiers.intersection(
                    archive_identifiers
                )
            )

        # When neither source has usable identifiers, only consider the
        # rows duplicates if their timestamps are extremely close.
        if (
            not aiq_identifiers
            and not archive_identifiers
        ):
            return difference <= 30

        return False

    def _transaction_identifiers(
        self,
        transaction: UnifiedTransaction,
    ) -> set[str]:
        values = (
            transaction.ground_truth_truck_number,
            transaction.ground_truth_trailer_number_1,
            transaction.ground_truth_trailer_number_2,
        )

        return {
            normalized
            for value in values
            if (
                normalized
                := self._normalize_identifier(
                    value
                )
            )
        }

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_source(
        *,
        aiq_count: int,
        archive_count: int,
    ) -> str:
        if aiq_count and archive_count:
            return "AIQ_AND_ARCHIVE"

        if aiq_count:
            return "AIQ"

        if archive_count:
            return "ARCHIVE"

        return "NONE"

    @staticmethod
    def _build_reason(
        *,
        aiq_count: int,
        archive_count: int,
        raw_archive_count: int,
        aiq_error: Optional[str],
        archive_error: Optional[str],
    ) -> str:
        if aiq_count and archive_count:
            return (
                "AIQ was loaded as the preferred UUID-based ground "
                "truth, and Archive was retained for transactions not "
                "covered by AIQ."
            )

        if aiq_count:
            if archive_error:
                return (
                    "AIQ transactions were loaded successfully, but "
                    "the Archive lookup failed."
                )

            if raw_archive_count:
                return (
                    "AIQ transactions were loaded. All eligible Archive "
                    "transactions were identified as duplicates of AIQ."
                )

            return (
                "AIQ transactions were loaded and Archive returned no "
                "additional eligible transactions."
            )

        if archive_count:
            if aiq_error:
                return (
                    "AIQ lookup failed, so Archive supplied the "
                    "available ground truth."
                )

            return (
                "AIQ returned no eligible transactions, so Archive "
                "supplied the available ground truth."
            )

        if aiq_error and not archive_error:
            return (
                "AIQ lookup failed and Archive returned no eligible "
                "transactions."
            )

        if archive_error and not aiq_error:
            return (
                "AIQ returned no eligible transactions and Archive "
                "lookup failed."
            )

        return (
            "Neither AIQ nor Archive returned eligible transactions "
            "for the requested yard and period."
        )

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_dates(
        values: Sequence[
            date | datetime | str
        ],
    ) -> list[date]:
        normalized: list[
            date
        ] = []

        seen: set[
            date
        ] = set()

        for value in values:
            if isinstance(
                value,
                datetime,
            ):
                normalized_value = (
                    value.date()
                )

            elif isinstance(
                value,
                date,
            ):
                normalized_value = value

            else:
                text = str(
                    value
                ).strip()

                if not text:
                    continue

                normalized_value = (
                    datetime.fromisoformat(
                        text
                    ).date()
                )

            if normalized_value in seen:
                continue

            seen.add(
                normalized_value
            )

            normalized.append(
                normalized_value
            )

        normalized.sort()

        return normalized

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
    def _normalize_yard_id(
        value: Optional[str],
    ) -> str:
        return str(
            value or ""
        ).strip().lower()

    @staticmethod
    def _clean_string(
        value: object,
    ) -> str:
        if value is None:
            return ""

        return str(
            value
        ).strip()

    @staticmethod
    def _normalize_identifier(
        value: Optional[str],
    ) -> str:
        """
        Normalize equipment identifiers for duplicate detection.

        This intentionally removes punctuation and spaces but does not
        remove letters such as a trailing trailer check letter.
        """

        cleaned = re.sub(
            r"[^A-Z0-9]",
            "",
            str(
                value or ""
            ).upper(),
        )

        return cleaned

    @staticmethod
    def _normalize_direction(
        value: Optional[str],
    ) -> str:
        cleaned = str(
            value or ""
        ).strip().lower()

        if cleaned in {
            "in",
            "enter",
            "enters",
            "entry",
            "inbound",
        }:
            return "IN"

        if cleaned in {
            "out",
            "exit",
            "exits",
            "outbound",
        }:
            return "OUT"

        return cleaned.upper()

    @classmethod
    def _transaction_sort_key(
        cls,
        transaction: UnifiedTransaction,
    ) -> tuple[
        datetime,
        int,
        str,
    ]:
        timestamp = (
            cls._ensure_utc(
                transaction.timestamp_utc
            )
            if transaction.timestamp_utc
            else datetime.max.replace(
                tzinfo=timezone.utc
            )
        )

        source_rank = (
            0
            if transaction.ground_truth_source
            == GroundTruthSource.AIQ
            else 1
        )

        transaction_id = str(
            transaction.ground_truth_transaction_id
            or ""
        )

        return (
            timestamp,
            source_rank,
            transaction_id,
        )