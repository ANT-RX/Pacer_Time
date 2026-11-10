import sys
import threading
import time
import types

import pytest

from pacer_time import db, repo
from pacer_time.capture import launcher, llrp
from pacer_time.capture.base import ReadEvent
from pacer_time.capture.runner import run_capture
from pacer_time.capture.simulated import SimulatedReader
from pacer_time.capture.writer import ReadWriter
from pacer_time.simulator import Noise, seed_demo


def make_running_race(tmp_path, runners=25, elapsed_ms=0):
    path = tmp_path / "live.db"
    conn = db.create_database(path)
    info = seed_demo(conn, runners=runners, seed=3, elapsed_ms=elapsed_ms, noise=Noise(dns=0))
    return path, conn, info["race_id"]


# ------------------------------------------------------------------ escritor
def test_writer_persists_everything_and_is_idempotent(tmp_path, race, conn):
    race_id, _ = race
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    pid = repo.list_points(conn, race_id)[0].id
    w = ReadWriter(path)
    w.start()
    for i in range(500):
        w.submit(ReadEvent("E2000001", 1000 + i, pid, 1, -50.0, "t"))
    for i in range(500):  # reenvío de lo mismo: no duplica
        w.submit(ReadEvent("E2000001", 1000 + i, pid, 1, -50.0, "t"))
    assert w.stop()
    assert repo.count_reads(conn, race_id) == 500
    assert w.written == 1000 and w.submitted == 1000


def test_writer_retries_when_db_write_fails(tmp_path, race, conn, monkeypatch):
    race_id, _ = race
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    pid = repo.list_points(conn, race_id)[0].id
    real = repo.insert_reads
    calls = {"n": 0}

    def flaky(c, rows):
        calls["n"] += 1
        if calls["n"] <= 2:
            import sqlite3

            raise sqlite3.OperationalError("database is locked")
        return real(c, rows)

    monkeypatch.setattr(repo, "insert_reads", flaky)
    w = ReadWriter(path)
    w.start()
    w.submit(ReadEvent("E2000002", 42, pid, 1, None, "t"))
    assert w.stop()
    assert calls["n"] >= 3
    assert repo.count_reads(conn, race_id) == 1


def test_writer_does_not_block_while_another_process_reads(tmp_path, race, conn):
    """WAL: un lector con transacción abierta no impide escribir."""
    race_id, _ = race
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    pid = repo.list_points(conn, race_id)[0].id
    reader = db.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM raw_read").fetchone()
    w = ReadWriter(path)
    w.start()
    t0 = time.monotonic()
    w.submit(ReadEvent("E2000003", 7, pid, 1, None, "t"))
    assert w.stop()
    assert time.monotonic() - t0 < 3
    reader.execute("COMMIT")
    reader.close()
    assert repo.count_reads(conn, race_id) == 1


# ------------------------------------------------------------------ proceso de captura
def test_run_capture_with_simulated_reader_finishes(tmp_path):
    path, conn, race_id = make_running_race(tmp_path)
    reader = SimulatedReader(str(path), race_id, speed=3000, seed=5)
    final = run_capture(path, race_id, reader, "sim-test", heartbeat_s=0.2)
    assert final == "finished"
    status = launcher.list_status(conn, race_id)[0]
    assert status["state"] == "finished" and status["total_reads"] > 50
    comp = repo.compute_race(conn, race_id)
    assert sum(1 for r in comp.results if r.status in ("FIN", "RUN")) > 0
    assert comp.unknown_chips  # chips ajenos simulados


def test_stop_request_via_db_stops_capture_and_flushes(tmp_path):
    path, conn, race_id = make_running_race(tmp_path)
    reader = SimulatedReader(str(path), race_id, speed=3000, seed=5)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("final", run_capture(path, race_id, SimulatedReader(str(path), race_id, speed=1.0), "sim-slow", heartbeat_s=0.2)))
    t.start()
    time.sleep(0.6)
    status = launcher.list_status(conn, race_id)[0]
    assert status["alive"] and status["state"] == "running"
    launcher.request_stop(conn, "sim-slow")
    t.join(8)
    assert not t.is_alive()
    assert out["final"] == "stopped"
    assert launcher.list_status(conn, race_id)[0]["state"] == "stopped"


def test_failing_reader_reports_error(tmp_path):
    path, conn, race_id = make_running_race(tmp_path, runners=5)

    class Boom(SimulatedReader):
        def run(self, emit, stop):
            raise RuntimeError("lector desconectado")

    final = run_capture(path, race_id, Boom(str(path), race_id), "boom", heartbeat_s=0.1)
    assert final == "error"
    st = launcher.list_status(conn, race_id)[0]
    assert "lector desconectado" in st["detail"]


def test_capture_runs_as_separate_process(tmp_path):
    path, conn, race_id = make_running_race(tmp_path, runners=15)
    proc = launcher.start_capture_process(str(path), race_id, "sim", "sim-proc", ["--speed", "3000", "--seed", "2"])
    try:
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            rows = launcher.list_status(conn, race_id)
            state = rows[0]["state"] if rows else None
            if state in ("finished", "error", "stopped"):
                break
            time.sleep(0.2)
        assert state == "finished", launcher.list_status(conn, race_id)
        assert repo.count_reads(conn, race_id) > 20
    finally:
        proc.wait(10)


def test_stale_heartbeat_is_reported(conn, race):
    race_id, _ = race
    conn.execute(
        "INSERT INTO reader_status(source, race_id, state, heartbeat_ms) VALUES ('muerto', ?, 'running', ?)",
        (race_id, 1),
    )
    st = launcher.list_status(conn, race_id)[0]
    assert st["stale"] and not st["alive"]


# ------------------------------------------------------------------ LLRP
def test_llrp_tag_report_parsing():
    tags = [
        {"EPC": b"\xe2\x00\x00\x01", "AntennaID": 2, "PeakRSSI": -48, "FirstSeenTimestampUTC": 1_700_000_000_123_456},
        {"EPC-96": "e200 0002"},
        {"EPC": None},
        {},
    ]
    evs = llrp.events_from_tag_reports(tags, 9, "llrp:test", use_reader_time=True, received_ms=5)
    assert [e.chip_code for e in evs] == ["E2000001", "E2000002"]
    assert evs[0].ts_ms == 1_700_000_000_123 and evs[0].antenna == 2 and evs[0].rssi == -48
    assert evs[1].ts_ms == 5 and evs[1].point_id == 9
    host_time = llrp.events_from_tag_reports(tags[:1], 9, "x", use_reader_time=False, received_ms=77)
    assert host_time[0].ts_ms == 77


def test_llrp_reader_with_fake_sllurp(monkeypatch):
    class FakeConfig:
        def __init__(self, d):
            self.d = d

    class FakeClient:
        def __init__(self, host, port, config):
            self.cbs = []
            self.disconnected = False

        def add_tag_report_callback(self, cb):
            self.cbs.append(cb)

        def connect(self):
            for cb in self.cbs:
                cb(self, [{"EPC": b"\xaa\xbb", "AntennaID": 1}, {"EPC": b"\xcc\xdd", "AntennaID": 2}])

        def is_alive(self):
            return True

        def disconnect(self):
            self.disconnected = True

    mod_llrp = types.SimpleNamespace(LLRPReaderClient=FakeClient, LLRPReaderConfig=FakeConfig)
    mod = types.ModuleType("sllurp")
    mod.llrp = mod_llrp
    monkeypatch.setitem(sys.modules, "sllurp", mod)
    monkeypatch.setitem(sys.modules, "sllurp.llrp", mod_llrp)

    got = []
    stop = threading.Event()
    reader = llrp.LLRPReader("10.0.0.5", 3, antennas=(1, 2))
    t = threading.Thread(target=reader.run, args=(got.append, stop))
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(3)
    assert [e.chip_code for e in got] == ["AABB", "CCDD"]
    assert got[0].source == "llrp:10.0.0.5"


def test_real_sllurp_config_is_accepted():
    """La configuración que generamos debe ser válida para la librería real (si está instalada)."""
    sllurp = pytest.importorskip("sllurp.llrp")
    r = llrp.LLRPReader("127.0.0.1", 1, antennas=(1, 2), session=1, tag_population=32)
    cfg = r._config(sllurp)
    assert cfg.antennas == [1, 2] and cfg.session == 1 and cfg.tag_population == 32
    assert cfg.tag_content_selector["EnableFirstSeenTimestamp"] is True


def test_kill9_of_capture_leaves_db_consistent_and_reads_durable(tmp_path):
    """Simula un corte brusco: kill -9 al proceso de captura en pleno funcionamiento."""
    import os
    import signal

    if sys.platform == "win32":
        pytest.skip("SIGKILL no existe en Windows")
    path, conn, race_id = make_running_race(tmp_path, runners=60)
    proc = launcher.start_capture_process(str(path), race_id, "sim", "sim-kill", ["--speed", "400", "--seed", "3"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and repo.count_reads(conn, race_id) < 50:
        time.sleep(0.1)
    saved_before = repo.count_reads(conn, race_id)
    assert saved_before >= 50
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(10)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert repo.count_reads(conn, race_id) >= saved_before  # nada de lo confirmado se perdió
    # La UI detecta que el proceso murió (estado 'running' sin latido) al pasar el umbral
    conn.execute("UPDATE reader_status SET heartbeat_ms = heartbeat_ms - 10000 WHERE source='sim-kill'")
    st = launcher.list_status(conn, race_id)[0]
    assert st["stale"] and not st["alive"]
    # Y se puede relanzar la captura sobre la misma base sin problemas
    launcher.clear_status(conn, "sim-kill")
    proc2 = launcher.start_capture_process(str(path), race_id, "sim", "sim-kill", ["--speed", "3000", "--seed", "3"])
    proc2.wait(30)
    assert launcher.list_status(conn, race_id)[0]["state"] == "finished"
    comp = repo.compute_race(conn, race_id)
    assert any(r.status == "FIN" for r in comp.results)
