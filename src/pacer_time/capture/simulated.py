"""Lector simulado: reproduce en 'vivo' una carrera generada con ruido, comprimida por un factor de velocidad."""

from __future__ import annotations

import threading

from .. import db, repo
from ..simulator import Noise, build_events
from ..timeutil import now_ms
from .base import Emit, Reader, ReadEvent


class SimulatedReader(Reader):
    kind = "sim"

    def __init__(self, db_path: str, race_id: int, speed: float = 10.0, seed: int = 1, noise: Noise | None = None, source: str = "sim"):
        if speed <= 0:
            raise ValueError("speed debe ser > 0")
        self.db_path, self.race_id, self.speed, self.seed = db_path, race_id, speed, seed
        self.noise = noise
        self.source = source

    def run(self, emit: Emit, stop: threading.Event) -> None:
        conn = db.connect(self.db_path)
        try:
            race = repo.get_race(conn, self.race_id)
            points = repo.list_points(conn, self.race_id)
            runners = repo.list_runners(conn, self.race_id)
        finally:
            conn.close()
        gun = race.gun_time_ms if race.gun_time_ms is not None else now_ms()
        events = build_events(runners, points, race.distance_m, seed=self.seed, noise=self.noise)
        for off, pid, chip in events:
            due = gun + int(off / self.speed)
            while not stop.is_set():
                wait = (due - now_ms()) / 1000
                if wait <= 0:
                    break
                stop.wait(min(wait, 0.25))
            if stop.is_set():
                return
            emit(ReadEvent(chip, due, pid, 1, -50.0, self.source))
        # fin del guion: el proceso de captura marcará 'finished'
