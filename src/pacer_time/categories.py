"""Categorías: edad el día del evento y selección automática."""

from __future__ import annotations

import unicodedata
from datetime import date

from .models import Category

_M = {"m", "h", "masculino", "hombre", "varonil", "male", "varon", "man"}
_F = {"f", "mujer", "femenino", "femenil", "female", "woman"}


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize_gender(value: str | None) -> str:
    v = _strip_accents((value or "").strip().lower())
    if v in _M:
        return "M"
    if v in _F:
        return "F"
    return "X"


def parse_date(text: str | None) -> date | None:
    """Acepta YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, YYYY/MM/DD."""
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Fecha inválida: {text!r} (use AAAA-MM-DD o DD/MM/AAAA)")


def age_on(birth: date | None, on: date) -> int | None:
    if birth is None:
        return None
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def pick_category(categories: list[Category], gender: str, age: int | None) -> Category | None:
    """Primera categoría (por sort_order) que coincide. Sin edad solo aplican categorías sin límite de edad."""
    for cat in sorted(categories, key=lambda c: (c.sort_order, c.id)):
        if cat.gender is not None and cat.gender != gender:
            continue
        if age is None:
            if cat.min_age is None and cat.max_age is None:
                return cat
            continue
        if cat.min_age is not None and age < cat.min_age:
            continue
        if cat.max_age is not None and age > cat.max_age:
            continue
        return cat
    return None


DEFAULT_BRACKETS = [
    ("Juvenil", 0, 17),
    ("18-29", 18, 29),
    ("30-39", 30, 39),
    ("40-49", 40, 49),
    ("50-59", 50, 59),
    ("60+", 60, None),
]


def default_category_specs() -> list[tuple[str, str | None, int | None, int | None]]:
    """(nombre, género, edad_min, edad_max) para la plantilla estándar."""
    specs = []
    for gender, label in (("M", "Varonil"), ("F", "Femenil")):
        for bracket, lo, hi in DEFAULT_BRACKETS:
            name = f"{label} {bracket}" if bracket != "Juvenil" else f"{label} Juvenil"
            specs.append((name, gender, lo, hi))
    return specs
