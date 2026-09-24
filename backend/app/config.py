from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local layout: <project>/backend/app/config.py.
# Container layout: /app/app/config.py.  Both resolve to the folder that owns data.
APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent if (APP_ROOT.parent / ".env.example").exists() else APP_ROOT
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    youtube_api_key: str = ""
    requests_per_minute: int = 20
    worker_poll_seconds: float = 2
    cors_origins: str = "http://localhost:5173"
    max_job_attempts: int = 5
    request_max_attempts: int = 4  # 1 try + 3 retries for transient external errors
    # Worker liveness (independent of per-job leases): a worker is "running" while its last heartbeat is younger than stale.
    worker_heartbeat_seconds: float = 10
    worker_stale_seconds: float = 45
    worker_record_retention_days: int = 7
    db_backup_dir: Path = Path(".runtime/backups")
    db_backup_keep_count: int = 10
    runtime_log_retention_days: int = 14
    job_history_retention_days: int = 30  # terminal jobs / sync runs older than this are deleted
    job_log_retention_days: int = 14
    maintenance_interval_hours: float = 6
    scan_failure_retry_minutes: int = 30  # a scheduled channel whose scan failed for good waits this long before the next auto scan
    sync_safety_window: int = 20  # known videos required after the cursor before an incremental scan may stop
    request_timeout_seconds: float = 30  # per-operation timeout for every outbound HTTP call
    job_lease_seconds: int = 300  # a worker that stops heartbeating loses its job after this
    job_max_runtime_seconds: int = 10800  # heartbeat stops renewing the lease after this, so a hung job is always reclaimable

    @model_validator(mode="after")
    def _sane_timing(self):
        # Heartbeat fires every lease/3, so a lease this long tolerates two missed beats
        # (e.g. SQLite busy_timeout of 5s) without another worker reclaiming a live job.
        if self.job_lease_seconds < 30:
            raise ValueError("JOB_LEASE_SECONDS phải >= 30")
        if self.job_max_runtime_seconds <= self.job_lease_seconds:
            raise ValueError("JOB_MAX_RUNTIME_SECONDS phải lớn hơn JOB_LEASE_SECONDS")
        if self.worker_heartbeat_seconds <= 0 or self.worker_stale_seconds < 2 * self.worker_heartbeat_seconds:
            raise ValueError("WORKER_STALE_SECONDS phải >= 2 x WORKER_HEARTBEAT_SECONDS (chịu được một nhịp bị trượt)")
        if (
            min(
                self.db_backup_keep_count,
                self.runtime_log_retention_days,
                self.job_history_retention_days,
                self.job_log_retention_days,
                self.worker_record_retention_days,
                self.scan_failure_retry_minutes,
            )
            < 1
            or self.maintenance_interval_hours <= 0
        ):
            raise ValueError("Các cấu hình retention/backup phải là số dương")
        if self.sync_safety_window < 1:
            raise ValueError("SYNC_SAFETY_WINDOW phải >= 1")
        if self.request_timeout_seconds <= 0 or self.request_max_attempts < 1:
            raise ValueError("REQUEST_TIMEOUT_SECONDS/REQUEST_MAX_ATTEMPTS không hợp lệ")
        return self


settings = Settings()


def _project_path(value: Path) -> Path:
    return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()


def _normalise_database_url(value: str) -> str:
    prefix = "sqlite:///"
    if not value.startswith(prefix):
        return value
    database_path = Path(value.removeprefix(prefix))
    if not database_path.is_absolute():
        database_path = (PROJECT_ROOT / database_path).resolve()
    return f"{prefix}{database_path.as_posix()}"


# Paths in .env may stay portable (for example ./data/app.db).  Convert them only
# after the project root is known, never to a machine-specific path in .env.
settings.data_dir = _project_path(settings.data_dir)
settings.db_backup_dir = _project_path(settings.db_backup_dir)
RUNTIME_LOG_DIR = PROJECT_ROOT / ".runtime" / "logs"
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.database_url = _normalise_database_url(settings.database_url)


PLACEHOLDER_KEYS = {"", "replace_me", "your_google_youtube_data_api_key"}


def read_legacy_youtube_api_key() -> str:
    """Key from a pre-database install (.env file, else process environment).

    Only used to seed the shared settings table; see services/runtime_settings.py.
    """
    candidates = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "YOUTUBE_API_KEY":
                candidates.append(value.strip().strip('"').strip("'"))
    candidates.append(settings.youtube_api_key.strip())
    return next((key for key in candidates if key.lower() not in PLACEHOLDER_KEYS), "")
