"""Estructuras de datos puras (sin dependencias de BD ni de Qt)."""

from __future__ import annotations

from dataclasses import dataclass, field

AWARD_MODES = ("gun", "net", "hybrid")
AWARD_LABELS = {
    "gun": "Todo por tiempo de pistola",
    "net": "Todo por tiempo neto (chip)",
    "hybrid": "Híbrido: general por pistola, categorías por neto",
}
STATUS_LABELS = {
    "FIN": "Finalizó",
    "RUN": "En carrera",
    "NT": "Sin tiempo",
    "PEND": "Pendiente",
    "DNS": "No salió (DNS)",
    "DNF": "No terminó (DNF)",
    "DQ": "Descalificado (DQ)",
}
GENDER_LABELS = {"M": "Varonil", "F": "Femenil", "X": "Otro/NE"}


@dataclass(frozen=True)
class Race:
    id: int
    event_id: int
    name: str
    distance_m: int
    status: str
    gun_time_ms: int | None
    award_mode: str
    dedupe_window_ms: int
    min_segment_ms: int


@dataclass(frozen=True)
class TimingPoint:
    id: int
    race_id: int
    name: str
    kind: str  # start | split | finish
    order_idx: int
    distance_m: int


@dataclass(frozen=True)
class Category:
    id: int
    race_id: int
    name: str
    gender: str | None
    min_age: int | None
    max_age: int | None
    sort_order: int


@dataclass(frozen=True)
class RunnerInfo:
    reg_id: int
    bib: int
    name: str
    gender: str
    category_id: int | None
    category_name: str
    status: str  # REG | DNS | DNF | DQ
    chip_codes: tuple[str, ...] = ()
    kit_delivered: bool = False


@dataclass(frozen=True)
class ReadRec:
    id: int
    point_id: int
    chip_code: str
    ts_ms: int


@dataclass(frozen=True)
class Override:
    """Tiempo fijado manualmente (add_pass / set_pass) para un corredor en un punto."""

    id: int
    reg_id: int
    point_id: int
    ts_ms: int


@dataclass(frozen=True)
class RaceConfig:
    gun_time_ms: int | None
    award_mode: str
    distance_m: int
    dedupe_window_ms: int
    min_segment_ms: int
    status: str  # setup | running | closed


@dataclass(frozen=True)
class Issue:
    severity: str  # error | warning | info
    code: str
    message: str
    reg_id: int | None = None
    bib: int | None = None
    point_id: int | None = None


@dataclass
class ResultRow:
    reg_id: int
    bib: int
    name: str
    gender: str
    category_name: str
    status: str  # FIN RUN NT PEND DNS DNF DQ
    position: int | None = None
    gender_pos: int | None = None
    category_pos: int | None = None
    gun_ms: int | None = None
    net_ms: int | None = None
    pace_ms_km: float | None = None
    passes: dict[int, int] = field(default_factory=dict)  # point_id -> ts_ms
    splits_ms: dict[int, int] = field(default_factory=dict)  # point_id -> tiempo acumulado desde la salida (neto)
    flags: tuple[str, ...] = ()


@dataclass
class Computation:
    results: list[ResultRow]
    issues: list[Issue]
    unknown_chips: dict[str, int]
    duplicate_reads: int = 0
    total_reads: int = 0
