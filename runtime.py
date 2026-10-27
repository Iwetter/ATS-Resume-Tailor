"""Load this installation's local configuration before choosing storage paths."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def load_environment(path=None):
    # An explicit file prevents an unrelated parent-directory .env from being
    # picked up. Exported environment variables (including Docker) take priority.
    return load_dotenv(dotenv_path=Path(path) if path is not None else ROOT / ".env",
                       override=False, encoding="utf-8-sig")
