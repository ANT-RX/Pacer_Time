"""Exportación de resultados: CSV (Excel) y HTML autocontenido para publicar/imprimir."""

from __future__ import annotations

import csv
import html
from pathlib import Path

from .models import STATUS_LABELS, Computation, Race, TimingPoint
from .timeutil import format_duration, format_pace

GENDER_SHORT = {"M": "M", "F": "F", "X": "-"}


def result_table(comp: Computation, race: Race, points: list[TimingPoint]) -> tuple[list[str], list[list[str]]]:
    split_pts = [p for p in points if p.kind == "split"]
    headers = ["Pos", "Dorsal", "Nombre", "Género", "Categoría", "Pos género", "Pos cat.", "Pistola", "Neto", "Ritmo/km"]
    headers += [p.name for p in split_pts] + ["Estado"]
    rows = []
    for r in comp.results:
        row = [
            str(r.position or ""),
            str(r.bib),
            r.name,
            GENDER_SHORT.get(r.gender, "-"),
            r.category_name,
            str(r.gender_pos or ""),
            str(r.category_pos or ""),
            format_duration(r.gun_ms),
            format_duration(r.net_ms),
            format_pace(r.pace_ms_km),
        ]
        row += [format_duration(r.splits_ms.get(p.id)) for p in split_pts]
        row.append(STATUS_LABELS.get(r.status, r.status) + (" *" if "manual" in r.flags else ""))
        rows.append(row)
    return headers, rows


def write_results_csv(path: str | Path, comp: Computation, race: Race, points: list[TimingPoint]) -> Path:
    path = Path(path)
    headers, rows = result_table(comp, race, points)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)
    return path


def write_results_html(path: str | Path, comp: Computation, race: Race, points: list[TimingPoint], title: str = "") -> Path:
    path = Path(path)
    headers, rows = result_table(comp, race, points)
    esc = html.escape
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "\n".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    doc = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{esc(title or race.name)}</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:#1b1f23}}
h1{{font-size:1.4rem;margin:0 0 4px}} p.sub{{color:#586069;margin:0 0 16px}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}
th,td{{padding:6px 10px;border-bottom:1px solid #e1e4e8;text-align:left;white-space:nowrap}}
th{{background:#f6f8fa;position:sticky;top:0}} tr:nth-child(even) td{{background:#fafbfc}}
</style></head><body>
<h1>{esc(title or race.name)}</h1>
<p class="sub">{esc(race.name)} · {len(rows)} participantes · * = tiempo con corrección manual</p>
<table><thead><tr>{head}</tr></thead><tbody>
{body}
</tbody></table></body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path
