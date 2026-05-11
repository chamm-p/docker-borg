from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    agent_name: str = ""
    central_url: str = "http://central:8080"
    registration_token: str = ""
    poll_interval: int = 30

    borg_repo: str = ""
    borg_passphrase: str = ""

    docker_socket: str = "/var/run/docker.sock"
    docker_host_dir: str = "/host/docker"

    root_file_globs: list[str] = [
        "docker-compose*.yml",
        "docker-compose*.yaml",
        "compose*.yml",
        "compose*.yaml",
        ".env",
        ".env.*",
        "Dockerfile",
        "Dockerfile.*",
        ".dockerignore",
        "*.conf",
        "*.toml",
        "*.ini",
    ]

    data_dir: Path = Path("/data")
    token_file: Path = Path("/data/agent_token")
    log_level: str = "INFO"

    model_config = {"env_prefix": "DBORG_"}


settings = Settings()
