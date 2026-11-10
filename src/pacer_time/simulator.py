"""Simulador de carreras con ruido realista (lecturas repetidas, perdidas, chips ajenos, pasos extra).

Sirve para probar todo sin hardware, para demostrar robustez y para poblar una base de demostración.

    pacer-sim seed --db demo.db --runners 300        # carrera completa ya terminada, con anomalías
    pacer-sim seed --db demo.db --runners 300 --elapsed-min 12   # a mitad de carrera (en curso)
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from . import repo
from .capture.base import ReadEvent
from .db import create_database, tx
from .models import RunnerInfo, TimingPoint
from .timeutil import now_ms

FIRST_NAMES = (
    "Ana Beto Carla Diego Elena Fabián Gloria Hugo Inés Javier Karla Luis Marta Nicolás Olga Pablo Quetzal Rosa Sergio "
    "Tania Ulises Valeria Xavier Yolanda Zacarías Alma Bruno Camila Daniel Esther Felipe Gabriela Héctor Irma Jorge"
).split()
LAST_NAMES = (
    "García López Martínez Hernández González Pérez Rodríguez Sánchez Ramírez Cruz Flores Gómez Morales Vázquez Jiménez "
    "Reyes Díaz Torres Gutiérrez Ruiz Mendoza Aguilar Ortiz Castillo Moreno Romero Álvarez Chávez Rivera Juárez Navarro"
).split()
CLUBS = ["", "", "", "Corredores del Norte", "Club Pacer", "Trotamundos", "Runners Sur", "Atletas Unidos"]


@dataclass
class Noise:
    miss_start: float = 0.03
    miss_split: float = 0.04
    miss_finish: float = 0.02
    max_repeats: int = 6  # lecturas repetidas por paso (el lector lee varias veces al cruzar)
    foreign_chips: int = 5  # chips que no son de la carrera (tarjetas, etc.)
    stationary: float = 0.01  # corredores que se quedan cerca de la antena (paso extra en meta)
    abandon: float = 0.02  # abandonan sin avisar (no cruzan meta)
    dns: float = 0.02  # inscritos que no salieron


@dataclass
class RunPlan:
    reg_id: int
    bib: int
    chip: str
    start_delay_ms: int
    pace_ms_km: float
    abandon_at_m: int | None = None


def plan_runs(runners: list[RunnerInfo], rng: random.Random, noise: Noise, distance_m: int) -> list[RunPlan]:
    plans = []
    ordered = sorted(runners, key=lambda r: r.bib)
    for idx, r in enumerate(ordered):
        if not r.chip_codes or r.status == "DNS":
            continue
        base = 330_000 if r.gender != "F" else 372_000  # 5:30 y 6:12 por km
        pace = base * rng.lognormvariate(0, 0.17) * (1 + max(distance_m - 5000, 0) / 100_000 * 0.05)
        # Los más rápidos salen delante: menos retraso en el tapete de salida
        frac = min(1.0, pace / 600_000)
        delay = int(rng.uniform(0, 4_000) + frac * rng.uniform(5_000, 60_000))
        abandon = int(distance_m * rng.uniform(0.2, 0.9)) if rng.random() < noise.abandon else None
        plans.append(RunPlan(r.reg_id, r.bib, r.chip_codes[0], delay, pace, abandon))
    return plans


def reads_for_plan(
    plan: RunPlan, points: list[TimingPoint], noise: Noise, rng: random.Random, source: str = "sim"
) -> list[tuple[int, int, str]]:
    """Devuelve (offset_ms desde el disparo, point_id, chip) para todas las lecturas (con repeticiones y pérdidas)."""
    out: list[tuple[int, int, str]] = []
    for pt in points:
        if plan.abandon_at_m is not None and pt.distance_m > plan.abandon_at_m:
            break
        t = plan.start_delay_ms + int(plan.pace_ms_km * pt.distance_m / 1000 * rng.uniform(0.995, 1.005))
        miss = {"start": noise.miss_start, "split": noise.miss_split, "finish": noise.miss_finish}[pt.kind]
        if rng.random() < miss:
            continue
        repeats = 1 + min(noise.max_repeats - 1, int(rng.expovariate(1 / 1.5)))
        for _ in range(repeats):
            out.append((t + rng.randint(0, 1800), pt.id, plan.chip))
        if pt.kind == "finish" and rng.random() < noise.stationary:
            out.append((t + rng.randint(120_000, 300_000), pt.id, plan.chip))
    return out


def foreign_reads(points: list[TimingPoint], noise: Noise, rng: random.Random, horizon_ms: int) -> list[tuple[int, int, str]]:
    finish = next(p for p in reversed(points) if p.kind == "finish")
    out = []
    for i in range(noise.foreign_chips):
        chip = f"FOREIGN{i + 1:02d}"
        t = rng.randint(horizon_ms // 3, horizon_ms)
        for _ in range(rng.randint(1, 4)):
            out.append((t + rng.randint(0, 2000), finish.id, chip))
    return out


def build_events(
    runners: list[RunnerInfo], points: list[TimingPoint], distance_m: int, *, seed: int = 1, noise: Noise | None = None
) -> list[tuple[int, int, str]]:
    """Todas las lecturas de una carrera simulada como (offset_ms, point_id, chip), ordenadas por tiempo."""
    noise = noise or Noise()
    rng = random.Random(seed)
    events: list[tuple[int, int, str]] = []
    plans = plan_runs(runners, rng, noise, distance_m)
    for plan in plans:
        events.extend(reads_for_plan(plan, points, noise, rng))
    horizon = max((o for o, _, _ in events), default=60_000)
    events.extend(foreign_reads(points, noise, rng, horizon))
    events.sort()
    return events


def seed_demo(
    conn,
    *,
    runners: int = 200,
    seed: int = 1,
    distance_m: int = 5000,
    splits: int = 1,
    event_name: str = "Carrera Demo Pacer Time",
    elapsed_ms: int | None = None,
    noise: Noise | None = None,
    award_mode: str = "hybrid",
) -> dict:
    """Crea evento, carrera, inscritos con chip y lecturas simuladas.

    elapsed_ms=None: carrera completa y cerrada. Con valor: carrera en curso con ese tiempo transcurrido.
    """
    noise = noise or Noise()
    rng = random.Random(seed)
    from datetime import date

    ev = repo.create_event(conn, event_name, date.today().isoformat(), "Ciudad Demo")
    race_id = repo.create_race(conn, ev, f"{distance_m / 1000:g}K", distance_m, award_mode=award_mode, splits=splits)

    used_docs: set[str] = set()
    with tx(conn):
        for i in range(runners):
            gender = "F" if rng.random() < 0.42 else "M"
            doc = f"D{rng.randrange(10**7, 10**8)}"
            while doc in used_docs:
                doc = f"D{rng.randrange(10**7, 10**8)}"
            used_docs.add(doc)
            year = rng.choice([rng.randint(1958, 2007), rng.randint(1975, 2000), rng.randint(1965, 1995)])
            repo.register(
                conn,
                race_id,
                {
                    "first_name": rng.choice(FIRST_NAMES),
                    "last_name": f"{rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}",
                    "document": doc,
                    "birth_date": f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "gender": gender,
                    "club": rng.choice(CLUBS),
                },
                chip=f"E2000{i + 1:05d}",
                kit_delivered=True,
                paid=True,
                operator="demo",
            )
    # Algunos no salieron (DNS) con motivo registrado
    regs = repo.list_registrations(conn, race_id)
    for r in regs:
        if rng.random() < noise.dns:
            repo.set_status(conn, r["reg_id"], "DNS", "No se presentó (demo)", "demo")

    points = repo.list_points(conn, race_id)
    run_infos = repo.list_runners(conn, race_id)
    events = build_events(run_infos, points, distance_m, seed=seed + 1, noise=noise)
    full_ms = max((o for o, _, _ in events), default=0) + 120_000

    if elapsed_ms is None:
        gun = now_ms() - full_ms - 60_000
        cutoff = None
    else:
        gun = now_ms() - elapsed_ms
        cutoff = elapsed_ms
    repo.start_race(conn, race_id, "demo", ts_ms=gun)
    rows = [
        (pid, chip, gun + off, 1 + (off % 2), -45.0 - (off % 25), "sim")
        for off, pid, chip in events
        if cutoff is None or off <= cutoff
    ]
    repo.insert_reads(conn, rows)
    if cutoff is None:
        repo.close_race(conn, race_id, "demo")
    return {"event_id": ev, "race_id": race_id, "runners": runners, "reads": len(rows), "gun_ms": gun}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pacer-sim", description="Genera una base de demostración con lecturas simuladas.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sd = sub.add_parser("seed", help="Crea una base de datos nueva con una carrera simulada.")
    sd.add_argument("--db", required=True, type=Path)
    sd.add_argument("--runners", type=int, default=200)
    sd.add_argument("--seed", type=int, default=1)
    sd.add_argument("--distance-m", type=int, default=5000)
    sd.add_argument("--splits", type=int, default=1)
    sd.add_argument("--elapsed-min", type=float, default=None, help="Si se indica, la carrera queda EN CURSO con ese tiempo transcurrido.")
    args = ap.parse_args(argv)
    conn = create_database(args.db)
    info = seed_demo(
        conn,
        runners=args.runners,
        seed=args.seed,
        distance_m=args.distance_m,
        splits=args.splits,
        elapsed_ms=int(args.elapsed_min * 60_000) if args.elapsed_min else None,
    )
    conn.close()
    print(f"Base creada en {args.db}: carrera {info['race_id']}, {info['runners']} inscritos, {info['reads']} lecturas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
