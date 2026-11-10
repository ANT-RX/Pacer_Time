"""Lanzar/detener procesos de captura desde la UI (sin Qt) y consultar su estado."""

from __future__ import annotations

import subprocess
import sys

from .. import db
from ..timeutil import now_ms
from .runner import HEARTBEAT_STALE_MS


def start_capture_process(db_path: str, race_id: int, kind: str, source: str, extra_args: list[str] | None = None) -> subprocess.Popen:
    """Arranca `python -m pacer_time.capture.cli` desacoplado de la UI (sobrevive si la UI se cierra)."""
    cmd = [sys.executable, "-m", "pacer_time.capture.cli", "--db", str(db_path), "--race", str(race_id), "--source", kind, "--name", source]
    cmd += extra_args or []
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def request_stop(conn, source: str) -> None:
    conn.execute("UPDATE reader_status SET stop_requested=1 WHERE source=?", (source,))


def list_status(conn, race_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM reader_status" + (" WHERE race_id=?" if race_id is not None else "") + " ORDER BY source"
    rows = conn.execute(q, (race_id,) if race_id is not None else ()).fetchall()
    out = []
    t = now_ms()
    for r in rows:
        d = dict(r)
        d["alive"] = d["state"] == "running" and (t - d["heartbeat_ms"]) <= HEARTBEAT_STALE_MS
        d["stale"] = d["state"] == "running" and not d["alive"]  # dice 'running' pero sin latido: se cayó
        out.append(d)
    return out


def clear_status(conn, source: str) -> None:
    conn.execute("DELETE FROM reader_status WHERE source=?", (source,))
