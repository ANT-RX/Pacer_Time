"""Conexión SQLite (WAL, durable), migraciones y respaldo."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .schema import MIGRATIONS, SCHEMA_VERSION


class DatabaseError(Exception):
    pass


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Abre una conexión. Modo autocommit: usar `tx(conn)` para agrupar escrituras.

    - WAL: lectores concurrentes mientras otro proceso escribe.
    - synchronous=FULL: un commit confirmado sobrevive a un corte de luz.
    - busy_timeout: espera (no falla) si otro proceso está escribiendo.
    """
    path = str(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    else:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transacción atómica (BEGIN IMMEDIATE). Anidable mediante SAVEPOINT."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT nested")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO nested")
            conn.execute("RELEASE nested")
            raise
        else:
            conn.execute("RELEASE nested")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise DatabaseError(
            f"La base de datos es de una versión más nueva ({current}) que esta aplicación ({SCHEMA_VERSION}). "
            "Actualice Pacer Time."
        )
    for version in range(current, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_script(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return schema_version(conn)


def _split_script(script: str) -> list[str]:
    """Separa un script en sentencias respetando los cuerpos BEGIN ... END de los triggers."""
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        candidate = "\n".join(buf)
        if stripped.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements


def create_database(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        raise DatabaseError(f"Ya existe un archivo en {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn)
    return conn


def open_database(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"No existe {path}")
    conn = connect(path)
    try:
        migrate(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DatabaseError(f"{path} no es una base de datos válida de Pacer Time: {exc}") from exc
    return conn


def backup_database(conn: sqlite3.Connection, dest: str | Path) -> Path:
    """Copia consistente en caliente (API de backup de SQLite)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def backup_to_dir(conn: sqlite3.Connection, directory: str | Path, prefix: str = "evento", keep: int = 20) -> Path:
    """Respaldo con marca de tiempo; conserva solo los últimos `keep`."""
    directory = Path(directory)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    dest = backup_database(conn, directory / f"{prefix}-{stamp}.db")
    olds = sorted(directory.glob(f"{prefix}-*.db"))
    for old in olds[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dest
