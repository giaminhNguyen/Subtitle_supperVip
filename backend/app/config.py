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


def get_youtube_api_key() -> str:
    """Read the key from disk so a running worker sees web/CLI updates."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "YOUTUBE_API_KEY":
                key = value.strip().strip('"').strip("'")
                return "" if key.lower() in {"", "replace_me", "your_google_youtube_data_api_key"} else key
    return settings.youtube_api_key.strip()


def youtube_api_key_configured() -> bool:
    return bool(get_youtube_api_key())


def set_youtube_api_key(api_key: str) -> None:
    """Persist a key without exposing it through the API response or logs."""
    key = api_key.strip()
    if len(key) < 10 or any(character in key for character in "\r\n"):
        raise ValueError("API key không hợp lệ")

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    replacement = f"YOUTUBE_API_KEY={key}"
    for index, line in enumerate(lines):
        name, separator, _ = line.partition("=")
        if separator and name.strip() == "YOUTUBE_API_KEY":
            lines[index] = replacement
            break
    else:
        lines.append(replacement)

    temporary_file = ENV_FILE.with_name(".env.tmp")
    temporary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary_file.replace(ENV_FILE)
    settings.youtube_api_key = key
