"""Genera las capturas de pantalla del README con datos de demostración (sin pantalla: QT_QPA_PLATFORM=offscreen).

    python scripts/make_screenshots.py docs/screenshots
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp())

from pacer_time import db, repo
from pacer_time.capture import launcher
from pacer_time.simulator import seed_demo
from pacer_time.ui.app import STYLE
from pacer_time.ui.main_window import MainWindow
from pacer_time.ui.session import Session

out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
out.mkdir(parents=True, exist_ok=True)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLE)
tmp = Path(tempfile.mkdtemp())


def pump(sec):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.02)


def open_window(path):
    s = Session(path)
    w = MainWindow(s)
    w.operator.setText("Operador")
    w._operator_changed()
    w.resize(1280, 780)
    w.show()
    pump(1.5)
    return w


# Carrera terminada, con anomalías típicas
c = db.create_database(tmp / "done.db")
seed_demo(c, runners=150, seed=4, event_name="Carrera Demo Pacer Time")
c.close()
w = open_window(tmp / "done.db")
s = w.s
w.tabs.setCurrentIndex(0)
pump(0.5)
w.grab().save(str(out / "1-participantes.png"))

reg = repo.list_registrations(s.conn, s.race_id)[11]
ci = w.tabs.widget(1)
ci.search.setText(str(reg["bib"]))
ci.lookup()
w.tabs.setCurrentIndex(1)
pump(0.5)
w.grab().save(str(out / "2-checkin.png"))

w.tabs.setCurrentIndex(3)
pump(0.8)
w.grab().save(str(out / "4-resultados.png"))

q = w.tabs.widget(4)
w.tabs.setCurrentIndex(4)
pump(0.5)
issue_row = next(i for i, r in enumerate(range(q.issues.source.rowCount())) if q.issues.source.row(i)[1] == "no_finish")
q.issues.selectRow(issue_row)
pump(0.5)
w.grab().save(str(out / "5-calidad.png"))
w.shutdown()

# Carrera en curso con captura simulada en proceso aparte
c = db.create_database(tmp / "live.db")
info = seed_demo(c, runners=150, seed=5, elapsed_ms=0, event_name="Carrera Demo Pacer Time")
c.close()
w = open_window(tmp / "live.db")
w.tabs.setCurrentIndex(2)
proc = launcher.start_capture_process(str(tmp / "live.db"), info["race_id"], "sim", "sim", ["--speed", "120", "--seed", "5"])
pump(9)
w.grab().save(str(out / "3-carrera-en-vivo.png"))
launcher.request_stop(w.s.conn, "sim")
proc.wait(20)
w.shutdown()
print("Capturas en", out)
