import io
import sqlite3

import pytest

from pacer_time import db, importers, repo, timeutil
from pacer_time.schema import SCHEMA_VERSION


# ------------------------------------------------------------------ db
def test_wal_and_foreign_keys(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.schema_version(conn) == SCHEMA_VERSION


def test_migrate_is_idempotent_and_rejects_newer(tmp_path):
    c = db.create_database(tmp_path / "a.db")
    assert db.migrate(c) == SCHEMA_VERSION
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(db.DatabaseError):
        db.migrate(c)
    c.close()


def test_create_refuses_to_overwrite(tmp_path):
    db.create_database(tmp_path / "a.db").close()
    with pytest.raises(db.DatabaseError):
        db.create_database(tmp_path / "a.db")


def test_raw_reads_and_corrections_are_immutable(conn, race):
    race_id, _ = race
    pid = repo.list_points(conn, race_id)[0].id
    repo.insert_reads(conn, [(pid, "E20000001", 1000, 1, -50.0, "t")])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE raw_read SET ts_ms = 5")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM raw_read")
    rid = conn.execute("SELECT id FROM raw_read").fetchone()[0]
    repo.void_read(conn, rid, "prueba", "op")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE correction SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM correction")


def test_tx_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            conn.execute("INSERT INTO event(name, event_date) VALUES ('x','2026-01-01')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0


def test_backup_is_consistent_and_pruned(conn, race, tmp_path):
    race_id, _ = race
    dest = db.backup_database(conn, tmp_path / "bk" / "copy.db")
    copy = db.open_database(dest)
    assert copy.execute("SELECT COUNT(*) FROM registration").fetchone()[0] == 3
    copy.close()
    for _ in range(5):
        db.backup_to_dir(conn, tmp_path / "bk2", keep=3)
    assert len(list((tmp_path / "bk2").glob("evento-*.db"))) == 3


# ------------------------------------------------------------------ timeutil
def test_format_duration_truncates():
    assert timeutil.format_duration(3_723_999) == "1:02:03.9"
    assert timeutil.format_duration(59_999, decimals=0) == "0:00:59"
    assert timeutil.format_duration(None) == ""
    assert timeutil.format_pace(359_999) == "5:59"


def test_parse_clock_roundtrip():
    ref = timeutil.now_ms()
    ms = timeutil.parse_clock("08:15:30.250", ref)
    assert timeutil.format_clock(ms) == "08:15:30.250"
    with pytest.raises(ValueError):
        timeutil.parse_clock("25:00:00", ref)


# ------------------------------------------------------------------ inscripciones
def test_auto_bib_category_and_chip(conn, race):
    race_id, regs = race
    rows = {r["bib"]: r for r in repo.list_registrations(conn, race_id)}
    assert sorted(rows) == [1, 2, 3]
    assert rows[1]["category_name"] == "Femenil 30-39"  # Ana, 36 años el día del evento
    assert rows[2]["category_name"] == "Varonil 40-49"
    assert rows[3]["category_name"] == "Femenil 50-59"
    assert rows[1]["chips"] == "E2000001"


def test_register_rejects_duplicates_and_conflicts(conn, race):
    race_id, _ = race
    with pytest.raises(ValueError, match="ya está inscrito"):
        repo.register(conn, race_id, {"first_name": "Ana", "last_name": "López", "document": "DOC1"})
    with pytest.raises(ValueError, match="dorsal 2"):
        repo.register(conn, race_id, {"first_name": "Nuevo", "document": "N1"}, bib=2)
    with pytest.raises(ValueError, match="chip"):
        repo.register(conn, race_id, {"first_name": "Otro", "document": "N2"}, chip="e200 0001")
    # Una inscripción fallida no deja participantes huérfanos
    assert conn.execute("SELECT COUNT(*) FROM participant WHERE document IN ('N1','N2')").fetchone()[0] == 0


def test_chip_limits_and_reassign(conn, race):
    race_id, regs = race
    repo.assign_chip(conn, regs[0], "AAA1")
    with pytest.raises(ValueError, match="2 chips"):
        repo.assign_chip(conn, regs[0], "AAA2")
    repo.remove_chip(conn, race_id, "aaa1")
    repo.assign_chip(conn, regs[1], "aaa1")  # normaliza y ya está libre
    with pytest.raises(ValueError, match="no está asignado"):
        repo.remove_chip(conn, race_id, "NOEXISTE")


def test_same_person_can_join_other_race_reusing_participant(conn, race):
    race_id, _ = race
    ev2 = repo.create_event(conn, "Otra", "2026-12-01")
    r2 = repo.create_race(conn, ev2, "10K", 10000)
    repo.register(conn, r2, {"first_name": "Ana", "last_name": "López", "document": "DOC1"})
    assert conn.execute("SELECT COUNT(*) FROM participant WHERE document='DOC1'").fetchone()[0] == 1


# ------------------------------------------------------------------ flujo completo con correcciones
def _reads(conn, race_id, spec):
    pts = {p.kind: p.id for p in repo.list_points(conn, race_id)}
    repo.insert_reads(conn, [(pts[k], chip, ts, 1, None, "t") for chip, k, ts in spec])


def test_end_to_end_with_corrections_and_audit(conn, race):
    race_id, regs = race
    gun = repo.start_race(conn, race_id, "op", ts_ms=1_000_000_000)
    with pytest.raises(ValueError):
        repo.start_race(conn, race_id, "op")
    _reads(conn, race_id, [
        ("E2000001", "start", gun + 1000), ("E2000001", "finish", gun + 1_000_000),
        ("E2000002", "start", gun + 2000), ("E2000002", "finish", gun + 900_000),
        # Carla (3) sin lectura de meta
    ])
    repo.close_race(conn, race_id, "op")
    comp = repo.compute_race(conn, race_id)
    assert [r.bib for r in comp.results[:2]] == [2, 1]
    assert any(i.code == "no_finish" and i.bib == 3 for i in comp.issues)

    # Sin motivo / sin operador no se puede corregir
    pts = {p.kind: p.id for p in repo.list_points(conn, race_id)}
    with pytest.raises(ValueError, match="motivo"):
        repo.set_pass(conn, regs[2], pts["finish"], gun + 1_300_000, "  ", "op")
    with pytest.raises(ValueError, match="operador"):
        repo.set_pass(conn, regs[2], pts["finish"], gun + 1_300_000, "tiempo de video", "")

    repo.set_pass(conn, regs[2], pts["finish"], gun + 1_300_000, "tiempo de video", "Juan")
    comp = repo.compute_race(conn, race_id)
    carla = next(r for r in comp.results if r.bib == 3)
    assert carla.status == "FIN" and carla.gun_ms == 1_300_000 and "manual" in carla.flags

    # Anular una lectura y restaurarla
    rid = conn.execute("SELECT id FROM raw_read WHERE chip_code='E2000002' AND point_id=?", (pts["finish"],)).fetchone()[0]
    repo.void_read(conn, rid, "lectura de otro corredor", "Juan")
    with pytest.raises(ValueError, match="ya está anulada"):
        repo.void_read(conn, rid, "otra vez", "Juan")
    assert next(r for r in repo.compute_race(conn, race_id).results if r.bib == 2).status == "NT"
    repo.unvoid_read(conn, rid, "me equivoqué", "Juan")
    assert next(r for r in repo.compute_race(conn, race_id).results if r.bib == 2).status == "FIN"

    # Cambios de estado y de disparo quedan registrados con antes/después
    repo.set_status(conn, regs[2], "DNF", "se lesionó", "Juan")
    repo.set_gun(conn, race_id, gun + 500, "ajuste por video", "Juan")
    log = repo.list_corrections(conn, race_id)
    actions = [c["action"] for c in log]
    assert {"add_pass", "void_read", "unvoid_read", "set_status", "set_gun"} <= set(actions)
    status_row = next(c for c in log if c["action"] == "set_status")
    assert (status_row["old_value"], status_row["new_value"], status_row["operator"]) == ("REG", "DNF", "Juan")
    # Las lecturas crudas siguen intactas
    assert repo.count_reads(conn, race_id) == 4


def test_insert_reads_is_idempotent(conn, race):
    race_id, _ = race
    pid = repo.list_points(conn, race_id)[0].id
    batch = [(pid, "E2000001", 5000, 1, None, "src"), (pid, "e2000001", 5000, 1, None, "src")]
    assert repo.insert_reads(conn, batch) == 1
    assert repo.insert_reads(conn, batch) == 0


def test_manual_read_by_bib(conn, race):
    race_id, regs = race
    pid = repo.list_points(conn, race_id)[0].id
    code = repo.add_manual_read(conn, race_id, pid, bib=1, ts_ms=1234, operator="ana")
    assert code == "E2000001"
    # Corredor sin chip: se crea un chip virtual BIBn
    reg = repo.register(conn, race_id, {"first_name": "Sin", "last_name": "Chip", "document": "SC"})
    bib = conn.execute("SELECT bib FROM registration WHERE id=?", (reg,)).fetchone()[0]
    assert repo.add_manual_read(conn, race_id, pid, bib=bib, ts_ms=2000) == f"BIB{bib}"
    with pytest.raises(ValueError):
        repo.add_manual_read(conn, race_id, pid, bib=999)


# ------------------------------------------------------------------ importadores
CSV_OK = """Nombre;Apellidos;Documento;Fecha de nacimiento;Sexo;Club;Dorsal;Chip;Categoría;Pagado
José;García;D100;15/03/1992;Hombre;Club A;50;CHIP50;;sí
María;Núñez;D101;1988-07-04;Mujer;;;CHIP51;Femenil 30-39;
Ana;López;DOC1;01/05/1990;F;;;;;
Luis;Mal;D103;32/13/2000;M;;;;;
Pedro;Dup;D104;1990-01-01;M;;50;;;
Eva;Cat;D105;1990-01-01;F;;;;Inventada;
;;;;;;;;;
"""


def test_import_participants_csv(conn, race):
    race_id, _ = race
    rep = importers.import_participants_csv(conn, race_id, io.StringIO(CSV_OK), "tester")
    assert rep.created == 2
    assert rep.duplicates == 1  # Ana López ya estaba (mismo documento)
    errs = dict(rep.errors)
    assert "Fecha inválida" in errs[5]
    assert "dorsal 50" in errs[6]
    assert "Categoría desconocida" in errs[7]
    rows = {r["bib"]: r for r in repo.list_registrations(conn, race_id)}
    assert rows[50]["first_name"] == "José" and rows[50]["gender"] == "M" and rows[50]["paid"] == 1
    assert rows[50]["chips"] == "CHIP50"
    assert rows[51]["first_name"] == "María" and rows[51]["category_name"] == "Femenil 30-39"
    # Las filas con error no dejaron basura
    assert conn.execute("SELECT COUNT(*) FROM participant WHERE document IN ('D103','D104','D105')").fetchone()[0] == 0


def test_import_participants_requires_name_column(conn, race):
    race_id, _ = race
    rep = importers.import_participants_csv(conn, race_id, io.StringIO("foo,bar\n1,2\n"))
    assert rep.created == 0 and rep.errors


def test_import_reads_csv_idempotent(conn, race):
    race_id, _ = race
    finish = [p for p in repo.list_points(conn, race_id) if p.kind == "finish"][0]
    ref = timeutil.parse_clock("09:00:00", timeutil.now_ms())
    text = "chip,hora\nE2000001,09:30:15.250\nE2000002,09:31:00\nE2000003,basura\n"
    n, errors = importers.import_reads_csv(conn, race_id, finish.id, io.StringIO(text), ref)
    assert n == 2 and len(errors) == 1
    n2, _ = importers.import_reads_csv(conn, race_id, finish.id, io.StringIO(text), ref)
    assert n2 == 0
