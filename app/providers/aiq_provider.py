from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class AIQProviderError(RuntimeError):
    """Raised when AIQ transaction retrieval fails."""


class AIQProvider:
    """
    Loads finished AIQ transactions.

    AIQ provides:

    - Preferred transaction ground truth
    - Alisa prediction values
    - UUID used to connect the transaction to container logs

    This provider does not:

    - Parse container logs
    - Fall back to Archive
    - Match Archive transactions
    - Compare prediction values
    - Export results
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        connect_timeout_seconds: int = 10,
    ) -> None:
        self.connection_parameters = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": user,
            "password": password,
            "connect_timeout": connect_timeout_seconds,
        }

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
        Retrieve finished AIQ transactions for the requested yards and UTC
        time range.

        The interval is inclusive at the start and exclusive at the end:

            start_utc <= transaction timestamp < end_utc
        """

        normalized_yards = self._normalize_yard_ids(yard_ids)

        if not normalized_yards:
            return []

        normalized_start = self._ensure_utc(start_utc)
        normalized_end = self._ensure_utc(end_utc)

        if normalized_start >= normalized_end:
            raise ValueError(
                "start_utc must be earlier than end_utc"
            )

        query = self._build_transaction_query()

        parameters = {
            "yard_ids": normalized_yards,
            "start_utc": normalized_start,
            "end_utc": normalized_end,
        }

        logging.info(
            "Loading AIQ transactions for %d yards from %s to %s",
            len(normalized_yards),
            normalized_start.isoformat(),
            normalized_end.isoformat(),
        )

        try:
            rows = self._execute_query(
                query=query,
                parameters=parameters,
            )
        except Exception as exc:
            logging.exception(
                "Failed to retrieve AIQ transactions"
            )
            raise AIQProviderError(
                "Failed to retrieve AIQ transactions"
            ) from exc

        transactions: list[UnifiedTransaction] = []

        for row in rows:
            try:
                transaction = self._row_to_transaction(row)
            except Exception:
                logging.exception(
                    "Failed to convert AIQ transaction row: %s",
                    row,
                )
                continue

            transactions.append(transaction)

        logging.info(
            "Loaded %d AIQ transactions",
            len(transactions),
        )

        return transactions

    def get_transactions_by_uuid(
        self,
        *,
        uuids: Sequence[str],
    ) -> list[UnifiedTransaction]:
        """
        Retrieve finished AIQ transactions for a list of UUIDs.

        This method is useful when container-log UUIDs are already known.
        """

        normalized_uuids = self._normalize_strings(uuids)

        if not normalized_uuids:
            return []

        query = self._build_uuid_query()

        parameters = {
            "uuids": normalized_uuids,
        }

        try:
            rows = self._execute_query(
                query=query,
                parameters=parameters,
            )
        except Exception as exc:
            logging.exception(
                "Failed to retrieve AIQ transactions by UUID"
            )
            raise AIQProviderError(
                "Failed to retrieve AIQ transactions by UUID"
            ) from exc

        transactions: list[UnifiedTransaction] = []

        for row in rows:
            try:
                transactions.append(
                    self._row_to_transaction(row)
                )
            except Exception:
                logging.exception(
                    "Failed to convert AIQ UUID row: %s",
                    row,
                )

        return transactions

    def has_transactions(
        self,
        *,
        yard_id: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> bool:
        """
        Return True when at least one eligible AIQ transaction exists.

        This method can later be used by the ground-truth selector before
        falling back to Archive.
        """

        cleaned_yard_id = yard_id.strip()

        if not cleaned_yard_id:
            return False

        normalized_start = self._ensure_utc(start_utc)
        normalized_end = self._ensure_utc(end_utc)

        query = """
        SELECT EXISTS (
            SELECT 1
            FROM truck_transaction tt
            JOIN truck_transaction_automation tta
                ON tt.id = tta.id
            LEFT JOIN gate g
                ON tt.gate_id = g.id
            LEFT JOIN location l
                ON g.location_id = l.id
            WHERE
                tt.status = 'FINISHED'
                AND l.name = %(yard_id)s
                AND tt.started >= %(start_utc)s
                AND tt.started < %(end_utc)s
        ) AS has_transactions;
        """

        rows = self._execute_query(
            query=query,
            parameters={
                "yard_id": cleaned_yard_id,
                "start_utc": normalized_start,
                "end_utc": normalized_end,
            },
        )

        if not rows:
            return False

        return bool(rows[0].get("has_transactions"))

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------

    @staticmethod
    def _base_select() -> str:
        """
        Shared SELECT clause.

        Important:
        Final AIQ values are always selected as ground truth.

        Alisa prediction values are populated only when the corresponding
        source column is 'alisa'.
        """

        return """
        SELECT
            tt.id AS transaction_id,
            tt.external_uuid AS uuid,
            tt.started AS started_utc,

            l.full_name AS location_name,
            l.name AS yard_id,

            g.id AS gate_id,
            g.name AS gate_name,

            tt.gate_direction AS direction,

            tt.vehicle_unit_number
                AS ground_truth_truck_number,

            CASE
                WHEN tta.vehicle_unit_number_source = 'alisa'
                THEN tta.vehicle_unit_number_value
                ELSE NULL
            END AS alisa_prediction_truck_number,

            tt.trailer_trailer_number
                AS ground_truth_trailer_number_1,

            CASE
                WHEN tta.trailer_trailer_number_source = 'alisa'
                THEN tta.trailer_trailer_number_value
                ELSE NULL
            END AS alisa_prediction_trailer_number_1,

            tt.trailer_trailer_number_two
                AS ground_truth_trailer_number_2,

            CASE
                WHEN tta.trailer_trailer_number_two_source = 'alisa'
                THEN tta.trailer_trailer_number_two_value
                ELSE NULL
            END AS alisa_prediction_trailer_number_2,

            tt.vehicle_transport_type
                AS ground_truth_vehicle_type,

            CASE
                WHEN tta.vehicle_transport_type_source = 'alisa'
                THEN tta.vehicle_transport_type_value
                ELSE NULL
            END AS alisa_prediction_vehicle_type,

            tta.vehicle_unit_number_source
                AS truck_prediction_source,

            tta.trailer_trailer_number_source
                AS trailer_1_prediction_source,

            tta.trailer_trailer_number_two_source
                AS trailer_2_prediction_source,

            tta.vehicle_transport_type_source
                AS vehicle_type_prediction_source

        FROM truck_transaction tt

        JOIN truck_transaction_automation tta
            ON tt.id = tta.id

        LEFT JOIN gate g
            ON tt.gate_id = g.id

        LEFT JOIN location l
            ON g.location_id = l.id
        """

    def _build_transaction_query(self) -> str:
        return (
            self._base_select()
            + """
            WHERE
                tt.status = 'FINISHED'
                AND l.name = ANY(%(yard_ids)s)
                AND tt.started >= %(start_utc)s
                AND tt.started < %(end_utc)s

            ORDER BY
                tt.started ASC,
                tt.id ASC;
            """
        )

    def _build_uuid_query(self) -> str:
        return (
            self._base_select()
            + """
            WHERE
                tt.status = 'FINISHED'
                AND tt.external_uuid = ANY(%(uuids)s)

            ORDER BY
                tt.started ASC,
                tt.id ASC;
            """
        )

    # ------------------------------------------------------------------
    # Database execution
    # ------------------------------------------------------------------

    def _execute_query(
        self,
        *,
        query: str,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with closing(
            psycopg2.connect(
                **self.connection_parameters
            )
        ) as connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                cursor.execute(
                    query,
                    parameters,
                )

                rows = cursor.fetchall()

        return [
            dict(row)
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    def _row_to_transaction(
        self,
        row: dict[str, Any],
    ) -> UnifiedTransaction:
        yard_id = self._clean_optional_string(
            row.get("yard_id")
        )

        if not yard_id:
            raise ValueError(
                "AIQ transaction row is missing yard_id"
            )

        timestamp_utc = self._coerce_database_timestamp_to_utc(
            row.get("started_utc")
        )

        transaction = UnifiedTransaction(
            yard_id=yard_id,
            uuid=self._clean_optional_string(
                row.get("uuid")
            ),
            ground_truth_transaction_id=(
                self._clean_optional_string(
                    row.get("transaction_id")
                )
            ),
            timestamp_utc=timestamp_utc,

            # The local timestamp will be populated later by a timezone
            # service because the AIQ database only provides the UTC
            # transaction timestamp here.
            timestamp_local=None,

            gate_id=self._clean_optional_string(
                row.get("gate_id")
            ),
            gate_name=self._clean_optional_string(
                row.get("gate_name")
            ),
            direction=self._normalize_direction(
                row.get("direction")
            ),

            ground_truth_source=GroundTruthSource.AIQ,

            ground_truth_truck_number=(
                self._clean_optional_string(
                    row.get(
                        "ground_truth_truck_number"
                    )
                )
            ),
            alisa_prediction_truck_number=(
                self._clean_optional_string(
                    row.get(
                        "alisa_prediction_truck_number"
                    )
                )
            ),

            ground_truth_trailer_number_1=(
                self._clean_optional_string(
                    row.get(
                        "ground_truth_trailer_number_1"
                    )
                )
            ),
            alisa_prediction_trailer_number_1=(
                self._clean_optional_string(
                    row.get(
                        "alisa_prediction_trailer_number_1"
                    )
                )
            ),

            ground_truth_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "ground_truth_trailer_number_2"
                    )
                )
            ),
            alisa_prediction_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "alisa_prediction_trailer_number_2"
                    )
                )
            ),

            ground_truth_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "ground_truth_vehicle_type"
                    )
                )
            ),
            alisa_prediction_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "alisa_prediction_vehicle_type"
                    )
                )
            ),

            metadata={
                "location_name": (
                    self._clean_optional_string(
                        row.get("location_name")
                    )
                ),
                "prediction_sources": {
                    "truck_number": (
                        self._clean_optional_string(
                            row.get(
                                "truck_prediction_source"
                            )
                        )
                    ),
                    "trailer_number_1": (
                        self._clean_optional_string(
                            row.get(
                                "trailer_1_prediction_source"
                            )
                        )
                    ),
                    "trailer_number_2": (
                        self._clean_optional_string(
                            row.get(
                                "trailer_2_prediction_source"
                            )
                        )
                    ),
                    "vehicle_type": (
                        self._clean_optional_string(
                            row.get(
                                "vehicle_type_prediction_source"
                            )
                        )
                    ),
                },
            },
        )

        if not transaction.uuid:
            transaction.add_flag(
                "AIQ_UUID_MISSING"
            )

        if not transaction.gate_id:
            transaction.add_flag(
                "AIQ_GATE_ID_MISSING"
            )

        if not transaction.direction:
            transaction.add_flag(
                "AIQ_DIRECTION_MISSING"
            )

        return transaction

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_yard_ids(
        yard_ids: Sequence[str],
    ) -> list[str]:
        return AIQProvider._normalize_strings(
            yard_ids
        )

    @staticmethod
    def _normalize_strings(
        values: Sequence[str],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = str(value).strip()

            if not cleaned:
                continue

            if cleaned in seen:
                continue

            seen.add(cleaned)
            normalized.append(cleaned)

        return normalized

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

    @classmethod
    def _coerce_database_timestamp_to_utc(
        cls,
        value: Any,
    ) -> Optional[datetime]:
        if value is None:
            return None

        if isinstance(value, datetime):
            return cls._ensure_utc(value)

        text = str(value).strip()

        if not text:
            return None

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid AIQ timestamp: {value}"
            ) from exc

        return cls._ensure_utc(parsed)