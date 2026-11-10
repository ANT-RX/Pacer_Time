"""Esquema SQLite versionado (PRAGMA user_version). Añadir migraciones al final de MIGRATIONS; nunca editar las anteriores."""

MIGRATION_1 = """
CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    event_date TEXT NOT NULL,           -- YYYY-MM-DD
    city TEXT NOT NULL DEFAULT ''
);

CREATE TABLE race (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id),
    name TEXT NOT NULL,
    distance_m INTEGER NOT NULL CHECK (distance_m >= 0),
    status TEXT NOT NULL DEFAULT 'setup' CHECK (status IN ('setup','running','closed')),
    gun_time_ms INTEGER,                -- instante del disparo de salida
    award_mode TEXT NOT NULL DEFAULT 'hybrid' CHECK (award_mode IN ('gun','net','hybrid')),
    dedupe_window_ms INTEGER NOT NULL DEFAULT 5000,
    min_segment_ms INTEGER NOT NULL DEFAULT 30000
);

CREATE TABLE category (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES race(id),
    name TEXT NOT NULL,
    gender TEXT CHECK (gender IN ('M','F','X') OR gender IS NULL),
    min_age INTEGER,
    max_age INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (race_id, name)
);

CREATE TABLE participant (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL DEFAULT '',
    document TEXT,                      -- identificación (CURP, DNI, etc.)
    birth_date TEXT,                    -- YYYY-MM-DD
    gender TEXT NOT NULL DEFAULT 'X' CHECK (gender IN ('M','F','X')),
    club TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    emergency_contact TEXT NOT NULL DEFAULT '',
    emergency_phone TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_participant_document ON participant(document) WHERE document IS NOT NULL AND document <> '';

CREATE TABLE registration (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES race(id),
    participant_id INTEGER NOT NULL REFERENCES participant(id),
    bib INTEGER NOT NULL CHECK (bib > 0),
    category_id INTEGER REFERENCES category(id),
    status TEXT NOT NULL DEFAULT 'REG' CHECK (status IN ('REG','DNS','DNF','DQ')),
    paid INTEGER NOT NULL DEFAULT 0,
    kit_delivered INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL,
    UNIQUE (race_id, bib),
    UNIQUE (race_id, participant_id)
);

CREATE TABLE chip (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES race(id),
    code TEXT NOT NULL,                 -- normalizado: MAYÚSCULAS, sin espacios
    registration_id INTEGER NOT NULL REFERENCES registration(id),
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (race_id, code)
);

CREATE TABLE timing_point (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES race(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start','split','finish')),
    order_idx INTEGER NOT NULL,
    distance_m INTEGER NOT NULL DEFAULT 0,
    UNIQUE (race_id, order_idx)
);

-- Lecturas crudas: APPEND-ONLY (triggers de abajo). Nunca se corrigen; se anulan con 'correction'.
CREATE TABLE raw_read (
    id INTEGER PRIMARY KEY,
    point_id INTEGER NOT NULL REFERENCES timing_point(id),
    chip_code TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,             -- instante de la lectura
    antenna INTEGER NOT NULL DEFAULT 0,   -- 0 = desconocida (NULL rompería el UNIQUE)
    rssi REAL,
    source TEXT NOT NULL DEFAULT '',
    received_ms INTEGER NOT NULL,       -- cuándo la recibió/guardó el sistema
    UNIQUE (source, point_id, chip_code, ts_ms, antenna)
);
CREATE INDEX ix_raw_read_point ON raw_read(point_id, ts_ms);
CREATE INDEX ix_raw_read_chip ON raw_read(chip_code);

-- Correcciones: APPEND-ONLY, con operador y motivo obligatorios.
CREATE TABLE correction (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES race(id),
    action TEXT NOT NULL CHECK (action IN ('void_read','unvoid_read','add_pass','set_pass','set_status','set_gun')),
    registration_id INTEGER REFERENCES registration(id),
    point_id INTEGER REFERENCES timing_point(id),
    raw_read_id INTEGER REFERENCES raw_read(id),
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    operator TEXT NOT NULL CHECK (length(trim(operator)) > 0),
    created_ms INTEGER NOT NULL
);
CREATE INDEX ix_correction_race ON correction(race_id, id);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);

-- Estado/latido de cada proceso de captura (la UI lo consulta; también le pide detenerse).
CREATE TABLE reader_status (
    source TEXT PRIMARY KEY,
    race_id INTEGER,
    state TEXT NOT NULL DEFAULT 'starting',    -- starting | running | stopped | finished | error
    last_read_ms INTEGER,
    total_reads INTEGER NOT NULL DEFAULT 0,
    heartbeat_ms INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TRIGGER raw_read_no_update BEFORE UPDATE ON raw_read
BEGIN SELECT RAISE(ABORT, 'raw_read es inmutable'); END;
CREATE TRIGGER raw_read_no_delete BEFORE DELETE ON raw_read
BEGIN SELECT RAISE(ABORT, 'raw_read es inmutable'); END;
CREATE TRIGGER correction_no_update BEFORE UPDATE ON correction
BEGIN SELECT RAISE(ABORT, 'correction es inmutable'); END;
CREATE TRIGGER correction_no_delete BEFORE DELETE ON correction
BEGIN SELECT RAISE(ABORT, 'correction es inmutable'); END;
"""

MIGRATIONS = [MIGRATION_1]
SCHEMA_VERSION = len(MIGRATIONS)
