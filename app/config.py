"""
Runtime settings and filesystem layout, read once from the environment.

Every module imports its paths and limits from here, so `.env` is loaded
exactly once and the directory layout is defined in a single place.
"""

import logging
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env"))

# Where uploads/, backups/ and data/ live. Defaults to the project folder;
# set ENG2SQL_HOME to keep runtime data elsewhere (another volume, a sandbox).
HOME_DIR = os.path.abspath(os.getenv("ENG2SQL_HOME") or BASE_DIR)

STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(HOME_DIR, "uploads")
BACKUP_DIR = os.path.join(HOME_DIR, "backups")
DATA_DIR = os.path.join(HOME_DIR, "data")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
VECTORSTORE_DIR = os.path.join(DATA_DIR, "vectorstore")

for _directory in (UPLOAD_DIR, BACKUP_DIR, DATA_DIR, MEMORY_DIR, VECTORSTORE_DIR):
    os.makedirs(_directory, exist_ok=True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


MAX_UPLOAD_MB = _int_env("MAX_UPLOAD_MB", 50)
MAX_SQL_RESULT_ROWS = _int_env("MAX_SQL_RESULT_ROWS", 5000)
MAX_EXPORT_ROWS = _int_env("MAX_EXPORT_ROWS", 50000)
SQL_TIMEOUT_MS = _int_env("SQL_TIMEOUT_MS", 5000)

# Backups kept per database; the oldest are removed beyond this. 0 keeps all.
MAX_BACKUPS_PER_DB = _int_env("MAX_BACKUPS_PER_DB", 25)

# Accounts. The first account can always be created; set ALLOW_SIGNUP=false
# afterwards to stop anyone else registering.
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "true").strip().lower() not in ("0", "false", "no", "off")
SESSION_DAYS = _int_env("SESSION_DAYS", 14)

# Comma-separated list of origins allowed to call the API cross-origin.
# Empty (the default) means same-origin only, which is all the bundled UI needs.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="[%(levelname)s] %(name)s: %(message)s",
    )
