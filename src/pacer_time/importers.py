"""Importación desde CSV: inscripciones (con alias de columnas en español/inglés) y lecturas de decodificadores."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import categories as cats
from . import repo
from .db import tx
from .timeutil import parse_clock

ALIASES = {
    "first_name": ["nombre", "nombres", "first_name", "firstname", "name"],
    "last_name": ["apellido", "apellidos", "last_name", "lastname", "surname"],
    "document": ["documento", "doc", "curp", "dni", "id", "identificacion", "cedula", "document"],
    "birth_date": ["fecha_nacimiento", "fecha_de_nacimiento", "nacimiento", "birth_date", "birthdate", "dob"],
    "gender": ["genero", "sexo", "gender", "sex", "rama"],
    "club": ["club", "equipo", "team"],
    "email": ["email", "correo", "e_mail", "correo_electronico"],
    "phone": ["telefono", "tel", "celular", "phone"],
    "emergency_contact": ["contacto_emergencia", "contacto_de_emergencia", "emergency_contact"],
    "emergency_phone": ["telefono_emergencia", "tel_emergencia", "emergency_phone"],
    "bib": ["dorsal", "numero", "bib", "no", "num"],
    "chip": ["chip", "tag", "epc"],
    "category": ["categoria", "category"],
    "paid": ["pagado", "paid", "pago"],
    "kit_delivered": ["kit", "kit_entregado", "kit_delivered"],
}
_ALIAS_LOOKUP = {alias: field_ for field_, aliases in ALIASES.items() for alias in aliases}
_TRUE = {"1", "si", "sí", "s", "x", "true", "yes", "y", "pagado"}


def _norm_header(h: str) -> str:
    h = "".join(c for c in unicodedata.normalize("NFD", h or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


@dataclass
class ImportReport:
    created: int = 0
    duplicates: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (línea, mensaje)
    unknown_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"{self.created} inscritos", f"{self.duplicates} duplicados omitidos", f"{len(self.errors)} con error"]
        return ", ".join(parts)


def _open_text(source: str | Path | TextIO) -> tuple[TextIO, bool]:
    if hasattr(source, "read"):
        return source, False  # type: ignore[return-value]
    return open(source, newline="", encoding="utf-8-sig"), True


def _dict_reader(fh: TextIO) -> csv.DictReader:
    sample = fh.read(4096)
    fh.seek(0)
    first_line = sample.splitlines()[0] if sample else ""
    delimiter = max((",", ";", "\t"), key=first_line.count)
    return csv.DictReader(fh, delimiter=delimiter)


def import_participants_csv(
    conn,
    race_id: int,
    source: str | Path | TextIO,
    operator: str = "importación",
    *,
    mark_kit: bool = False,
) -> ImportReport:
    """Crea participantes + inscripciones. Cada fila es atómica: una fila mala no afecta a las demás."""
    report = ImportReport()
    categories = {c.name.strip().lower(): c for c in repo.list_categories(conn, race_id)}
    fh, close = _open_text(source)
    try:
        reader = _dict_reader(fh)
        mapping: dict[str, str] = {}
        for col in reader.fieldnames or []:
            f = _ALIAS_LOOKUP.get(_norm_header(col))
            if f and f not in mapping.values():
                mapping[col] = f
            else:
                report.unknown_columns.append(col)
        if "first_name" not in mapping.values() and "last_name" not in mapping.values():
            report.errors.append((1, "No se encontró columna de nombre (nombre/apellido)."))
            return report

        with tx(conn):
            for line_no, raw in enumerate(reader, start=2):
                row = {mapping[k]: (v or "").strip() for k, v in raw.items() if k in mapping and v is not None}
                if not any(row.values()):
                    continue
                try:
                    with tx(conn):
                        outcome = _import_row(conn, race_id, row, categories, operator, mark_kit)
                    if outcome == "duplicate":
                        report.duplicates += 1
                    else:
                        report.created += 1
                except ValueError as exc:
                    report.errors.append((line_no, str(exc)))
    finally:
        if close:
            fh.close()
    return report


def _import_row(conn, race_id: int, row: dict, categories: dict, operator: str, mark_kit: bool) -> str:
    participant = {k: row.get(k, "") for k in repo.PARTICIPANT_FIELDS}
    cleaned = repo._clean_participant(participant)  # valida nombre/fecha
    doc = cleaned["document"]

    # Duplicado dentro de la carrera
    if doc:
        existing = repo.find_participant_by_document(conn, doc)
        if existing and conn.execute(
            "SELECT 1 FROM registration WHERE race_id=? AND participant_id=?", (race_id, existing["id"])
        ).fetchone():
            return "duplicate"
    else:
        dup = conn.execute(
            """SELECT 1 FROM registration r JOIN participant p ON p.id=r.participant_id
               WHERE r.race_id=? AND lower(p.first_name)=lower(?) AND lower(p.last_name)=lower(?) AND COALESCE(p.birth_date,'')=COALESCE(?, '')""",
            (race_id, cleaned["first_name"], cleaned["last_name"], cleaned["birth_date"]),
        ).fetchone()
        if dup:
            return "duplicate"

    bib = None
    if row.get("bib"):
        try:
            bib = int(float(row["bib"]))
        except ValueError:
            raise ValueError(f"Dorsal inválido: {row['bib']!r}") from None

    category_id = None
    if row.get("category"):
        cat = categories.get(row["category"].strip().lower())
        if cat is None:
            raise ValueError(f"Categoría desconocida: {row['category']!r}")
        category_id = cat.id

    repo.register(
        conn,
        race_id,
        participant,
        bib=bib,
        category_id=category_id,
        chip=row.get("chip") or None,
        paid=row.get("paid", "").strip().lower() in _TRUE,
        kit_delivered=mark_kit or row.get("kit_delivered", "").strip().lower() in _TRUE,
        operator=operator,
    )
    return "created"


# ---------------------------------------------------------------- lecturas de decodificadores
def _parse_read_time(text: str, ref_ms: int) -> int:
    t = text.strip()
    if re.fullmatch(r"\d{10,13}(\.\d+)?", t):  # epoch s o ms
        v = float(t)
        return int(v if v > 1e11 else v * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(t, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return parse_clock(t, ref_ms)


def import_reads_csv(conn, race_id: int, point_id: int, source: str | Path | TextIO, ref_ms: int, source_name: str = "archivo") -> tuple[int, list[tuple[int, str]]]:
    """Importa lecturas (columnas: chip, hora). La hora puede ser epoch, fecha-hora ISO o HH:MM:SS(.mmm) del día de ref_ms.
    Devuelve (lecturas_nuevas, errores). Idempotente: reimportar el mismo archivo no duplica nada."""
    if not conn.execute("SELECT 1 FROM timing_point WHERE id=? AND race_id=?", (point_id, race_id)).fetchone():
        raise ValueError("El punto no pertenece a la carrera.")
    fh, close = _open_text(source)
    errors: list[tuple[int, str]] = []
    batch: list[tuple] = []
    try:
        reader = _dict_reader(fh)
        cols = {_norm_header(c): c for c in (reader.fieldnames or [])}
        chip_col = next((cols[k] for k in ("chip", "tag", "epc", "transponder") if k in cols), None)
        time_col = next((cols[k] for k in ("hora", "time", "timestamp", "tiempo", "fecha_hora", "datetime") if k in cols), None)
        if not chip_col or not time_col:
            raise ValueError("El archivo debe tener columnas de chip y hora (chip/tag/epc y hora/time/timestamp).")
        for line_no, raw in enumerate(reader, start=2):
            code = (raw.get(chip_col) or "").strip()
            if not code:
                continue
            try:
                batch.append((point_id, code, _parse_read_time(raw.get(time_col) or "", ref_ms), None, None, source_name))
            except ValueError as exc:
                errors.append((line_no, str(exc)))
    finally:
        if close:
            fh.close()
    return repo.insert_reads(conn, batch), errors


def parse_csv_text(text: str) -> TextIO:
    return io.StringIO(text)
