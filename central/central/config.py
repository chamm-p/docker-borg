from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    registration_token: str = "change-me"
    secret_key: str = "change-me-random-secret"
    database_url: str = "sqlite:///data/docker_borg.db"
    data_dir: Path = Path("data")
    admin_password: str = "change-me"
    log_level: str = "INFO"
    agent_offline_seconds: int = 120

    # E-Mail-Benachrichtigung (alles via .env; leer = deaktiviert)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""            # leer = smtp_user
    smtp_to: str = ""              # Empfänger (mehrere: kommagetrennt)
    smtp_tls: bool = True          # STARTTLS (Port 587)
    smtp_ssl: bool = False         # implizites TLS (Port 465)
    notify: str = "failure"        # failure | always | off

    model_config = {"env_prefix": "DBORG_"}


settings = Settings()
