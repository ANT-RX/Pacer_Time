"""Ventana principal."""

from __future__ import annotations

import getpass
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QToolBar,
    QWidget,
)

from .. import db, repo
from ..capture import launcher
from .session import Session
from .tab_live import LiveTab
from .tab_participants import CheckInTab, ParticipantsTab
from .tab_quality import QualityTab
from .tab_results import ResultsTab

AUTO_BACKUP_MS = 5 * 60 * 1000


class MainWindow(QMainWindow):
    def __init__(self, session: Session, app_actions=None):
        super().__init__()
        self.s = session
        self.app_actions = app_actions or {}
        self.settings = QSettings("PacerTime", "PacerTime")
        self.s.operator = self.settings.value("operator", "") or ""
        self.resize(1280, 780)

        self.tabs = QTabWidget()
        self.tabs.addTab(ParticipantsTab(session), "Participantes")
        self.tabs.addTab(CheckInTab(session), "Check-in")
        self.live = LiveTab(session)
        self.tabs.addTab(self.live, "Carrera en vivo")
        self.tabs.addTab(ResultsTab(session), "Resultados")
        self.tabs.addTab(QualityTab(session), "Calidad y correcciones")
        self.setCentralWidget(self.tabs)

        self._build_menu()
        self._build_toolbar()
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)
        self.s.error.connect(self._show_error)
        self.s.race_changed.connect(self._update_title)
        self.s.changed.connect(self._update_title)
        self._update_title()

        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(lambda: self.backup_now(silent=True))
        self._backup_timer.start(AUTO_BACKUP_MS)
        self.s.request_results()

    # ---------------------------------------------------------------- UI
    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&Archivo")
        for text, key, slot in (
            ("Nuevo evento…", "new", None),
            ("Abrir evento…", "open", None),
        ):
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, k=key: self.app_actions.get(k, lambda: None)())
            m.addAction(act)
        m.addSeparator()
        bk = QAction("Respaldar ahora", self)
        bk.setShortcut("Ctrl+B")
        bk.triggered.connect(lambda: self.backup_now())
        m.addAction(bk)
        m.addSeparator()
        quit_ = QAction("Salir", self)
        quit_.triggered.connect(self.close)
        m.addAction(quit_)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Principal")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Carrera: "))
        self.race_combo = QComboBox()
        for r in repo.list_races(self.s.conn):
            self.race_combo.addItem(f"{r.name} ({r.distance_m / 1000:g} km)", r.id)
        self.race_combo.currentIndexChanged.connect(lambda: self.s.set_race(self.race_combo.currentData()))
        tb.addWidget(self.race_combo)
        tb.addSeparator()
        tb.addWidget(QLabel(" Operador: "))
        self.operator = QLineEdit(self.s.operator or getpass.getuser())
        self.operator.setFixedWidth(160)
        self.operator.setToolTip("Su nombre queda registrado en cada cambio y corrección.")
        self.operator.editingFinished.connect(self._operator_changed)
        tb.addWidget(self.operator)
        self.s.operator = self.operator.text().strip()

    def _operator_changed(self) -> None:
        self.s.operator = self.operator.text().strip()
        self.settings.setValue("operator", self.s.operator)

    def _update_title(self) -> None:
        ev = self.s.event_row()
        race = self.s.race()
        self.setWindowTitle(f"Pacer Time — {ev['name']} · {race.name}")
        self.status_label.setText(f"{self.s.db_path}")

    def _show_error(self, tb: str) -> None:
        self.statusBar().showMessage("Error al calcular resultados (ver detalle)", 10000)
        QMessageBox.critical(self, "Error interno", tb[-1500:])

    # ---------------------------------------------------------------- respaldo
    def backup_dir(self) -> Path:
        p = Path(self.s.db_path)
        return p.parent / f"{p.stem}.respaldos"

    def backup_now(self, silent: bool = False) -> None:
        try:
            dest = db.backup_to_dir(self.s.conn, self.backup_dir(), prefix=Path(self.s.db_path).stem, keep=30)
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "Respaldo", f"No se pudo respaldar: {exc}")
            self.statusBar().showMessage(f"⚠ Falló el respaldo automático: {exc}", 15000)
            return
        self.statusBar().showMessage(f"Respaldo guardado: {dest.name}", 8000)
        if not silent:
            QMessageBox.information(self, "Respaldo", f"Copia guardada en:\n{dest}")

    # ---------------------------------------------------------------- cierre
    def closeEvent(self, ev: QCloseEvent) -> None:
        alive = [r for r in launcher.list_status(self.s.conn) if r["alive"]]
        note = ""
        if alive:
            note = "\n\nLa captura SEGUIRÁ funcionando en segundo plano (no se pierden lecturas). Puede detenerla al volver a abrir el evento."
        if QMessageBox.question(self, "Salir", f"¿Cerrar Pacer Time?{note}") != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        self.backup_now(silent=True)
        self.shutdown()
        ev.accept()

    def shutdown(self) -> None:
        """Detiene temporizadores y cierra la sesión (sin preguntar). Idempotente."""
        if getattr(self, "_down", False):
            return
        self._down = True
        self._backup_timer.stop()
        for t in (self.live._t_reads, self.live._t_slow, self.live._t_clock):
            t.stop()
        self.s.close()
