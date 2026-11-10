"""Contrato de los lectores de captura."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ReadEvent:
    chip_code: str
    ts_ms: int
    point_id: int
    antenna: int = 0
    rssi: float | None = None
    source: str = ""

    def as_row(self) -> tuple:
        return (self.point_id, self.chip_code, self.ts_ms, self.antenna, self.rssi, self.source)


Emit = Callable[[ReadEvent], None]


class Reader(ABC):
    """Una fuente de lecturas. `run` es bloqueante: emite eventos hasta que `stop` se active o la fuente termine."""

    kind = "reader"

    @abstractmethod
    def run(self, emit: Emit, stop: threading.Event) -> None: ...
