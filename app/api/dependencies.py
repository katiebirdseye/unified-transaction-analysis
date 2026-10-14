from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.archive.gdrive_archive_parser import (
    GDriveArchiveParser,
)
from app.config import settings
from app.matching.transaction_matcher import (
    TransactionMatcher,
    TransactionMatcherConfig,
)
from app.providers.aiq_database_provider import (
    AIQDatabaseProvider,
)
from app.providers.archive_provider import (
    ArchiveProvider,
)
from app.services.analysis_service import (
    AnalysisService,
)
from app.services.ground_truth_selector import (
    GroundTruthSelector,
)
from app.services.timezone_service import (
    TimezoneService,
)
from app.exporters.google_sheets_exporter import (
    GoogleSheetsExporter,
)


@dataclass(frozen=True)
class ApplicationServices:
    """
    Shared application services used by API routes.
    """

    timezone_service: TimezoneService
    analysis_service: AnalysisService
    google_sheets_exporter: GoogleSheetsExporter


@lru_cache(maxsize=1)
def get_application_services() -> ApplicationServices:
    """
    Construct and cache the unified analysis dependencies.

    AIQ data is queried directly from PostgreSQL during analysis requests.
    Caching here avoids rebuilding clients and parsers unnecessarily.
    """

    timezone_service = TimezoneService(
        spreadsheet_id=(
            settings.timezone_spreadsheet_id
        ),
        credentials_file=(
            settings.google_credentials_file
        ),
        environment=(
            settings.runtime_environment
        ),
        worksheet_name=(
            settings.resolved_timezone_worksheet_name
        ),
        lookback_days=(
            settings.analysis_lookback_days
        ),
    )

    # Load timezone mappings during application initialization.
    timezone_service.refresh()

    aiq_provider = AIQDatabaseProvider(
        host=settings.aiq_db_host,
        port=settings.aiq_db_port,
        database=settings.aiq_db_name,
        user=settings.aiq_db_user,
        password=settings.aiq_db_password,
        connect_timeout_seconds=(
            settings.aiq_db_connect_timeout_seconds
        ),
        utc_to_local=(
            timezone_service.utc_to_local
        ),
    )

    archive_parser = GDriveArchiveParser(
        service_account_key=(
            settings.google_credentials_file
        ),
        live_archives_id=(
            settings.live_archives_id
        ),
        live_archives_ss=(
            settings.live_archives_worksheet
        ),
    )

    archive_provider = ArchiveProvider(
        archive_parser=archive_parser,
        local_to_utc=(
            timezone_service.local_to_utc
        ),
    )

    ground_truth_selector = GroundTruthSelector(
        aiq_provider=aiq_provider,
        archive_provider=archive_provider,
    )

    transaction_matcher = TransactionMatcher(
        config=TransactionMatcherConfig(
            archive_time_threshold_seconds=(
                settings.archive_time_threshold_seconds
            ),
            require_direction_for_archive=(
                settings.require_direction_for_archive
            ),
            prefer_gate_match=(
                settings.prefer_gate_match
            ),
            include_container_logs_only=(
                settings.include_container_logs_only
            ),
        )
    )

    analysis_service = AnalysisService(
        ground_truth_selector=(
            ground_truth_selector
        ),
        transaction_matcher=(
            transaction_matcher
        ),
        container_logs_dir=(
            settings.container_logs_dir
        ),
        local_to_utc=(
            timezone_service.local_to_utc
        ),
        utc_to_local=(
            timezone_service.utc_to_local
        ),
    )

    google_sheets_exporter = GoogleSheetsExporter(
        credentials_file=(
            settings.google_credentials_file
        ),
        spreadsheet_id=(
            settings.google_spreadsheet_id
        ),
        worksheet_name=(
            settings.google_worksheet_name
        ),
    )

    return ApplicationServices(
        timezone_service=timezone_service,
        analysis_service=analysis_service,
        google_sheets_exporter=(
            google_sheets_exporter
        ),
    )
