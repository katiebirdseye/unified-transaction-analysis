from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import gspread
from google.oauth2 import service_account

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class AIQSheetsProviderError(RuntimeError):
    """Raised when AIQ transaction retrieval from Google Sheets fails."""


class AIQSheetsProvider:
    """
    Load AIQ transactions from an existing Google Sheets worksheet.

    The worksheet is expected to be maintained externally by the existing
    Apps Script and Fenix workflow.

    This provider:

    - Opens the configured AIQ spreadsheet.
    - Reads the existing Data worksheet.
    - Filters rows by yard ID.
    - Filters rows by an inclusive-start, exclusive-end UTC interval.
    - Converts matching rows into UnifiedTransaction objects.

    This provider does not:

    - Call Fenix.
    - Query PostgreSQL directly.
    - Update or clear the AIQ worksheet.
    - Trigger the Apps Script.
    - Fall back to Archive.
    - Parse container logs.
    """

    EXPECTED_HEADERS = (
        "Transaction ID",
        "UUID",
        "Date/Time UTC",
        "Location",
        "yard_id",
        "Gate Name",
        "Direction",
        "Vehicle Transport Type Alisa predicted",
        "Vehicle Transport Type enteredValue",
        "Truck Alisa predicted",
        "Truck enteredValue",
        "Trailer Alisa predicted",
        "Trailer enteredValue",
        "Second Trailer Alisa predicted",
        "Second Trailer enteredValue",
    )

    HEADER_ALIASES = {
        "Transaction ID": "Transaction ID",
        "UUID": "UUID",

        "Date/Time UTC": "Date/Time UTC",
        "DateTime UTC": "Date/Time UTC",

        "Location": "Location",
        "yard_id": "yard_id",
        "Gate Name": "Gate Name",
        "Direction": "Direction",

        "Vehicle Transport Type Alisa predicted": (
            "Vehicle Transport Type Alisa predicted"
        ),
        "Vehicle Alisa": (
            "Vehicle Transport Type Alisa predicted"
        ),

        "Vehicle Transport Type enteredValue": (
            "Vehicle Transport Type enteredValue"
        ),
        "Vehicle Final": (
            "Vehicle Transport Type enteredValue"
        ),
        "Vehicle FInal": (
            "Vehicle Transport Type enteredValue"
        ),

        "Truck Alisa predicted": (
            "Truck Alisa predicted"
        ),
        "Truck Alisa": (
            "Truck Alisa predicted"
        ),

        "Truck enteredValue": (
            "Truck enteredValue"
        ),
        "Truck Final": (
            "Truck enteredValue"
        ),

        "Trailer Alisa predicted": (
            "Trailer Alisa predicted"
        ),
        "Trailer Alisa": (
            "Trailer Alisa predicted"
        ),

        "Trailer enteredValue": (
            "Trailer enteredValue"
        ),
        "Trailer Final": (
            "Trailer enteredValue"
        ),

        "Second Trailer Alisa predicted": (
            "Second Trailer Alisa predicted"
        ),
        "Trailer 2 Alisa": (
            "Second Trailer Alisa predicted"
        ),

        "Second Trailer enteredValue": (
            "Second Trailer enteredValue"
        ),
        "Trailer 2 Final": (
            "Second Trailer enteredValue"
        ),
    }

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        worksheet_name: str,
        credentials_file: str,
        utc_to_local,
    ) -> None:
        self.spreadsheet_id = str(
            spreadsheet_id
        ).strip()

        self.worksheet_name = str(
            worksheet_name
        ).strip()

        self.credentials_file = str(
            credentials_file
        ).strip()

        self.utc_to_local = utc_to_local

        self._worksheet: Optional[
            gspread.Worksheet
        ] = None

        self._validate_configuration()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_transactions(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[UnifiedTransaction]:
        """
        Return AIQ transactions for the requested yards and UTC interval.

        The interval follows:

            start_utc <= timestamp_utc < end_utc

        Rows are filtered by yard and timestamp before conversion. This
        prevents unrelated yards from being passed into the timezone
        service and avoids unnecessary conversion work.
        """

        normalized_yards = self._normalize_strings(
            yard_ids
        )

        if not normalized_yards:
            return []

        normalized_yard_lookup = {
            self._normalize_lookup(value)
            for value in normalized_yards
        }

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

        logging.info(
            "Loading AIQ transactions from Google Sheets for "
            "%d yards from %s to %s",
            len(normalized_yards),
            normalized_start.isoformat(),
            normalized_end.isoformat(),
        )

        rows = self._load_rows()

        transactions: list[
            UnifiedTransaction
        ] = []

        for source_index, row in enumerate(
            rows,
            start=2,
        ):
            # Filter by yard before converting the row. This avoids
            # timezone lookups for unrelated yards in the worksheet.
            row_yard_id = self._clean_optional_string(
                row.get("yard_id")
            )

            if not row_yard_id:
                continue

            if (
                self._normalize_lookup(
                    row_yard_id
                )
                not in normalized_yard_lookup
            ):
                continue

            # Filter by timestamp before converting the row.
            row_timestamp_utc = (
                self._parse_timestamp_utc(
                    row.get("Date/Time UTC")
                )
            )

            if row_timestamp_utc is None:
                logging.warning(
                    "Skipping AIQ worksheet row %d with "
                    "invalid timestamp %r",
                    source_index,
                    row.get("Date/Time UTC"),
                )
                continue

            if not (
                normalized_start
                <= row_timestamp_utc
                < normalized_end
            ):
                continue

            try:
                transaction = (
                    self._row_to_transaction(
                        row=row,
                        source_index=source_index,
                    )
                )
            except Exception:
                logging.exception(
                    "Could not convert eligible AIQ "
                    "worksheet row %d for yard %s",
                    source_index,
                    row_yard_id,
                )
                continue

            if transaction is not None:
                transactions.append(
                    transaction
                )

        transactions.sort(
            key=lambda transaction: (
                transaction.timestamp_utc
                or datetime.max.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        logging.info(
            "Loaded %d eligible AIQ transactions "
            "from worksheet %s",
            len(transactions),
            self.worksheet_name,
        )

        return transactions

    def get_transactions_by_uuid(
        self,
        *,
        uuids: Sequence[str],
    ) -> list[UnifiedTransaction]:
        """
        Return AIQ transactions matching the supplied UUIDs.
        """

        normalized_uuids = {
            self._normalize_lookup(value)
            for value in self._normalize_strings(
                uuids
            )
        }

        if not normalized_uuids:
            return []

        rows = self._load_rows()

        transactions: list[
            UnifiedTransaction
        ] = []

        for source_index, row in enumerate(
            rows,
            start=2,
        ):
            try:
                transaction = (
                    self._row_to_transaction(
                        row=row,
                        source_index=source_index,
                    )
                )
            except Exception:
                logging.exception(
                    "Could not convert AIQ worksheet row %d",
                    source_index,
                )
                continue

            if transaction is None:
                continue

            if not transaction.uuid:
                continue

            if (
                self._normalize_lookup(
                    transaction.uuid
                )
                not in normalized_uuids
            ):
                continue

            transactions.append(
                transaction
            )

        transactions.sort(
            key=lambda transaction: (
                transaction.timestamp_utc
                or datetime.max.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        return transactions

    def has_transactions(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> bool:
        """
        Return True when at least one eligible AIQ transaction exists.
        """

        return bool(
            self.get_transactions(
                yard_ids=yard_ids,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        )

    def refresh(
        self,
    ) -> None:
        """
        Clear the cached worksheet object.

        The next request will reopen the spreadsheet and worksheet.
        """

        self._worksheet = None

    # ------------------------------------------------------------------
    # Google Sheets
    # ------------------------------------------------------------------

    def _get_worksheet(
        self,
    ) -> gspread.Worksheet:
        if self._worksheet is not None:
            return self._worksheet

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]

        try:
            credentials = (
                service_account.Credentials
                .from_service_account_file(
                    self.credentials_file,
                    scopes=scopes,
                )
            )

            client = gspread.authorize(
                credentials
            )

            spreadsheet = client.open_by_key(
                self.spreadsheet_id
            )

            worksheet = spreadsheet.worksheet(
                self.worksheet_name
            )
        except Exception as exc:
            logging.exception(
                "Could not open AIQ spreadsheet %s worksheet %s",
                self.spreadsheet_id,
                self.worksheet_name,
            )

            raise AIQSheetsProviderError(
                "Could not open the configured AIQ Google Sheet"
            ) from exc

        self._worksheet = worksheet

        return worksheet

    def _load_rows(
        self,
    ) -> list[dict[str, Any]]:
        worksheet = self._get_worksheet()

        try:
            raw_values = worksheet.get_all_values()
        except Exception as exc:
            logging.exception(
                "Could not read AIQ worksheet %s",
                self.worksheet_name,
            )

            raise AIQSheetsProviderError(
                "Could not read AIQ transactions from Google Sheets"
            ) from exc

        if not raw_values:
            logging.warning(
                "AIQ worksheet %s is empty",
                self.worksheet_name,
            )
            return []

        original_headers = [
            str(value).strip()
            for value in raw_values[0]
        ]

        headers = [
            self.HEADER_ALIASES.get(
                header,
                header,
            )
            for header in original_headers
        ]

        self._validate_headers(
            headers
        )

        rows: list[
            dict[str, Any]
        ] = []

        for values in raw_values[1:]:
            if not any(
                str(value).strip()
                for value in values
            ):
                continue

            padded_values = list(values)

            if len(padded_values) < len(headers):
                padded_values.extend(
                    [""] * (
                        len(headers)
                        - len(padded_values)
                    )
                )

            row = {
                headers[index]: (
                    padded_values[index]
                    if index < len(
                        padded_values
                    )
                    else ""
                )
                for index in range(
                    len(headers)
                )
                if headers[index]
            }

            rows.append(
                row
            )

        return rows

    def _validate_headers(
        self,
        headers: Sequence[str],
    ) -> None:
        available = {
            str(value).strip()
            for value in headers
            if str(value).strip()
        }

        required = {
            "Transaction ID",
            "UUID",
            "Date/Time UTC",
            "yard_id",
        }

        missing_required = sorted(
            required - available
        )

        if missing_required:
            raise AIQSheetsProviderError(
                "AIQ worksheet is missing required headers: "
                + ", ".join(missing_required)
            )

        missing_expected = [
            header
            for header in self.EXPECTED_HEADERS
            if header not in available
        ]

        if missing_expected:
            logging.warning(
                "AIQ worksheet is missing optional expected headers: %s",
                ", ".join(missing_expected),
            )

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    def _row_to_transaction(
        self,
        *,
        row: dict[str, Any],
        source_index: int,
    ) -> Optional[UnifiedTransaction]:
        yard_id = self._clean_optional_string(
            row.get("yard_id")
        )

        timestamp_utc = self._parse_timestamp_utc(
            row.get("Date/Time UTC")
        )

        if not yard_id:
            logging.warning(
                "Skipping AIQ row %d without yard_id",
                source_index,
            )
            return None

        if timestamp_utc is None:
            logging.warning(
                "Skipping AIQ row %d with invalid timestamp %r",
                source_index,
                row.get("Date/Time UTC"),
            )
            return None

        timestamp_local = self.utc_to_local(
            timestamp_utc,
            yard_id,
        )

        if not isinstance(
            timestamp_local,
            datetime,
        ):
            raise TypeError(
                "utc_to_local must return a datetime"
            )

        transaction_id = (
            self._clean_optional_string(
                row.get("Transaction ID")
            )
        )

        if not transaction_id:
            transaction_id = (
                f"AIQ:{yard_id}:"
                f"{timestamp_utc.strftime('%Y%m%dT%H%M%S')}:"
                f"{source_index}"
            )

        return UnifiedTransaction(
            yard_id=yard_id,
            uuid=self._clean_optional_string(
                row.get("UUID")
            ),
            ground_truth_transaction_id=(
                transaction_id
            ),
            ground_truth_lookup_yard_id=(
                yard_id
            ),
            timestamp_local=timestamp_local,
            timestamp_utc=timestamp_utc,
            gate_name=self._clean_optional_string(
                row.get("Gate Name")
            ),
            direction=self._normalize_direction(
                row.get("Direction")
            ),
            ground_truth_source=(
                GroundTruthSource.AIQ
            ),
            ground_truth_truck_number=(
                self._clean_optional_string(
                    row.get(
                        "Truck enteredValue"
                    )
                )
            ),
            ground_truth_trailer_number_1=(
                self._clean_optional_string(
                    row.get(
                        "Trailer enteredValue"
                    )
                )
            ),
            ground_truth_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "Second Trailer enteredValue"
                    )
                )
            ),
            ground_truth_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "Vehicle Transport Type enteredValue"
                    )
                )
            ),
            alisa_prediction_truck_number=(
                self._clean_optional_string(
                    row.get(
                        "Truck Alisa predicted"
                    )
                )
            ),
            alisa_prediction_trailer_number_1=(
                self._clean_optional_string(
                    row.get(
                        "Trailer Alisa predicted"
                    )
                )
            ),
            alisa_prediction_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "Second Trailer Alisa predicted"
                    )
                )
            ),
            alisa_prediction_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "Vehicle Transport Type Alisa predicted"
                    )
                )
            ),
            metadata={
                "location": (
                    self._clean_optional_string(
                        row.get("Location")
                    )
                ),
                "aiq_provider": "google_sheets",
                "aiq_worksheet": (
                    self.worksheet_name
                ),
                "aiq_source_row": (
                    source_index
                ),
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_configuration(
        self,
    ) -> None:
        missing: list[str] = []

        if not self.spreadsheet_id:
            missing.append(
                "AIQ_SPREADSHEET_ID"
            )

        if not self.worksheet_name:
            missing.append(
                "AIQ_WORKSHEET_NAME"
            )

        if not self.credentials_file:
            missing.append(
                "GOOGLE_CREDENTIALS_FILE"
            )

        if not callable(
            self.utc_to_local
        ):
            missing.append(
                "utc_to_local callback"
            )

        if missing:
            raise ValueError(
                "Missing AIQ Sheets configuration: "
                + ", ".join(missing)
            )

    @staticmethod
    def _parse_timestamp_utc(
        value: Any,
    ) -> Optional[datetime]:
        if isinstance(
            value,
            datetime,
        ):
            return AIQSheetsProvider._ensure_utc(
                value
            )

        text = str(
            value or ""
        ).strip()

        if not text:
            return None

        normalized_text = (
            text
            .replace(
                "Z",
                "+00:00",
            )
            .replace(
                " UTC",
                "+00:00",
            )
        )

        try:
            parsed = datetime.fromisoformat(
                normalized_text
            )
        except ValueError:
            parsed = None

        if parsed is None:
            formats = (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%m/%d/%Y %I:%M:%S %p",
                "%m/%d/%Y %I:%M %p",
            )

            for value_format in formats:
                try:
                    parsed = datetime.strptime(
                        text,
                        value_format,
                    )
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

        return AIQSheetsProvider._ensure_utc(
            parsed
        )

    @staticmethod
    def _ensure_utc(
        value: datetime,
    ) -> datetime:
        if not isinstance(
            value,
            datetime,
        ):
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

    @staticmethod
    def _normalize_strings(
        values: Sequence[str],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = str(
                value
            ).strip()

            if not cleaned:
                continue

            normalized_key = (
                AIQSheetsProvider
                ._normalize_lookup(
                    cleaned
                )
            )

            if normalized_key in seen:
                continue

            seen.add(
                normalized_key
            )
            normalized.append(
                cleaned
            )

        return normalized

    @staticmethod
    def _normalize_lookup(
        value: Any,
    ) -> str:
        return str(
            value or ""
        ).strip().lower()

    @staticmethod
    def _clean_optional_string(
        value: Any,
    ) -> Optional[str]:
        if value is None:
            return None

        cleaned = str(
            value
        ).strip()

        if not cleaned:
            return None

        return cleaned

    @staticmethod
    def _normalize_direction(
        value: Any,
    ) -> Optional[str]:
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

        return (
            cleaned.upper()
            if cleaned
            else None
        )
