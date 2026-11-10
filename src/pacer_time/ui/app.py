"""Punto de entrada de la aplicación de escritorio."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .. import db, repo
from ..simulator import seed_demo
from .dialogs import NewEventDialog
from .main_window import MainWindow
from .session import Session

DATA_DIR = Path.home() / "PacerTime"

STYLE = """
QToolBar { spacing: 6px; padding: 4px; border-bottom: 1px solid #d0d7de; }
QTabBar::tab { padding: 8px 16px; }
QTabBar::tab:selected { font-weight: 700; }
QPushButton { padding: 5px 12px; }
QHeaderView::section { background: #f6f8fa; padding: 5px; border: none; border-bottom: 1px solid #d0d7de; font-weight: 600; }
QTableView { gridline-color: #eaeef2; selection-background-color: #cfe3ff; selection-color: #111; }
"""


class App:
    def __init__(self, qt: QApplication):
        self.qt = qt
        self.window: MainWindow | None = None

    # ---- acciones de archivo
    def new_event(self) -> bool:
        dlg = NewEventDialog()
        if not dlg.exec():
            return False
        v = dlg.values()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        default = DATA_DIR / (v["name"].replace(" ", "_").replace("/", "-") + ".db")
        path, _ = QFileDialog.getSaveFileName(None, "Guardar evento como", str(default), "Evento Pacer Time (*.db)")
        if not path:
            return False
        try:
            conn = db.create_database(path)
        except db.DatabaseError as exc:
            QMessageBox.warning(None, "No se pudo crear", str(exc))
            return False
        ev = repo.create_event(conn, v["name"], v["date"], v["city"])
        repo.create_race(conn, ev, v["race_name"], v["distance_m"], award_mode=v["award_mode"], splits=v["splits"])
        conn.close()
        return self.open_path(path)

    def open_event(self) -> bool:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(None, "Abrir evento", str(DATA_DIR), "Evento Pacer Time (*.db)")
        return bool(path) and self.open_path(path)

    def demo(self) -> bool:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "demo.db"
        n = 1
        while path.exists():
            n += 1
            path = DATA_DIR / f"demo{n}.db"
        conn = db.create_database(path)
        seed_demo(conn, runners=150, seed=n)
        conn.close()
        return self.open_path(str(path))

    def open_path(self, path: str) -> bool:
        try:
            session = Session(path)
        except Exception as exc:
            QMessageBox.critical(None, "No se pudo abrir", str(exc))
            return False
        old = self.window
        self.window = MainWindow(session, {"new": self._switch_new, "open": self._switch_open})
        self.window.show()
        if old is not None:
            old.backup_now(silent=True)
            old.shutdown()
            old.hide()
            old.deleteLater()
        return True

    def _switch_new(self) -> None:
        self.new_event()

    def _switch_open(self) -> None:
        self.open_event()

    # ---- bienvenida
    def welcome(self) -> bool | None:
        """True si abrió algo, False si se canceló un paso, None si el usuario eligió salir."""
        box = QMessageBox()
        box.setWindowTitle("Pacer Time")
        box.setText("<h2>Pacer Time</h2>Registro y cronometraje de carreras. Todo funciona en esta computadora, sin internet.")
        b_new = box.addButton("Nuevo evento", QMessageBox.ButtonRole.AcceptRole)
        b_open = box.addButton("Abrir evento", QMessageBox.ButtonRole.AcceptRole)
        b_demo = box.addButton("Probar con datos de demostración", QMessageBox.ButtonRole.AcceptRole)
        b_exit = box.addButton("Salir", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_exit or clicked is None:
            return None
        if clicked is b_new:
            return self.new_event()
        if clicked is b_open:
            return self.open_event()
        if clicked is b_demo:
            return self.demo()
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pacer-time")
    ap.add_argument("evento", nargs="?", help="Archivo .db del evento a abrir")
    args, qt_args = ap.parse_known_args(argv if argv is not None else sys.argv[1:])
    qt = QApplication([sys.argv[0]] + qt_args)
    qt.setStyle("Fusion")
    qt.setStyleSheet(STYLE)
    qt.setApplicationName("Pacer Time")
    app = App(qt)
    if args.evento:
        app.open_path(args.evento)
    while app.window is None:  # bienvenida hasta abrir algo o elegir «Salir»
        if app.welcome() is None:
            return 0
    return qt.exec()


if __name__ == "__main__":
    sys.exit(main())
