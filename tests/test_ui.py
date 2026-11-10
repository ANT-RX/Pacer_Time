import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pacer_time import db, repo  # noqa: E402
from pacer_time.simulator import seed_demo  # noqa: E402
from pacer_time.ui.main_window import MainWindow  # noqa: E402
from pacer_time.ui.session import Session  # noqa: E402


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    from PySide6.QtCore import QSettings

    # Las pruebas no deben tocar la configuración real del usuario
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("qsettings")))
    app = QApplication.instance() or QApplication([])
    yield app


def pump(app, cond, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    path = tmp_path / "ui.db"
    conn = db.create_database(path)
    info = seed_demo(conn, runners=80, seed=11)
    conn.close()
    session = Session(path)
    w = MainWindow(session)
    w.operator.setText("tester")
    w._operator_changed()
    w.show()
    assert pump(qapp, lambda: session.last_computation is not None), "no llegaron resultados"
    yield w, info
    w.shutdown()
    w.deleteLater()


def test_tabs_are_populated(qapp, window):
    w, info = window
    s = w.s
    tabs = {w.tabs.tabText(i): w.tabs.widget(i) for i in range(w.tabs.count())}
    assert set(tabs) == {"Participantes", "Check-in", "Carrera en vivo", "Resultados", "Calidad y correcciones"}
    n = len(repo.list_registrations(s.conn, s.race_id))
    assert tabs["Participantes"].table.source.rowCount() == n
    res = tabs["Resultados"]
    assert pump(qapp, lambda: res.table.source.rowCount() == n)
    first = res.table.source.row(0)
    assert first[0] == 1 and first[-1] == "FIN"  # el líder es el primero
    assert tabs["Calidad y correcciones"].issues.source.rowCount() > 0
    assert tabs["Carrera en vivo"].reads.source.rowCount() > 0


def test_checkin_flow_assigns_chip_and_kit(qapp, window):
    w, _ = window
    s = w.s
    reg = repo.register(s.conn, s.race_id, {"first_name": "Nueva", "last_name": "Persona", "document": "NP1"})
    bib = s.conn.execute("SELECT bib FROM registration WHERE id=?", (reg,)).fetchone()[0]
    ci = w.tabs.widget(1)
    ci.search.setText(str(bib))
    ci.lookup()
    assert ci._reg is not None and "Nueva" in ci.card.text()
    ci.chip.setText("abc 123")
    ci.confirm()
    row = s.conn.execute("SELECT kit_delivered FROM registration WHERE id=?", (reg,)).fetchone()
    assert row[0] == 1
    assert repo.find_registration(s.conn, s.race_id, chip="ABC123")["id"] == reg
    # Chip ya usado: muestra el error y no cambia nada
    other = repo.register(s.conn, s.race_id, {"first_name": "Otra", "last_name": "X", "document": "NP2"})
    ci.search.setText(str(s.conn.execute("SELECT bib FROM registration WHERE id=?", (other,)).fetchone()[0]))
    ci.lookup()
    ci.chip.setText("ABC123")
    ci.confirm()
    assert "ya está asignado" in ci.msg.text()
    assert s.conn.execute("SELECT kit_delivered FROM registration WHERE id=?", (other,)).fetchone()[0] == 0


def test_live_reads_poll_and_manual_pass(qapp, window):
    w, _ = window
    live = w.tabs.widget(2)
    s = w.s
    before = live.reads.source.rowCount()
    pid = repo.list_points(s.conn, s.race_id)[-1].id
    repo.insert_reads(s.conn, [(pid, "NEWCHIP1", 123456, 1, None, "t")])
    live.poll_reads()
    assert live.reads.source.rowCount() == min(before + 1, 300)
    assert live.reads.source.row(0)[3] == "NEWCHIP1"
    live.m_bib.setValue(1)
    live.m_point.setCurrentIndex(live.m_point.findData(pid))
    live.manual_read()
    live.poll_reads()
    assert any(str(r[7]).startswith("manual:tester") for r in (live.reads.source.row(i) for i in range(live.reads.source.rowCount())))


def test_race_buttons_follow_state(qapp, window):
    w, _ = window
    live = w.tabs.widget(2)
    assert not live.b_start.isEnabled() and not live.b_close.isEnabled() and live.b_reopen.isEnabled()
    live.reopen_race()
    live.refresh_static()
    assert live.b_close.isEnabled() and not live.b_reopen.isEnabled()


def test_correction_from_quality_tab_changes_results(qapp, window):
    w, _ = window
    s = w.s
    q = w.tabs.widget(4)
    # Un corredor sin meta (error no_finish) se corrige con el diálogo (aceptado a mano)
    comp = s.last_computation
    target = next(i for i in comp.issues if i.code == "no_finish")
    pts = s.points()
    gun = s.race().gun_time_ms
    repo.set_pass(s.conn, target.reg_id, pts[-1].id, gun + 2_000_000, "video", "tester")
    s.notify_changed()
    assert pump(qapp, lambda: any(r.bib == target.bib and r.status == "FIN" for r in s.last_computation.results))
    assert pump(qapp, lambda: q.hist.source.rowCount() >= 1)
    assert q.hist.source.row(0)[1] == "tester"
