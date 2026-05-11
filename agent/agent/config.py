from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    agent_name: str = ""
    central_url: str = "http://central:8080"
    registration_token: str = ""
    poll_interval: int = 30

    backup_type: str = "scp"
    borg_repo: str = ""
    borg_passphrase: str = ""

    scp_host: str = ""
    scp_user: str = ""
    scp_path: str = ""
    scp_port: int = 22

    local_path: str = ""

    webdav_url: str = ""
    webdav_user: str = ""
    webdav_password: str = ""
    webdav_verify_ssl: bool = True
    webdav_mount: str = "/mnt/webdav"

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
