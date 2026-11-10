"""Escritor de lecturas: hilo dedicado con conexión propia. Agrupa en lotes (commit cada ~200 ms) y reintenta si la BD está ocupada."""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path

from .. import db, repo
from .base import ReadEvent

log = logging.getLogger("pacer_time.capture")


class ReadWriter:
    def __init__(self, db_path: str | Path, *, flush_interval: float = 0.2, max_batch: int = 1000, stop_grace: float = 10.0):
        self.db_path = str(db_path)
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self.stop_grace = stop_grace
        self._q: queue.Queue[ReadEvent] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="read-writer", daemon=True)
        self._pending: list[ReadEvent] = []
        self._lock = threading.Lock()
        self.submitted = 0
        self.written = 0
        self.last_read_ms: int | None = None
        self.last_error: str = ""

    def start(self) -> None:
        self._thread.start()

    def submit(self, ev: ReadEvent) -> None:
        self._q.put(ev)
        with self._lock:
            self.submitted += 1

    def stop(self, timeout: float = 15.0) -> bool:
        """Vacía la cola a disco y detiene el hilo. True si no quedó nada pendiente."""
        self._stop.set()
        self._thread.join(timeout)
        return not self._thread.is_alive() and not self._pending and self._q.empty()

    def _drain(self) -> None:
        deadline = time.monotonic() + self.flush_interval
        while len(self._pending) < self.max_batch:
            remaining = deadline - time.monotonic()
            try:
                self._pending.append(self._q.get(timeout=max(remaining, 0.001)))
            except queue.Empty:
                break
            if remaining <= 0:
                break

    def _flush(self, conn: sqlite3.Connection) -> bool:
        if not self._pending:
            return True
        batch = self._pending
        try:
            repo.insert_reads(conn, [e.as_row() for e in batch])
        except sqlite3.Error as exc:  # BD ocupada, disco, etc.: se conserva el lote y se reintenta
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("No se pudo escribir un lote de %d lecturas: %s", len(batch), self.last_error)
            return False
        with self._lock:
            self.written += len(batch)
        self.last_read_ms = max(e.ts_ms for e in batch)
        self._pending = []
        self.last_error = ""
        return True

    def _loop(self) -> None:
        conn = db.connect(self.db_path)
        backoff = 0.2
        stop_seen_at: float | None = None
        try:
            while not self._stop.is_set() or not self._q.empty() or self._pending:
                self._drain()
                if self._flush(conn):
                    backoff = 0.2
                    continue
                if self._stop.is_set():
                    stop_seen_at = stop_seen_at or time.monotonic()
                    if time.monotonic() - stop_seen_at > self.stop_grace:
                        log.error("Se pierden %d lecturas: la BD no aceptó la escritura final (%s)", len(self._pending), self.last_error)
                        break
                time.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
        finally:
            conn.close()
