from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    registration_token: str = "change-me"
    secret_key: str = "change-me-random-secret"
    database_url: str = "sqlite:///data/docker_borg.db"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    agent_offline_seconds: int = 120

    model_config = {"env_prefix": "DBORG_"}


settings = Settings()
