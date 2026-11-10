"""Carrera en vivo: disparo, captura (proceso aparte), lecturas en tiempo real, salud de lectores y paso manual."""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import repo
from ..capture import launcher
from ..models import STATUS_LABELS
from ..timeutil import format_clock, format_duration, now_ms
from .session import Session
from .widgets import COLOR_ERROR, COLOR_OK, COLOR_WARN, Badge, DataTable, require_operator

RACE_STATE = {"setup": ("Pendiente de salida", "muted"), "running": ("EN CURSO", "ok"), "closed": ("Cerrada", "info")}
READ_COLS = ["id", "Hora", "Punto", "Chip", "Dorsal", "Antena", "RSSI", "Origen"]
MAX_READS_SHOWN = 300


class LiveTab(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self._last_read_id = 0
        self._shown: deque = deque(maxlen=MAX_READS_SHOWN)
        self._rate: deque = deque()  # (monotonic, n)
        self._total_reads = 0

        root = QVBoxLayout(self)
        # ---- cabecera
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size:20px;font-weight:700;")
        self.state = Badge()
        self.clock = QLabel("—")
        self.clock.setStyleSheet("font-family:monospace;font-size:34px;font-weight:700;")
        self.b_start = QPushButton("⏱ Disparo de salida")
        self.b_start.setStyleSheet("font-size:15px;font-weight:700;padding:8px 16px;background:#2d6a4f;color:white;")
        self.b_close = QPushButton("Cerrar carrera")
        self.b_reopen = QPushButton("Reabrir")
        head.addWidget(self.title)
        head.addWidget(self.state)
        head.addStretch(1)
        head.addWidget(self.clock)
        head.addSpacing(16)
        for b in (self.b_start, self.b_close, self.b_reopen):
            b.setAutoDefault(False)
            head.addWidget(b)
        root.addLayout(head)
        self.summary = QLabel()
        self.summary.setStyleSheet("font-size:14px;color:#374151;")
        root.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # ---- izquierda: captura + paso manual + puntos
        left = QWidget()
        ll = QVBoxLayout(left)
        cap = QGroupBox("Captura de lecturas (proceso independiente)")
        cf = QFormLayout(cap)
        self.source = QComboBox()
        self.source.addItem("Simulador (demostración)", "sim")
        self.source.addItem("Lector UHF por red (LLRP / Impinj)", "llrp")
        self.speed = QDoubleSpinBox()
        self.speed.setRange(1, 500)
        self.speed.setValue(10)
        self.speed.setPrefix("×")
        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5084)
        self.point = QComboBox()
        self.antennas = QLineEdit("1,2")
        cf.addRow("Fuente", self.source)
        self.row_speed = (QLabel("Velocidad"), self.speed)
        cf.addRow(*self.row_speed)
        self.llrp_rows = [(QLabel("IP del lector"), self.host), (QLabel("Puerto"), self.port), (QLabel("Punto"), self.point), (QLabel("Antenas"), self.antennas)]
        for lab, w in self.llrp_rows:
            cf.addRow(lab, w)
        btns = QHBoxLayout()
        self.b_cap_start = QPushButton("▶ Iniciar captura")
        self.b_cap_stop = QPushButton("■ Detener")
        btns.addWidget(self.b_cap_start)
        btns.addWidget(self.b_cap_stop)
        cf.addRow(btns)
        ll.addWidget(cap)
        self.readers = DataTable(["Fuente", "Estado", "Lecturas", "Última lectura", "Latido"])
        self.readers.setMaximumHeight(120)
        self.readers.source.set_row_color(self._reader_color)
        ll.addWidget(QLabel("Estado de lectores"))
        ll.addWidget(self.readers)

        man = QGroupBox("Paso manual (respaldo)")
        mf = QHBoxLayout(man)
        self.m_bib = QSpinBox()
        self.m_bib.setRange(1, 999999)
        self.m_point = QComboBox()
        self.b_manual = QPushButton("Registrar ahora")
        mf.addWidget(QLabel("Dorsal"))
        mf.addWidget(self.m_bib)
        mf.addWidget(self.m_point, 1)
        mf.addWidget(self.b_manual)
        ll.addWidget(man)

        ll.addWidget(QLabel("Lecturas por punto"))
        self.points_table = DataTable(["Punto", "Lecturas", "Última lectura"])
        self.points_table.setMaximumHeight(130)
        ll.addWidget(self.points_table)
        ll.addStretch(1)
        split.addWidget(left)

        # ---- derecha: lecturas en vivo
        right = QWidget()
        rl = QVBoxLayout(right)
        self.reads_title = QLabel("Lecturas en vivo")
        self.reads_title.setStyleSheet("font-weight:700;")
        rl.addWidget(self.reads_title)
        self.reads = DataTable(READ_COLS)
        self.reads.source.set_formatter(1, lambda v: format_clock(v))
        self.reads.source.set_row_color(lambda r: COLOR_WARN if r[4] is None else None)
        self.reads.setSortingEnabled(False)
        self.reads.setColumnHidden(0, True)
        rl.addWidget(self.reads, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([480, 700])

        # ---- señales
        self.b_start.clicked.connect(self.start_race)
        self.b_close.clicked.connect(self.close_race)
        self.b_reopen.clicked.connect(self.reopen_race)
        self.b_cap_start.clicked.connect(self.start_capture)
        self.b_cap_stop.clicked.connect(self.stop_capture)
        self.b_manual.clicked.connect(self.manual_read)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.s.results_ready.connect(self._on_results)
        self.s.changed.connect(self.refresh_static)
        self.s.race_changed.connect(self._race_changed)

        self._t_reads = QTimer(self)
        self._t_reads.timeout.connect(self.poll_reads)
        self._t_reads.start(500)
        self._t_slow = QTimer(self)
        self._t_slow.timeout.connect(self.refresh_slow)
        self._t_slow.start(1000)
        self._t_clock = QTimer(self)
        self._t_clock.timeout.connect(self.update_clock)
        self._t_clock.start(100)
        self._source_changed()
        self.refresh_static()
        self.poll_reads(initial=True)
        self.refresh_slow()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _reader_color(row):
        text = row[1]
        if text.startswith("En marcha"):
            return COLOR_OK
        if text.startswith(("SIN SEÑAL", "Error")):
            return COLOR_ERROR
        return None

    def _source_changed(self) -> None:
        llrp = self.source.currentData() == "llrp"
        self.row_speed[0].setVisible(not llrp)
        self.row_speed[1].setVisible(not llrp)
        for lab, w in self.llrp_rows:
            lab.setVisible(llrp)
            w.setVisible(llrp)

    def _race_changed(self) -> None:
        self._last_read_id = 0
        self._shown.clear()
        self.reads.set_rows([])
        self.refresh_static()
        self.poll_reads(initial=True)

    def _err(self, exc) -> None:
        QMessageBox.warning(self, "No se pudo completar", str(exc))

    # ---------------------------------------------------------------- carrera
    def refresh_static(self) -> None:
        race = self.s.race()
        pts = self.s.points()
        self.title.setText(f"{race.name}")
        text, kind = RACE_STATE[race.status]
        self.state.set(text, kind)
        self.b_start.setEnabled(race.status == "setup")
        self.b_close.setEnabled(race.status == "running")
        self.b_reopen.setEnabled(race.status == "closed")
        for combo in (self.point, self.m_point):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for p in pts:
                combo.addItem(p.name, p.id)
            if cur is not None:
                combo.setCurrentIndex(max(0, combo.findData(cur)))
            else:
                combo.setCurrentIndex(len(pts) - 1)
            combo.blockSignals(False)
        self.update_clock()

    def update_clock(self) -> None:
        try:
            race = self.s.race()
        except Exception:
            return
        if race.status == "running" and race.gun_time_ms is not None:
            self.clock.setText(format_duration(now_ms() - race.gun_time_ms, decimals=1))
        elif race.status == "closed" and race.gun_time_ms is not None:
            self.clock.setText("Cerrada")
        else:
            self.clock.setText(format_clock(now_ms(), with_millis=False))

    def start_race(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        ok = QMessageBox.question(
            self, "Disparo de salida", "¿Dar el disparo de salida AHORA?\n\nLa hora actual será el tiempo cero (tiempo de pistola).\n"
            "Si se equivoca puede ajustarla después en «Calidad y correcciones».",
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            repo.start_race(self.s.conn, self.s.race_id, op)
        except ValueError as exc:
            return self._err(exc)
        self.s.notify_changed()

    def close_race(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        running = [r for r in launcher.list_status(self.s.conn, self.s.race_id) if r["alive"]]
        msg = "¿Cerrar la carrera?\n\nLos corredores sin meta pasarán a «Sin tiempo» y se marcarán como errores para revisar."
        if running:
            msg += "\n\nAún hay captura en marcha; deténgala antes si ya no esperan más lecturas."
        if QMessageBox.question(self, "Cerrar carrera", msg) != QMessageBox.StandardButton.Yes:
            return
        try:
            repo.close_race(self.s.conn, self.s.race_id, op)
        except ValueError as exc:
            return self._err(exc)
        self.s.notify_changed()

    def reopen_race(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        try:
            repo.reopen_race(self.s.conn, self.s.race_id, op)
        except ValueError as exc:
            return self._err(exc)
        self.s.notify_changed()

    # ---------------------------------------------------------------- captura
    def start_capture(self) -> None:
        race = self.s.race()
        kind = self.source.currentData()
        if kind == "sim":
            if race.status != "running":
                return self._err(ValueError("Para simular, primero dé el disparo de salida."))
            name = "sim"
            args = ["--speed", str(self.speed.value())]
        else:
            host = self.host.text().strip()
            if not host:
                return self._err(ValueError("Escriba la IP del lector."))
            name = f"llrp:{host}"
            args = ["--host", host, "--port", str(self.port.value()), "--point-id", str(self.point.currentData()), "--antennas", self.antennas.text().strip() or "1"]
        for st in launcher.list_status(self.s.conn, self.s.race_id):
            if st["source"] == name and st["alive"]:
                return self._err(ValueError(f"La fuente «{name}» ya está capturando."))
        launcher.clear_status(self.s.conn, name)
        try:
            launcher.start_capture_process(self.s.db_path, self.s.race_id, kind, name, args)
        except OSError as exc:
            return self._err(exc)
        self.refresh_slow()

    def stop_capture(self) -> None:
        row = self.readers.current_row()
        sources = [row[0]] if row else [r["source"] for r in launcher.list_status(self.s.conn, self.s.race_id) if r["alive"]]
        for src in sources:
            launcher.request_stop(self.s.conn, src)
        self.refresh_slow()

    def manual_read(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        try:
            code = repo.add_manual_read(self.s.conn, self.s.race_id, self.m_point.currentData(), bib=self.m_bib.value(), operator=op)
        except ValueError as exc:
            return self._err(exc)
        self.m_bib.setFocus()
        self.m_bib.selectAll()
        self.poll_reads()
        self.s.request_results()
        self.summary.setText(f"Paso manual registrado: dorsal {self.m_bib.value()} ({code}).")

    # ---------------------------------------------------------------- sondeo
    def poll_reads(self, initial: bool = False) -> None:
        conn, rid = self.s.conn, self.s.race_id
        if initial:
            row = conn.execute(
                "SELECT COALESCE(MAX(rr.id), 0) FROM raw_read rr JOIN timing_point tp ON tp.id=rr.point_id WHERE tp.race_id=?", (rid,)
            ).fetchone()
            self._last_read_id = max(0, row[0] - MAX_READS_SHOWN)
        rows = repo.reads_after(conn, rid, self._last_read_id, limit=2000)
        if not rows:
            return
        self._last_read_id = rows[-1]["id"]
        now = time.monotonic()
        self._rate.append((now, len(rows)))
        for r in rows:
            self._shown.appendleft((r["id"], r["ts_ms"], r["point_name"], r["chip_code"], r["bib"], r["antenna"] or None, r["rssi"], r["source"]))
        self.reads.set_rows(list(self._shown), fit=False)
        if self.reads.columnWidth(1) <= 100:
            for c, w in enumerate([0, 110, 100, 190, 70, 60, 60, 120]):
                if c:
                    self.reads.setColumnWidth(c, w)

    def refresh_slow(self) -> None:
        conn, rid = self.s.conn, self.s.race_id
        now = time.monotonic()
        while self._rate and now - self._rate[0][0] > 10:
            self._rate.popleft()
        per_s = sum(n for _, n in self._rate) / 10
        self._total_reads = repo.count_reads(conn, rid)
        self.reads_title.setText(f"Lecturas en vivo — {self._total_reads} guardadas · {per_s:.1f}/s (últimos 10 s) · se muestran las {MAX_READS_SHOWN} más recientes")
        rows = []
        t = now_ms()
        for st in launcher.list_status(conn, rid):
            if st["stale"]:
                estado = "SIN SEÑAL (¿se cayó?)"
            elif st["alive"]:
                estado = "En marcha"
            else:
                estado = {"finished": "Terminó", "stopped": "Detenido", "error": f"Error: {st['detail']}"}.get(st["state"], st["state"])
            hb = f"hace {max(0, (t - st['heartbeat_ms']) // 1000)} s" if st["heartbeat_ms"] else ""
            rows.append((st["source"], estado, st["total_reads"], format_clock(st["last_read_ms"], False) if st["last_read_ms"] else "", hb))
        self.readers.set_rows(rows, key_col=0)
        if self.readers.columnWidth(0) <= 100:
            for c, w in enumerate([90, 140, 70, 100]):
                self.readers.setColumnWidth(c, w)
        prow = [(r["name"], r["reads"], format_clock(r["last_ts"], False) if r["last_ts"] else "") for r in repo.point_stats(conn, rid)]
        self.points_table.set_rows(prow)

    def _on_results(self, comp) -> None:
        counts: dict[str, int] = {}
        for r in comp.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        errs = sum(1 for i in comp.issues if i.severity == "error")
        warns = sum(1 for i in comp.issues if i.severity == "warning")
        parts = [f"{STATUS_LABELS[k]}: {counts[k]}" for k in ("FIN", "RUN", "NT", "PEND", "DNF", "DNS", "DQ") if k in counts]
        parts.append(f"Chips desconocidos: {len(comp.unknown_chips)}")
        parts.append(f"Lecturas repetidas filtradas: {comp.duplicate_reads}")
        parts.append(f"Errores: {errs} · Avisos: {warns}")
        self.summary.setText("   |   ".join(parts))
