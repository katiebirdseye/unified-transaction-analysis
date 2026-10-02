from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import gspread
import requests
from google.oauth2 import service_account

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class FenixAIQProviderError(RuntimeError):
    """Raised when AIQ retrieval through the Fenix API fails."""


class FenixAIQProvider:
    """
    Retrieve finished AIQ transactions through the Fenix /db/pull API.

    Processing flow:

    1. Clear the configured AIQ staging worksheet.
    2. Build a request-specific SQL query.
    3. Ask Fenix to write the query result into Google Sheets.
    4. Poll the worksheet until the result becomes available.
    5. Convert each row into a UnifiedTransaction.

    The worksheet is used as a temporary staging area. It should not be
    shared with another workflow that appends persistent report data.
    """

    HEADERS = (
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

    def __init__(
        self,
        *,
        fenix_url: str,
        fenix_api_token: str,
        database_name: str,
        spreadsheet_id: str,
        worksheet_name: str,
        credentials_file: str,
        utc_to_local,
        request_timeout_seconds: int = 60,
        result_wait_timeout_seconds: int = 120,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.fenix_url = str(fenix_url).strip().rstrip("/")
        self.fenix_api_token = str(fenix_api_token).strip()
        self.database_name = str(database_name).strip()
        self.spreadsheet_id = str(spreadsheet_id).strip()
        self.worksheet_name = str(worksheet_name).strip()
        self.credentials_file = str(credentials_file).strip()

        self.utc_to_local = utc_to_local

        self.request_timeout_seconds = int(
            request_timeout_seconds
        )
        self.result_wait_timeout_seconds = int(
            result_wait_timeout_seconds
        )
        self.poll_interval_seconds = float(
            poll_interval_seconds
        )

        self._worksheet: Optional[gspread.Worksheet] = None

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
        Retrieve AIQ transactions for the supplied yards and UTC interval.

        The interval is inclusive at the start and exclusive at the end.
        """

        normalized_yards = self._normalize_strings(
            yard_ids
        )

        if not normalized_yards:
            return []

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

        worksheet = self._get_worksheet()

        self._prepare_worksheet(
            worksheet
        )

        query = self._build_query(
            yard_ids=normalized_yards,
            start_utc=normalized_start,
            end_utc=normalized_end,
        )

        logging.info(
            "Requesting AIQ transactions through Fenix for %d yards "
            "from %s to %s",
            len(normalized_yards),
            normalized_start.isoformat(),
            normalized_end.isoformat(),
        )

        self._submit_query(
            query=query
        )

        rows = self._wait_for_rows(
            worksheet=worksheet,
        )

        transactions: list[UnifiedTransaction] = []

        for index, row in enumerate(rows):
            try:
                transaction = self._row_to_transaction(
                    row=row,
                    source_index=index,
                )
            except Exception:
                logging.exception(
                    "Could not convert Fenix AIQ row %d: %s",
                    index,
                    row,
                )
                continue

            if transaction is not None:
                transactions.append(
                    transaction
                )

        transactions.sort(
            key=lambda item: (
                item.timestamp_utc
                or datetime.max.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        logging.info(
            "Loaded %d AIQ transactions through Fenix",
            len(transactions),
        )

        return transactions

    def has_transactions(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> bool:
        return bool(
            self.get_transactions(
                yard_ids=yard_ids,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        )

    # ------------------------------------------------------------------
    # Fenix request
    # ------------------------------------------------------------------

    def _submit_query(
        self,
        *,
        query: str,
    ) -> None:
        payload = {
            "dbname": self.database_name,
            "query": query,
            "sheet_id": self.spreadsheet_id,
            "sheet_name": self.worksheet_name,
            "range": "A2:O",
        }

        headers = {
            "token": self.fenix_api_token,
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{self.fenix_url}/db/pull",
                headers=headers,
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FenixAIQProviderError(
                "Could not connect to the Fenix /db/pull endpoint"
            ) from exc

        if not response.ok:
            raise FenixAIQProviderError(
                "Fenix AIQ request failed with status "
                f"{response.status_code}: {response.text[:500]}"
            )

        response_text = response.text.strip()

        logging.info(
            "Fenix AIQ response: %s",
            response_text[:500],
        )

    # ------------------------------------------------------------------
    # Google Sheets staging
    # ------------------------------------------------------------------

    def _get_worksheet(
        self,
    ) -> gspread.Worksheet:
        if self._worksheet is not None:
            return self._worksheet

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

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

        try:
            worksheet = spreadsheet.worksheet(
                self.worksheet_name
            )
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=self.worksheet_name,
                rows=2000,
                cols=len(self.HEADERS),
            )

        self._worksheet = worksheet

        return worksheet

    def _prepare_worksheet(
        self,
        worksheet: gspread.Worksheet,
    ) -> None:
        """
        Clear previous request results while preserving the staging tab.
        """

        worksheet.batch_clear(
            ["A1:O"]
        )

        worksheet.update(
            range_name="A1:O1",
            values=[list(self.HEADERS)],
        )

    def _wait_for_rows(
        self,
        *,
        worksheet: gspread.Worksheet,
    ) -> list[dict[str, Any]]:
        deadline = (
            time.monotonic()
            + self.result_wait_timeout_seconds
        )

        while time.monotonic() < deadline:
            values = worksheet.get_all_values()

            if len(values) >= 2:
                rows = worksheet.get_all_records(
                    expected_headers=list(
                        self.HEADERS
                    )
                )

                if rows:
                    return rows

            time.sleep(
                self.poll_interval_seconds
            )

        # An empty result can be valid. Perform one final read.
        values = worksheet.get_all_values()

        if len(values) <= 1:
            logging.info(
                "Fenix AIQ query returned no rows"
            )
            return []

        return worksheet.get_all_records(
            expected_headers=list(
                self.HEADERS
            )
        )

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------

    def _build_query(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> str:
        quoted_yards = ", ".join(
            self._sql_quote(value)
            for value in yard_ids
        )

        start_text = self._format_postgres_timestamp(
            start_utc
        )
        end_text = self._format_postgres_timestamp(
            end_utc
        )

        return f"""
SELECT
    tt.id AS "Transaction ID",
    tt.external_uuid AS "UUID",
    tt.started AT TIME ZONE 'UTC' AS "Date/Time UTC",

    l.full_name AS "Location",
    l.name AS "yard_id",

    g.name AS "Gate Name",

    tt.gate_direction AS "Direction",

    CASE
        WHEN tta.vehicle_transport_type_source = 'alisa'
        THEN tta.vehicle_transport_type_value
    END AS "Vehicle Transport Type Alisa predicted",

    CASE
        WHEN tta.vehicle_transport_type_source = 'alisa'
        THEN tt.vehicle_transport_type
    END AS "Vehicle Transport Type enteredValue",

    CASE
        WHEN tta.vehicle_unit_number_source = 'alisa'
        THEN tta.vehicle_unit_number_value
    END AS "Truck Alisa predicted",

    CASE
        WHEN tta.vehicle_unit_number_source = 'alisa'
        THEN tt.vehicle_unit_number
    END AS "Truck enteredValue",

    CASE
        WHEN tta.trailer_trailer_number_source = 'alisa'
        THEN tta.trailer_trailer_number_value
    END AS "Trailer Alisa predicted",

    CASE
        WHEN tta.trailer_trailer_number_source = 'alisa'
        THEN tt.trailer_trailer_number
    END AS "Trailer enteredValue",

    CASE
        WHEN tta.trailer_trailer_number_two_source = 'alisa'
        THEN tta.trailer_trailer_number_two_value
    END AS "Second Trailer Alisa predicted",

    CASE
        WHEN tta.trailer_trailer_number_two_source = 'alisa'
        THEN tt.trailer_trailer_number_two
    END AS "Second Trailer enteredValue"

FROM truck_transaction tt

JOIN truck_transaction_automation tta
    ON tt.id = tta.id

LEFT JOIN gate g
    ON tt.gate_id = g.id

LEFT JOIN location l
    ON g.location_id = l.id

WHERE
    tt.status = 'FINISHED'
    AND l.name IN ({quoted_yards})
    AND tt.started >= TIMESTAMPTZ '{start_text}'
    AND tt.started < TIMESTAMPTZ '{end_text}'
    AND (
        tta.vehicle_unit_number_source = 'alisa'
        OR tta.vehicle_transport_type_source = 'alisa'
        OR tta.vehicle_license_plate_number_source = 'alisa'
        OR tta.trailer_trailer_number_source = 'alisa'
        OR tta.trailer_trailer_number_two_source = 'alisa'
    )

ORDER BY tt.started ASC;
""".strip()

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

        if not yard_id or timestamp_utc is None:
            logging.warning(
                "Skipping AIQ row without yard or timestamp: %s",
                row,
            )
            return None

        timestamp_local = self.utc_to_local(
            timestamp_utc,
            yard_id,
        )

        transaction_id = self._clean_optional_string(
            row.get("Transaction ID")
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
            ground_truth_transaction_id=transaction_id,
            ground_truth_lookup_yard_id=yard_id,
            timestamp_local=timestamp_local,
            timestamp_utc=timestamp_utc,
            gate_name=self._clean_optional_string(
                row.get("Gate Name")
            ),
            direction=self._normalize_direction(
                row.get("Direction")
            ),
            ground_truth_source=GroundTruthSource.AIQ,
            ground_truth_truck_number=(
                self._clean_optional_string(
                    row.get("Truck enteredValue")
                )
            ),
            ground_truth_trailer_number_1=(
                self._clean_optional_string(
                    row.get("Trailer enteredValue")
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
                    row.get("Truck Alisa predicted")
                )
            ),
            alisa_prediction_trailer_number_1=(
                self._clean_optional_string(
                    row.get("Trailer Alisa predicted")
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
                "location": self._clean_optional_string(
                    row.get("Location")
                ),
                "aiq_provider": "fenix",
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_configuration(
        self,
    ) -> None:
        missing: list[str] = []

        values = {
            "FENIX_URL": self.fenix_url,
            "FENIX_API_TOKEN": self.fenix_api_token,
            "AIQ_DATABASE_NAME": self.database_name,
            "AIQ_SPREADSHEET_ID": self.spreadsheet_id,
            "AIQ_WORKSHEET_NAME": self.worksheet_name,
            "GOOGLE_CREDENTIALS_FILE": self.credentials_file,
        }

        for name, value in values.items():
            if not value:
                missing.append(name)

        if missing:
            raise ValueError(
                "Missing Fenix AIQ configuration: "
                + ", ".join(missing)
            )

        if self.request_timeout_seconds <= 0:
            raise ValueError(
                "request_timeout_seconds must be greater than zero"
            )

        if self.result_wait_timeout_seconds <= 0:
            raise ValueError(
                "result_wait_timeout_seconds must be greater than zero"
            )

        if self.poll_interval_seconds <= 0:
            raise ValueError(
                "poll_interval_seconds must be greater than zero"
            )

    @staticmethod
    def _normalize_strings(
        values: Sequence[str],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = str(value).strip()

            if not cleaned or cleaned in seen:
                continue

            seen.add(cleaned)
            normalized.append(cleaned)

        return normalized

    @staticmethod
    def _sql_quote(
        value: str,
    ) -> str:
        escaped = str(value).replace(
            "'",
            "''",
        )

        return f"'{escaped}'"

    @staticmethod
    def _format_postgres_timestamp(
        value: datetime,
    ) -> str:
        normalized = FenixAIQProvider._ensure_utc(
            value
        )

        return normalized.strftime(
            "%Y-%m-%d %H:%M:%S+00"
        )

    @staticmethod
    def _parse_timestamp_utc(
        value: Any,
    ) -> Optional[datetime]:
        if isinstance(value, datetime):
            return FenixAIQProvider._ensure_utc(
                value
            )

        text = str(value or "").strip()

        if not text:
            return None

        normalized_text = text.replace(
            "Z",
            "+00:00",
        )

        try:
            parsed = datetime.fromisoformat(
                normalized_text
            )
        except ValueError:
            formats = (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%m/%d/%Y %H:%M:%S",
            )

            parsed = None

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

        return FenixAIQProvider._ensure_utc(
            parsed
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
        cleaned = str(value or "").strip().lower()

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

        return cleaned.upper() or None
