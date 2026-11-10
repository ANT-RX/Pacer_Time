"""Proceso de captura: conecta un Reader con el ReadWriter y publica su estado (latido) en la BD.

Corre en un proceso APARTE de la interfaz: si la UI se cierra o se cuelga, la captura sigue.
La UI solo lee `reader_status` y pide detener marcando `stop_requested`.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from pathlib import Path

from .. import db
from ..timeutil import now_ms
from .base import Reader, ReadEvent
from .writer import ReadWriter

log = logging.getLogger("pacer_time.capture")

HEARTBEAT_STALE_MS = 5000


def _set_status(conn, source: str, race_id: int | None, **fields) -> None:
    cols = ["source", "race_id", "heartbeat_ms", "pid"] + list(fields)
    vals = [source, race_id, now_ms(), os.getpid()] + list(fields.values())
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
    conn.execute(
        f"INSERT INTO reader_status({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT(source) DO UPDATE SET {updates}",
        vals,
    )


def run_capture(
    db_path: str | Path,
    race_id: int,
    reader: Reader,
    source: str,
    stop: threading.Event | None = None,
    heartbeat_s: float = 1.0,
) -> str:
    """Bloquea hasta que el lector termine, falle o se pida detener. Devuelve el estado final."""
    stop = stop or threading.Event()
    conn = db.connect(db_path)
    writer = ReadWriter(db_path)
    writer.start()
    result = {"error": ""}

    def emit(ev: ReadEvent) -> None:
        writer.submit(ev)

    def target() -> None:
        try:
            reader.run(emit, stop)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log.error("El lector falló:\n%s", traceback.format_exc())

    thread = threading.Thread(target=target, name=f"reader-{source}", daemon=True)
    _set_status(conn, source, race_id, state="running", stop_requested=0, total_reads=0, detail="")
    thread.start()
    final = "stopped"
    try:
        while True:
            thread.join(heartbeat_s)
            row = conn.execute("SELECT stop_requested FROM reader_status WHERE source=?", (source,)).fetchone()
            stop_requested = bool(row and row["stop_requested"])
            detail = writer.last_error or ""
            _set_status(
                conn, source, race_id, state="running", total_reads=writer.written, last_read_ms=writer.last_read_ms, detail=detail
            )
            if stop_requested:
                stop.set()
            if not thread.is_alive():
                final = "error" if result["error"] else ("stopped" if stop.is_set() else "finished")
                break
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        thread.join(5)
        flushed = writer.stop()
        detail = result["error"] or (writer.last_error if not flushed else "")
        _set_status(
            conn, source, race_id, state=final, total_reads=writer.written, last_read_ms=writer.last_read_ms, detail=detail
        )
        conn.close()
    return final
