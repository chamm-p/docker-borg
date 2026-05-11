from pathlib import Path

_version_file = Path(__file__).parent / "VERSION"
APP_VERSION = _version_file.read_text().strip() if _version_file.exists() else "dev"
