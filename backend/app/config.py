from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    youtube_api_key: str = ""
    requests_per_minute: int = 20
    worker_poll_seconds: float = 2
    cors_origins: str = "http://localhost:5173"
    max_job_attempts: int = 5


settings = Settings()
