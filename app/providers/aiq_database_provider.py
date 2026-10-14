from __future__ import annotations

import logging
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from app.models.unified_transaction import (
    GroundTruthSource,
    UnifiedTransaction,
)


class AIQDatabaseProviderError(RuntimeError):
    """Raised when AIQ transaction retrieval from PostgreSQL fails."""


class AIQDatabaseProvider:
    """
    Load AIQ transactions directly from PostgreSQL.

    This preserves the same logical dataset previously populated into the
    AIQ Google Sheet by the Apps Script / Fenix workflow.
    """

    BASE_SELECT = """
        SELECT
            tt.id AS transaction_id,
            tta.vehicle_unit_number_source AS truck_output_source,
            tta.trailer_trailer_number_source AS trailer_output_source,
            tta.trailer_trailer_number_two_source AS second_trailer_output_source,
            tta.vehicle_transport_type_source AS vehicle_output_source,
            tt.external_uuid AS uuid,
            tt.started AS timestamp_utc,

            l.full_name AS location,
            l.name AS yard_id,

            g.id AS gate_id,
            g.name AS gate_name,
            tt.gate_direction AS direction,

            CASE
                WHEN tta.vehicle_transport_type_source = 'alisa'
                THEN tta.vehicle_transport_type_value
            END AS vehicle_transport_type_alisa,

            tt.vehicle_transport_type AS vehicle_transport_type_entered,

            CASE
                WHEN tta.vehicle_unit_number_source = 'alisa'
                THEN tta.vehicle_unit_number_value
            END AS truck_alisa,

            tt.vehicle_unit_number AS truck_entered,

            CASE
                WHEN tta.trailer_trailer_number_source = 'alisa'
                THEN tta.trailer_trailer_number_value
            END AS trailer_alisa,

            tt.trailer_trailer_number AS trailer_entered,

            CASE
                WHEN tta.trailer_trailer_number_two_source = 'alisa'
                THEN tta.trailer_trailer_number_two_value
            END AS second_trailer_alisa,

            tt.trailer_trailer_number_two AS second_trailer_entered

        FROM public.truck_transaction tt

        JOIN public.truck_transaction_automation tta
            ON tt.id = tta.id

        LEFT JOIN public.gate g
            ON tt.gate_id = g.id

        LEFT JOIN public.location l
            ON g.location_id = l.id
    """

    ALISA_SOURCE_FILTER = """
        (
            tta.vehicle_unit_number_source = 'alisa'
            OR tta.vehicle_transport_type_source = 'alisa'
            OR tta.vehicle_license_plate_number_source = 'alisa'
            OR tta.trailer_trailer_number_source = 'alisa'
            OR tta.trailer_trailer_number_two_source = 'alisa'
        )
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        connect_timeout_seconds: int,
        utc_to_local,
        query_max_attempts: int = 5,
    ) -> None:
        self.host = str(host).strip()
        self.port = int(port)
        self.database = str(database).strip()
        self.user = str(user).strip()
        self.password = str(password)
        self.connect_timeout_seconds = int(
            connect_timeout_seconds
        )
        self.utc_to_local = utc_to_local
        if type(query_max_attempts) is not int or not 1 <= query_max_attempts <= 5:
            raise ValueError("query_max_attempts must be an integer from 1 to 5")
        self.query_max_attempts = query_max_attempts

        self._validate_configuration()

    def get_transactions(
        self,
        *,
        yard_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[UnifiedTransaction]:
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

        query = (
            self.BASE_SELECT.replace(
                "JOIN public.truck_transaction_automation tta",
                "LEFT JOIN public.truck_transaction_automation tta",
            )
            + """
            WHERE
                tt.status = 'FINISHED'

                AND LOWER(TRIM(l.name)) = ANY(%s)

                AND tt.started >= %s
                AND tt.started < %s

            ORDER BY tt.started ASC
            """
        )

        yard_lookup = [
            self._normalize_lookup(yard)
            for yard in normalized_yards
        ]

        rows = self._execute_query(
            query,
            (
                yard_lookup,
                normalized_start.replace(
                    tzinfo=None
                ),
                normalized_end.replace(
                    tzinfo=None
                ),
            ),
        )

        transactions: list[
            UnifiedTransaction
        ] = []

        for source_index, row in enumerate(
            rows,
            start=1,
        ):
            try:
                transaction = self._row_to_transaction(
                    row=row,
                    source_index=source_index,
                )
            except Exception:
                logging.exception(
                    "Could not convert AIQ database row %d",
                    source_index,
                )
                continue

            if transaction is not None:
                transactions.append(transaction)

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
            "from PostgreSQL",
            len(transactions),
        )

        return transactions

    def get_transactions_by_id(self, *, transaction_id: str) -> list[UnifiedTransaction]:
        """Exact text identity lookup, independent of date, yard and origin filters.

        Query globally so the caller can distinguish a wrong yard from absence.
        The left join retains transactions without automation records. Do not
        reuse the time-window FINISHED/Alisa filter for identity lookup.
        """
        cleaned = transaction_id.strip()
        if not cleaned:
            return []
        query = self.BASE_SELECT.replace(
            "JOIN public.truck_transaction_automation tta",
            "LEFT JOIN public.truck_transaction_automation tta",
        ) + " WHERE TRIM(tt.id::text) = %s ORDER BY tt.started ASC"
        rows = self._execute_query(query, (cleaned,))
        transactions = []
        for index, row in enumerate(rows, start=1):
            transaction = self._row_to_transaction(row=row, source_index=index)
            if transaction is None:
                # A found but unusable source record must never become NOT_FOUND.
                raise AIQDatabaseProviderError("Exact AIQ record lacks yard or transaction time")
            transactions.append(transaction)
        return transactions

    def get_transactions_for_matching(
        self, *, yard_id: str, start_utc: datetime, end_utc: datetime,
    ) -> list[UnifiedTransaction]:
        """Competition contenders, including manual/non-Alisa AIQ records.

        Keep exact-ID lookup semantics separate from this yard/time read.
        Missing or duplicate native identities must fail closed in the caller.
        """
        start, end = self._ensure_utc(start_utc), self._ensure_utc(end_utc)
        if start >= end:
            raise ValueError("start_utc must be earlier than end_utc")
        query = self.BASE_SELECT.replace(
            "JOIN public.truck_transaction_automation tta",
            "LEFT JOIN public.truck_transaction_automation tta",
        ) + """
            WHERE l.name = %s AND tt.started >= %s AND tt.started < %s
            ORDER BY tt.started ASC, tt.id ASC
        """
        rows = self._execute_query(query, (
            yard_id, start.replace(tzinfo=None), end.replace(tzinfo=None),
        ))
        transactions = []
        for index, row in enumerate(rows, start=1):
            if not self._clean_optional_string(row.get("transaction_id")):
                raise AIQDatabaseProviderError("AIQ competitor lacks native transaction ID")
            transaction = self._row_to_transaction(row=row, source_index=index)
            if transaction is None:
                raise AIQDatabaseProviderError("AIQ competitor lacks yard or transaction time")
            transactions.append(transaction)
        return transactions

    def get_transactions_by_uuid(
        self,
        *,
        uuids: Sequence[str],
    ) -> list[UnifiedTransaction]:
        normalized_uuids = self._normalize_strings(
            uuids
        )

        if not normalized_uuids:
            return []

        query = (
            self.BASE_SELECT
            + """
            WHERE
                tt.status = 'FINISHED'

                AND LOWER(tt.external_uuid::text) = ANY(%s)

                AND
            """
            + self.ALISA_SOURCE_FILTER
            + """
            ORDER BY tt.started ASC
            """
        )

        rows = self._execute_query(
            query,
            (
                [
                    self._normalize_lookup(value)
                    for value in normalized_uuids
                ],
            ),
        )

        transactions: list[
            UnifiedTransaction
        ] = []

        for source_index, row in enumerate(
            rows,
            start=1,
        ):
            transaction = self._row_to_transaction(
                row=row,
                source_index=source_index,
            )

            if transaction is not None:
                transactions.append(transaction)

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

    def refresh(self) -> None:
        return None

    def _execute_query(
        self,
        query: str,
        params: tuple,
    ) -> list[dict[str, Any]]:
        for attempt in range(1, self.query_max_attempts + 1):
            try:
                # psycopg2's transaction context commits/rolls back but does
                # not close the connection. Close it before any retry/backoff.
                with closing(psycopg2.connect(
                    host=self.host,
                    port=self.port,
                    dbname=self.database,
                    user=self.user,
                    password=self.password,
                    connect_timeout=self.connect_timeout_seconds,
                    sslmode="require",
                )) as connection:
                    with connection:
                        connection.set_session(readonly=True)
                        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                            cursor.execute(query, params)
                            rows = cursor.fetchall()
                        return [dict(row) for row in rows]
            except Exception as exc:
                if attempt < self.query_max_attempts and self._is_transient_read_failure(exc):
                    logging.warning(
                        "AIQ PostgreSQL read attempt %d/%d failed (%s); retrying on a fresh connection",
                        attempt, self.query_max_attempts, type(exc).__name__,
                    )
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                logging.error(
                    "AIQ PostgreSQL read failed on attempt %d/%d (%s)",
                    attempt, self.query_max_attempts, type(exc).__name__,
                )
                raise AIQDatabaseProviderError(
                    "Could not retrieve AIQ transactions from PostgreSQL"
                ) from exc

    @staticmethod
    def _is_transient_read_failure(exc: Exception) -> bool:
        """Narrow allowlist: recovery cancellation and closed/reset connections."""
        # A failed rollback can mask the original query error. A closed
        # connection after a recovery conflict is retryable; one masking an
        # authentication/programming/application error must fail immediately.
        original = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        if original is not None and (
                original is exc or not AIQDatabaseProvider._is_transient_read_failure(original)):
            return False
        message = str(exc).casefold()
        if isinstance(exc, psycopg2.errors.SerializationFailure):
            return "conflict with recovery" in message
        if isinstance(exc, psycopg2.OperationalError):
            # Authentication errors can inherit OperationalError. Never retry
            # them even if a driver/server also mentions a closed connection.
            if (isinstance(exc, (psycopg2.errors.InvalidPassword,
                                 psycopg2.errors.InvalidAuthorizationSpecification)) or
                    str(getattr(exc, "pgcode", "") or "").startswith("28")):
                return False
            if any(marker in message for marker in (
                "authentication failed", "password authentication", "no pg_hba.conf entry",
            )):
                return False
            return any(marker in message for marker in (
                "ssl connection has been closed unexpectedly",
                "server closed the connection unexpectedly",
                "connection reset by peer",
                "connection already closed",
                "connection is closed",
            ))
        return False

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
            row.get("timestamp_utc")
        )

        if not yard_id:
            return None

        if timestamp_utc is None:
            return None

        timestamp_local = self.utc_to_local(
            timestamp_utc,
            yard_id,
        )

        transaction_id = (
            self._clean_optional_string(
                row.get("transaction_id")
            )
        )

        if not transaction_id:
            transaction_id = (
                f"AIQ:{yard_id}:"
                f"{timestamp_utc.strftime('%Y%m%dT%H%M%S')}:"
                f"{source_index}"
            )

        transaction = UnifiedTransaction(
            yard_id=yard_id,
            # external_uuid is retained below as AIQ source metadata only.
            uuid=None,
            ground_truth_transaction_id=(
                transaction_id
            ),
            ground_truth_lookup_yard_id=yard_id,
            timestamp_local=timestamp_local,
            timestamp_utc=timestamp_utc,
            gate_id=self._clean_optional_string(row.get("gate_id")),
            gate_name=self._clean_optional_string(
                row.get("gate_name")
            ),
            direction=self._normalize_direction(
                row.get("direction")
            ),
            ground_truth_source=(
                GroundTruthSource.AIQ
            ),
            ground_truth_truck_number=(
                self._clean_optional_string(
                    row.get("truck_entered")
                )
            ),
            ground_truth_trailer_number_1=(
                self._clean_optional_string(
                    row.get("trailer_entered")
                )
            ),
            ground_truth_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "second_trailer_entered"
                    )
                )
            ),
            ground_truth_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "vehicle_transport_type_entered"
                    )
                )
            ),
            alisa_prediction_truck_number=(
                self._clean_optional_string(
                    row.get("truck_alisa")
                )
            ),
            alisa_prediction_trailer_number_1=(
                self._clean_optional_string(
                    row.get("trailer_alisa")
                )
            ),
            alisa_prediction_trailer_number_2=(
                self._clean_optional_string(
                    row.get(
                        "second_trailer_alisa"
                    )
                )
            ),
            alisa_prediction_vehicle_type=(
                self._clean_optional_string(
                    row.get(
                        "vehicle_transport_type_alisa"
                    )
                )
            ),
            metadata={
                "location": (
                    self._clean_optional_string(
                        row.get("location")
                    )
                ),
                "aiq_provider": "postgresql",
            },
        )

        # Retain exact SQL values independently of trimmed comparator fields.
        # Field attribution never establishes automatic event origin.
        transaction._retained_aiq_fields = {
            "collector": "aiq-postgresql-fields.v1",
            "transaction_id": self._clean_optional_string(row.get("transaction_id")),
            "uuid": self._clean_optional_string(row.get("uuid")),
            "yard_id": yard_id,
            "fields": {
                name: {"raw_value": row.get(value_column),
                       "attribution": row.get(source_column)}
                for name, value_column, source_column in (
                    ("truck_number", "truck_alisa", "truck_output_source"),
                    ("trailer_number_1", "trailer_alisa", "trailer_output_source"),
                    ("trailer_number_2", "second_trailer_alisa", "second_trailer_output_source"),
                    ("vehicle_type", "vehicle_transport_type_alisa", "vehicle_output_source"),
                )
            },
        }
        transaction._decision_raw_fields = {
            name: {"ground_truth": row.get(truth_column), "prediction": row.get(prediction_column)}
            for name, truth_column, prediction_column in (
                ("truck_number", "truck_entered", "truck_alisa"),
                ("trailer_number_1", "trailer_entered", "trailer_alisa"),
                ("trailer_number_2", "second_trailer_entered", "second_trailer_alisa"),
                ("vehicle_type", "vehicle_transport_type_entered", "vehicle_transport_type_alisa"),
            )
        }
        return transaction

    def _validate_configuration(
        self,
    ) -> None:
        missing: list[str] = []

        if not self.host:
            missing.append("AIQ_DB_HOST")

        if not self.database:
            missing.append("AIQ_DB_NAME")

        if not self.user:
            missing.append("AIQ_DB_USER")

        if not self.password:
            missing.append("AIQ_DB_PASSWORD")

        if not callable(
            self.utc_to_local
        ):
            missing.append(
                "utc_to_local callback"
            )

        if missing:
            raise ValueError(
                "Missing AIQ PostgreSQL configuration: "
                + ", ".join(missing)
            )

    @staticmethod
    def _parse_timestamp_utc(
        value: Any,
    ) -> Optional[datetime]:
        if isinstance(value, datetime):
            return AIQDatabaseProvider._ensure_utc(
                value
            )

        text = str(value or "").strip()

        if not text:
            return None

        try:
            parsed = datetime.fromisoformat(
                text
            )
        except ValueError:
            return None

        return AIQDatabaseProvider._ensure_utc(
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
    def _normalize_strings(
        values: Sequence[str],
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for value in values:
            cleaned = str(value).strip()

            if not cleaned:
                continue

            key = cleaned.lower()

            if key in seen:
                continue

            seen.add(key)
            normalized.append(cleaned)

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

        cleaned = str(value).strip()

        return cleaned or None

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
