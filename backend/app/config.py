from pathlib import Path
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
    job_lease_seconds: int = 300  # a worker that stops heartbeating loses its job after this


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
