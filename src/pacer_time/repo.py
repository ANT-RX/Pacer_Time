"""Acceso a datos y reglas de negocio sobre SQLite. Las funciones reciben una conexión abierta."""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Iterable

from . import categories as cats
from .db import tx
from .engine import compute
from .models import (
    Category,
    Computation,
    Override,
    Race,
    RaceConfig,
    ReadRec,
    RunnerInfo,
    TimingPoint,
)
from .timeutil import now_ms

PARTICIPANT_FIELDS = (
    "first_name",
    "last_name",
    "document",
    "birth_date",
    "gender",
    "club",
    "email",
    "phone",
    "emergency_contact",
    "emergency_phone",
)
MAX_CHIPS_PER_REGISTRATION = 2
VALID_STATUS = ("REG", "DNS", "DNF", "DQ")


# ---------------------------------------------------------------- utilidades
def normalize_chip(code: str) -> str:
    return re.sub(r"\s+", "", code or "").upper()


def audit(conn: sqlite3.Connection, operator: str, action: str, entity: str, entity_id: int | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(ts_ms, operator, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
        (now_ms(), operator or "sistema", action, entity, entity_id, detail),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )


# ---------------------------------------------------------------- eventos y carreras
def create_event(conn: sqlite3.Connection, name: str, event_date: str, city: str = "") -> int:
    if not name.strip():
        raise ValueError("El evento necesita un nombre.")
    d = cats.parse_date(event_date)
    cur = conn.execute("INSERT INTO event(name, event_date, city) VALUES (?,?,?)", (name.strip(), d.isoformat(), city.strip()))
    return cur.lastrowid


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"No existe el evento {event_id}")
    return row


def _race(row: sqlite3.Row) -> Race:
    return Race(
        id=row["id"],
        event_id=row["event_id"],
        name=row["name"],
        distance_m=row["distance_m"],
        status=row["status"],
        gun_time_ms=row["gun_time_ms"],
        award_mode=row["award_mode"],
        dedupe_window_ms=row["dedupe_window_ms"],
        min_segment_ms=row["min_segment_ms"],
    )


def get_race(conn: sqlite3.Connection, race_id: int) -> Race:
    row = conn.execute("SELECT * FROM race WHERE id=?", (race_id,)).fetchone()
    if row is None:
        raise ValueError(f"No existe la carrera {race_id}")
    return _race(row)


def list_races(conn: sqlite3.Connection) -> list[Race]:
    return [_race(r) for r in conn.execute("SELECT * FROM race ORDER BY id")]


def create_race(
    conn: sqlite3.Connection,
    event_id: int,
    name: str,
    distance_m: int,
    award_mode: str = "hybrid",
    splits: int = 0,
    default_categories: bool = True,
    dedupe_window_ms: int = 5000,
    min_segment_ms: int = 30000,
) -> int:
    if not name.strip():
        raise ValueError("La carrera necesita un nombre.")
    if distance_m < 0:
        raise ValueError("La distancia no puede ser negativa.")
    if award_mode not in ("gun", "net", "hybrid"):
        raise ValueError(f"Modo de premiación inválido: {award_mode}")
    with tx(conn):
        race_id = conn.execute(
            "INSERT INTO race(event_id, name, distance_m, award_mode, dedupe_window_ms, min_segment_ms) VALUES (?,?,?,?,?,?)",
            (event_id, name.strip(), distance_m, award_mode, dedupe_window_ms, min_segment_ms),
        ).lastrowid
        conn.execute("INSERT INTO timing_point(race_id, name, kind, order_idx, distance_m) VALUES (?,?,?,?,?)", (race_id, "Salida", "start", 0, 0))
        for i in range(1, splits + 1):
            conn.execute(
                "INSERT INTO timing_point(race_id, name, kind, order_idx, distance_m) VALUES (?,?,?,?,?)",
                (race_id, f"Parcial {i}", "split", i, round(distance_m * i / (splits + 1))),
            )
        conn.execute(
            "INSERT INTO timing_point(race_id, name, kind, order_idx, distance_m) VALUES (?,?,?,?,?)",
            (race_id, "Meta", "finish", splits + 1, distance_m),
        )
        if default_categories:
            for i, (cname, gender, lo, hi) in enumerate(cats.default_category_specs()):
                conn.execute(
                    "INSERT INTO category(race_id, name, gender, min_age, max_age, sort_order) VALUES (?,?,?,?,?,?)",
                    (race_id, cname, gender, lo, hi, i),
                )
        audit(conn, "sistema", "create", "race", race_id, name)
    return race_id


def list_points(conn: sqlite3.Connection, race_id: int) -> list[TimingPoint]:
    return [
        TimingPoint(r["id"], r["race_id"], r["name"], r["kind"], r["order_idx"], r["distance_m"])
        for r in conn.execute("SELECT * FROM timing_point WHERE race_id=? ORDER BY order_idx", (race_id,))
    ]


def list_categories(conn: sqlite3.Connection, race_id: int) -> list[Category]:
    return [
        Category(r["id"], r["race_id"], r["name"], r["gender"], r["min_age"], r["max_age"], r["sort_order"])
        for r in conn.execute("SELECT * FROM category WHERE race_id=? ORDER BY sort_order, id", (race_id,))
    ]


def add_category(
    conn: sqlite3.Connection, race_id: int, name: str, gender: str | None, min_age: int | None, max_age: int | None
) -> int:
    order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM category WHERE race_id=?", (race_id,)).fetchone()[0]
    return conn.execute(
        "INSERT INTO category(race_id, name, gender, min_age, max_age, sort_order) VALUES (?,?,?,?,?,?)",
        (race_id, name.strip(), gender, min_age, max_age, order),
    ).lastrowid


def start_race(conn: sqlite3.Connection, race_id: int, operator: str, ts_ms: int | None = None) -> int:
    race = get_race(conn, race_id)
    if race.status != "setup":
        raise ValueError("La carrera ya fue iniciada.")
    ts = ts_ms if ts_ms is not None else now_ms()
    with tx(conn):
        conn.execute("UPDATE race SET status='running', gun_time_ms=? WHERE id=?", (ts, race_id))
        audit(conn, operator, "start_race", "race", race_id, str(ts))
    return ts


def close_race(conn: sqlite3.Connection, race_id: int, operator: str) -> None:
    race = get_race(conn, race_id)
    if race.status != "running":
        raise ValueError("Solo se puede cerrar una carrera en curso.")
    with tx(conn):
        conn.execute("UPDATE race SET status='closed' WHERE id=?", (race_id,))
        audit(conn, operator, "close_race", "race", race_id)


def reopen_race(conn: sqlite3.Connection, race_id: int, operator: str) -> None:
    race = get_race(conn, race_id)
    if race.status != "closed":
        raise ValueError("La carrera no está cerrada.")
    with tx(conn):
        conn.execute("UPDATE race SET status='running' WHERE id=?", (race_id,))
        audit(conn, operator, "reopen_race", "race", race_id)


# ---------------------------------------------------------------- participantes e inscripciones
def next_free_bib(conn: sqlite3.Connection, race_id: int, start: int = 1) -> int:
    row = conn.execute("SELECT MAX(bib) FROM registration WHERE race_id=?", (race_id,)).fetchone()
    return max(start, (row[0] or 0) + 1)


def _clean_participant(data: dict) -> dict:
    out = {k: (str(data.get(k) or "").strip()) for k in PARTICIPANT_FIELDS}
    if not out["first_name"] and not out["last_name"]:
        raise ValueError("Falta el nombre del participante.")
    if not out["first_name"]:
        out["first_name"], out["last_name"] = out["last_name"], ""
    out["gender"] = cats.normalize_gender(out["gender"])
    bd = cats.parse_date(out["birth_date"])
    out["birth_date"] = bd.isoformat() if bd else None
    out["document"] = out["document"] or None
    return out


def find_participant_by_document(conn: sqlite3.Connection, document: str) -> sqlite3.Row | None:
    if not document:
        return None
    return conn.execute("SELECT * FROM participant WHERE document=?", (document.strip(),)).fetchone()


def create_participant(conn: sqlite3.Connection, data: dict) -> int:
    p = _clean_participant(data)
    if p["document"] and find_participant_by_document(conn, p["document"]):
        raise ValueError(f"Ya existe un participante con documento {p['document']}.")
    cols = ", ".join(PARTICIPANT_FIELDS)
    marks = ", ".join("?" for _ in PARTICIPANT_FIELDS)
    return conn.execute(
        f"INSERT INTO participant({cols}, created_ms) VALUES ({marks}, ?)",
        [p[k] for k in PARTICIPANT_FIELDS] + [now_ms()],
    ).lastrowid


def update_participant(conn: sqlite3.Connection, participant_id: int, data: dict, operator: str = "sistema") -> None:
    p = _clean_participant(data)
    if p["document"]:
        other = find_participant_by_document(conn, p["document"])
        if other and other["id"] != participant_id:
            raise ValueError(f"El documento {p['document']} ya pertenece a otro participante.")
    sets = ", ".join(f"{k}=?" for k in PARTICIPANT_FIELDS)
    with tx(conn):
        conn.execute(f"UPDATE participant SET {sets} WHERE id=?", [p[k] for k in PARTICIPANT_FIELDS] + [participant_id])
        audit(conn, operator, "update", "participant", participant_id)


def _race_event_date(conn: sqlite3.Connection, race_id: int) -> date:
    row = conn.execute("SELECT e.event_date FROM race r JOIN event e ON e.id=r.event_id WHERE r.id=?", (race_id,)).fetchone()
    return date.fromisoformat(row["event_date"])


def suggest_category(conn: sqlite3.Connection, race_id: int, gender: str, birth_date: str | None) -> int | None:
    bd = cats.parse_date(birth_date) if birth_date else None
    age = cats.age_on(bd, _race_event_date(conn, race_id))
    cat = cats.pick_category(list_categories(conn, race_id), gender, age)
    return cat.id if cat else None


def register(
    conn: sqlite3.Connection,
    race_id: int,
    participant: dict | int,
    *,
    bib: int | None = None,
    category_id: int | None = None,
    chip: str | None = None,
    paid: bool = False,
    kit_delivered: bool = False,
    operator: str = "sistema",
    auto_category: bool = True,
) -> int:
    """Inscribe a un participante (existente o nuevo) en una carrera. Atómico."""
    get_race(conn, race_id)
    with tx(conn):
        if isinstance(participant, int):
            pid = participant
            prow = conn.execute("SELECT * FROM participant WHERE id=?", (pid,)).fetchone()
            if prow is None:
                raise ValueError(f"No existe el participante {pid}")
        else:
            cleaned = _clean_participant(participant)
            existing = find_participant_by_document(conn, cleaned["document"] or "")
            pid = existing["id"] if existing else create_participant(conn, participant)
            prow = conn.execute("SELECT * FROM participant WHERE id=?", (pid,)).fetchone()

        if conn.execute("SELECT 1 FROM registration WHERE race_id=? AND participant_id=?", (race_id, pid)).fetchone():
            raise ValueError(f"{prow['first_name']} {prow['last_name']} ya está inscrito en esta carrera.")

        if bib is None:
            bib = next_free_bib(conn, race_id)
        elif bib <= 0:
            raise ValueError("El dorsal debe ser un número positivo.")
        elif conn.execute("SELECT 1 FROM registration WHERE race_id=? AND bib=?", (race_id, bib)).fetchone():
            raise ValueError(f"El dorsal {bib} ya está asignado.")

        if category_id is None and auto_category:
            category_id = suggest_category(conn, race_id, prow["gender"], prow["birth_date"])

        reg_id = conn.execute(
            "INSERT INTO registration(race_id, participant_id, bib, category_id, paid, kit_delivered, created_ms) VALUES (?,?,?,?,?,?,?)",
            (race_id, pid, bib, category_id, int(paid), int(kit_delivered), now_ms()),
        ).lastrowid
        if chip:
            assign_chip(conn, reg_id, chip, operator)
        audit(conn, operator, "register", "registration", reg_id, f"dorsal {bib}")
    return reg_id


def update_registration(conn: sqlite3.Connection, reg_id: int, operator: str = "sistema", **fields) -> None:
    """Campos permitidos: bib, category_id, paid, kit_delivered. (El estado se cambia con set_status, que exige motivo.)"""
    allowed = {"bib", "category_id", "paid", "kit_delivered"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Campos no editables: {', '.join(sorted(bad))}")
    if not fields:
        return
    row = conn.execute("SELECT * FROM registration WHERE id=?", (reg_id,)).fetchone()
    if row is None:
        raise ValueError(f"No existe la inscripción {reg_id}")
    if "bib" in fields:
        bib = int(fields["bib"])
        if bib <= 0:
            raise ValueError("El dorsal debe ser un número positivo.")
        clash = conn.execute("SELECT 1 FROM registration WHERE race_id=? AND bib=? AND id<>?", (row["race_id"], bib, reg_id)).fetchone()
        if clash:
            raise ValueError(f"El dorsal {bib} ya está asignado.")
        fields["bib"] = bib
    for k in ("paid", "kit_delivered"):
        if k in fields:
            fields[k] = int(bool(fields[k]))
    sets = ", ".join(f"{k}=?" for k in fields)
    with tx(conn):
        conn.execute(f"UPDATE registration SET {sets} WHERE id=?", list(fields.values()) + [reg_id])
        audit(conn, operator, "update", "registration", reg_id, ", ".join(f"{k}={v}" for k, v in fields.items()))


def assign_chip(conn: sqlite3.Connection, reg_id: int, code: str, operator: str = "sistema") -> int:
    code = normalize_chip(code)
    if not code:
        raise ValueError("Código de chip vacío.")
    reg = conn.execute("SELECT * FROM registration WHERE id=?", (reg_id,)).fetchone()
    if reg is None:
        raise ValueError(f"No existe la inscripción {reg_id}")
    existing = conn.execute(
        "SELECT c.*, r.bib FROM chip c JOIN registration r ON r.id=c.registration_id WHERE c.race_id=? AND c.code=?",
        (reg["race_id"], code),
    ).fetchone()
    if existing:
        if existing["registration_id"] == reg_id:
            return existing["id"]
        raise ValueError(f"El chip {code} ya está asignado al dorsal {existing['bib']}.")
    n = conn.execute("SELECT COUNT(*) FROM chip WHERE registration_id=? AND active=1", (reg_id,)).fetchone()[0]
    if n >= MAX_CHIPS_PER_REGISTRATION:
        raise ValueError(f"El dorsal {reg['bib']} ya tiene {MAX_CHIPS_PER_REGISTRATION} chips.")
    chip_id = conn.execute(
        "INSERT INTO chip(race_id, code, registration_id) VALUES (?,?,?)", (reg["race_id"], code, reg_id)
    ).lastrowid
    audit(conn, operator, "assign_chip", "chip", chip_id, f"{code} -> dorsal {reg['bib']}")
    return chip_id


def remove_chip(conn: sqlite3.Connection, race_id: int, code: str, operator: str = "sistema") -> None:
    """Desvincula un chip (p. ej. chip perdido o mal colocado). Las lecturas pasadas se conservan sin dueño."""
    code = normalize_chip(code)
    with tx(conn):
        cur = conn.execute("DELETE FROM chip WHERE race_id=? AND code=?", (race_id, code))
        if cur.rowcount == 0:
            raise ValueError(f"El chip {code} no está asignado.")
        audit(conn, operator, "remove_chip", "chip", None, code)


def find_registration(conn: sqlite3.Connection, race_id: int, *, bib: int | None = None, chip: str | None = None, document: str | None = None) -> sqlite3.Row | None:
    if bib is not None:
        return conn.execute("SELECT * FROM registration WHERE race_id=? AND bib=?", (race_id, bib)).fetchone()
    if chip:
        return conn.execute(
            "SELECT r.* FROM chip c JOIN registration r ON r.id=c.registration_id WHERE c.race_id=? AND c.code=?",
            (race_id, normalize_chip(chip)),
        ).fetchone()
    if document:
        return conn.execute(
            "SELECT r.* FROM registration r JOIN participant p ON p.id=r.participant_id WHERE r.race_id=? AND p.document=?",
            (race_id, document.strip()),
        ).fetchone()
    return None


def list_registrations(conn: sqlite3.Connection, race_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.id AS reg_id, r.bib, r.status, r.paid, r.kit_delivered, r.category_id, p.id AS participant_id,
               p.first_name, p.last_name, p.document, p.birth_date, p.gender, p.club, p.email, p.phone,
               p.emergency_contact, p.emergency_phone, COALESCE(c.name, '') AS category_name,
               COALESCE((SELECT group_concat(code, ' ') FROM chip WHERE registration_id=r.id AND active=1), '') AS chips
        FROM registration r
        JOIN participant p ON p.id = r.participant_id
        LEFT JOIN category c ON c.id = r.category_id
        WHERE r.race_id = ?
        ORDER BY r.bib
        """,
        (race_id,),
    ).fetchall()


def list_runners(conn: sqlite3.Connection, race_id: int) -> list[RunnerInfo]:
    chips: dict[int, list[str]] = {}
    for c in conn.execute("SELECT registration_id, code FROM chip WHERE race_id=? AND active=1 ORDER BY id", (race_id,)):
        chips.setdefault(c["registration_id"], []).append(c["code"])
    out = []
    for r in list_registrations(conn, race_id):
        name = f"{r['first_name']} {r['last_name']}".strip()
        out.append(
            RunnerInfo(
                reg_id=r["reg_id"],
                bib=r["bib"],
                name=name,
                gender=r["gender"],
                category_id=r["category_id"],
                category_name=r["category_name"],
                status=r["status"],
                chip_codes=tuple(chips.get(r["reg_id"], ())),
                kit_delivered=bool(r["kit_delivered"]),
            )
        )
    return out


# ---------------------------------------------------------------- lecturas
def insert_reads(conn: sqlite3.Connection, reads: Iterable[tuple]) -> int:
    """reads: (point_id, chip_code, ts_ms, antenna, rssi, source). Idempotente: ignora lecturas ya guardadas."""
    ts_recv = now_ms()
    rows = [(pid, normalize_chip(code), ts, ant or 0, rssi, src, ts_recv) for pid, code, ts, ant, rssi, src in reads]
    if not rows:
        return 0
    before = conn.total_changes
    with tx(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO raw_read(point_id, chip_code, ts_ms, antenna, rssi, source, received_ms) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    return conn.total_changes - before


def add_manual_read(conn: sqlite3.Connection, race_id: int, point_id: int, *, bib: int | None = None, chip: str | None = None, ts_ms: int | None = None, operator: str = "manual") -> str:
    """Registra una lectura manual (como si el chip pasara por el punto). Devuelve el chip usado."""
    if bib is not None:
        reg = find_registration(conn, race_id, bib=bib)
        if reg is None:
            raise ValueError(f"No existe el dorsal {bib}.")
        row = conn.execute("SELECT code FROM chip WHERE registration_id=? AND active=1 ORDER BY id LIMIT 1", (reg["id"],)).fetchone()
        code = row["code"] if row else f"BIB{bib}"
        if row is None:
            # Sin chip: se crea el chip virtual BIB<n> para poder trazar la lectura manual
            assign_chip(conn, reg["id"], code, operator)
    elif chip:
        code = normalize_chip(chip)
    else:
        raise ValueError("Indique dorsal o chip.")
    p = conn.execute("SELECT 1 FROM timing_point WHERE id=? AND race_id=?", (point_id, race_id)).fetchone()
    if p is None:
        raise ValueError("Punto de cronometraje inválido.")
    insert_reads(conn, [(point_id, code, ts_ms if ts_ms is not None else now_ms(), None, None, f"manual:{operator}")])
    return code


def reads_after(conn: sqlite3.Connection, race_id: int, after_id: int = 0, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT rr.id, rr.ts_ms, rr.chip_code, rr.antenna, rr.rssi, rr.source, tp.name AS point_name, tp.id AS point_id,
               reg.bib AS bib
        FROM raw_read rr
        JOIN timing_point tp ON tp.id = rr.point_id
        LEFT JOIN chip ch ON ch.race_id = tp.race_id AND ch.code = rr.chip_code
        LEFT JOIN registration reg ON reg.id = ch.registration_id
        WHERE tp.race_id = ? AND rr.id > ?
        ORDER BY rr.id
        LIMIT ?
        """,
        (race_id, after_id, limit),
    ).fetchall()


def count_reads(conn: sqlite3.Connection, race_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM raw_read rr JOIN timing_point tp ON tp.id=rr.point_id WHERE tp.race_id=?", (race_id,)
    ).fetchone()[0]


def point_stats(conn: sqlite3.Connection, race_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT tp.id AS point_id, tp.name, COUNT(rr.id) AS reads, MAX(rr.ts_ms) AS last_ts
        FROM timing_point tp LEFT JOIN raw_read rr ON rr.point_id = tp.id
        WHERE tp.race_id = ? GROUP BY tp.id ORDER BY tp.order_idx
        """,
        (race_id,),
    ).fetchall()


# ---------------------------------------------------------------- correcciones (siempre con motivo y operador)
def _require(reason: str, operator: str) -> tuple[str, str]:
    if not (reason or "").strip():
        raise ValueError("Toda corrección requiere un motivo.")
    if not (operator or "").strip():
        raise ValueError("Toda corrección requiere el nombre del operador.")
    return reason.strip(), operator.strip()


def _insert_correction(conn, race_id, action, reason, operator, *, reg_id=None, point_id=None, raw_read_id=None, old=None, new=None) -> int:
    return conn.execute(
        """INSERT INTO correction(race_id, action, registration_id, point_id, raw_read_id, old_value, new_value, reason, operator, created_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (race_id, action, reg_id, point_id, raw_read_id, old, new, reason, operator, now_ms()),
    ).lastrowid


def _voided_ids(conn: sqlite3.Connection, race_id: int) -> set[int]:
    voided: set[int] = set()
    for c in conn.execute(
        "SELECT action, raw_read_id FROM correction WHERE race_id=? AND action IN ('void_read','unvoid_read') ORDER BY id", (race_id,)
    ):
        if c["action"] == "void_read":
            voided.add(c["raw_read_id"])
        else:
            voided.discard(c["raw_read_id"])
    return voided


def void_read(conn: sqlite3.Connection, raw_read_id: int, reason: str, operator: str) -> int:
    reason, operator = _require(reason, operator)
    rd = conn.execute(
        "SELECT rr.*, tp.race_id FROM raw_read rr JOIN timing_point tp ON tp.id=rr.point_id WHERE rr.id=?", (raw_read_id,)
    ).fetchone()
    if rd is None:
        raise ValueError(f"No existe la lectura {raw_read_id}.")
    if raw_read_id in _voided_ids(conn, rd["race_id"]):
        raise ValueError("La lectura ya está anulada.")
    reg = conn.execute(
        "SELECT registration_id FROM chip WHERE race_id=? AND code=?", (rd["race_id"], rd["chip_code"])
    ).fetchone()
    with tx(conn):
        return _insert_correction(
            conn, rd["race_id"], "void_read", reason, operator,
            reg_id=reg["registration_id"] if reg else None, point_id=rd["point_id"], raw_read_id=raw_read_id,
            old=f"{rd['chip_code']}@{rd['ts_ms']}",
        )


def unvoid_read(conn: sqlite3.Connection, raw_read_id: int, reason: str, operator: str) -> int:
    reason, operator = _require(reason, operator)
    rd = conn.execute(
        "SELECT rr.*, tp.race_id FROM raw_read rr JOIN timing_point tp ON tp.id=rr.point_id WHERE rr.id=?", (raw_read_id,)
    ).fetchone()
    if rd is None:
        raise ValueError(f"No existe la lectura {raw_read_id}.")
    if raw_read_id not in _voided_ids(conn, rd["race_id"]):
        raise ValueError("La lectura no está anulada.")
    with tx(conn):
        return _insert_correction(conn, rd["race_id"], "unvoid_read", reason, operator, point_id=rd["point_id"], raw_read_id=raw_read_id)


def set_pass(conn: sqlite3.Connection, reg_id: int, point_id: int, ts_ms: int, reason: str, operator: str) -> int:
    """Fija el tiempo de paso de un corredor en un punto (agrega un tiempo perdido o reemplaza el leído)."""
    reason, operator = _require(reason, operator)
    reg = conn.execute("SELECT * FROM registration WHERE id=?", (reg_id,)).fetchone()
    if reg is None:
        raise ValueError(f"No existe la inscripción {reg_id}.")
    pt = conn.execute("SELECT * FROM timing_point WHERE id=? AND race_id=?", (point_id, reg["race_id"])).fetchone()
    if pt is None:
        raise ValueError("El punto no pertenece a la carrera del corredor.")
    voided = _voided_ids(conn, reg["race_id"])
    old_ts = None
    for r in conn.execute(
        """SELECT rr.id, rr.ts_ms FROM raw_read rr JOIN chip c ON c.code=rr.chip_code AND c.race_id=?
           WHERE c.registration_id=? AND rr.point_id=? ORDER BY rr.ts_ms""",
        (reg["race_id"], reg_id, point_id),
    ):
        if r["id"] not in voided:
            old_ts = r["ts_ms"]
            break
    prev_override = conn.execute(
        "SELECT new_value FROM correction WHERE race_id=? AND registration_id=? AND point_id=? AND action IN ('add_pass','set_pass') ORDER BY id DESC LIMIT 1",
        (reg["race_id"], reg_id, point_id),
    ).fetchone()
    if prev_override:
        old_ts = int(prev_override["new_value"])
    action = "set_pass" if old_ts is not None else "add_pass"
    with tx(conn):
        return _insert_correction(
            conn, reg["race_id"], action, reason, operator, reg_id=reg_id, point_id=point_id,
            old=str(old_ts) if old_ts is not None else None, new=str(int(ts_ms)),
        )


def set_status(conn: sqlite3.Connection, reg_id: int, new_status: str, reason: str, operator: str) -> int:
    reason, operator = _require(reason, operator)
    if new_status not in VALID_STATUS:
        raise ValueError(f"Estado inválido: {new_status}")
    reg = conn.execute("SELECT * FROM registration WHERE id=?", (reg_id,)).fetchone()
    if reg is None:
        raise ValueError(f"No existe la inscripción {reg_id}.")
    with tx(conn):
        conn.execute("UPDATE registration SET status=? WHERE id=?", (new_status, reg_id))
        return _insert_correction(conn, reg["race_id"], "set_status", reason, operator, reg_id=reg_id, old=reg["status"], new=new_status)


def set_gun(conn: sqlite3.Connection, race_id: int, ts_ms: int, reason: str, operator: str) -> int:
    reason, operator = _require(reason, operator)
    race = get_race(conn, race_id)
    with tx(conn):
        conn.execute("UPDATE race SET gun_time_ms=? WHERE id=?", (int(ts_ms), race_id))
        return _insert_correction(
            conn, race_id, "set_gun", reason, operator,
            old=str(race.gun_time_ms) if race.gun_time_ms is not None else None, new=str(int(ts_ms)),
        )


def list_corrections(conn: sqlite3.Connection, race_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, reg.bib AS bib, tp.name AS point_name
        FROM correction c
        LEFT JOIN registration reg ON reg.id = c.registration_id
        LEFT JOIN timing_point tp ON tp.id = c.point_id
        WHERE c.race_id = ? ORDER BY c.id DESC
        """,
        (race_id,),
    ).fetchall()


# ---------------------------------------------------------------- cálculo
def compute_race(conn: sqlite3.Connection, race_id: int) -> Computation:
    race = get_race(conn, race_id)
    points = list_points(conn, race_id)
    runners = list_runners(conn, race_id)
    reads = [
        ReadRec(r["id"], r["point_id"], r["chip_code"], r["ts_ms"])
        for r in conn.execute(
            "SELECT rr.id, rr.point_id, rr.chip_code, rr.ts_ms FROM raw_read rr JOIN timing_point tp ON tp.id=rr.point_id WHERE tp.race_id=?",
            (race_id,),
        )
    ]
    voided: set[int] = set()
    overrides: list[Override] = []
    for c in conn.execute(
        "SELECT * FROM correction WHERE race_id=? AND action IN ('void_read','unvoid_read','add_pass','set_pass') ORDER BY id", (race_id,)
    ):
        if c["action"] == "void_read":
            voided.add(c["raw_read_id"])
        elif c["action"] == "unvoid_read":
            voided.discard(c["raw_read_id"])
        else:
            overrides.append(Override(c["id"], c["registration_id"], c["point_id"], int(c["new_value"])))
    cfg = RaceConfig(race.gun_time_ms, race.award_mode, race.distance_m, race.dedupe_window_ms, race.min_segment_ms, race.status)
    return compute(cfg, points, runners, reads, voided, overrides)
