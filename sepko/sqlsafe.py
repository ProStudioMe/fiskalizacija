"""SQL identifikatori — samo allowlist, nikad f-string sa korisničkim ulazom."""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TYPE = re.compile(r"^(VARCHAR\(\d+\)|INTEGER|NUMERIC\(\d+,\d+\)|TEXT|DATE|BOOLEAN)$", re.I)


def safe_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def add_column_if_missing(conn: Connection, table: str, column: str, coltype: str) -> None:
    table_s = safe_ident(table)
    column_s = safe_ident(column)
    if not _TYPE.match(coltype.strip()):
        raise ValueError(f"unsafe SQL type: {coltype!r}")
    conn.execute(text(f"ALTER TABLE {table_s} ADD COLUMN {column_s} {coltype}"))
