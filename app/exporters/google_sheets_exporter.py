from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from app.exporters.dataframe_exporter import DataFrameExporter
from app.models.unified_transaction import UnifiedTransaction


class GoogleSheetsExporterError(RuntimeError):
    """Raised when exporting transaction data to Google Sheets fails."""


class GoogleSheetsExporter:
    """
    Export unified transaction rows to Google Sheets.

    The exporter replaces the contents of one worksheet on every run.

    Responsibilities:

    - Authenticate using a Google service account
    - Open a spreadsheet by ID
    - Create the worksheet when it does not exist
    - Clear previous worksheet contents
    - Write headers and transaction rows
    - Freeze the first row
    - Resize columns for readability

    This class does not run the analysis pipeline itself.
    """

    DEFAULT_SCOPES = (
        "https://www.googleapis.com/auth/spreadsheets",
    )

    def __init__(
        self,
        *,
        credentials_file: str,
        spreadsheet_id: str,
        worksheet_name: str = "Unified Transactions",
        scopes: Optional[Sequence[str]] = None,
        dataframe_exporter: Optional[DataFrameExporter] = None,
        batch_size: int = 1000,
    ) -> None:
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self.worksheet_name = worksheet_name
        self.scopes = tuple(scopes or self.DEFAULT_SCOPES)
        self.batch_size = batch_size

        self.dataframe_exporter = (
            dataframe_exporter
            or DataFrameExporter()
        )

        self._service: Optional[Resource] = None

        self._validate_configuration()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_transactions(
        self,
        transactions: Sequence[UnifiedTransaction],
    ) -> dict[str, Any]:
        """
        Convert transactions to a DataFrame and replace the worksheet.

        Returns summary information about the export.
        """

        dataframe = self.dataframe_exporter.build_dataframe(
            transactions
        )

        return self.export_dataframe(dataframe)

    def with_worksheet(
        self,
        worksheet_name: str,
    ) -> GoogleSheetsExporter:
        """
        Return a new exporter using the same spreadsheet configuration
        but targeting a different worksheet.

        Each yard can therefore have its own worksheet while sharing the
        same destination spreadsheet and service-account credentials.
        """

        cleaned_worksheet_name = str(
            worksheet_name
        ).strip()

        if not cleaned_worksheet_name:
            raise ValueError(
                "worksheet_name cannot be empty"
            )

        return GoogleSheetsExporter(
            credentials_file=self.credentials_file,
            spreadsheet_id=self.spreadsheet_id,
            worksheet_name=cleaned_worksheet_name,
            scopes=self.scopes,
            dataframe_exporter=self.dataframe_exporter,
            batch_size=self.batch_size,
        )

    def export_dataframe(
        self,
        dataframe: pd.DataFrame,
    ) -> dict[str, Any]:
        """
        Replace the configured worksheet with DataFrame contents.
        """

        values = self._dataframe_to_values(dataframe)

        try:
            service = self._get_service()

            worksheet = self._get_or_create_worksheet(
                service=service,
                required_rows=max(len(values), 2),
                required_columns=max(
                    len(dataframe.columns),
                    1,
                ),
            )

            worksheet_id = worksheet["properties"]["sheetId"]

            self._clear_worksheet(service)

            if values:
                self._write_values_in_batches(
                    service=service,
                    values=values,
                )

            self._format_worksheet(
                service=service,
                worksheet_id=worksheet_id,
                column_count=max(
                    len(dataframe.columns),
                    1,
                ),
            )

        except HttpError as exc:
            logging.exception(
                "Google Sheets API error while exporting worksheet %s",
                self.worksheet_name,
            )
            raise GoogleSheetsExporterError(
                "Google Sheets export failed"
            ) from exc

        except Exception as exc:
            logging.exception(
                "Unexpected error while exporting worksheet %s",
                self.worksheet_name,
            )
            raise GoogleSheetsExporterError(
                "Google Sheets export failed"
            ) from exc

        result = {
            "spreadsheet_id": self.spreadsheet_id,
            "worksheet_name": self.worksheet_name,
            "row_count": len(dataframe),
            "column_count": len(dataframe.columns),
            "written_cell_rows": len(values),
        }

        logging.info(
            "Exported %d transaction rows to spreadsheet %s, worksheet %s",
            len(dataframe),
            self.spreadsheet_id,
            self.worksheet_name,
        )

        return result

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_service(self) -> Resource:
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

    # ------------------------------------------------------------------
    # Worksheet management
    # ------------------------------------------------------------------

    def _get_or_create_worksheet(
        self,
        *,
        service: Resource,
        required_rows: int,
        required_columns: int,
    ) -> dict[str, Any]:
        spreadsheet = (
            service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                fields=(
                    "sheets.properties("
                    "sheetId,title,gridProperties"
                    ")"
                ),
            )
            .execute()
        )

        for worksheet in spreadsheet.get("sheets", []):
            properties = worksheet.get(
                "properties",
                {},
            )

            if properties.get("title") == self.worksheet_name:
                self._ensure_grid_size(
                    service=service,
                    worksheet=worksheet,
                    required_rows=required_rows,
                    required_columns=required_columns,
                )

                return worksheet

        response = (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": self.worksheet_name,
                                    "gridProperties": {
                                        "rowCount": max(
                                            required_rows,
                                            1000,
                                        ),
                                        "columnCount": max(
                                            required_columns,
                                            26,
                                        ),
                                        "frozenRowCount": 1,
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )

        replies = response.get("replies", [])

        if not replies:
            raise GoogleSheetsExporterError(
                "Google Sheets did not return the created worksheet"
            )

        created_properties = (
            replies[0]
            .get("addSheet", {})
            .get("properties", {})
        )

        if not created_properties:
            raise GoogleSheetsExporterError(
                "Created worksheet properties were missing"
            )

        return {
            "properties": created_properties
        }

    def _ensure_grid_size(
        self,
        *,
        service: Resource,
        worksheet: dict[str, Any],
        required_rows: int,
        required_columns: int,
    ) -> None:
        properties = worksheet.get(
            "properties",
            {},
        )

        grid_properties = properties.get(
            "gridProperties",
            {},
        )

        current_rows = int(
            grid_properties.get(
                "rowCount",
                0,
            )
        )

        current_columns = int(
            grid_properties.get(
                "columnCount",
                0,
            )
        )

        target_rows = max(
            current_rows,
            required_rows,
        )

        target_columns = max(
            current_columns,
            required_columns,
        )

        if (
            target_rows == current_rows
            and target_columns == current_columns
        ):
            return

        sheet_id = properties.get("sheetId")

        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {
                                    "rowCount": target_rows,
                                    "columnCount": target_columns,
                                },
                            },
                            "fields": (
                                "gridProperties.rowCount,"
                                "gridProperties.columnCount"
                            ),
                        }
                    }
                ]
            },
        ).execute()

    def _clear_worksheet(
        self,
        service: Resource,
    ) -> None:
        range_name = self._quote_sheet_name(
            self.worksheet_name
        )

        (
            service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                body={},
            )
            .execute()
        )

    # ------------------------------------------------------------------
    # Value writing
    # ------------------------------------------------------------------

    def _write_values_in_batches(
        self,
        *,
        service: Resource,
        values: list[list[Any]],
    ) -> None:
        if not values:
            return

        total_rows = len(values)

        for start_index in range(
            0,
            total_rows,
            self.batch_size,
        ):
            batch = values[
                start_index:
                start_index + self.batch_size
            ]

            start_row = start_index + 1
            end_row = start_row + len(batch) - 1

            end_column = self._column_number_to_letter(
                max(len(row) for row in batch)
            )

            range_name = (
                f"{self._quote_sheet_name(self.worksheet_name)}"
                f"!A{start_row}:{end_column}{end_row}"
            )

            (
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    body={
                        "majorDimension": "ROWS",
                        "values": batch,
                    },
                )
                .execute()
            )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------
    def _format_worksheet(
        self,
        *,
        service: Resource,
        worksheet_id: int,
        column_count: int,
    ) -> None:
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": worksheet_id,
                        "gridProperties": {
                            "frozenRowCount": 1,
                        },
                    },
                    "fields": (
                        "gridProperties.frozenRowCount"
                    ),
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {
                                "bold": True,
                            },
                            "wrapStrategy": "CLIP",
                            "verticalAlignment": "MIDDLE",
                        }
                    },
                    "fields": (
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.wrapStrategy,"
                        "userEnteredFormat.verticalAlignment"
                    ),
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "wrapStrategy": "CLIP",
                            "verticalAlignment": "TOP",
                        }
                    },
                    "fields": (
                        "userEnteredFormat.wrapStrategy,"
                        "userEnteredFormat.verticalAlignment"
                    ),
                }
            },
        ]

        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": requests,
            },
        ).execute()

    # ------------------------------------------------------------------
    # Data conversion
    # ------------------------------------------------------------------

    def _dataframe_to_values(
        self,
        dataframe: pd.DataFrame,
    ) -> list[list[Any]]:
        """
        Convert DataFrame values into a Google Sheets-compatible matrix.

        NaN, NaT, and None values become empty strings.
        """

        headers = [
            str(column)
            for column in dataframe.columns
        ]

        rows: list[list[Any]] = []

        for record in dataframe.itertuples(
            index=False,
            name=None,
        ):
            rows.append(
                [
                    self._sanitize_cell_value(value)
                    for value in record
                ]
            )

        return [
            headers,
            *rows,
        ]

    @classmethod
    def _sanitize_cell_value(
        cls,
        value: Any,
    ) -> Any:
        if value is None:
            return ""

        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass

        if isinstance(value, datetime):
            return value.isoformat(
                sep=" ",
                timespec="seconds",
            )

        if isinstance(value, dict):
            return str(value)

        if isinstance(value, (list, tuple, set)):
            return ", ".join(
                str(item)
                for item in value
                if item is not None
            )

        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float, str)):
            return value

        return str(value)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_configuration(self) -> None:
        if not self.credentials_file:
            raise ValueError(
                "credentials_file cannot be empty"
            )

        if not Path(self.credentials_file).exists():
            raise FileNotFoundError(
                f"Google credentials file not found: "
                f"{self.credentials_file}"
            )

        if not self.spreadsheet_id.strip():
            raise ValueError(
                "spreadsheet_id cannot be empty"
            )

        if not self.worksheet_name.strip():
            raise ValueError(
                "worksheet_name cannot be empty"
            )

        if self.batch_size <= 0:
            raise ValueError(
                "batch_size must be greater than zero"
            )

    @staticmethod
    def _quote_sheet_name(
        sheet_name: str,
    ) -> str:
        escaped = sheet_name.replace(
            "'",
            "''",
        )

        return f"'{escaped}'"

    @staticmethod
    def _column_number_to_letter(
        column_number: int,
    ) -> str:
        """
        Convert a 1-based column number to an Excel/Sheets letter.

        Examples:
            1  -> A
            26 -> Z
            27 -> AA
        """

        if column_number < 1:
            raise ValueError(
                "column_number must be at least 1"
            )

        letters: list[str] = []
        number = column_number

        while number:
            number, remainder = divmod(
                number - 1,
                26,
            )

            letters.append(
                chr(
                    ord("A")
                    + remainder
                )
            )

        return "".join(
            reversed(letters)
        )

