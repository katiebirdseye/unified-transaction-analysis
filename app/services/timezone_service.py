from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError


class TimezoneServiceError(RuntimeError):
    """Base error raised by the dynamic timezone service."""


class YardNotFoundError(TimezoneServiceError):
    """Raised when a Yard Name or Yard ID cannot be resolved."""


class DuplicateYardError(TimezoneServiceError):
    """Raised when the Google Sheet contains conflicting yard records."""


@dataclass(frozen=True)
class ResolvedYard:
    """
    Current yard configuration loaded from the Google timezone worksheet.

    timezone_name is the descriptive value stored by MALET, such as:

        Eastern (GMT-5)
        Pacific (GMT-8)

    The actual conversion is performed with the standard and DST offsets.
    """

    yard_name: str
    yard_id: str
    timezone_name: str

    standard_utc_offset_hours: float
    dst_utc_offset_hours: float

    jetson_ip_address: Optional[str] = None
    environment: str = "production"

    @property
    def observes_dst(self) -> bool:
        return (
            self.standard_utc_offset_hours
            != self.dst_utc_offset_hours
        )


@dataclass(frozen=True)
class AnalysisDateWindow:
    """
    Exact local and UTC boundaries for an analysis request.

    local_dates contains exactly lookback_days dates.

    start_utc is inclusive.
    end_utc is exclusive.
    """

    yard: ResolvedYard
    local_dates: tuple[date, ...]

    start_local: datetime
    end_local: datetime

    start_utc: datetime
    end_utc: datetime

    lookback_days: int

    @property
    def start_local_date(self) -> date:
        return self.local_dates[0]

    @property
    def end_local_date(self) -> date:
        return self.local_dates[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "yard_name": self.yard.yard_name,
            "yard_id": self.yard.yard_id,
            "timezone_name": self.yard.timezone_name,
            "standard_utc_offset_hours": (
                self.yard.standard_utc_offset_hours
            ),
            "dst_utc_offset_hours": (
                self.yard.dst_utc_offset_hours
            ),
            "observes_dst": self.yard.observes_dst,
            "lookback_days": self.lookback_days,
            "start_local_date": (
                self.start_local_date.isoformat()
            ),
            "end_local_date": (
                self.end_local_date.isoformat()
            ),
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "local_dates": [
                value.isoformat()
                for value in self.local_dates
            ],
        }


class TimezoneService:
    """
    Load Yard Name, Yard ID, and timezone offsets dynamically from
    Google Sheets.

    The worksheet is refreshed on every call to refresh(), resolve_by_name(),
    resolve_by_id(), or build_analysis_window(), unless refresh=False is
    explicitly supplied.

    This intentionally avoids MALET's module-level timezone dictionaries.
    Newly downloaded timezone rows are available during the same request.
    """

    DEFAULT_SCOPES = (
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    )

    WORKSHEET_BY_ENVIRONMENT = {
        "production": "Timezones",
        "staging": "Timezones Staging",
        "develop": "Timezones Develop",
    }

    EXPECTED_COLUMNS = (
        "Yard ID",
        "Yard Name",
        "Timezone Name",
        "Timezone Offset",
        "Timezone Offset DST",
        "Jetson IP address",
    )

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        credentials_file: str,
        environment: str = "production",
        worksheet_name: Optional[str] = None,
        lookback_days: int = 10,
        scopes: Optional[Sequence[str]] = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id.strip()
        self.credentials_file = str(
            Path(credentials_file).expanduser()
        )
        self.environment = self._normalize_environment(
            environment
        )
        self.worksheet_name = (
            worksheet_name.strip()
            if worksheet_name
            else self.WORKSHEET_BY_ENVIRONMENT[
                self.environment
            ]
        )
        self.lookback_days = lookback_days
        self.scopes = tuple(
            scopes or self.DEFAULT_SCOPES
        )

        self._service: Optional[Resource] = None
        self._yards_by_name: dict[str, ResolvedYard] = {}
        self._yards_by_id: dict[str, ResolvedYard] = {}
        self._last_refreshed_at_utc: Optional[datetime] = None

        self._validate_configuration()

    # ---------------------------------------------------------
    # Public API
    # ---------------------------------------------------------

    def refresh(self) -> list[ResolvedYard]:
        """
        Download the current timezone table and rebuild all mappings.

        No stale local CSV is used when this operation fails.
        """

        rows = self._download_rows()
        yards = self._parse_rows(rows)

        yards_by_name: dict[str, ResolvedYard] = {}
        yards_by_id: dict[str, ResolvedYard] = {}

        for yard in yards:
            normalized_name = self._normalize_lookup(
                yard.yard_name
            )
            normalized_id = self._normalize_lookup(
                yard.yard_id
            )

            existing_by_name = yards_by_name.get(
                normalized_name
            )

            if (
                existing_by_name is not None
                and existing_by_name.yard_id != yard.yard_id
            ):
                raise DuplicateYardError(
                    "The timezone worksheet contains duplicate "
                    f"Yard Name {yard.yard_name!r} for IDs "
                    f"{existing_by_name.yard_id!r} and "
                    f"{yard.yard_id!r}."
                )

            existing_by_id = yards_by_id.get(
                normalized_id
            )

            if (
                existing_by_id is not None
                and existing_by_id.yard_name
                != yard.yard_name
            ):
                raise DuplicateYardError(
                    "The timezone worksheet contains duplicate "
                    f"Yard ID {yard.yard_id!r} for names "
                    f"{existing_by_id.yard_name!r} and "
                    f"{yard.yard_name!r}."
                )

            yards_by_name[normalized_name] = yard
            yards_by_id[normalized_id] = yard

        if not yards_by_id:
            raise TimezoneServiceError(
                f"No valid timezone rows were found in "
                f"worksheet {self.worksheet_name!r}."
            )

        self._yards_by_name = yards_by_name
        self._yards_by_id = yards_by_id
        self._last_refreshed_at_utc = datetime.now(
            timezone.utc
        )

        logging.info(
            "Loaded %d yards from timezone worksheet %s",
            len(yards_by_id),
            self.worksheet_name,
        )

        return self.list_yards(refresh=False)

    def resolve_by_name(
        self,
        yard_name: str,
        *,
        refresh: bool = True,
    ) -> ResolvedYard:
        """
        Resolve a human-readable Yard Name.

        Matching is case-insensitive and ignores repeated whitespace.
        """

        if refresh:
            self.refresh()

        normalized_name = self._normalize_lookup(
            yard_name
        )

        if not normalized_name:
            raise ValueError(
                "yard_name cannot be empty"
            )

        yard = self._yards_by_name.get(
            normalized_name
        )

        if yard is None:
            suggestions = self._find_name_suggestions(
                normalized_name
            )

            detail = ""

            if suggestions:
                detail = (
                    " Similar Yard Names: "
                    + ", ".join(suggestions)
                    + "."
                )

            raise YardNotFoundError(
                f"Yard Name was not found in "
                f"{self.worksheet_name!r}: "
                f"{yard_name!r}.{detail}"
            )

        return yard

    def resolve_by_id(
        self,
        yard_id: str,
        *,
        refresh: bool = True,
    ) -> ResolvedYard:
        """
        Resolve a canonical Yard ID.
        """

        if refresh:
            self.refresh()

        normalized_id = self._normalize_lookup(
            yard_id
        )

        if not normalized_id:
            raise ValueError(
                "yard_id cannot be empty"
            )

        yard = self._yards_by_id.get(
            normalized_id
        )

        if yard is None:
            raise YardNotFoundError(
                f"Yard ID was not found in "
                f"{self.worksheet_name!r}: "
                f"{yard_id!r}."
            )

        return yard

    def list_yards(
        self,
        *,
        refresh: bool = True,
    ) -> list[ResolvedYard]:
        if refresh:
            self.refresh()

        return sorted(
            self._yards_by_id.values(),
            key=lambda yard: (
                yard.yard_name.casefold(),
                yard.yard_id.casefold(),
            ),
        )

    def build_analysis_window(
        self,
        *,
        yard_name: str,
        now_utc: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
    ) -> AnalysisDateWindow:
        """
        Resolve a Yard Name and calculate exactly the last N local dates.

        For a 10-day window, this includes today in the yard's local time
        plus the previous nine local calendar dates.
        """

        yard = self.resolve_by_name(
            yard_name,
            refresh=True,
        )

        return self.build_analysis_window_for_yard(
            yard=yard,
            now_utc=now_utc,
            lookback_days=lookback_days,
        )

    def build_analysis_window_for_yard(
        self,
        *,
        yard: ResolvedYard,
        now_utc: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
    ) -> AnalysisDateWindow:
        days = (
            lookback_days
            if lookback_days is not None
            else self.lookback_days
        )

        if days <= 0:
            raise ValueError(
                "lookback_days must be greater than zero"
            )

        current_utc = self._ensure_utc(
            now_utc or datetime.now(timezone.utc)
        )

        current_local = self.utc_to_local(
            current_utc,
            yard,
        )
        final_local_date = current_local.date()
        first_local_date = (
            final_local_date
            - timedelta(days=days - 1)
        )

        local_dates = tuple(
            first_local_date + timedelta(days=index)
            for index in range(days)
        )

        start_local = datetime.combine(
            first_local_date,
            time.min,
        )
        end_local = datetime.combine(
            final_local_date + timedelta(days=1),
            time.min,
        )

        start_utc = self.local_to_utc(
            start_local,
            yard,
        )
        end_utc = self.local_to_utc(
            end_local,
            yard,
        )

        return AnalysisDateWindow(
            yard=yard,
            local_dates=local_dates,
            start_local=start_local,
            end_local=end_local,
            start_utc=start_utc,
            end_utc=end_utc,
            lookback_days=days,
        )

    def local_to_utc(
        self,
        local_datetime: datetime,
        yard: ResolvedYard | str,
    ) -> datetime:
        """
        Convert a local yard datetime to an aware UTC datetime.

        The conversion preserves MALET's North American DST rules:

        - DST begins on the second Sunday in March.
        - DST ends on the first Sunday in November.
        - Equal standard and DST offsets mean no DST is observed.
        """

        resolved = self._resolve_yard_argument(
            yard
        )

        local_naive = self._as_naive_datetime(
            local_datetime
        )

        offset = self.get_offset_for_local_datetime(
            local_naive,
            resolved,
        )

        converted = local_naive - timedelta(
            hours=offset
        )

        return converted.replace(
            tzinfo=timezone.utc
        )

    def utc_to_local(
        self,
        utc_datetime: datetime,
        yard: ResolvedYard | str,
    ) -> datetime:
        """
        Convert a UTC datetime to a naive yard-local datetime.

        This follows the existing MALET approach by first testing DST
        against a local time estimated with the standard offset.
        """

        resolved = self._resolve_yard_argument(
            yard
        )

        utc_aware = self._ensure_utc(
            utc_datetime
        )
        utc_naive = utc_aware.replace(
            tzinfo=None
        )

        if not resolved.observes_dst:
            offset = (
                resolved.standard_utc_offset_hours
            )
        else:
            estimated_standard_local = (
                utc_naive
                + timedelta(
                    hours=(
                        resolved
                        .standard_utc_offset_hours
                    )
                )
            )

            offset = (
                resolved.dst_utc_offset_hours
                if self.is_dst_active_na(
                    estimated_standard_local
                )
                else (
                    resolved
                    .standard_utc_offset_hours
                )
            )

        return utc_naive + timedelta(
            hours=offset
        )

    def get_offset_for_local_datetime(
        self,
        local_datetime: datetime,
        yard: ResolvedYard,
    ) -> float:
        if not yard.observes_dst:
            return yard.standard_utc_offset_hours

        if self.is_dst_active_na(local_datetime):
            return yard.dst_utc_offset_hours

        return yard.standard_utc_offset_hours

    @staticmethod
    def is_dst_active_na(
        local_datetime: datetime,
    ) -> bool:
        """
        Return whether North American DST is active.

        This intentionally preserves MALET's calendar rule rather than
        introducing a separate timezone database dependency.
        """

        local_naive = (
            local_datetime.replace(tzinfo=None)
        )
        year = local_naive.year

        march_anchor = datetime(
            year,
            3,
            8,
        )
        dst_start = (
            march_anchor
            + timedelta(
                days=(
                    6 - march_anchor.weekday()
                ) % 7
            )
        )

        november_anchor = datetime(
            year,
            11,
            1,
        )
        dst_end = (
            november_anchor
            + timedelta(
                days=(
                    6 - november_anchor.weekday()
                ) % 7
            )
        )

        return (
            dst_start
            <= local_naive
            < dst_end
        )

    @property
    def last_refreshed_at_utc(
        self,
    ) -> Optional[datetime]:
        return self._last_refreshed_at_utc

    # ---------------------------------------------------------
    # Google Sheets
    # ---------------------------------------------------------

    def _download_rows(
        self,
    ) -> list[list[Any]]:
        range_name = (
            f"'{self._escape_sheet_name(self.worksheet_name)}'"
            "!A2:F1000"
        )

        try:
            response = (
                self._get_service()
                .spreadsheets()
                .values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=range_name,
                )
                .execute()
            )
        except HttpError as exc:
            logging.exception(
                "Could not load timezone worksheet %s",
                self.worksheet_name,
            )
            raise TimezoneServiceError(
                "Google Sheets timezone refresh failed for "
                f"worksheet {self.worksheet_name!r}."
            ) from exc
        except Exception as exc:
            logging.exception(
                "Unexpected timezone refresh failure"
            )
            raise TimezoneServiceError(
                "Could not refresh timezone data from "
                "Google Sheets."
            ) from exc

        rows = response.get(
            "values",
            [],
        )

        if not rows:
            raise TimezoneServiceError(
                f"Timezone worksheet "
                f"{self.worksheet_name!r} returned no rows."
            )

        return rows

    def _get_service(
        self,
    ) -> Resource:
        if self._service is not None:
            return self._service

        credentials = (
            service_account.Credentials
            .from_service_account_file(
                self.credentials_file,
                scopes=list(self.scopes),
            )
        )

        self._service = build(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )

        return self._service

    # ---------------------------------------------------------
    # Row parsing
    # ---------------------------------------------------------

    def _parse_rows(
        self,
        rows: Sequence[Sequence[Any]],
    ) -> list[ResolvedYard]:
        yards: list[ResolvedYard] = []

        for sheet_row_number, values in enumerate(
            rows,
            start=2,
        ):
            padded = list(values) + [""] * (
                len(self.EXPECTED_COLUMNS)
                - len(values)
            )

            yard_id = self._clean_string(
                padded[0]
            )
            yard_name = self._clean_string(
                padded[1]
            )
            timezone_name = self._clean_string(
                padded[2]
            )
            standard_offset_raw = self._clean_string(
                padded[3]
            )
            dst_offset_raw = self._clean_string(
                padded[4]
            )
            jetson_ip = self._clean_string(
                padded[5]
            )

            if not any(
                (
                    yard_id,
                    yard_name,
                    timezone_name,
                    standard_offset_raw,
                    dst_offset_raw,
                    jetson_ip,
                )
            ):
                continue

            # Accommodate a header row if the configured range changes
            # from A2:F1000 to A1:F1000 in the future.
            if (
                yard_id.casefold() == "yard id"
                and yard_name.casefold() == "yard name"
            ):
                continue

            if not yard_id:
                raise TimezoneServiceError(
                    f"Missing Yard ID on timezone worksheet "
                    f"row {sheet_row_number}."
                )

            if not yard_name:
                raise TimezoneServiceError(
                    f"Missing Yard Name on timezone worksheet "
                    f"row {sheet_row_number}."
                )

            if not timezone_name:
                raise TimezoneServiceError(
                    f"Missing Timezone Name on timezone "
                    f"worksheet row {sheet_row_number}."
                )

            standard_offset = self._parse_offset(
                standard_offset_raw,
                column_name="Timezone Offset",
                row_number=sheet_row_number,
            )
            dst_offset = self._parse_offset(
                dst_offset_raw,
                column_name="Timezone Offset DST",
                row_number=sheet_row_number,
            )

            normalized_yard_id = (
                self._normalize_yard_id_for_environment(
                    yard_id
                )
            )

            yards.append(
                ResolvedYard(
                    yard_name=yard_name,
                    yard_id=normalized_yard_id,
                    timezone_name=timezone_name,
                    standard_utc_offset_hours=(
                        standard_offset
                    ),
                    dst_utc_offset_hours=dst_offset,
                    jetson_ip_address=jetson_ip,
                    environment=self.environment,
                )
            )

        return yards

    # ---------------------------------------------------------
    # Environment handling
    # ---------------------------------------------------------

    def _normalize_yard_id_for_environment(
        self,
        yard_id: str,
    ) -> str:
        """
        Preserve MALET's production/staging naming convention.

        Production:
            adp_rey_staging -> adp_rey

        Staging/develop:
            adp_rey -> adp_rey_staging
        """

        cleaned = yard_id.strip()

        if cleaned.endswith("_staging"):
            base_id = cleaned[
                :-len("_staging")
            ]
        else:
            base_id = cleaned

        if self.environment in {
            "staging",
            "develop",
        }:
            return f"{base_id}_staging"

        return base_id

    @staticmethod
    def _normalize_environment(
        value: str,
    ) -> str:
        normalized = str(
            value or "production"
        ).strip().casefold()

        aliases = {
            "": "production",
            "main": "production",
            "prod": "production",
            "production": "production",
            "stage": "staging",
            "staging": "staging",
            "dev": "develop",
            "develop": "develop",
        }

        return aliases.get(
            normalized,
            "production",
        )

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    def _resolve_yard_argument(
        self,
        yard: ResolvedYard | str,
    ) -> ResolvedYard:
        if isinstance(yard, ResolvedYard):
            return yard

        # This does not refresh because conversions often occur many
        # times within one already-refreshed request.
        return self.resolve_by_id(
            yard,
            refresh=False,
        )

    def _find_name_suggestions(
        self,
        normalized_name: str,
        *,
        limit: int = 5,
    ) -> list[str]:
        search_words = {
            word
            for word in normalized_name.split()
            if len(word) >= 3
        }

        scored: list[tuple[int, str]] = []

        for yard in self._yards_by_id.values():
            candidate = self._normalize_lookup(
                yard.yard_name
            )

            score = sum(
                word in candidate
                for word in search_words
            )

            if (
                normalized_name in candidate
                or candidate in normalized_name
            ):
                score += 5

            if score:
                scored.append(
                    (
                        int(score),
                        yard.yard_name,
                    )
                )

        scored.sort(
            key=lambda value: (
                -value[0],
                value[1].casefold(),
            )
        )

        return [
            name
            for _, name in scored[:limit]
        ]

    def _validate_configuration(
        self,
    ) -> None:
        if not self.spreadsheet_id:
            raise ValueError(
                "spreadsheet_id cannot be empty"
            )

        credentials_path = Path(
            self.credentials_file
        )

        if not credentials_path.exists():
            raise FileNotFoundError(
                "Google credentials file does not exist: "
                f"{credentials_path}"
            )

        if not credentials_path.is_file():
            raise ValueError(
                "Google credentials path is not a file: "
                f"{credentials_path}"
            )

        if not self.worksheet_name:
            raise ValueError(
                "worksheet_name cannot be empty"
            )

        if self.lookback_days <= 0:
            raise ValueError(
                "lookback_days must be greater than zero"
            )

    @staticmethod
    def _parse_offset(
        value: str,
        *,
        column_name: str,
        row_number: int,
    ) -> float:
        if not value:
            raise TimezoneServiceError(
                f"Missing {column_name} on timezone "
                f"worksheet row {row_number}."
            )

        try:
            parsed = float(value)
        except ValueError as exc:
            raise TimezoneServiceError(
                f"Invalid {column_name} on timezone "
                f"worksheet row {row_number}: {value!r}."
            ) from exc

        if not -14 <= parsed <= 14:
            raise TimezoneServiceError(
                f"{column_name} is outside the supported "
                f"range on row {row_number}: {parsed}."
            )

        return parsed

    @staticmethod
    def _normalize_lookup(
        value: object,
    ) -> str:
        if value is None:
            return ""

        return " ".join(
            str(value)
            .strip()
            .casefold()
            .split()
        )

    @staticmethod
    def _clean_string(
        value: object,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @staticmethod
    def _escape_sheet_name(
        value: str,
    ) -> str:
        return value.replace(
            "'",
            "''",
        )

    @staticmethod
    def _as_naive_datetime(
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None:
            return value

        return value.replace(
            tzinfo=None
        )

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

