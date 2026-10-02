from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol, Sequence

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class ArchiveProviderError(RuntimeError):
    """Raised when Archive transaction retrieval fails."""


class ArchiveTransactionRecord(Protocol):
    """
    Minimum interface expected from an Archive transaction record.

    This allows the new project to consume the existing MALET
    TransactionRecord objects without importing that class directly.
    """

    mmddyy: Optional[str]
    hhmmss: Optional[str]
    enters_exits: Optional[str]
    vehicle_information: Optional[str]
    unit_number: Optional[str]
    trailer: Optional[list[str]]


class ArchiveParserProtocol(Protocol):
    """
    Interface expected from the existing GDrive Archive parser.
    """

    def get_transaction(
        self,
        archive: str,
        dates: list[str],
        time_range: tuple[int, int],
        sheet: Optional[str] = None,
        filter_dict: Optional[dict[str, Any]] = None,
    ) -> list[ArchiveTransactionRecord]:
        ...


class TimezoneConverterProtocol(Protocol):
    """
    Converts a local yard datetime to UTC.

    The concrete implementation can reuse the existing MALET
    local_to_utc function.
    """

    def __call__(
        self,
        local_datetime: datetime,
        yard_id: str,
    ) -> datetime:
        ...


class ArchiveProvider:
    """
    Loads Archive transactions and converts them into UnifiedTransaction
    objects.

    Archive is the fallback ground-truth source when AIQ data is not
    available for the requested yard and period.

    This provider does not:

    - Query AIQ
    - Parse container logs
    - Select AIQ versus Archive
    - Match OCR events
    - Compare values
    - Export results
    """

    def __init__(
        self,
        *,
        archive_parser: ArchiveParserProtocol,
        local_to_utc: TimezoneConverterProtocol,
        time_range: tuple[int, int] = (0, 23),
        staging_suffix: str = "_staging",
    ) -> None:
        self.archive_parser = archive_parser
        self.local_to_utc = local_to_utc
        self.time_range = time_range
        self.staging_suffix = staging_suffix

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_transactions(
        self,
        *,
        yard_id: str,
        dates: Sequence[date | datetime | str],
    ) -> list[UnifiedTransaction]:
        """
        Load Archive transactions for one yard and a collection of dates.

        Staging yards use the corresponding production yard as the
        Archive lookup key, while the resulting UnifiedTransaction keeps
        the original requested yard ID.
        """

        cleaned_yard_id = str(yard_id).strip()

        if not cleaned_yard_id:
            raise ValueError("yard_id cannot be empty")

        normalized_dates = self._normalize_dates(dates)

        if not normalized_dates:
            return []

        archive_lookup_yard_id = self._get_archive_lookup_yard_id(
            cleaned_yard_id
        )

        logging.info(
            "Loading Archive transactions for yard %s "
            "using Archive key %s for %d dates",
            cleaned_yard_id,
            archive_lookup_yard_id,
            len(normalized_dates),
        )

        try:
            records = self.archive_parser.get_transaction(
                archive=archive_lookup_yard_id,
                dates=normalized_dates,
                time_range=self.time_range,
                sheet=None,
                filter_dict=None,
            ) or []
        except Exception as exc:
            logging.exception(
                "Failed to load Archive transactions for yard %s",
                cleaned_yard_id,
            )
            raise ArchiveProviderError(
                f"Failed to load Archive transactions for "
                f"{cleaned_yard_id}"
            ) from exc

        transactions: list[UnifiedTransaction] = []

        for index, record in enumerate(records):
            try:
                transaction = self._record_to_transaction(
                    record=record,
                    requested_yard_id=cleaned_yard_id,
                    archive_lookup_yard_id=archive_lookup_yard_id,
                    source_index=index,
                )
            except Exception:
                logging.exception(
                    "Failed to convert Archive record at index %d "
                    "for yard %s",
                    index,
                    cleaned_yard_id,
                )
                continue

            if transaction is not None:
                transactions.append(transaction)

        transactions.sort(
            key=lambda transaction: (
                transaction.timestamp_utc
                or datetime.max.replace(tzinfo=timezone.utc)
            )
        )

        logging.info(
            "Loaded %d Archive transactions for yard %s",
            len(transactions),
            cleaned_yard_id,
        )

        return transactions

    def has_transactions(
        self,
        *,
        yard_id: str,
        dates: Sequence[date | datetime | str],
    ) -> bool:
        """
        Return True when the Archive contains at least one transaction.
        """

        return bool(
            self.get_transactions(
                yard_id=yard_id,
                dates=dates,
            )
        )

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _record_to_transaction(
        self,
        *,
        record: ArchiveTransactionRecord,
        requested_yard_id: str,
        archive_lookup_yard_id: str,
        source_index: int,
    ) -> Optional[UnifiedTransaction]:
        timestamp_local = self._parse_record_local_timestamp(record)

        if timestamp_local is None:
            logging.warning(
                "Skipping Archive record with invalid timestamp: "
                "yard=%s date=%s time=%s",
                requested_yard_id,
                getattr(record, "mmddyy", None),
                getattr(record, "hhmmss", None),
            )
            return None

        timestamp_utc = self.local_to_utc(
            timestamp_local,
            requested_yard_id,
        )

        timestamp_utc = self._ensure_utc(timestamp_utc)

        trailers = self._clean_trailer_values(
            getattr(record, "trailer", None)
        )

        transaction = UnifiedTransaction(
            yard_id=requested_yard_id,
            uuid=None,
            ground_truth_transaction_id=self._build_archive_transaction_id(
                archive_lookup_yard_id=archive_lookup_yard_id,
                timestamp_local=timestamp_local,
                source_index=source_index,
            ),
            ground_truth_lookup_yard_id=archive_lookup_yard_id,

            timestamp_local=timestamp_local,
            timestamp_utc=timestamp_utc,

            gate_id=None,
            gate_name=None,
            direction=self._normalize_direction(
                getattr(record, "enters_exits", None)
            ),

            ground_truth_source=GroundTruthSource.ARCHIVE,

            ground_truth_truck_number=self._clean_optional_string(
                getattr(record, "unit_number", None)
            ),
            ground_truth_trailer_number_1=(
                trailers[0]
                if len(trailers) >= 1
                else None
            ),
            ground_truth_trailer_number_2=(
                trailers[1]
                if len(trailers) >= 2
                else None
            ),
            ground_truth_vehicle_type=self._clean_optional_string(
                getattr(record, "vehicle_information", None)
            ),

            # Archive does not provide Alisa prediction values.
            alisa_prediction_truck_number=None,
            alisa_prediction_trailer_number_1=None,
            alisa_prediction_trailer_number_2=None,
            alisa_prediction_vehicle_type=None,

            metadata={
                "archive_lookup_yard_id": archive_lookup_yard_id,
                "archive_source_index": source_index,
                "archive_raw": {
                    "date": self._clean_optional_string(
                        getattr(record, "mmddyy", None)
                    ),
                    "time": self._clean_optional_string(
                        getattr(record, "hhmmss", None)
                    ),
                    "drivers_name": self._clean_optional_string(
                        getattr(record, "drivers_name", None)
                    ),
                    "company_name": self._clean_optional_string(
                        getattr(record, "company_name", None)
                    ),
                    "chassis_number": self._clean_optional_string(
                        getattr(record, "chassis_number", None)
                    ),
                    "additional_info": self._clean_optional_string(
                        getattr(record, "additional_info", None)
                    ),
                },
            },
        )

        if requested_yard_id != archive_lookup_yard_id:
            transaction.add_flag(
                "ARCHIVE_LOOKUP_USES_PRODUCTION_YARD"
            )

        if not transaction.direction:
            transaction.add_flag(
                "ARCHIVE_DIRECTION_MISSING"
            )

        if not transaction.ground_truth_truck_number:
            transaction.add_flag(
                "ARCHIVE_TRUCK_NUMBER_MISSING"
            )

        if len(trailers) > 2:
            transaction.add_flag(
                "ARCHIVE_HAS_MORE_THAN_TWO_TRAILERS"
            )

            transaction.metadata[
                "additional_archive_trailers"
            ] = trailers[2:]

        return transaction

    # ------------------------------------------------------------------
    # Timestamp handling
    # ------------------------------------------------------------------

    def _parse_record_local_timestamp(
        self,
        record: ArchiveTransactionRecord,
    ) -> Optional[datetime]:
        date_text = self._clean_optional_string(
            getattr(record, "mmddyy", None)
        )
        time_text = self._clean_optional_string(
            getattr(record, "hhmmss", None)
        )

        if not date_text or not time_text:
            return None

        combined = f"{date_text} {time_text}"

        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%m-%d-%Y %H:%M:%S",
            "%m-%d-%Y %H:%M",
            "%m/%d/%y %H:%M:%S",
            "%m/%d/%y %H:%M",
            "%m-%d-%y %H:%M:%S",
            "%m-%d-%y %H:%M",
        )

        for timestamp_format in formats:
            try:
                return datetime.strptime(
                    combined,
                    timestamp_format,
                )
            except ValueError:
                continue

        return None

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Yard and date handling
    # ------------------------------------------------------------------

    def _get_archive_lookup_yard_id(
        self,
        yard_id: str,
    ) -> str:
        """
        Convert a staging yard ID to its production Archive key.

        Only a suffix match is removed. Embedded occurrences of the text
        are preserved.
        """

        if (
            self.staging_suffix
            and yard_id.endswith(self.staging_suffix)
        ):
            return yard_id[
                : -len(self.staging_suffix)
            ]

        return yard_id

    @classmethod
    def _normalize_dates(
        cls,
        values: Sequence[date | datetime | str],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for value in values:
            normalized_date = cls._normalize_single_date(value)

            if normalized_date is None:
                raise ValueError(
                    f"Invalid Archive date value: {value!r}"
                )

            if normalized_date in seen:
                continue

            seen.add(normalized_date)
            normalized.append(normalized_date)

        normalized.sort()

        return normalized

    @staticmethod
    def _normalize_single_date(
        value: date | datetime | str,
    ) -> Optional[str]:
        if isinstance(value, datetime):
            return value.date().isoformat()

        if isinstance(value, date):
            return value.isoformat()

        text = str(value).strip()

        if not text:
            return None

        formats = (
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%m/%d/%y",
            "%m-%d-%y",
        )

        for date_format in formats:
            try:
                parsed = datetime.strptime(
                    text,
                    date_format,
                )
                return parsed.date().isoformat()
            except ValueError:
                continue

        return None

    # ------------------------------------------------------------------
    # Value handling
    # ------------------------------------------------------------------

    @classmethod
    def _clean_trailer_values(
        cls,
        values: Optional[list[str]],
    ) -> list[str]:
        if not values:
            return []

        cleaned_values: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = cls._clean_optional_string(value)

            if not cleaned:
                continue

            if cleaned in {"/", "-", "N/A", "NA"}:
                continue

            if cleaned in seen:
                continue

            seen.add(cleaned)
            cleaned_values.append(cleaned)

        return cleaned_values

    @staticmethod
    def _clean_optional_string(
        value: Any,
    ) -> Optional[str]:
        if value is None:
            return None

        cleaned = str(value).strip()

        if not cleaned:
            return None

        return cleaned

    @staticmethod
    def _normalize_direction(
        value: Any,
    ) -> Optional[str]:
        if value is None:
            return None

        cleaned = str(value).strip().upper()

        if cleaned in {
            "IN",
            "ENTER",
            "ENTERS",
            "ENTRY",
        }:
            return "IN"

        if cleaned in {
            "OUT",
            "EXIT",
            "EXITS",
        }:
            return "OUT"

        return cleaned or None

    @staticmethod
    def _build_archive_transaction_id(
        *,
        archive_lookup_yard_id: str,
        timestamp_local: datetime,
        source_index: int,
    ) -> str:
        """
        Build a deterministic identifier for an Archive row.

        The Archive source currently does not expose a permanent
        transaction ID, so this combines:

        - Archive yard key
        - Local transaction timestamp
        - Source row index

        This is an internal traceability key, not a global transaction UUID.
        """

        timestamp_part = timestamp_local.strftime(
            "%Y%m%dT%H%M%S"
        )

        return (
            f"ARCHIVE:"
            f"{archive_lookup_yard_id}:"
            f"{timestamp_part}:"
            f"{source_index}"
        )
