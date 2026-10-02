from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import gspread
import numpy as np
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from googleapiclient.errors import HttpError

from app.archive.transaction_record import TransactionRecord
from app.archive.utilities import normalize_enter_exit


# ---------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 60


# ---------------------------------------------------------------------
# Quota retry helpers
# ---------------------------------------------------------------------

def _is_quota_error(
    exc: Exception,
) -> bool:
    """
    Return True when an exception appears to be caused by a Google API
    quota or rate limit.
    """

    if isinstance(
        exc,
        gspread.exceptions.APIError,
    ):
        status_code = exc.response.status_code
        return status_code in {403, 429}

    if isinstance(
        exc,
        HttpError,
    ):
        status_code = exc.resp.status

        if status_code in {403, 429}:
            return True

        try:
            reason = (
                exc.error_details[0].get(
                    "reason",
                    "",
                )
                if (
                    exc.error_details
                    and isinstance(
                        exc.error_details,
                        list,
                    )
                )
                else ""
            )
        except Exception:
            reason = ""

        return reason in {
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "quotaExceeded",
            "dailyLimitExceeded",
        }

    return False


def _retry_quota(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Execute a Google API operation and retry quota-related failures.
    """

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            return func(
                *args,
                **kwargs,
            )
        except Exception as exc:
            is_last_attempt = (
                attempt >= MAX_RETRIES
            )

            if (
                not _is_quota_error(exc)
                or is_last_attempt
            ):
                raise

            logging.warning(
                "Google API quota error on attempt %d/%d. "
                "Retrying in %d seconds.",
                attempt,
                MAX_RETRIES,
                RETRY_DELAY_SECONDS,
            )

            time.sleep(
                RETRY_DELAY_SECONDS
            )

    raise RuntimeError(
        "Google API operation failed unexpectedly"
    )


# ---------------------------------------------------------------------
# Date and time helpers
# ---------------------------------------------------------------------

_GOOGLE_SHEETS_EPOCH = datetime(
    1899,
    12,
    30,
)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
)

_TIME_FORMATS = (
    "%H:%M:%S",
    "%H:%M",
    "%I:%M:%S %p",
    "%I:%M %p",
)


def _serial_to_time_str(
    serial: float,
) -> str:
    """
    Convert a Google Sheets numeric time serial into HH:MM:SS.
    """

    seconds = int(
        round(
            (serial % 1) * 86400
        )
    )

    hours, seconds = divmod(
        seconds,
        3600,
    )

    minutes, seconds = divmod(
        seconds,
        60,
    )

    hours %= 24

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{seconds:02}"
    )


def _normalize_date(
    cell: object,
) -> str:
    """
    Normalize a Google Sheets date value to YYYY-MM-DD.
    """

    if cell in {
        "",
        None,
        "/",
    }:
        return ""

    if isinstance(
        cell,
        datetime,
    ):
        return cell.strftime(
            "%Y-%m-%d"
        )

    if isinstance(
        cell,
        (int, float),
    ):
        try:
            parsed = (
                _GOOGLE_SHEETS_EPOCH
                + timedelta(
                    days=float(cell)
                )
            )

            return parsed.strftime(
                "%Y-%m-%d"
            )
        except Exception:
            return ""

    text = str(cell).strip()

    if re.match(
        r"^\d{4}-\d{2}-\d{2}",
        text,
    ):
        return text[:10]

    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(
                text,
                date_format,
            ).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            continue

    return ""


def _extract_hour(
    cell: object,
) -> int:
    """
    Extract the hour component from a Google Sheets time value.
    """

    if cell in {
        "",
        None,
        "/",
    }:
        return -1

    if isinstance(
        cell,
        (int, float),
    ):
        hours = int(
            (float(cell) % 1) * 24
        )

        return hours % 24

    if isinstance(
        cell,
        datetime,
    ):
        return cell.hour

    text = str(cell).strip()

    for time_format in _TIME_FORMATS:
        try:
            return datetime.strptime(
                text,
                time_format,
            ).hour
        except ValueError:
            continue

    return -1


def _normalize_header(
    value: object,
) -> str:
    """
    Normalize a worksheet header for reliable matching.
    """

    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "_")
    )


# ---------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------

class GDriveArchiveParser:
    """
    Retrieve and filter transaction Archive data stored in Google Sheets.

    The parser:

    - Downloads the Archive metadata worksheet during initialization
    - Resolves one Yard ID to one or more Archive spreadsheets
    - Reads all configured Archive tabs for that yard
    - Filters rows by date and hour
    - Returns TransactionRecord objects
    - Retries Google quota and rate-limit failures
    """

    def __init__(
        self,
        service_account_key: str,
        live_archives_id: str,
        live_archives_ss: str,
    ) -> None:
        cleaned_service_account_key = str(
            service_account_key
        ).strip()

        cleaned_live_archives_id = str(
            live_archives_id
        ).strip()

        cleaned_live_archives_ss = str(
            live_archives_ss
        ).strip()

        if not cleaned_service_account_key:
            raise ValueError(
                "service_account_key cannot be empty"
            )

        if not cleaned_live_archives_id:
            raise ValueError(
                "live_archives_id cannot be empty"
            )

        if not cleaned_live_archives_ss:
            raise ValueError(
                "live_archives_ss cannot be empty"
            )

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        credentials = (
            service_account.Credentials
            .from_service_account_file(
                cleaned_service_account_key,
                scopes=scopes,
            )
        )

        self._client = gspread.Client(
            auth=credentials
        )

        self._client.session = (
            AuthorizedSession(
                credentials
            )
        )

        self.sheet_id = (
            cleaned_live_archives_id
        )

        self.sheet_name = (
            cleaned_live_archives_ss
        )

        self._archives = (
            self._load_archives()
        )

    # ------------------------------------------------------------------
    # Archive metadata
    # ------------------------------------------------------------------

    def refresh(
        self,
    ) -> None:
        """
        Reload Archive metadata from Google Sheets.
        """

        self._archives = (
            self._load_archives()
        )

    def _load_archives(
        self,
    ) -> dict[
        str,
        list[dict[str, Any]],
    ]:
        """
        Load the mapping between Yard IDs and Archive spreadsheets.
        """

        spreadsheet = _retry_quota(
            self._client.open_by_key,
            self.sheet_id,
        )

        worksheet = _retry_quota(
            spreadsheet.worksheet,
            self.sheet_name,
        )

        raw_values = _retry_quota(
            worksheet.get_all_values,
        )

        if not raw_values:
            logging.warning(
                "Archive metadata worksheet %s is empty",
                self.sheet_name,
            )

            return {}

        header_row_index: Optional[int] = None

        for index, row in enumerate(
            raw_values[:10]
        ):
            normalized = [
                str(cell or "")
                .strip()
                .lower()
                for cell in row
            ]

            if (
                "yard id" in normalized
                and "archive id" in normalized
            ):
                header_row_index = index
                break

        if header_row_index is None:
            raise ValueError(
                "Could not find an Archive metadata header row "
                "containing 'Yard ID' and 'Archive ID'"
            )

        raw_headers = (
            raw_values[
                header_row_index
            ]
        )

        headers: list[str] = []

        for index, header in enumerate(
            raw_headers
        ):
            cleaned_header = (
                str(header or "")
                .strip()
                .lower()
            )

            if not cleaned_header:
                cleaned_header = (
                    f"unnamed_{index}"
                )

            headers.append(
                cleaned_header
            )

        records: list[
            dict[str, object]
        ] = []

        for row in raw_values[
            header_row_index + 1:
        ]:
            if not any(
                str(cell or "").strip()
                for cell in row
            ):
                continue

            padded_row = list(row)

            if len(padded_row) < len(headers):
                padded_row.extend(
                    [""]
                    * (
                        len(headers)
                        - len(padded_row)
                    )
                )

            records.append(
                dict(
                    zip(
                        headers,
                        padded_row,
                    )
                )
            )

        archives: dict[
            str,
            list[dict[str, Any]],
        ] = {}

        for record in records:
            normalized_record = {
                str(key or "")
                .strip()
                .lower()
                .replace("_", " "): (
                    value.strip()
                    if isinstance(
                        value,
                        str,
                    )
                    else value
                )
                for key, value
                in record.items()
            }

            yard_id = (
                normalized_record.get(
                    "yard id"
                )
                or normalized_record.get(
                    "yardid"
                )
                or normalized_record.get(
                    "yard"
                )
                or normalized_record.get(
                    "yard name"
                )
            )

            archive_id = (
                normalized_record.get(
                    "archive id"
                )
                or normalized_record.get(
                    "archiveid"
                )
            )

            worksheet_name = (
                normalized_record.get(
                    "sheet name"
                )
                or normalized_record.get(
                    "archive sheet/s"
                )
                or normalized_record.get(
                    "sheet"
                )
                or normalized_record.get(
                    "tab"
                )
            )

            archive_link = (
                normalized_record.get(
                    "link"
                )
                or normalized_record.get(
                    "archive link"
                )
            )

            if (
                not yard_id
                or not archive_id
            ):
                continue

            yard_key = str(
                yard_id
            ).lower().strip()

            archive_key = str(
                archive_id
            ).strip()

            yard_archives = (
                archives.setdefault(
                    yard_key,
                    [],
                )
            )

            existing = next(
                (
                    entry
                    for entry
                    in yard_archives
                    if entry["sheet_id"]
                    == archive_key
                ),
                None,
            )

            if existing is None:
                yard_archives.append(
                    {
                        "sheet_id": (
                            archive_key
                        ),
                        "sheets": (
                            [worksheet_name]
                            if worksheet_name
                            else []
                        ),
                        "link": archive_link,
                    }
                )

                continue

            if (
                worksheet_name
                and worksheet_name
                not in existing[
                    "sheets"
                ]
            ):
                existing[
                    "sheets"
                ].append(
                    worksheet_name
                )

            if (
                archive_link
                and not existing.get(
                    "link"
                )
            ):
                existing[
                    "link"
                ] = archive_link

        logging.info(
            "Loaded Archive metadata for %d yards",
            len(archives),
        )

        return archives

    # ------------------------------------------------------------------
    # Public transaction API
    # ------------------------------------------------------------------

    def get_transaction(
        self,
        archive: str,
        dates: list[str],
        time_range: tuple[int, int],
        sheet: Optional[str] = None,
        filter_dict: Optional[
            dict[str, Any]
        ] = None,
    ) -> list[TransactionRecord]:
        """
        Return Archive transactions matching the supplied filters.
        """

        archive_key = (
            str(archive)
            .replace(
                "_staging",
                "",
            )
            .lower()
            .strip()
        )

        if not archive_key:
            raise ValueError(
                "archive cannot be empty"
            )

        normalized_dates = sorted(
            {
                _normalize_date(
                    value
                )
                for value in dates
                if _normalize_date(
                    value
                )
            }
        )

        if not normalized_dates:
            raise ValueError(
                "dates list cannot be empty"
            )

        if (
            len(time_range) != 2
            or not all(
                isinstance(
                    value,
                    int,
                )
                for value in time_range
            )
        ):
            raise ValueError(
                "time_range must be a tuple of two integers"
            )

        minimum_hour, maximum_hour = (
            time_range
        )

        if not (
            0
            <= minimum_hour
            <= 23
        ):
            raise ValueError(
                "Minimum time_range hour must be between 0 and 23"
            )

        if not (
            0
            <= maximum_hour
            <= 23
        ):
            raise ValueError(
                "Maximum time_range hour must be between 0 and 23"
            )

        if minimum_hour > maximum_hour:
            raise ValueError(
                "Minimum time_range hour cannot exceed maximum hour"
            )

        yard_archives = (
            self._archives.get(
                archive_key
            )
        )

        if not yard_archives:
            available_keys = ", ".join(
                list(
                    self._archives.keys()
                )[:10]
            )

            raise ValueError(
                f"Unknown Archive yard {archive!r}. "
                f"Available sample: "
                f"{available_keys or 'NONE'}"
            )

        if not isinstance(
            yard_archives,
            list,
        ):
            yard_archives = [
                yard_archives
            ]

        tasks = self._build_tasks(
            archive=archive,
            yard_archives=yard_archives,
            requested_sheet=sheet,
        )

        combined_headers: Optional[
            list[str]
        ] = None

        combined_rows: list[
            list[object]
        ] = []

        sheet_failures: list[
            tuple[str, Exception]
        ] = []

        for metadata, sheet_name in tasks:
            sheet_to_use = (
                self._resolve_sheet_alias(
                    archive_key=archive_key,
                    sheet_name=sheet_name,
                )
            )

            if sheet_to_use is None:
                continue

            try:
                headers, matched_rows = (
                    self._load_matching_rows(
                        archive=archive,
                        metadata=metadata,
                        sheet_name=sheet_to_use,
                        dates=normalized_dates,
                        minimum_hour=minimum_hour,
                        maximum_hour=maximum_hour,
                        filter_dict=filter_dict,
                    )
                )
            except Exception as exc:
                logging.exception(
                    "Failed to process Archive sheet %s for yard %s",
                    sheet_to_use,
                    archive,
                )

                sheet_failures.append(
                    (
                        sheet_to_use,
                        exc,
                    )
                )

                continue

            if not matched_rows:
                continue

            if combined_headers is None:
                combined_headers = headers
                combined_rows.extend(
                    matched_rows
                )

                continue

            combined_rows.extend(
                self._align_rows(
                    canonical_headers=(
                        combined_headers
                    ),
                    sheet_headers=headers,
                    rows=matched_rows,
                    archive=archive,
                    sheet_name=sheet_to_use,
                )
            )

        if (
            not combined_headers
            or not combined_rows
        ):
            if sheet_failures:
                failure_summary = "; ".join(
                    (
                        f"{sheet_name}: "
                        f"{type(error).__name__}"
                    )
                    for sheet_name, error
                    in sheet_failures
                )

                raise ValueError(
                    "All Archive sheets failed or returned no matching "
                    f"data for yard {archive!r}. "
                    f"Details: {failure_summary}"
                )

            logging.info(
                "No Archive transactions matched for yard %s",
                archive,
            )

            return []

        return self._array_to_transactions_list(
            [
                combined_headers,
                *combined_rows,
            ]
        )

    # ------------------------------------------------------------------
    # Sheet processing
    # ------------------------------------------------------------------

    def _build_tasks(
        self,
        *,
        archive: str,
        yard_archives: list[
            dict[str, Any]
        ],
        requested_sheet: Optional[str],
    ) -> list[
        tuple[
            dict[str, Any],
            str,
        ]
    ]:
        tasks: list[
            tuple[
                dict[str, Any],
                str,
            ]
        ] = []

        if requested_sheet is not None:
            for metadata in yard_archives:
                sheets = (
                    metadata.get(
                        "sheets"
                    )
                    or []
                )

                if requested_sheet in sheets:
                    tasks.append(
                        (
                            metadata,
                            requested_sheet,
                        )
                    )

            if tasks:
                return tasks

            available_sheets = sorted(
                {
                    sheet_name
                    for metadata
                    in yard_archives
                    for sheet_name
                    in (
                        metadata.get(
                            "sheets"
                        )
                        or []
                    )
                }
            )

            raise ValueError(
                f"Sheet {requested_sheet!r} is not configured "
                f"for yard {archive!r}. Available sheets: "
                f"{', '.join(available_sheets) or 'NONE'}"
            )

        for metadata in yard_archives:
            for sheet_name in (
                metadata.get(
                    "sheets"
                )
                or []
            ):
                tasks.append(
                    (
                        metadata,
                        sheet_name,
                    )
                )

        if not tasks:
            raise ValueError(
                f"No Archive sheet names are configured for yard "
                f"{archive!r}"
            )

        return tasks

    @staticmethod
    def _resolve_sheet_alias(
        *,
        archive_key: str,
        sheet_name: str,
    ) -> Optional[str]:
        """
        Apply historical MALET Archive sheet aliases.
        """

        if (
            archive_key == "cc_dvd"
            and sheet_name
            == "Current (Parking)"
        ):
            return None

        if sheet_name == "Archive / YMS":
            return "Archive"

        return sheet_name

    def _load_matching_rows(
        self,
        *,
        archive: str,
        metadata: dict[str, Any],
        sheet_name: str,
        dates: list[str],
        minimum_hour: int,
        maximum_hour: int,
        filter_dict: Optional[
            dict[str, Any]
        ],
    ) -> tuple[
        list[str],
        list[list[object]],
    ]:
        archive_spreadsheet = (
            _retry_quota(
                self._client.open_by_key,
                metadata["sheet_id"],
            )
        )

        worksheet = _retry_quota(
            archive_spreadsheet.worksheet,
            sheet_name,
        )

        rows = _retry_quota(
            worksheet.get_values,
            value_render_option=(
                "UNFORMATTED_VALUE"
            ),
            date_time_render_option=(
                "SERIAL_NUMBER"
            ),
        )

        logging.info(
            "Pulled %d rows from Archive sheet %s for yard %s",
            len(rows),
            sheet_name,
            archive,
        )

        if not rows:
            return [], []

        headers = [
            _normalize_header(
                value
            )
            for value in rows[0]
        ]

        if (
            "date" not in headers
            or "time" not in headers
        ):
            raise ValueError(
                f"Archive sheet {sheet_name!r} must contain "
                "'date' and 'time' columns"
            )

        data_rows = rows[1:]

        if not data_rows:
            return headers, []

        expected_width = len(headers)

        normalized_data_rows = [
            (
                list(row)
                + [""] * max(
                    0,
                    expected_width
                    - len(row),
                )
            )[:expected_width]
            for row in data_rows
        ]

        data = np.array(
            normalized_data_rows,
            dtype=object,
        )

        date_index = headers.index(
            "date"
        )

        time_index = headers.index(
            "time"
        )

        normalized_date_values = (
            np.vectorize(
                _normalize_date,
                otypes=[str],
            )(
                data[
                    :,
                    date_index,
                ]
            )
        )

        hour_values = np.vectorize(
            _extract_hour,
            otypes=[int],
        )(
            data[
                :,
                time_index,
            ]
        )

        date_mask = np.isin(
            normalized_date_values,
            np.array(
                dates,
                dtype="U10",
            ),
        )

        hour_mask = (
            (hour_values >= minimum_hour)
            & (
                hour_values
                <= maximum_hour
            )
        )

        filter_mask = np.ones(
            len(data),
            dtype=bool,
        )

        if filter_dict:
            for raw_column_name, wanted_value in (
                filter_dict.items()
            ):
                column_name = (
                    _normalize_header(
                        raw_column_name
                    )
                )

                if column_name not in headers:
                    raise ValueError(
                        f"Column {raw_column_name!r} was not found "
                        f"in Archive sheet {sheet_name!r}"
                    )

                column_index = (
                    headers.index(
                        column_name
                    )
                )

                filter_mask &= (
                    data[
                        :,
                        column_index,
                    ]
                    == wanted_value
                )

        row_mask = (
            date_mask
            & hour_mask
            & filter_mask
        )

        if not np.any(
            row_mask
        ):
            logging.info(
                "No Archive rows matched yard %s sheet %s",
                archive,
                sheet_name,
            )

            return headers, []

        matched_data = (
            data[row_mask].copy()
        )

        matched_data[
            :,
            date_index,
        ] = normalized_date_values[
            row_mask
        ]

        matched_data[
            :,
            time_index,
        ] = np.vectorize(
            self._normalize_time_value,
            otypes=[str],
        )(
            data[
                row_mask,
                time_index,
            ]
        )

        matched_rows = (
            matched_data.tolist()
        )

        logging.info(
            "Matched %d Archive rows for yard %s sheet %s",
            len(matched_rows),
            archive,
            sheet_name,
        )

        return headers, matched_rows

    @staticmethod
    def _normalize_time_value(
        value: object,
    ) -> str:
        if isinstance(
            value,
            (int, float),
        ):
            return _serial_to_time_str(
                float(value)
            )

        return str(
            value or ""
        ).strip()

    @staticmethod
    def _align_rows(
        *,
        canonical_headers: list[str],
        sheet_headers: list[str],
        rows: list[list[object]],
        archive: str,
        sheet_name: str,
    ) -> list[list[object]]:
        """
        Align rows from a sheet whose column order differs from the first
        successful Archive sheet.
        """

        if (
            sheet_headers
            == canonical_headers
        ):
            return rows

        logging.warning(
            "Archive header mismatch for yard %s sheet %s. "
            "Aligning columns by header name.",
            archive,
            sheet_name,
        )

        header_indexes = {
            header: index
            for index, header
            in enumerate(
                sheet_headers
            )
        }

        aligned_rows: list[
            list[object]
        ] = []

        for row in rows:
            aligned_rows.append(
                [
                    (
                        row[
                            header_indexes[
                                header
                            ]
                        ]
                        if header
                        in header_indexes
                        else ""
                    )
                    for header
                    in canonical_headers
                ]
            )

        return aligned_rows

    # ------------------------------------------------------------------
    # Transaction conversion
    # ------------------------------------------------------------------

    def _array_to_transactions_list(
        self,
        data: list[list[object]],
    ) -> list[TransactionRecord]:
        """
        Convert normalized Archive rows into TransactionRecord objects.
        """

        if not data:
            return []

        headers = [
            _normalize_header(
                value
            )
            for value in data[0]
        ]

        transactions: list[
            TransactionRecord
        ] = []

        pedestrian_tags = {
            "person",
            "walk",
            "walk/pedestrian",
        }

        previous_record: Optional[
            TransactionRecord
        ] = None

        for row in data[1:]:
            record = TransactionRecord()

            for column, raw_value in zip(
                headers,
                row,
            ):
                value = (
                    None
                    if raw_value
                    in {
                        "",
                        "/",
                        None,
                    }
                    else str(
                        raw_value
                    ).strip()
                )

                match column:
                    case "date":
                        record.mmddyy = value

                    case "time":
                        record.hhmmss = value

                    case "enter_exit":
                        record.enters_exits = (
                            normalize_enter_exit(
                                value
                            )
                        )

                    case "person_name":
                        record.drivers_name = (
                            value
                        )

                    case "company_name":
                        record.company_name = (
                            value
                        )

                    case "vehicle_type":
                        record.vehicle_information = (
                            value
                        )

                    case "vehicle_unit_n":
                        record.unit_number = (
                            value
                        )

                    case "vehicle_chassis_n":
                        record.chassis_number = (
                            value
                        )

                    case "add_info":
                        record.additional_info = (
                            value
                        )

                    case "vehicle_trailer_n":
                        record.trailer = (
                            [
                                trailer.strip()
                                for trailer
                                in value.split(
                                    "/"
                                )
                                if trailer.strip()
                            ]
                            if value
                            else None
                        )

            if (
                record.additional_info
                and any(
                    tag
                    in record.additional_info.lower()
                    for tag
                    in pedestrian_tags
                )
            ):
                record.vehicle_information = (
                    "walk"
                )

            if self._should_merge_double_trailer(
                previous_record=(
                    previous_record
                ),
                current_record=record,
            ):
                self._merge_trailers(
                    target=previous_record,
                    source=record,
                )

                continue

            transactions.append(
                record
            )

            previous_record = record

        return transactions

    @staticmethod
    def _should_merge_double_trailer(
        *,
        previous_record: Optional[
            TransactionRecord
        ],
        current_record: TransactionRecord,
    ) -> bool:
        """
        Return True when the current row is a second trailer row belonging
        to the previous truck transaction.
        """

        if previous_record is None:
            return False

        return (
            current_record.vehicle_information
            == "truck"
            and previous_record.vehicle_information
            == "truck"
            and current_record.mmddyy
            == previous_record.mmddyy
            and current_record.hhmmss
            == previous_record.hhmmss
            and current_record.enters_exits
            == previous_record.enters_exits
            and current_record.unit_number
            is None
            and bool(
                current_record.trailer
            )
        )

    @staticmethod
    def _merge_trailers(
        *,
        target: TransactionRecord,
        source: TransactionRecord,
    ) -> None:
        """
        Merge trailers from a secondary Archive row into the first row.
        """

        if target.trailer is None:
            target.trailer = []

        for trailer in (
            source.trailer
            or []
        ):
            if trailer not in target.trailer:
                target.trailer.append(
                    trailer
                )
