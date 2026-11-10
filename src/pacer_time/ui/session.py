"""Estado compartido de la UI: conexión, carrera activa, operador y cálculo de resultados en segundo plano."""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from .. import db, repo
from ..models import Computation, Race


class _ComputeSignals(QObject):
    done = Signal(object, int)  # Computation, race_id
    failed = Signal(str)


class _ComputeTask(QRunnable):
    """Calcula resultados en un hilo del pool con conexión propia: la UI nunca se bloquea por el cálculo."""

    def __init__(self, db_path: str, race_id: int, signals: _ComputeSignals):
        super().__init__()
        self.db_path, self.race_id, self.signals = db_path, race_id, signals

    def run(self) -> None:
        try:
            conn = db.connect(self.db_path)
            try:
                comp = repo.compute_race(conn, self.race_id)
            finally:
                conn.close()
            self.signals.done.emit(comp, self.race_id)
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class Session(QObject):
    changed = Signal()  # los datos cambiaron (inscripción, correcciones, estado de carrera...)
    race_changed = Signal()  # cambió la carrera activa
    results_ready = Signal(object)  # Computation
    error = Signal(str)

    LIVE_REFRESH_MS = 2000

    def __init__(self, db_path: str | Path, parent=None):
        super().__init__(parent)
        self.db_path = str(db_path)
        self.conn = db.open_database(self.db_path)
        self.operator = ""
        self.last_computation: Computation | None = None
        races = repo.list_races(self.conn)
        if not races:
            raise db.DatabaseError("El evento no tiene carreras.")
        self.race_id = races[0].id
        self._signals = _ComputeSignals()
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._busy = False
        self._dirty = False
        self._closed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.LIVE_REFRESH_MS)
        self.changed.connect(self.request_results)

    # ---- acceso rápido
    def race(self) -> Race:
        return repo.get_race(self.conn, self.race_id)

    def points(self):
        return repo.list_points(self.conn, self.race_id)

    def event_row(self):
        return repo.get_event(self.conn, self.race().event_id)

    def set_race(self, race_id: int) -> None:
        if race_id != self.race_id:
            self.race_id = race_id
            self.last_computation = None
            self.race_changed.emit()
            self.request_results()

    def notify_changed(self) -> None:
        self.changed.emit()

    # ---- cálculo en segundo plano
    def request_results(self) -> None:
        if self._closed:
            return
        if self._busy:
            self._dirty = True
            return
        self._busy = True
        self._dirty = False
        QThreadPool.globalInstance().start(_ComputeTask(self.db_path, self.race_id, self._signals))

    def _tick(self) -> None:
        # Mientras la carrera corre, se recalcula periódicamente (nuevas lecturas llegan por otro proceso)
        try:
            if self.race().status == "running":
                self.request_results()
        except Exception:
            pass

    @Slot(object, int)
    def _on_done(self, comp, race_id) -> None:
        self._busy = False
        if self._closed:
            return
        if race_id == self.race_id:
            self.last_computation = comp
            self.results_ready.emit(comp)
        if self._dirty:
            self.request_results()

    @Slot(str)
    def _on_failed(self, tb: str) -> None:
        self._busy = False
        if self._closed:
            return
        self.error.emit(tb)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        QThreadPool.globalInstance().waitForDone(3000)
        self.conn.close()
