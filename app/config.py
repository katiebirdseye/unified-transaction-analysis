from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Load environment variables from the project-level .env file.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """
    Application configuration loaded from environment variables.

    Secrets such as database passwords and service-account paths should
    be stored in .env and should never be committed to Git.
    """

    # =========================================================
    # Runtime environment
    # =========================================================

    runtime_environment: str

    # =========================================================
    # AIQ PostgreSQL database
    # =========================================================

    aiq_db_host: str
    aiq_db_port: int
    aiq_db_name: str
    aiq_db_user: str
    aiq_db_password: str
    aiq_db_connect_timeout_seconds: int

    # =========================================================
    # Container logs
    # =========================================================

    container_logs_dir: str

    # =========================================================
    # Google service account
    # =========================================================

    google_credentials_file: str

    # =========================================================
    # Google Drive / Archive source
    # =========================================================

    live_archives_id: str
    live_archives_worksheet: str

    # =========================================================
    # Dynamic timezone source
    # =========================================================

    timezone_spreadsheet_id: str
    timezone_worksheet_name: str
    analysis_lookback_days: int

    # =========================================================
    # Google Sheets output
    # =========================================================

    google_spreadsheet_id: str
    google_worksheet_name: str

    # =========================================================
    # Transaction matching
    # =========================================================

    archive_time_threshold_seconds: int
    require_direction_for_archive: bool
    prefer_gate_match: bool
    include_container_logs_only: bool

    # =========================================================
    # FastAPI
    # =========================================================

    api_host: str
    api_port: int
    api_reload: bool

    fenix_url: str
    fenix_api_token: str

    aiq_spreadsheet_id: str
    aiq_worksheet_name: str
    aiq_database_name: str

    # =========================================================
    # Construction
    # =========================================================

    @classmethod
    def from_environment(cls) -> "Settings":
        """
        Create a Settings instance from environment variables.
        """

        return cls(
            # -------------------------------------------------
            # Runtime environment
            # -------------------------------------------------
            runtime_environment=cls._normalize_environment(
                os.getenv(
                    "RUNTIME_ENVIRONMENT",
                    "production",
                )
            ),

            # -------------------------------------------------
            # AIQ database
            # -------------------------------------------------
            aiq_db_host=os.getenv(
                "AIQ_DB_HOST",
                "",
            ).strip(),
            aiq_db_port=cls._get_int(
                "AIQ_DB_PORT",
                default=5432,
            ),
            aiq_db_name=os.getenv(
                "AIQ_DB_NAME",
                "",
            ).strip(),
            aiq_db_user=os.getenv(
                "AIQ_DB_USER",
                "",
            ).strip(),
            aiq_db_password=os.getenv(
                "AIQ_DB_PASSWORD",
                "",
            ),
            aiq_db_connect_timeout_seconds=cls._get_int(
                "AIQ_DB_CONNECT_TIMEOUT_SECONDS",
                default=10,
            ),

            # -------------------------------------------------
            # Container logs
            # -------------------------------------------------
            container_logs_dir=os.getenv(
                "CONTAINER_LOGS_DIR",
                "/central_logs",
            ).strip(),

            # -------------------------------------------------
            # Google service account
            # -------------------------------------------------
            google_credentials_file=os.getenv(
                "GOOGLE_CREDENTIALS_FILE",
                "",
            ).strip(),

            # -------------------------------------------------
            # Archive source
            # -------------------------------------------------
            live_archives_id=os.getenv(
                "LIVE_ARCHIVES_ID",
                "",
            ).strip(),
            live_archives_worksheet=os.getenv(
                "LIVE_ARCHIVES_WORKSHEET",
                "",
            ).strip(),

            # -------------------------------------------------
            # Dynamic timezone source
            # -------------------------------------------------
            timezone_spreadsheet_id=os.getenv(
                "TIMEZONE_SPREADSHEET_ID",
                "",
            ).strip(),
            timezone_worksheet_name=os.getenv(
                "TIMEZONE_WORKSHEET_NAME",
                "",
            ).strip(),
            analysis_lookback_days=cls._get_int(
                "ANALYSIS_LOOKBACK_DAYS",
                default=10,
            ),

            # -------------------------------------------------
            # Google Sheets output
            # -------------------------------------------------
            google_spreadsheet_id=os.getenv(
                "GOOGLE_SPREADSHEET_ID",
                "",
            ).strip(),
            google_worksheet_name=os.getenv(
                "GOOGLE_WORKSHEET_NAME",
                "Unified Transactions",
            ).strip(),

            # -------------------------------------------------
            # Matching
            # -------------------------------------------------
            archive_time_threshold_seconds=cls._get_int(
                "ARCHIVE_TIME_THRESHOLD_SECONDS",
                default=300,
            ),
            require_direction_for_archive=cls._get_bool(
                "REQUIRE_DIRECTION_FOR_ARCHIVE",
                default=True,
            ),
            prefer_gate_match=cls._get_bool(
                "PREFER_GATE_MATCH",
                default=True,
            ),
            include_container_logs_only=cls._get_bool(
                "INCLUDE_CONTAINER_LOGS_ONLY",
                default=True,
            ),

            # -------------------------------------------------
            # FastAPI
            # -------------------------------------------------
            api_host=os.getenv(
                "API_HOST",
                "0.0.0.0",
            ).strip(),
            api_port=cls._get_int(
                "API_PORT",
                default=8012,
            ),
            api_reload=cls._get_bool(
                "API_RELOAD",
                default=False,
            ),

            # -------------------------------------------------
            # Fenix AIQ API
            # -------------------------------------------------
            fenix_url=os.getenv(
                "FENIX_URL",
                "https://fenix.birdseyesecurity.com",
            ).strip().rstrip("/"),

            fenix_api_token=os.getenv(
                "FENIX_API_TOKEN",
                "",
            ).strip(),

            aiq_spreadsheet_id=os.getenv(
                "AIQ_SPREADSHEET_ID",
                "",
            ).strip(),

            aiq_worksheet_name=os.getenv(
                "AIQ_WORKSHEET_NAME",
                "AIQ Raw",
            ).strip(),

            aiq_database_name=os.getenv(
                "AIQ_DATABASE_NAME",
                "aiq",
            ).strip(),
        )

    # =========================================================
    # Validation
    # =========================================================

    def validate_aiq(self) -> None:
        """
        Validate configuration required for direct AIQ PostgreSQL access.
        """

        missing: list[str] = []

        if not self.aiq_db_host:
            missing.append(
                "AIQ_DB_HOST"
            )

        if not self.aiq_db_name:
            missing.append(
                "AIQ_DB_NAME"
            )

        if not self.aiq_db_user:
            missing.append(
                "AIQ_DB_USER"
            )

        if not self.aiq_db_password:
            missing.append(
                "AIQ_DB_PASSWORD"
            )

        if not 1 <= self.aiq_db_port <= 65535:
            raise RuntimeError(
                "AIQ_DB_PORT must be between 1 and 65535"
            )

        if self.aiq_db_connect_timeout_seconds <= 0:
            raise RuntimeError(
                "AIQ_DB_CONNECT_TIMEOUT_SECONDS "
                "must be greater than zero"
            )

        if missing:
            raise RuntimeError(
                "Missing AIQ PostgreSQL configuration: "
                + ", ".join(missing)
            )

    def validate_archive(self) -> None:
        """
        Validate configuration required for Archive access.
        """

        missing: list[str] = []

        if not self.google_credentials_file:
            missing.append(
                "GOOGLE_CREDENTIALS_FILE"
            )

        if not self.live_archives_id:
            missing.append(
                "LIVE_ARCHIVES_ID"
            )

        if not self.live_archives_worksheet:
            missing.append(
                "LIVE_ARCHIVES_WORKSHEET"
            )

        if missing:
            raise RuntimeError(
                "Missing Archive configuration: "
                + ", ".join(missing)
            )

        self._validate_credentials_file()

    def validate_timezone_service(self) -> None:
        """
        Validate configuration required for dynamic timezone loading.
        """

        missing: list[str] = []

        if not self.google_credentials_file:
            missing.append(
                "GOOGLE_CREDENTIALS_FILE"
            )

        if not self.timezone_spreadsheet_id:
            missing.append(
                "TIMEZONE_SPREADSHEET_ID"
            )

        if missing:
            raise RuntimeError(
                "Missing timezone configuration: "
                + ", ".join(missing)
            )

        self._validate_credentials_file()

        if self.analysis_lookback_days <= 0:
            raise RuntimeError(
                "ANALYSIS_LOOKBACK_DAYS must be greater than zero"
            )

    def validate_google_sheets(self) -> None:
        """
        Validate configuration required for Google Sheets output.
        """

        missing: list[str] = []

        if not self.google_credentials_file:
            missing.append(
                "GOOGLE_CREDENTIALS_FILE"
            )

        if not self.google_spreadsheet_id:
            missing.append(
                "GOOGLE_SPREADSHEET_ID"
            )

        if not self.google_worksheet_name:
            missing.append(
                "GOOGLE_WORKSHEET_NAME"
            )

        if missing:
            raise RuntimeError(
                "Missing Google Sheets output configuration: "
                + ", ".join(missing)
            )

        self._validate_credentials_file()

    def validate_container_logs(self) -> None:
        """
        Validate the container-log root directory.
        """

        if not self.container_logs_dir:
            raise RuntimeError(
                "CONTAINER_LOGS_DIR cannot be empty"
            )

        path = self.container_logs_path

        if not path.exists():
            raise RuntimeError(
                "Container logs directory does not exist: "
                f"{path}"
            )

        if not path.is_dir():
            raise RuntimeError(
                "Container logs path is not a directory: "
                f"{path}"
            )

    def validate_matching(self) -> None:
        """
        Validate transaction-matching settings.
        """

        if self.archive_time_threshold_seconds <= 0:
            raise RuntimeError(
                "ARCHIVE_TIME_THRESHOLD_SECONDS must be greater than zero"
            )

    def validate_api(self) -> None:
        """
        Validate FastAPI server configuration.
        """

        if not self.api_host:
            raise RuntimeError(
                "API_HOST cannot be empty"
            )

        if not 1 <= self.api_port <= 65535:
            raise RuntimeError(
                "API_PORT must be between 1 and 65535"
            )

    def validate_analysis_dependencies(self) -> None:
        """
        Validate integrations needed to run an analysis.

        Google Sheets output is not validated here because exporting is
        optional for an analysis request.
        """

        self.validate_aiq()
        self.validate_archive()
        self.validate_timezone_service()
        self.validate_container_logs()
        self.validate_matching()

    def validate_all(self) -> None:
        """
        Validate every configured application integration.
        """

        self.validate_analysis_dependencies()
        self.validate_google_sheets()
        self.validate_api()

    # =========================================================
    # Convenience properties
    # =========================================================

    @property
    def google_credentials_path(self) -> Path:
        return Path(
            self.google_credentials_file
        ).expanduser()

    @property
    def container_logs_path(self) -> Path:
        return Path(
            self.container_logs_dir
        ).expanduser()

    @property
    def resolved_timezone_worksheet_name(self) -> str:
        """
        Return an explicitly configured timezone worksheet or choose one
        automatically from RUNTIME_ENVIRONMENT.
        """

        if self.timezone_worksheet_name:
            return self.timezone_worksheet_name

        worksheet_by_environment = {
            "production": "Timezones",
            "staging": "Timezones Staging",
            "develop": "Timezones Develop",
        }

        return worksheet_by_environment[
            self.runtime_environment
        ]

    # =========================================================
    # Internal validation helpers
    # =========================================================

    def _validate_credentials_file(self) -> None:
        credentials_path = (
            self.google_credentials_path
        )

        if not credentials_path.exists():
            raise RuntimeError(
                "Google credentials file does not exist: "
                f"{credentials_path}"
            )

        if not credentials_path.is_file():
            raise RuntimeError(
                "Google credentials path is not a file: "
                f"{credentials_path}"
            )

    # =========================================================
    # Environment-value parsers
    # =========================================================

    @staticmethod
    def _get_int(
        variable_name: str,
        *,
        default: int,
    ) -> int:
        raw_value = os.getenv(
            variable_name,
            str(default),
        ).strip()

        try:
            return int(raw_value)
        except ValueError as exc:
            raise RuntimeError(
                f"{variable_name} must be an integer, "
                f"received: {raw_value!r}"
            ) from exc

    @staticmethod
    def _get_bool(
        variable_name: str,
        *,
        default: bool,
    ) -> bool:
        default_text = (
            "true"
            if default
            else "false"
        )

        raw_value = os.getenv(
            variable_name,
            default_text,
        ).strip().lower()

        true_values = {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

        false_values = {
            "0",
            "false",
            "no",
            "n",
            "off",
        }

        if raw_value in true_values:
            return True

        if raw_value in false_values:
            return False

        raise RuntimeError(
            f"{variable_name} must be a boolean value, "
            f"received: {raw_value!r}"
        )

    @staticmethod
    def _normalize_environment(
        value: str,
    ) -> str:
        normalized = str(
            value or "production"
        ).strip().lower()

        aliases = {
            "prod": "production",
            "production": "production",
            "main": "production",

            "stage": "staging",
            "staging": "staging",

            "dev": "develop",
            "development": "develop",
            "develop": "develop",
        }

        resolved = aliases.get(
            normalized
        )

        if resolved is None:
            raise RuntimeError(
                "RUNTIME_ENVIRONMENT must be one of: "
                "production, staging, develop. "
                f"Received: {value!r}"
            )

        return resolved


settings = Settings.from_environment()
