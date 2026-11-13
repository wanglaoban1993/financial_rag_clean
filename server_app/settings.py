from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# docker-compose injects .env into the container automatically; running this
# module directly (e.g. `python query.py --agent` from a plain shell) does
# not, so POSTGRES_PASSWORD etc. are silently empty and psycopg fails with
# "fe_sendauth: no password supplied". override=False so real env vars
# (docker-compose, an already-exported shell var) still win over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


@dataclass(frozen=True)
class DbSettings:
    host: str = os.getenv("POSTGRES_HOST") or os.getenv("POSTGRES_HOST_LOCAL") or "127.0.0.1"
    port: int = int(os.getenv("POSTGRES_PORT_INTERNAL") or os.getenv("POSTGRES_PORT") or "5432")
    dbname: str = os.getenv("POSTGRES_DB", "financial_rag")
    user: str = os.getenv("POSTGRES_USER", "financial_user")
    password: str = os.getenv("POSTGRES_PASSWORD", "")

    @property
    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def db_settings() -> DbSettings:
    return DbSettings()
