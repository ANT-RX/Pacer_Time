"""Utilidades de tiempo. Todos los instantes se guardan como enteros: milisegundos desde epoch (UTC)."""

from __future__ import annotations

import re
import time
from datetime import datetime


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def format_duration(ms: int | None, decimals: int = 1) -> str:
    """H:MM:SS(.d). Se TRUNCA (no se redondea), como en los resultados de carreras."""
    if ms is None:
        return ""
    sign = "-" if ms < 0 else ""
    ms = abs(int(ms))
    if decimals <= 0:
        total_s = ms // 1000
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        return f"{sign}{h}:{m:02d}:{s:02d}"
    tenths = ms // 100
    h, rem = divmod(tenths, 36000)
    m, rem = divmod(rem, 600)
    s, t = divmod(rem, 10)
    return f"{sign}{h}:{m:02d}:{s:02d}.{t}"


def format_pace(ms_per_km: float | None) -> str:
    """m:ss por km (truncado)."""
    if ms_per_km is None:
        return ""
    total_s = int(ms_per_km // 1000)
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}"


def format_clock(ms: int | None, with_millis: bool = True) -> str:
    """Hora local del día HH:MM:SS(.mmm)."""
    if ms is None:
        return ""
    dt = datetime.fromtimestamp(ms / 1000)
    base = dt.strftime("%H:%M:%S")
    return f"{base}.{dt.microsecond // 1000:03d}" if with_millis else base


_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,3}))?)?\s*$")


def parse_clock(text: str, ref_ms: int) -> int:
    """Interpreta 'HH:MM[:SS[.mmm]]' como hora local en el día de ref_ms."""
    m = _CLOCK_RE.match(text or "")
    if not m:
        raise ValueError(f"Hora inválida: {text!r} (use HH:MM:SS o HH:MM:SS.mmm)")
    hh, mm, ss, frac = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if hh > 23 or mm > 59 or ss > 59:
        raise ValueError(f"Hora fuera de rango: {text!r}")
    millis = int((frac or "0").ljust(3, "0")) if frac else 0
    ref = datetime.fromtimestamp(ref_ms / 1000)
    dt = ref.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    return int(dt.timestamp() * 1000) + millis
