"""Lector UHF real por LLRP (Impinj y compatibles) usando la librería `sllurp` (instalación opcional).

NOTA: probado con un cliente simulado (ver tests); falta validarlo con un lector físico antes de una carrera real.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable

from ..timeutil import now_ms
from .base import Emit, Reader, ReadEvent

log = logging.getLogger("pacer_time.capture.llrp")


def epc_to_code(raw) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).hex().upper()
    return "".join(str(raw).split()).upper() or None


def events_from_tag_reports(tags: Iterable[dict], point_id: int, source: str, use_reader_time: bool = False, received_ms: int | None = None) -> list[ReadEvent]:
    """Convierte los TagReportData de LLRP en ReadEvent. Pura (sin red) para poder probarla."""
    out = []
    recv = received_ms if received_ms is not None else now_ms()
    for tag in tags or []:
        code = epc_to_code(tag.get("EPC") or tag.get("EPC-96"))
        if not code:
            continue
        ts = recv
        if use_reader_time:
            us = tag.get("FirstSeenTimestampUTC") or tag.get("LastSeenTimestampUTC")
            if us:
                ts = int(us) // 1000
        out.append(ReadEvent(code, ts, point_id, int(tag.get("AntennaID") or 0), tag.get("PeakRSSI"), source))
    return out


class LLRPReader(Reader):
    kind = "llrp"

    def __init__(
        self,
        host: str,
        point_id: int,
        *,
        port: int = 5084,
        antennas: tuple[int, ...] = (1,),
        tx_power: int = 0,
        session: int | None = None,
        tag_population: int | None = None,
        use_reader_time: bool = False,
        source: str | None = None,
    ):
        self.host, self.port, self.point_id = host, port, point_id
        self.antennas, self.tx_power = tuple(antennas), tx_power
        self.session, self.tag_population = session, tag_population
        self.use_reader_time = use_reader_time
        self.source = source or f"llrp:{host}"

    def _config(self, llrp) -> object:
        cfg = {
            "antennas": list(self.antennas),
            "tx_power": self.tx_power,
            "report_every_n_tags": 1,
            "start_inventory": True,
            "reconnect": True,
            "reconnect_retries": 1000,
            "tag_content_selector": {
                "EnableROSpecID": False,
                "EnableSpecIndex": False,
                "EnableInventoryParameterSpecID": False,
                "EnableAntennaID": True,
                "EnableChannelIndex": False,
                "EnablePeakRSSI": True,
                "EnableFirstSeenTimestamp": True,
                "EnableLastSeenTimestamp": False,
                "EnableTagSeenCount": False,
                "EnableAccessSpecID": False,
            },
        }
        if self.session is not None:
            cfg["session"] = self.session
        if self.tag_population is not None:
            cfg["tag_population"] = self.tag_population
        return llrp.LLRPReaderConfig(cfg)

    def run(self, emit: Emit, stop: threading.Event) -> None:
        try:
            from sllurp import llrp
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Falta la librería sllurp: pip install 'pacer-time[llrp]'") from exc

        def on_tags(_reader, tag_reports):
            for ev in events_from_tag_reports(tag_reports, self.point_id, self.source, self.use_reader_time):
                emit(ev)

        client = llrp.LLRPReaderClient(self.host, self.port, self._config(llrp))
        client.add_tag_report_callback(on_tags)
        client.connect()
        log.info("Conectado a lector LLRP %s:%s", self.host, self.port)
        try:
            while not stop.wait(0.5):
                if hasattr(client, "is_alive") and not client.is_alive():
                    raise RuntimeError(f"Se perdió la conexión con el lector {self.host}")
        finally:
            try:
                client.disconnect()
            except Exception:  # pragma: no cover
                log.exception("Error al desconectar del lector")
