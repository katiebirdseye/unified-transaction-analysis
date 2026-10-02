from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Protocol, Sequence

from app.matching.transaction_matcher import (
    TransactionMatcher,
    TransactionMatcherConfig,
)
from app.models.unified_transaction import UnifiedTransaction
from app.parsers.container_logs_parser import (
    ContainerLogValues,
    ContainerLogsParser,
)
from app.services.ground_truth_selector import (
    GroundTruthSelection,
    GroundTruthSelector,
)


class LocalToUTCProtocol(Protocol):
    """
    Converts a naive local datetime for a yard into UTC.
    """

    def __call__(
        self,
        local_datetime: datetime,
        yard_id: str,
    ) -> datetime:
        ...


class UTCToLocalProtocol(Protocol):
    """
    Converts a UTC datetime into the yard's local timezone.
    """

    def __call__(
        self,
        utc_datetime: datetime,
        yard_id: str,
    ) -> datetime:
        ...


class ContainerLogsParserFactoryProtocol(Protocol):
    """
    Builds a parser for one container-log file.
    """

    def __call__(
        self,
        log_file_path: str,
    ) -> ContainerLogsParser:
        ...


@dataclass
class AnalysisRequest:
    """
    Input for one-yard transaction analysis.

    Dates represent the yard's local calendar dates.

    When dates are not provided, the service analyzes the most recent
    lookback_days local calendar dates for the requested yard.
    """

    yard_id: str
    dates: Optional[
        list[date | datetime | str]
    ] = None

    lookback_days: int = 10
    include_container_logs_only: bool = True
    normalized_ocr_deduplication: bool = False


@dataclass
class AnalysisResult:
    """
    Complete result of one yard analysis.
    """

    yard_id: str
    dates: list[str]

    ground_truth_source: str
    ground_truth_reason: str
    fallback_used: bool

    transactions: list[UnifiedTransaction]

    aiq_transaction_count: int = 0
    archive_transaction_count: int = 0
    container_log_uuid_count: int = 0

    matched_transaction_count: int = 0
    ground_truth_only_count: int = 0
    container_logs_only_count: int = 0
    ambiguous_count: int = 0

    aiq_error: Optional[str] = None
    archive_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "yard_id": self.yard_id,
            "dates": list(self.dates),
            "ground_truth_source": self.ground_truth_source,
            "ground_truth_reason": self.ground_truth_reason,
            "fallback_used": self.fallback_used,
            "aiq_transaction_count": (
                self.aiq_transaction_count
            ),
            "archive_transaction_count": (
                self.archive_transaction_count
            ),
            "container_log_uuid_count": (
                self.container_log_uuid_count
            ),
            "matched_transaction_count": (
                self.matched_transaction_count
            ),
            "ground_truth_only_count": (
                self.ground_truth_only_count
            ),
            "container_logs_only_count": (
                self.container_logs_only_count
            ),
            "ambiguous_count": self.ambiguous_count,
            "aiq_error": self.aiq_error,
            "archive_error": self.archive_error,
            "transactions": [
                transaction.to_dict()
                for transaction in self.transactions
            ],
        }


class AnalysisService:
    """
    Orchestrates one complete unified transaction analysis.

    Processing flow:

    1. Resolve the requested local dates.
    2. If no dates were provided, generate the most recent local dates
       for the yard.
    3. Convert the local date range to a DST-aware UTC interval.
    4. Select AIQ ground truth, with Archive fallback.
    5. Parse all UUID-based values from the yard's container log.
    6. Filter container-log UUIDs to the requested UTC interval.
    7. Match ground-truth transactions to container-log UUIDs.
    8. Return one UnifiedTransaction row per transaction.

    The service does not:

    - Compare prediction values
    - Write to Google Sheets
    - Build DataFrames
    - Perform METT diagnostics
    """

    def __init__(
        self,
        *,
        ground_truth_selector: GroundTruthSelector,
        transaction_matcher: TransactionMatcher,
        container_logs_dir: str,
        local_to_utc: LocalToUTCProtocol,
        utc_to_local: UTCToLocalProtocol,
        parser_factory: Optional[
            ContainerLogsParserFactoryProtocol
        ] = None,
    ) -> None:
        self.ground_truth_selector = ground_truth_selector
        self.transaction_matcher = transaction_matcher
        self.container_logs_dir = container_logs_dir
        self.local_to_utc = local_to_utc
        self.utc_to_local = utc_to_local
        self.parser_factory = parser_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        request: AnalysisRequest,
    ) -> AnalysisResult:
        """
        Run a unified transaction analysis for one yard.
        """

        yard_id = self._clean_yard_id(
            request.yard_id
        )

        dates = self._resolve_dates(
            yard_id=yard_id,
            requested_dates=request.dates,
            lookback_days=request.lookback_days,
        )

        start_utc, end_utc = self._build_utc_range(
            yard_id=yard_id,
            dates=dates,
        )

        logging.info(
            "Starting unified analysis for yard %s from %s to %s "
            "using %d local analysis dates",
            yard_id,
            start_utc.isoformat(),
            end_utc.isoformat(),
            len(dates),
        )

        ground_truth_selection = (
            self.ground_truth_selector.select(
                yard_id=yard_id,
                dates=dates,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        )

        container_logs_by_uuid = (
            self._load_container_log_values(
                yard_id=yard_id,
                start_utc=start_utc,
                end_utc=end_utc,
                normalized_deduplication=(
                    request.normalized_ocr_deduplication
                ),
            )
        )

        transactions = self._match_transactions(
            yard_id=yard_id,
            ground_truth_selection=(
                ground_truth_selection
            ),
            container_logs_by_uuid=(
                container_logs_by_uuid
            ),
            include_container_logs_only=(
                request.include_container_logs_only
            ),
        )

        result = self._build_result(
            yard_id=yard_id,
            dates=dates,
            selection=ground_truth_selection,
            container_logs_by_uuid=(
                container_logs_by_uuid
            ),
            transactions=transactions,
        )

        logging.info(
            "Completed unified analysis for yard %s: "
            "%d total rows, %d matched, "
            "%d ground-truth-only, "
            "%d container-log-only, %d ambiguous",
            yard_id,
            len(result.transactions),
            result.matched_transaction_count,
            result.ground_truth_only_count,
            result.container_logs_only_count,
            result.ambiguous_count,
        )

        return result

    def analyze_many(
        self,
        requests: Sequence[AnalysisRequest],
    ) -> list[AnalysisResult]:
        """
        Run several yard analyses sequentially.

        Parallel execution can be added later after database, Google Drive,
        and filesystem concurrency limits are established.
        """

        results: list[AnalysisResult] = []

        for request in requests:
            try:
                results.append(
                    self.analyze(request)
                )
            except Exception:
                logging.exception(
                    "Unified analysis failed for yard %s",
                    request.yard_id,
                )
                raise

        return results

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _match_transactions(
        self,
        *,
        yard_id: str,
        ground_truth_selection: GroundTruthSelection,
        container_logs_by_uuid: dict[
            str,
            ContainerLogValues,
        ],
        include_container_logs_only: bool,
    ) -> list[UnifiedTransaction]:
        """
        Run matching with a request-specific matcher configuration.

        TransactionMatcherConfig is immutable, so the shared matcher
        configuration is never modified.
        """

        current_config = self.transaction_matcher.config

        request_matcher = TransactionMatcher(
            config=TransactionMatcherConfig(
                archive_time_threshold_seconds=(
                    current_config.archive_time_threshold_seconds
                ),
                require_direction_for_archive=(
                    current_config.require_direction_for_archive
                ),
                prefer_gate_match=(
                    current_config.prefer_gate_match
                ),
                include_container_logs_only=(
                    include_container_logs_only
                ),
            )
        )

        return request_matcher.match(
            ground_truth_transactions=(
                ground_truth_selection.transactions
            ),
            container_logs_by_uuid=container_logs_by_uuid,
            yard_id=yard_id,
        )
    # ------------------------------------------------------------------
    # Container logs
    # ------------------------------------------------------------------

    def _load_container_log_values(
        self,
        *,
        yard_id: str,
        start_utc: datetime,
        end_utc: datetime,
        normalized_deduplication: bool,
    ) -> dict[str, ContainerLogValues]:
        log_file_path = os.path.join(
            self.container_logs_dir,
            yard_id,
            "container_log.txt",
        )

        parser = self._build_parser(
            log_file_path=log_file_path,
            normalized_deduplication=(
                normalized_deduplication
            ),
        )

        parsed = parser.parse()

        return self._filter_container_logs_by_time(
            container_logs_by_uuid=parsed,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    def _build_parser(
        self,
        *,
        log_file_path: str,
        normalized_deduplication: bool,
    ) -> ContainerLogsParser:
        if self.parser_factory is not None:
            return self.parser_factory(
                log_file_path
            )

        return ContainerLogsParser(
            log_file_path,
            normalized_deduplication=(
                normalized_deduplication
            ),
        )

    def _filter_container_logs_by_time(
        self,
        *,
        container_logs_by_uuid: dict[
            str,
            ContainerLogValues,
        ],
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, ContainerLogValues]:
        """
        Keep UUID records having at least one event inside the requested
        UTC interval.

        Events outside the interval are removed from the timestamp list.

        Collected OCR values remain attached because the parser currently
        aggregates values at UUID level. In normal operation, one UUID should
        represent one short-lived transaction.
        """

        filtered: dict[str, ContainerLogValues] = {}

        for uuid, values in container_logs_by_uuid.items():
            timestamps_in_range: list[datetime] = []

            for timestamp in values.timestamps_utc:
                timestamp_utc = self._ensure_utc(
                    timestamp
                )

                if (
                    start_utc
                    <= timestamp_utc
                    < end_utc
                ):
                    timestamps_in_range.append(
                        timestamp_utc
                    )

            reference_timestamp = (
                self._get_reference_timestamp(
                    values
                )
            )

            reference_in_range = False

            if reference_timestamp is not None:
                reference_timestamp_utc = (
                    self._ensure_utc(
                        reference_timestamp
                    )
                )

                reference_in_range = (
                    start_utc
                    <= reference_timestamp_utc
                    < end_utc
                )

            if (
                not timestamps_in_range
                and not reference_in_range
            ):
                continue

            values.timestamps_utc = sorted(
                set(timestamps_in_range)
            )

            if values.timestamps_utc:
                values.first_timestamp_utc = (
                    values.timestamps_utc[0]
                )
                values.last_timestamp_utc = (
                    values.timestamps_utc[-1]
                )
            elif reference_timestamp is not None:
                reference_timestamp_utc = (
                    self._ensure_utc(
                        reference_timestamp
                    )
                )

                values.first_timestamp_utc = (
                    reference_timestamp_utc
                )
                values.last_timestamp_utc = (
                    reference_timestamp_utc
                )

            filtered[uuid] = values

        return filtered

    @staticmethod
    def _get_reference_timestamp(
        values: ContainerLogValues,
    ) -> Optional[datetime]:
        confirmation_timestamp = values.metadata.get(
            "confirmation_alert_timestamp_utc"
        )

        if isinstance(
            confirmation_timestamp,
            datetime,
        ):
            return confirmation_timestamp

        return values.first_timestamp_utc

    # ------------------------------------------------------------------
    # Date resolution
    # ------------------------------------------------------------------

    def _resolve_dates(
        self,
        *,
        yard_id: str,
        requested_dates: Optional[
            Sequence[date | datetime | str]
        ],
        lookback_days: int,
    ) -> list[date]:
        """
        Resolve explicit dates or generate a default yard-local date range.

        When requested_dates is empty or omitted, the returned range includes
        the yard's current local date and the preceding lookback_days - 1
        calendar dates.
        """

        if requested_dates:
            dates = self._normalize_dates(
                requested_dates
            )

            if not dates:
                raise ValueError(
                    "At least one valid analysis date is required"
                )

            return dates

        if lookback_days <= 0:
            raise ValueError(
                "lookback_days must be greater than zero"
            )

        now_utc = datetime.now(
            timezone.utc
        )

        now_local = self.utc_to_local(
            now_utc,
            yard_id,
        )

        if not isinstance(
            now_local,
            datetime,
        ):
            raise TypeError(
                "utc_to_local must return a datetime"
            )

        local_today = now_local.date()

        start_date = local_today - timedelta(
            days=lookback_days - 1
        )

        dates = [
            start_date + timedelta(days=offset)
            for offset in range(lookback_days)
        ]

        logging.info(
            "No dates supplied for yard %s. "
            "Using %d local dates from %s through %s",
            yard_id,
            lookback_days,
            dates[0].isoformat(),
            dates[-1].isoformat(),
        )

        return dates

    def _build_utc_range(
        self,
        *,
        yard_id: str,
        dates: Sequence[date],
    ) -> tuple[datetime, datetime]:
        """
        Convert the requested local dates into a DST-aware UTC interval.

        Example for one date:

            local start: 2026-07-28 00:00:00
            local end:   2026-07-29 00:00:00

        The end is exclusive.
        """

        if not dates:
            raise ValueError(
                "At least one analysis date is required"
            )

        first_date = min(dates)
        last_date = max(dates)

        start_local = datetime.combine(
            first_date,
            time.min,
        )

        end_local = datetime.combine(
            last_date + timedelta(days=1),
            time.min,
        )

        start_utc = self._ensure_utc(
            self.local_to_utc(
                start_local,
                yard_id,
            )
        )

        end_utc = self._ensure_utc(
            self.local_to_utc(
                end_local,
                yard_id,
            )
        )

        if start_utc >= end_utc:
            raise ValueError(
                "Resolved UTC date range is invalid"
            )

        return start_utc, end_utc

    # ------------------------------------------------------------------
    # Result construction
    # ------------------------------------------------------------------

    def _build_result(
        self,
        *,
        yard_id: str,
        dates: Sequence[date],
        selection: GroundTruthSelection,
        container_logs_by_uuid: dict[
            str,
            ContainerLogValues,
        ],
        transactions: list[UnifiedTransaction],
    ) -> AnalysisResult:
        status_counts = {
            "MATCHED": 0,
            "GROUND_TRUTH_ONLY": 0,
            "CONTAINER_LOGS_ONLY": 0,
            "AMBIGUOUS": 0,
        }

        for transaction in transactions:
            status = (
                transaction.transaction_match_status
            )

            if status is not None:
                status_value = (
                    status.value
                    if hasattr(status, "value")
                    else str(status)
                )

                if status_value in status_counts:
                    status_counts[
                        status_value
                    ] += 1

            if (
                "AMBIGUOUS_ARCHIVE_CONTAINER_LOG_MATCH"
                in transaction.flags
            ):
                status_counts["AMBIGUOUS"] += 1

        return AnalysisResult(
            yard_id=yard_id,
            dates=[
                value.isoformat()
                for value in dates
            ],
            ground_truth_source=selection.source,
            ground_truth_reason=selection.reason,
            fallback_used=selection.fallback_used,
            transactions=transactions,
            aiq_transaction_count=(
                selection.aiq_transaction_count
            ),
            archive_transaction_count=(
                selection.archive_transaction_count
            ),
            container_log_uuid_count=len(
                container_logs_by_uuid
            ),
            matched_transaction_count=(
                status_counts["MATCHED"]
            ),
            ground_truth_only_count=(
                status_counts[
                    "GROUND_TRUTH_ONLY"
                ]
            ),
            container_logs_only_count=(
                status_counts[
                    "CONTAINER_LOGS_ONLY"
                ]
            ),
            ambiguous_count=(
                status_counts["AMBIGUOUS"]
            ),
            aiq_error=selection.aiq_error,
            archive_error=selection.archive_error,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_yard_id(
        yard_id: str,
    ) -> str:
        cleaned = str(yard_id).strip()

        if not cleaned:
            raise ValueError(
                "yard_id cannot be empty"
            )

        return cleaned

    @classmethod
    def _normalize_dates(
        cls,
        values: Sequence[
            date | datetime | str
        ],
    ) -> list[date]:
        normalized: list[date] = []
        seen: set[date] = set()

        for value in values:
            parsed = cls._normalize_single_date(
                value
            )

            if parsed is None:
                raise ValueError(
                    f"Invalid date value: {value!r}"
                )

            if parsed in seen:
                continue

            seen.add(parsed)
            normalized.append(parsed)

        normalized.sort()

        return normalized

    @staticmethod
    def _normalize_single_date(
        value: date | datetime | str,
    ) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

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

        for value_format in formats:
            try:
                return datetime.strptime(
                    text,
                    value_format,
                ).date()
            except ValueError:
                continue

        return None

    @staticmethod
    def _ensure_utc(
        value: datetime,
    ) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError(
                "Expected a datetime value"
            )

        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )