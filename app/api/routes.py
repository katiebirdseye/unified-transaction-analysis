from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import (
    ApplicationServices,
    get_application_services,
)
from app.config import settings
from app.models.ticket_analysis import TicketAnalysisRequest
from app.services.ticket_analysis import build_ticket_analysis
from app.models.proof import ProofResponse
from app.models.system_output import OutputProofResponse
from app.services.system_output_serializer import extend_output_proof
from app.services.proof_serializer import build_proof_response
from app.exporters.dataframe_exporter import (
    DataFrameExporter,
)
from app.exporters.google_sheets_exporter import (
    GoogleSheetsExporterError,
)
from app.services.analysis_service import (
    AnalysisRequest,
)
from app.services.ground_truth_selector import (
    GroundTruthSelectionError,
)
from app.services.timezone_service import (
    TimezoneServiceError,
    YardNotFoundError,
)


router = APIRouter(
    prefix="/api",
    tags=["analysis"],
)


class UnifiedAnalysisRequest(BaseModel):
    """
    Request one-yard unified transaction analysis.

    Supply exactly one of yard_name or yard_id.

    When dates are omitted, the service analyzes the most recent
    lookback_days local calendar dates for the requested yard.
    """

    yard_name: Optional[str] = Field(
        default=None,
        description=(
            "Yard Name from the Timezones worksheet, "
            "for example Bison (Regina)."
        ),
    )

    yard_id: Optional[str] = Field(
        default=None,
        description=(
            "Canonical yard ID, for example biz_rgn."
        ),
    )

    dates: Optional[
        list[date | datetime | str]
    ] = Field(
        default=None,
        description=(
            "Optional yard-local calendar dates. "
            "When omitted, the latest rolling window is used."
        ),
    )

    lookback_days: int = Field(
        default=settings.analysis_lookback_days,
        ge=1,
        le=90,
        description=(
            "Number of recent yard-local calendar dates to analyze "
            "when dates are not supplied."
        ),
    )

    include_container_logs_only: bool = Field(
        default=settings.include_container_logs_only,
        description=(
            "Include unmatched container-log UUIDs in the final output."
        ),
    )

    normalized_ocr_deduplication: bool = Field(
        default=False,
        description=(
            "Enable normalized OCR value deduplication while parsing "
            "container logs."
        ),
    )

    export_to_google_sheets: bool = Field(
        default=False,
        description=(
            "When true, overwrite the Google Sheets worksheet assigned "
            "to this yard."
        ),
    )

class YardListItem(BaseModel):
    yard_id: str
    yard_name: str


class CSVExportRequest(BaseModel):
    """
    Request unified transaction rows as one CSV file.

    The supplied time range is interpreted as local time for the
    requested yard.

    start_local is inclusive.
    end_local is exclusive.

    Local timestamps must not contain a timezone offset. The yard's
    timezone and DST configuration are resolved dynamically from
    Google Sheets.
    """

    yard_id: str = Field(
        min_length=1,
        description=(
            "Canonical yard ID, for example biz_rgn."
        ),
    )

    start_local: datetime = Field(
        description=(
            "Inclusive local start date and time. "
            "Do not include a timezone offset or Z suffix."
        ),
    )

    end_local: datetime = Field(
        description=(
            "Exclusive local end date and time. "
            "Do not include a timezone offset or Z suffix."
        ),
    )

    include_container_logs_only: bool = Field(
        default=settings.include_container_logs_only,
        description=(
            "Include unmatched container-log UUIDs in the CSV."
        ),
    )

    normalized_ocr_deduplication: bool = Field(
        default=False,
        description=(
            "Enable normalized OCR value deduplication while parsing "
            "container logs."
        ),
    )


class TimezoneRefreshResponse(BaseModel):
    status: str
    loaded_yards: int
    worksheet: str
    refreshed_at_utc: Optional[str]


def _build_yard_worksheet_name(
    yard_id: str,
) -> str:
    """
    Build a valid Google Sheets worksheet title from a yard ID.
    """

    candidate = str(
        yard_id
    ).strip()

    candidate = re.sub(
        r"[:\\/?*\[\]]",
        "-",
        candidate,
    )

    candidate = re.sub(
        r"\s+",
        "_",
        candidate,
    ).strip()

    if not candidate:
        raise ValueError(
            "yard_id cannot be empty."
        )

    return candidate[:100]


def _clean_yard_id(
    yard_id: str,
) -> str:
    """
    Validate and normalize a requested yard ID.
    """

    cleaned = str(
        yard_id or ""
    ).strip()

    if not cleaned:
        raise ValueError(
            "yard_id cannot be empty."
        )

    return cleaned


def _ensure_naive_local_datetime(
    value: datetime,
    *,
    field_name: str,
) -> datetime:
    """
    Validate a yard-local datetime.

    The timestamp must not contain an offset because the application
    resolves the yard-specific UTC offset and DST rules.
    """

    if value.tzinfo is not None:
        raise ValueError(
            f"{field_name} must be a local datetime without "
            "a timezone offset or Z suffix."
        )

    return value


def _build_local_dates_for_range(
    *,
    start_local: datetime,
    end_local: datetime,
) -> list[date]:
    """
    Return every local calendar date touched by the interval.

    end_local is exclusive.
    """

    final_included_datetime = (
        end_local
        - timedelta(
            microseconds=1
        )
    )

    first_date = start_local.date()
    last_date = final_included_datetime.date()

    dates: list[date] = []
    current_date = first_date

    while current_date <= last_date:
        dates.append(
            current_date
        )

        current_date += timedelta(
            days=1
        )

    return dates


def _ensure_utc_datetime(
    value: datetime,
) -> datetime:
    """
    Normalize a datetime to timezone-aware UTC.
    """

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


def _transaction_is_in_utc_range(
    transaction,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> bool:
    """
    Keep transactions whose primary UTC timestamp falls in the range.

    start_utc is inclusive.
    end_utc is exclusive.
    """

    timestamp = (
        transaction.timestamp_utc
    )

    if timestamp is None:
        return False

    normalized_timestamp = (
        _ensure_utc_datetime(
            timestamp
        )
    )

    return (
        start_utc
        <= normalized_timestamp
        < end_utc
    )


@router.get("/status")
def analysis_status() -> dict[str, object]:
    """
    Return the current analysis configuration summary.

    Secret values are intentionally excluded.
    """

    return {
        "status": "ready",
        "runtime_environment": (
            settings.runtime_environment
        ),
        "default_lookback_days": (
            settings.analysis_lookback_days
        ),
        "container_logs_dir": (
            settings.container_logs_dir
        ),
        "aiq_worksheet": (
            settings.aiq_worksheet_name
        ),
        "archive_worksheet": (
            settings.live_archives_worksheet
        ),
        "timezone_worksheet": (
            settings.resolved_timezone_worksheet_name
        ),
        "output_spreadsheet_id": (
            settings.google_spreadsheet_id
        ),
        "output_strategy": (
            "one worksheet per yard ID; overwrite on each run"
        ),
        "csv_export_strategy": (
            "one yard per request; CSV generated in memory"
        ),
        "csv_time_range": (
            "yard-local time; start inclusive and end exclusive"
        ),
    }

@router.get("/yards")
def list_yards(
    services: ApplicationServices = Depends(
        get_application_services
    ),
) -> list[YardListItem]:
    """
    Return the current yard list from the Timezones worksheet.

    The Timezones worksheet is refreshed on every request so the
    returned yard list reflects the latest configuration.
    """
    try:
        yards = services.timezone_service.refresh()

        return [
            YardListItem(
                yard_id=yard.yard_id,
                yard_name=yard.yard_name,
            )
            for yard in sorted(
                yards,
                key=lambda item: (
                    item.yard_name or ""
                ).lower(),
            )
        ]

    except TimezoneServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logging.exception(
            "Failed to load yard list"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to load yard list. "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc
    
@router.post("/timezone/refresh")
def refresh_timezones(
    services: ApplicationServices = Depends(
        get_application_services
    ),
) -> TimezoneRefreshResponse:
    """
    Reload timezone and yard mappings from Google Sheets.
    """

    try:
        yards = (
            services.timezone_service.refresh()
        )

    except TimezoneServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    refreshed_at = (
        services.timezone_service
        .last_refreshed_at_utc
    )

    return TimezoneRefreshResponse(
        status="success",
        loaded_yards=len(yards),
        worksheet=(
            services.timezone_service
            .worksheet_name
        ),
        refreshed_at_utc=(
            refreshed_at.isoformat()
            if refreshed_at is not None
            else None
        ),
    )


@router.post("/analyze")
def analyze_transactions(
    payload: UnifiedAnalysisRequest,
    services: ApplicationServices = Depends(
        get_application_services
    ),
) -> dict:
    """
    Run unified transaction analysis for one yard.

    Ground-truth priority:

    1. AIQ transactions are matched by UUID.
    2. Archive transactions are used for remaining unmatched
       container-log transactions.

    When export_to_google_sheets is true, only the worksheet assigned
    to the requested yard ID is cleared and replaced.
    """

    supplied_yard_name = bool(
        str(
            payload.yard_name or ""
        ).strip()
    )

    supplied_yard_id = bool(
        str(
            payload.yard_id or ""
        ).strip()
    )

    if supplied_yard_name == supplied_yard_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Supply exactly one of yard_name or yard_id."
            ),
        )

    export_result = None
    export_worksheet_name = None

    try:
        services.timezone_service.refresh()

        if supplied_yard_name:
            yard = (
                services.timezone_service
                .resolve_by_name(
                    str(
                        payload.yard_name
                    ),
                    refresh=False,
                )
            )

        else:
            yard = (
                services.timezone_service
                .resolve_by_id(
                    str(
                        payload.yard_id
                    ),
                    refresh=False,
                )
            )

        request = AnalysisRequest(
            yard_id=yard.yard_id,
            dates=payload.dates,
            lookback_days=(
                payload.lookback_days
            ),
            include_container_logs_only=(
                payload.include_container_logs_only
            ),
            normalized_ocr_deduplication=(
                payload.normalized_ocr_deduplication
            ),
        )

        result = (
            services.analysis_service.analyze(
                request
            )
        )

        if payload.export_to_google_sheets:
            export_worksheet_name = (
                _build_yard_worksheet_name(
                    yard.yard_id
                )
            )

            yard_exporter = (
                services.google_sheets_exporter
                .with_worksheet(
                    export_worksheet_name
                )
            )

            export_result = (
                yard_exporter
                .export_transactions(
                    result.transactions
                )
            )

    except YardNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except GroundTruthSelectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except TimezoneServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except GoogleSheetsExporterError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logging.exception(
            "Unified analysis API request failed"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Unified analysis failed. "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    response = result.to_dict()

    response["yard_name"] = (
        yard.yard_name
    )

    response["timezone_name"] = (
        yard.timezone_name
    )

    response["standard_utc_offset_hours"] = (
        yard.standard_utc_offset_hours
    )

    response["dst_utc_offset_hours"] = (
        yard.dst_utc_offset_hours
    )

    response["observes_dst"] = (
        yard.observes_dst
    )

    response["exported_to_google_sheets"] = (
        export_result is not None
    )

    response["export_worksheet_name"] = (
        export_worksheet_name
    )

    response["google_sheets_export"] = (
        export_result
    )

    return response


@router.post("/export/csv")
def export_transactions_csv(
    payload: CSVExportRequest,
    services: ApplicationServices = Depends(
        get_application_services
    ),
) -> StreamingResponse:
    """
    Return unified transaction rows for one yard as a CSV download.

    The CSV is generated entirely in memory. It is not saved to the
    application server.

    Processing steps:

    1. Resolve the requested yard and its timezone configuration.
    2. Interpret start_local and end_local in that yard's local time.
    3. Convert the local interval to UTC.
    4. Load all local calendar dates touched by the interval.
    5. Run unified transaction analysis.
    6. Filter the final transactions to the exact interval.
    7. Return one CSV response.
    """

    try:
        requested_yard_id = (
            _clean_yard_id(
                payload.yard_id
            )
        )

        start_local = (
            _ensure_naive_local_datetime(
                payload.start_local,
                field_name="start_local",
            )
        )

        end_local = (
            _ensure_naive_local_datetime(
                payload.end_local,
                field_name="end_local",
            )
        )

        if start_local >= end_local:
            raise ValueError(
                "start_local must be earlier than end_local."
            )

        requested_duration = (
            end_local - start_local
        )

        if requested_duration > timedelta(
            days=90
        ):
            raise ValueError(
                "The CSV local time range cannot exceed 90 days."
            )

        local_dates = (
            _build_local_dates_for_range(
                start_local=start_local,
                end_local=end_local,
            )
        )

        services.timezone_service.refresh()

        yard = (
            services.timezone_service
            .resolve_by_id(
                requested_yard_id,
                refresh=False,
            )
        )

        yard_start_utc = (
            services.timezone_service
            .local_to_utc(
                start_local,
                yard,
            )
        )

        yard_end_utc = (
            services.timezone_service
            .local_to_utc(
                end_local,
                yard,
            )
        )

        yard_start_utc = (
            _ensure_utc_datetime(
                yard_start_utc
            )
        )

        yard_end_utc = (
            _ensure_utc_datetime(
                yard_end_utc
            )
        )

        if yard_start_utc >= yard_end_utc:
            raise ValueError(
                "The converted UTC range is invalid for yard "
                f"{yard.yard_id}."
            )

        request = AnalysisRequest(
            yard_id=yard.yard_id,
            dates=local_dates,
            lookback_days=max(
                len(local_dates),
                1,
            ),
            include_container_logs_only=(
                payload.include_container_logs_only
            ),
            normalized_ocr_deduplication=(
                payload.normalized_ocr_deduplication
            ),
        )

        result = (
            services.analysis_service.analyze(
                request
            )
        )

        exact_transactions = [
            transaction
            for transaction
            in result.transactions
            if _transaction_is_in_utc_range(
                transaction,
                start_utc=yard_start_utc,
                end_utc=yard_end_utc,
            )
        ]

        dataframe_exporter = (
            DataFrameExporter()
        )

        dataframe = (
            dataframe_exporter
            .build_dataframe(
                exact_transactions
            )
        )

        if not dataframe.empty:
            sort_columns = [
                column
                for column in (
                    "Timestamp UTC",
                    "UUID",
                )
                if column
                in dataframe.columns
            ]

            if sort_columns:
                dataframe = (
                    dataframe
                    .sort_values(
                        by=sort_columns,
                        kind="stable",
                    )
                    .reset_index(
                        drop=True
                    )
                )

        csv_buffer = StringIO()

        dataframe.to_csv(
            csv_buffer,
            index=False,
        )

        csv_content = (
            csv_buffer.getvalue()
        )

        filename = (
            f"{yard.yard_id}_"
            f"{start_local.strftime('%Y%m%dT%H%M%S')}_"
            f"{end_local.strftime('%Y%m%dT%H%M%S')}.csv"
        )

        return StreamingResponse(
            iter(
                [csv_content]
            ),
            media_type=(
                "text/csv; charset=utf-8"
            ),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
                "X-Yard-ID": (
                    yard.yard_id
                ),
                "X-Exported-Row-Count": str(
                    len(dataframe)
                ),
                "X-Local-Start": (
                    start_local.isoformat()
                ),
                "X-Local-End": (
                    end_local.isoformat()
                ),
                "X-UTC-Start": (
                    yard_start_utc.isoformat()
                ),
                "X-UTC-End": (
                    yard_end_utc.isoformat()
                ),
            },
        )

    except YardNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except GroundTruthSelectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except TimezoneServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logging.exception(
            "CSV transaction export failed"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "CSV export failed. "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

# The existing date-based /analyze and /export/csv contracts are preserved.
class ProofAnalysisRequest(CSVExportRequest):
    """Exact yard-local scope; accepts no ticket or inferred camera/gate fields."""
    model_config = ConfigDict(extra='forbid')


@router.post('/analyze/proof', response_model=ProofResponse)
def analyze_transaction_proof(
    payload: ProofAnalysisRequest,
    services: ApplicationServices = Depends(get_application_services),
):
    return _analyze_transaction_proof(payload, services)


def _analyze_transaction_proof(payload, services, *, output_provenance=False):
    """Shared scope and acquisition for both explicit proof versions."""
    try:
        yard_id = _clean_yard_id(payload.yard_id)
        start = _ensure_naive_local_datetime(payload.start_local, field_name='start_local')
        end = _ensure_naive_local_datetime(payload.end_local, field_name='end_local')
        if start >= end or end - start > timedelta(days=90):
            raise ValueError('Local range must be increasing and no longer than 90 days.')
        services.timezone_service.refresh()
        yard = services.timezone_service.resolve_by_id(yard_id, refresh=False)
        start_utc = services.timezone_service.local_to_utc(start, yard)
        end_utc = services.timezone_service.local_to_utc(end, yard)
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError('Timezone service must return aware UTC boundaries.')
        start_utc, end_utc = _ensure_utc_datetime(start_utc), _ensure_utc_datetime(end_utc)
        if start_utc >= end_utc:
            raise ValueError('Converted UTC range must be increasing.')
        dates = _build_local_dates_for_range(start_local=start, end_local=end)
    except YardNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimezoneServiceError as exc:
        raise HTTPException(status_code=502, detail='Timezone resolution failed.') from exc

    scope = dict(yard_id=yard.yard_id, start_local=start.isoformat(), end_local=end.isoformat(),
        start_utc=start_utc.isoformat(), end_utc=end_utc.isoformat(), interval='[start,end)',
        iana_timezone=None, timezone_config=dict(name=yard.timezone_name,
            standard_utc_offset_hours=yard.standard_utc_offset_hours,
            dst_utc_offset_hours=yard.dst_utc_offset_hours,
            conversion='configured-offsets-north-american-dst'))
    request = AnalysisRequest(yard_id=yard.yard_id, dates=dates, lookback_days=max(len(dates), 1),
        include_container_logs_only=payload.include_container_logs_only,
        normalized_ocr_deduplication=payload.normalized_ocr_deduplication)
    try:
        result = services.analysis_service.analyze(request)
    except Exception:
        logging.exception('Unified proof analysis failed')
        # Do not disclose provider exception text (connection details/secrets).
        proof = build_proof_response(request_scope=scope, failed=True)
        if output_provenance:
            proof = extend_output_proof(proof, [])
        return JSONResponse(status_code=502, content=proof.model_dump(mode='json'))
    exact = [transaction for transaction in result.transactions
             if _transaction_is_in_utc_range(transaction, start_utc=start_utc, end_utc=end_utc)]
    proof = build_proof_response(request_scope=scope, result=result, transactions=exact)
    for candidate in proof.candidates:
        candidate.timestamps.local['timezone'] = None
        candidate.timestamps.local['timezone_config'] = scope['timezone_config']
    return extend_output_proof(proof, exact, analysis_complete=result.decision_analysis_complete) if output_provenance else proof


@router.post('/analyze/proof/v2', response_model=OutputProofResponse)
def analyze_transaction_output_proof(
    payload: ProofAnalysisRequest,
    services: ApplicationServices = Depends(get_application_services),
):
    """Opt-in retained field provenance; v1 and CSV semantics remain unchanged."""
    return _analyze_transaction_proof(payload, services, output_provenance=True)


@router.post("/analyze/ticket")
def analyze_ticket_transaction(
    payload: TicketAnalysisRequest,
    services: ApplicationServices = Depends(get_application_services),
) -> dict:
    """Locate by exact AIQ identity, then correlate using the source clock."""
    return build_ticket_analysis(services.analysis_service, payload)
