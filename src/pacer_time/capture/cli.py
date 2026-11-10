"""Línea de comandos del proceso de captura.

Ejemplos:
    pacer-capture --db evento.db --race 1 --source sim --speed 10
    pacer-capture --db evento.db --race 1 --source llrp --host 192.168.1.50 --point-id 3 --antennas 1,2
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .. import db, repo
from .runner import run_capture


def build_reader(args, db_path):
    if args.source == "sim":
        from .simulated import SimulatedReader

        return SimulatedReader(str(db_path), args.race, speed=args.speed, seed=args.seed, source=args.name)
    if args.source == "llrp":
        from .llrp import LLRPReader

        if not args.host or args.point_id is None:
            raise SystemExit("--source llrp requiere --host y --point-id")
        conn = db.connect(db_path)
        try:
            ok = conn.execute("SELECT 1 FROM timing_point WHERE id=? AND race_id=?", (args.point_id, args.race)).fetchone()
        finally:
            conn.close()
        if not ok:
            raise SystemExit(f"El punto {args.point_id} no pertenece a la carrera {args.race}")
        return LLRPReader(
            args.host,
            args.point_id,
            port=args.port,
            antennas=tuple(int(a) for a in args.antennas.split(",") if a.strip()),
            tx_power=args.tx_power,
            session=args.session,
            tag_population=args.tag_population,
            use_reader_time=args.reader_time,
            source=args.name,
        )
    raise SystemExit(f"Fuente desconocida: {args.source}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pacer-capture", description="Proceso de captura de lecturas (independiente de la interfaz).")
    ap.add_argument("--db", required=True)
    ap.add_argument("--race", required=True, type=int)
    ap.add_argument("--source", choices=["sim", "llrp"], default="sim")
    ap.add_argument("--name", default=None, help="Nombre de la fuente (clave de estado). Por defecto, derivado.")
    ap.add_argument("--speed", type=float, default=10.0, help="[sim] factor de aceleración")
    ap.add_argument("--seed", type=int, default=1, help="[sim] semilla")
    ap.add_argument("--host", help="[llrp] IP del lector")
    ap.add_argument("--port", type=int, default=5084)
    ap.add_argument("--point-id", type=int, help="[llrp] id del punto de cronometraje donde está el lector")
    ap.add_argument("--antennas", default="1", help="[llrp] lista de antenas, ej. 1,2")
    ap.add_argument("--tx-power", type=int, default=0)
    ap.add_argument("--session", type=int, default=None)
    ap.add_argument("--tag-population", type=int, default=None)
    ap.add_argument("--reader-time", action="store_true", help="[llrp] usar la hora del lector en vez de la del PC")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = db.open_database(args.db)
    try:
        repo.get_race(conn, args.race)
    finally:
        conn.close()
    args.name = args.name or (f"sim" if args.source == "sim" else f"llrp:{args.host}")
    reader = build_reader(args, args.db)

    stop = threading.Event()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())
    final = run_capture(args.db, args.race, reader, args.name, stop)
    logging.getLogger("pacer_time.capture").info("Captura terminada: %s", final)
    return 1 if final == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
