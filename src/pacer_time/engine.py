"""Motor de cálculo: lecturas crudas -> pasos depurados -> resultados + anomalías.

Función PURA (sin BD): mismas entradas => mismas salidas, sin importar el orden de las lecturas.
Las lecturas crudas nunca se modifican; las correcciones entran como `voided` (lecturas anuladas)
y `overrides` (tiempos fijados a mano).

Reglas:
- Un chip -> un dorsal (tabla de chips). Chips desconocidos se cuentan y se reportan.
- Por (corredor, punto): las lecturas separadas por <= dedupe_window_ms forman un grupo; el primer grupo
  es el paso (se toma su PRIMERA lectura). Grupos posteriores se reportan como pasos extra (informativo).
- Pasos intermedios/meta anteriores al disparo se descartan (error). En la salida se permiten (calentamiento).
- Tiempo de pistola (gun) = meta - disparo. Tiempo neto (net) = meta - max(paso por salida, disparo).
  Sin lectura de salida: net = gun (y se marca 'net_fallback').
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .models import (
    Computation,
    Issue,
    Override,
    RaceConfig,
    ReadRec,
    ResultRow,
    RunnerInfo,
    TimingPoint,
)

_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
_STATUS_ORDER = {"FIN": 0, "RUN": 1, "NT": 2, "PEND": 3, "DNF": 4, "DQ": 5, "DNS": 6}


def _clusters(sorted_ts: list[int], window_ms: int) -> list[list[int]]:
    groups: list[list[int]] = [[sorted_ts[0]]]
    for t in sorted_ts[1:]:
        if t - groups[-1][-1] <= window_ms:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


def _rank(rows: list[ResultRow], key) -> dict[int, int]:
    """Ranking de competencia (1,1,3). Devuelve reg_id -> posición."""
    ordered = sorted(rows, key=lambda r: (key(r), r.bib))
    out: dict[int, int] = {}
    prev_key = None
    prev_pos = 0
    for i, r in enumerate(ordered, start=1):
        k = key(r)
        pos = prev_pos if k == prev_key else i
        out[r.reg_id] = pos
        prev_key, prev_pos = k, pos
    return out


def compute(
    config: RaceConfig,
    points: list[TimingPoint],
    runners: list[RunnerInfo],
    reads: list[ReadRec],
    voided: set[int] | frozenset[int] = frozenset(),
    overrides: list[Override] = (),  # type: ignore[assignment]
) -> Computation:
    points = sorted(points, key=lambda p: p.order_idx)
    point_by_id = {p.id: p for p in points}
    start_pt = next((p for p in points if p.kind == "start"), None)
    finish_pt = next((p for p in reversed(points) if p.kind == "finish"), None)
    runner_by_reg = {r.reg_id: r for r in runners}
    chip_map = {code: r for r in runners for code in r.chip_codes}
    gun = config.gun_time_ms

    issues: list[Issue] = []
    unknown: Counter[str] = Counter()
    by_key: dict[tuple[int, int], list[int]] = defaultdict(list)
    total_reads = 0

    for rd in reads:
        if rd.id in voided or rd.point_id not in point_by_id:
            continue
        total_reads += 1
        runner = chip_map.get(rd.chip_code)
        if runner is None:
            unknown[rd.chip_code] += 1
            continue
        by_key[(runner.reg_id, rd.point_id)].append(rd.ts_ms)

    passes: dict[int, dict[int, int]] = defaultdict(dict)
    manual: dict[int, set[int]] = defaultdict(set)
    duplicate_reads = 0

    for (reg_id, pid), tss in sorted(by_key.items()):
        tss.sort()
        groups = _clusters(tss, config.dedupe_window_ms)
        duplicate_reads += len(groups[0]) - 1
        passes[reg_id][pid] = groups[0][0]
        if len(groups) > 1:
            r = runner_by_reg[reg_id]
            issues.append(
                Issue(
                    "info",
                    "extra_pass",
                    f"Dorsal {r.bib}: {len(groups) - 1} paso(s) adicional(es) por «{point_by_id[pid].name}» "
                    f"(se usa el primero).",
                    reg_id,
                    r.bib,
                    pid,
                )
            )

    for ov in sorted(overrides, key=lambda o: o.id):
        if ov.reg_id in runner_by_reg and ov.point_id in point_by_id:
            passes[ov.reg_id][ov.point_id] = ov.ts_ms
            manual[ov.reg_id].add(ov.point_id)

    rows: list[ResultRow] = []
    for r in sorted(runners, key=lambda x: x.bib):
        p = passes.get(r.reg_id, {})
        flags: list[str] = []

        # Pasos anteriores al disparo (salvo salida y tiempos manuales)
        if gun is not None:
            for pid in list(p):
                pt = point_by_id[pid]
                if pt.kind != "start" and p[pid] < gun and pid not in manual[r.reg_id]:
                    issues.append(
                        Issue(
                            "error",
                            "before_start",
                            f"Dorsal {r.bib}: lectura en «{pt.name}» anterior al disparo (descartada).",
                            r.reg_id,
                            r.bib,
                            pid,
                        )
                    )
                    del p[pid]
        if manual[r.reg_id]:
            flags.append("manual")

        start_ts = p.get(start_pt.id) if start_pt else None
        finish_ts = p.get(finish_pt.id) if finish_pt else None
        start_ref = start_ts
        if start_ts is not None and gun is not None and start_ts < gun:
            start_ref = gun

        gun_ms = net_ms = None
        if finish_ts is not None:
            if gun is not None:
                gun_ms = finish_ts - gun
            if start_ref is not None:
                net_ms = finish_ts - start_ref
            elif gun_ms is not None:
                net_ms = gun_ms
                if start_pt is not None:
                    flags.append("net_fallback")
                    if r.status == "REG":
                        issues.append(
                            Issue(
                                "warning",
                                "no_start",
                                f"Dorsal {r.bib}: sin lectura de salida; neto = pistola.",
                                r.reg_id,
                                r.bib,
                                start_pt.id,
                            )
                        )
            if (gun_ms is not None and gun_ms < 0) or (net_ms is not None and net_ms < 0):
                issues.append(
                    Issue("error", "negative_time", f"Dorsal {r.bib}: tiempo negativo; revise salida/meta/disparo.", r.reg_id, r.bib)
                )
                gun_ms = net_ms = None
            elif gun_ms is None and net_ms is None:
                issues.append(
                    Issue("error", "no_time_reference", f"Dorsal {r.bib}: hay meta pero no hay disparo ni salida.", r.reg_id, r.bib)
                )

        valid_finish = finish_ts is not None and (gun_ms is not None or net_ms is not None)

        if r.status in ("DNS", "DNF", "DQ"):
            status = r.status
            if finish_ts is not None:
                issues.append(
                    Issue(
                        "warning",
                        "status_conflict",
                        f"Dorsal {r.bib}: marcado {r.status} pero tiene lectura de meta.",
                        r.reg_id,
                        r.bib,
                        finish_pt.id if finish_pt else None,
                    )
                )
        elif valid_finish:
            status = "FIN"
        elif config.status == "setup":
            status = "PEND"
        elif config.status == "running":
            status = "RUN"
        else:
            status = "NT"

        # Coherencia de pasos presentes (en orden de puntos)
        present = [(pt, p[pt.id]) for pt in points if pt.id in p]
        for (pa, ta), (pb, tb) in zip(present, present[1:]):
            if tb < ta:
                issues.append(
                    Issue(
                        "error",
                        "out_of_order",
                        f"Dorsal {r.bib}: «{pb.name}» ocurre antes que «{pa.name}».",
                        r.reg_id,
                        r.bib,
                        pb.id,
                    )
                )
            elif r.status == "REG" and tb - ta < config.min_segment_ms:
                issues.append(
                    Issue(
                        "warning",
                        "segment_too_short",
                        f"Dorsal {r.bib}: solo {(tb - ta) / 1000:.1f}s entre «{pa.name}» y «{pb.name}».",
                        r.reg_id,
                        r.bib,
                        pb.id,
                    )
                )

        if r.status == "REG":
            if finish_ts is not None:
                for pt in points:
                    if pt.kind == "split" and pt.id not in p:
                        issues.append(
                            Issue("warning", "no_split", f"Dorsal {r.bib}: sin lectura en «{pt.name}».", r.reg_id, r.bib, pt.id)
                        )
            elif config.status == "closed":
                last = present[-1][0].name if present else None
                detail = f"última lectura: «{last}»" if last else "sin ninguna lectura"
                issues.append(
                    Issue(
                        "error",
                        "no_finish",
                        f"Dorsal {r.bib} ({r.name}): sin meta ({detail}). ¿DNF/DNS o lectura perdida?",
                        r.reg_id,
                        r.bib,
                        finish_pt.id if finish_pt else None,
                    )
                )
            if not r.chip_codes and r.kit_delivered and config.status != "closed":
                issues.append(Issue("warning", "no_chip", f"Dorsal {r.bib}: kit entregado pero sin chip asignado.", r.reg_id, r.bib))

        ref = start_ref if start_ref is not None else gun
        splits = {pid: ts - ref for pid, ts in p.items() if ref is not None and point_by_id[pid].kind != "start"}

        pace = None
        t_for_pace = net_ms if net_ms is not None else gun_ms
        if status == "FIN" and t_for_pace is not None and config.distance_m > 0:
            pace = t_for_pace * 1000 / config.distance_m

        rows.append(
            ResultRow(
                reg_id=r.reg_id,
                bib=r.bib,
                name=r.name,
                gender=r.gender,
                category_name=r.category_name,
                status=status,
                gun_ms=gun_ms if status == "FIN" else None,
                net_ms=net_ms if status == "FIN" else None,
                pace_ms_km=pace,
                passes=dict(p),
                splits_ms=splits,
                flags=tuple(flags),
            )
        )

    # ---- Posiciones ----
    def gun_key(x: ResultRow) -> int:
        return x.gun_ms if x.gun_ms is not None else x.net_ms  # type: ignore[return-value]

    def net_key(x: ResultRow) -> int:
        return x.net_ms if x.net_ms is not None else x.gun_ms  # type: ignore[return-value]

    overall_key, cat_key = {
        "gun": (gun_key, gun_key),
        "net": (net_key, net_key),
        "hybrid": (gun_key, net_key),
    }.get(config.award_mode, (gun_key, net_key))

    fins = [x for x in rows if x.status == "FIN"]
    overall = _rank(fins, overall_key)
    for x in fins:
        x.position = overall[x.reg_id]
    groups_g: dict[str, list[ResultRow]] = defaultdict(list)
    groups_c: dict[str, list[ResultRow]] = defaultdict(list)
    for x in fins:
        groups_g[x.gender].append(x)
        if x.category_name:
            groups_c[x.category_name].append(x)
    for g_rows in groups_g.values():
        pos = _rank(g_rows, overall_key)
        for x in g_rows:
            x.gender_pos = pos[x.reg_id]
    for c_rows in groups_c.values():
        pos = _rank(c_rows, cat_key)
        for x in c_rows:
            x.category_pos = pos[x.reg_id]

    rows.sort(key=lambda x: (_STATUS_ORDER.get(x.status, 9), x.position if x.position is not None else 10**9, x.bib))

    for code, n in sorted(unknown.items(), key=lambda kv: (-kv[1], kv[0])):
        issues.append(Issue("warning", "unknown_chip", f"Chip {code} leído {n} vez/veces sin dorsal asignado."))

    issues.sort(key=lambda i: (_SEV_ORDER[i.severity], i.bib if i.bib is not None else 10**9, i.code, i.point_id or 0))
    return Computation(rows, issues, dict(unknown), duplicate_reads, total_reads)
