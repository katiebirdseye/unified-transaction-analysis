from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from app.models.unified_transaction import UnifiedTransaction


class DataFrameExporter:
    """
    Convert UnifiedTransaction objects into a one-row-per-transaction
    pandas DataFrame.

    Container-log truck, trailer, and vehicle values are displayed as a
    chronological evolution. Repeated values are intentionally preserved.
    """

    DEFAULT_SEPARATOR = ", "
    EVOLUTION_SEPARATOR = " -> "

    COLUMNS = [
        "Yard ID",
        "UUID",
        "Ground Truth Transaction ID",
        "Ground Truth Source",
        "Transaction Match Status",
        "Match Method",
        "Match Time Difference Seconds",

        "Timestamp Local",
        "Timestamp UTC",
        "Container Logs Last Timestamp UTC",

        "Gate ID",
        "Gate Name",
        "Direction",

        "Ground Truth Truck Number",
        "Alisa Prediction Truck Number",
        "Container Logs Truck Numbers",

        "Ground Truth Trailer Number 1",
        "Ground Truth Trailer Number 2",
        "Alisa Prediction Trailer Number 1",
        "Alisa Prediction Trailer Number 2",
        "Container Logs Trailer Numbers",

        "Ground Truth Vehicle Type",
        "Alisa Prediction Vehicle Type",
        "Container Logs Vehicle Types",

        "Container Logs Event Types",
        "Flags",
    ]

    def __init__(
        self,
        *,
        separator: str = DEFAULT_SEPARATOR,
        evolution_separator: str = EVOLUTION_SEPARATOR,
        include_metadata: bool = False,
    ) -> None:
        self.separator = separator
        self.evolution_separator = evolution_separator
        self.include_metadata = include_metadata

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_dataframe(
        self,
        transactions: Sequence[UnifiedTransaction],
    ) -> pd.DataFrame:
        """
        Build a DataFrame containing one row per transaction.

        Empty input returns an empty DataFrame with the expected columns.
        """

        rows = [
            self.transaction_to_row(
                transaction
            )
            for transaction in transactions
        ]

        columns = list(
            self.COLUMNS
        )

        if self.include_metadata:
            columns.append(
                "Metadata"
            )

        if not rows:
            return pd.DataFrame(
                columns=columns
            )

        dataframe = pd.DataFrame(
            rows
        )

        for column in columns:
            if column not in dataframe.columns:
                dataframe[column] = ""

        dataframe = dataframe[
            columns
        ]

        return dataframe

    def transaction_to_row(
        self,
        transaction: UnifiedTransaction,
    ) -> dict[str, Any]:
        """
        Convert one UnifiedTransaction into a readable flat row.
        """

        row: dict[str, Any] = {
            "Yard ID": (
                transaction.yard_id
                or ""
            ),
            "UUID": (
                transaction.uuid
                or ""
            ),
            "Ground Truth Transaction ID": (
                transaction.ground_truth_transaction_id
                or ""
            ),
            "Ground Truth Source": self._enum_value(
                transaction.ground_truth_source
            ),
            "Transaction Match Status": self._enum_value(
                transaction.transaction_match_status
            ),
            "Match Method": self._format_match_method(
                transaction.match_method
            ),
            "Match Time Difference Seconds": self._format_number(
                transaction.match_time_difference_seconds
            ),

            "Timestamp Local": self._format_datetime(
                transaction.timestamp_local
            ),
            "Timestamp UTC": self._format_datetime(
                transaction.timestamp_utc
            ),
            "Container Logs Last Timestamp UTC": (
                self._format_datetime(
                    transaction.container_logs_last_timestamp_utc
                )
            ),

            "Gate ID": (
                transaction.gate_id
                or ""
            ),
            "Gate Name": (
                transaction.gate_name
                or ""
            ),
            "Direction": (
                transaction.direction
                or ""
            ),

            "Ground Truth Truck Number": (
                transaction.ground_truth_truck_number
                or ""
            ),
            "Alisa Prediction Truck Number": (
                transaction.alisa_prediction_truck_number
                or ""
            ),
            "Container Logs Truck Numbers": (
                self._format_evolution(
                    transaction.container_logs_truck_numbers
                )
            ),

            "Ground Truth Trailer Number 1": (
                transaction.ground_truth_trailer_number_1
                or ""
            ),
            "Ground Truth Trailer Number 2": (
                transaction.ground_truth_trailer_number_2
                or ""
            ),
            "Alisa Prediction Trailer Number 1": (
                transaction.alisa_prediction_trailer_number_1
                or ""
            ),
            "Alisa Prediction Trailer Number 2": (
                transaction.alisa_prediction_trailer_number_2
                or ""
            ),
            "Container Logs Trailer Numbers": (
                self._format_evolution(
                    transaction.container_logs_trailer_numbers
                )
            ),

            "Ground Truth Vehicle Type": (
                transaction.ground_truth_vehicle_type
                or ""
            ),
            "Alisa Prediction Vehicle Type": (
                transaction.alisa_prediction_vehicle_type
                or ""
            ),
            "Container Logs Vehicle Types": (
                self._format_evolution(
                    transaction.container_logs_vehicle_types
                )
            ),

            "Container Logs Event Types": (
                self._format_values(
                    transaction.container_logs_event_types
                )
            ),
            "Flags": self._format_values(
                transaction.flags
            ),
        }

        if self.include_metadata:
            row["Metadata"] = (
                transaction.metadata
            )

        return row

    def export_csv(
        self,
        transactions: Sequence[UnifiedTransaction],
        output_path: str,
        *,
        index: bool = False,
    ) -> str:
        """
        Write the transaction table to CSV.

        Returns the output path.
        """

        dataframe = self.build_dataframe(
            transactions
        )

        dataframe.to_csv(
            output_path,
            index=index,
        )

        return output_path

    def export_excel(
        self,
        transactions: Sequence[UnifiedTransaction],
        output_path: str,
        *,
        sheet_name: str = "Transactions",
        index: bool = False,
    ) -> str:
        """
        Write the transaction table to an Excel workbook.

        Returns the output path.
        """

        dataframe = self.build_dataframe(
            transactions
        )

        dataframe.to_excel(
            output_path,
            sheet_name=sheet_name,
            index=index,
        )

        return output_path

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_evolution(
        self,
        values: Optional[Iterable[Any]],
    ) -> str:
        """
        Format chronological observations without deduplicating them.

        Example:

            1234 -> 12345 -> 1234
        """

        if not values:
            return ""

        cleaned_values: list[str] = []

        for value in values:
            cleaned = self._clean_value(
                value
            )

            if not cleaned:
                continue

            cleaned_values.append(
                cleaned
            )

        return self.evolution_separator.join(
            cleaned_values
        )

    def _format_values(
        self,
        values: Optional[Iterable[Any]],
    ) -> str:
        """
        Format non-chronological values while removing exact duplicates.

        Used for flags and event types.
        """

        if not values:
            return ""

        cleaned_values: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = self._clean_value(
                value
            )

            if not cleaned:
                continue

            if cleaned in seen:
                continue

            seen.add(
                cleaned
            )

            cleaned_values.append(
                cleaned
            )

        return self.separator.join(
            cleaned_values
        )

    def _format_datetime_list(
        self,
        values: Optional[Iterable[datetime]],
    ) -> str:
        """
        Retained for compatibility with callers that may use it directly.

        Timestamp-list columns are no longer included in the default
        spreadsheet report.
        """

        if not values:
            return ""

        formatted: list[str] = []
        seen: set[str] = set()

        for value in values:
            text = self._format_datetime(
                value
            )

            if not text:
                continue

            if text in seen:
                continue

            seen.add(
                text
            )

            formatted.append(
                text
            )

        return self.separator.join(
            formatted
        )

    @staticmethod
    def _format_datetime(
        value: Optional[datetime],
    ) -> str:
        """
        Format timestamps consistently.

        Timezone-aware datetimes retain their UTC offset.
        """

        if value is None:
            return ""

        return value.isoformat(
            sep=" ",
            timespec="seconds",
        )

    @staticmethod
    def _format_number(
        value: Optional[float],
    ) -> float | str:
        if value is None:
            return ""

        rounded = round(
            float(value),
            3,
        )

        if rounded.is_integer():
            return int(
                rounded
            )

        return rounded

    @staticmethod
    def _enum_value(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        if isinstance(
            value,
            Enum,
        ):
            return str(
                value.value
            )

        return str(
            value
        )

    @staticmethod
    def _format_match_method(
        value: Any,
    ) -> str:
        """
        Convert internal matching methods into report-friendly labels.
        """

        if value is None:
            return ""

        if isinstance(value, Enum):
            cleaned = str(value.value)
        else:
            cleaned = str(value)

        cleaned = cleaned.strip().upper()

        if cleaned == "UUID":
            return "CONTAINER_LOGS -> AIQ"

        if cleaned in {
            "TIMESTAMP_DIRECTION",
            "TIMESTAMP_DIRECTION_GATE",
        }:
            return "CONTAINER_LOGS -> ARCHIVE"

        if cleaned == "NONE":
            return "NONE"

        return cleaned

    @staticmethod
    def _clean_value(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        if isinstance(
            value,
            Enum,
        ):
            return str(
                value.value
            ).strip()

        return str(
            value
        ).strip()
