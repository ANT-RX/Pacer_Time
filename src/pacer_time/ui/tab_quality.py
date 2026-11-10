"""Calidad y correcciones: anomalías detectadas, lecturas por dorsal, correcciones auditadas e historial."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from .. import repo
from ..models import Computation
from ..timeutil import format_clock, now_ms, parse_clock
from .dialogs import ClockDialog, ManualTimeDialog, ReasonDialog
from .session import Session
from .widgets import COLOR_ERROR, COLOR_MUTED, COLOR_WARN, DataTable, require_operator

SEV = {0: "Error", 1: "Aviso", 2: "Info"}
SEV_RANK = {"error": 0, "warning": 1, "info": 2}
ACTION_LABELS = {
    "void_read": "Anular lectura",
    "unvoid_read": "Restaurar lectura",
    "add_pass": "Agregar tiempo",
    "set_pass": "Cambiar tiempo",
    "set_status": "Cambiar estado",
    "set_gun": "Ajustar disparo",
}


def fmt_value(action: str, v: str | None) -> str:
    if v is None:
        return ""
    try:
        if action in ("add_pass", "set_pass", "set_gun"):
            return format_clock(int(v))
        if action in ("void_read",) and "@" in v:
            chip, ts = v.rsplit("@", 1)
            return f"{chip} @ {format_clock(int(ts))}"
    except ValueError:
        pass
    return v


class QualityTab(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self._comp: Computation | None = None
        root = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split)

        # ---- anomalías
        top = QWidget()
        tl = QVBoxLayout(top)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>Anomalías detectadas</b>"))
        self.sev = QComboBox()
        self.sev.addItem("Errores y avisos", 1)
        self.sev.addItem("Solo errores", 0)
        self.sev.addItem("Todo (incluye informativos)", 2)
        bar.addWidget(self.sev)
        bar.addStretch(1)
        self.b_time = QPushButton("Fijar tiempo…")
        self.b_status = QPushButton("Cambiar estado…")
        self.b_gun = QPushButton("Ajustar disparo…")
        for b in (self.b_time, self.b_status, self.b_gun):
            b.setAutoDefault(False)
            bar.addWidget(b)
        tl.addLayout(bar)
        self.issues = DataTable(["Gravedad", "Código", "Dorsal", "Detalle", "reg_id", "point_id"])
        self.issues.setColumnHidden(4, True)
        self.issues.setColumnHidden(5, True)
        self.issues.source.set_formatter(0, lambda v: SEV.get(v, str(v)))
        self.issues.source.set_row_color(lambda r: COLOR_ERROR if r[0] == 0 else (COLOR_WARN if r[0] == 1 else None))
        self.issues.source.set_right_aligned([2])
        tl.addWidget(self.issues, 1)
        split.addWidget(top)

        # ---- lecturas del dorsal
        mid = QWidget()
        ml = QVBoxLayout(mid)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Lecturas del dorsal</b>"))
        self.bib = QSpinBox()
        self.bib.setRange(1, 999999)
        b_load = QPushButton("Ver")
        self.b_void = QPushButton("Anular / restaurar lectura seleccionada…")
        for b in (b_load, self.b_void):
            b.setAutoDefault(False)
        row.addWidget(self.bib)
        row.addWidget(b_load)
        row.addStretch(1)
        row.addWidget(self.b_void)
        ml.addLayout(row)
        self.reads = DataTable(["id", "Hora", "Punto", "Chip", "Antena", "Estado"])
        self.reads.source.set_formatter(1, format_clock)
        self.reads.source.set_row_color(lambda r: COLOR_MUTED if r[5] == "ANULADA" else None)
        ml.addWidget(self.reads, 1)
        split.addWidget(mid)

        # ---- historial
        bot = QWidget()
        bl = QVBoxLayout(bot)
        bl.addWidget(QLabel("<b>Historial de correcciones</b> (no se puede borrar ni editar)"))
        self.hist = DataTable(["Fecha", "Operador", "Acción", "Dorsal", "Punto", "Antes", "Después", "Motivo"])
        self.hist.setSortingEnabled(False)
        bl.addWidget(self.hist, 1)
        split.addWidget(bot)
        split.setSizes([300, 220, 200])

        self.s.results_ready.connect(self.on_results)
        self.s.changed.connect(self.refresh_history)
        self.s.race_changed.connect(self._race_changed)
        self.sev.currentIndexChanged.connect(self.render_issues)
        self.issues.selectionModel().selectionChanged.connect(self._issue_selected)
        b_load.clicked.connect(self.load_reads)
        self.bib.editingFinished.connect(self.load_reads)
        self.b_time.clicked.connect(self.set_time)
        self.b_status.clicked.connect(self.change_status)
        self.b_gun.clicked.connect(self.adjust_gun)
        self.b_void.clicked.connect(self.toggle_void)
        if self.s.last_computation:
            self.on_results(self.s.last_computation)
        self.refresh_history()

    # ---------------------------------------------------------------- datos
    def _race_changed(self) -> None:
        self._comp = None
        self.issues.set_rows([])
        self.reads.set_rows([])
        self.refresh_history()

    def on_results(self, comp: Computation) -> None:
        self._comp = comp
        self.render_issues()
        if self.reads.source.rowCount():
            self.load_reads()

    def render_issues(self) -> None:
        if self._comp is None:
            return
        limit = self.sev.currentData()
        rows = [
            (SEV_RANK[i.severity], i.code, i.bib, i.message, i.reg_id, i.point_id)
            for i in self._comp.issues
            if SEV_RANK[i.severity] <= limit
        ]
        self.issues.set_rows(rows)
        if self.issues.columnWidth(3) <= 100:
            for c, w in enumerate([80, 140, 70, 600]):
                self.issues.setColumnWidth(c, w)

    def refresh_history(self) -> None:
        rows = []
        for c in repo.list_corrections(self.s.conn, self.s.race_id):
            rows.append((
                datetime.fromtimestamp(c["created_ms"] / 1000).strftime("%d/%m %H:%M:%S"), c["operator"],
                ACTION_LABELS.get(c["action"], c["action"]), c["bib"], c["point_name"] or "",
                fmt_value(c["action"], c["old_value"]), fmt_value(c["action"], c["new_value"]), c["reason"],
            ))
        self.hist.set_rows(rows)
        if self.hist.columnWidth(7) <= 100:
            for c, w in enumerate([110, 100, 130, 60, 100, 170, 170]):
                self.hist.setColumnWidth(c, w)

    def _issue_selected(self) -> None:
        row = self.issues.current_row()
        if row and row[2] is not None:
            self.bib.setValue(int(row[2]))
            self.load_reads()

    def load_reads(self) -> None:
        conn, rid = self.s.conn, self.s.race_id
        reg = repo.find_registration(conn, rid, bib=self.bib.value())
        if reg is None:
            self.reads.set_rows([])
            return
        voided = repo._voided_ids(conn, rid)
        rows = []
        for r in conn.execute(
            """SELECT rr.id, rr.ts_ms, tp.name AS point, rr.chip_code, rr.antenna FROM raw_read rr
               JOIN timing_point tp ON tp.id = rr.point_id
               JOIN chip c ON c.race_id = tp.race_id AND c.code = rr.chip_code
               WHERE c.registration_id = ? ORDER BY rr.ts_ms""",
            (reg["id"],),
        ):
            rows.append((r["id"], r["ts_ms"], r["point"], r["chip_code"], r["antenna"] or "", "ANULADA" if r["id"] in voided else "válida"))
        self.reads.set_rows(rows, key_col=0)
        if self.reads.columnWidth(3) <= 100:
            for c, w in enumerate([60, 120, 120, 200, 60]):
                self.reads.setColumnWidth(c, w)
        self.reads.setColumnHidden(0, False)

    # ---------------------------------------------------------------- acciones
    def _err(self, exc) -> None:
        QMessageBox.warning(self, "No se pudo completar", str(exc))

    def set_time(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        row = self.issues.current_row()
        bib = int(row[2]) if row and row[2] is not None else self.bib.value()
        dlg = ManualTimeDialog(self.s.points(), bib, op, point_id=row[5] if row and row[5] else None, parent=self)
        while dlg.exec():
            try:
                reg = repo.find_registration(self.s.conn, self.s.race_id, bib=dlg.bib.value())
                if reg is None:
                    raise ValueError(f"No existe el dorsal {dlg.bib.value()}.")
                race = self.s.race()
                ref = race.gun_time_ms or 0
                ts = parse_clock(dlg.clock.text(), ref if ref else now_ms())
                reason, operator = dlg.values()
                repo.set_pass(self.s.conn, reg["id"], dlg.point.currentData(), ts, reason, operator)
            except ValueError as exc:
                self._err(exc)
                continue
            self.s.operator = operator
            self.s.notify_changed()
            return

    def change_status(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        row = self.issues.current_row()
        bib = int(row[2]) if row and row[2] is not None else self.bib.value()
        reg = repo.find_registration(self.s.conn, self.s.race_id, bib=bib)
        if reg is None:
            return self._err(ValueError("Seleccione una anomalía con dorsal o indique un dorsal válido."))
        combo = QComboBox()
        for code, label in (("REG", "Inscrito (normal)"), ("DNS", "No salió (DNS)"), ("DNF", "No terminó (DNF)"), ("DQ", "Descalificado (DQ)")):
            combo.addItem(label, code)
        combo.setCurrentIndex(max(0, combo.findData(reg["status"])))
        dlg = ReasonDialog("Cambiar estado", f"Dorsal {bib}: estado actual {reg['status']}.", op, self)
        dlg.form.insertRow(1, "Nuevo estado", combo)
        while dlg.exec():
            try:
                reason, operator = dlg.values()
                repo.set_status(self.s.conn, reg["id"], combo.currentData(), reason, operator)
            except ValueError as exc:
                self._err(exc)
                continue
            self.s.operator = operator
            self.s.notify_changed()
            return

    def adjust_gun(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        race = self.s.race()
        if race.gun_time_ms is None:
            return self._err(ValueError("La carrera aún no tiene hora de disparo."))
        dlg = ClockDialog(race.gun_time_ms, op, self)
        while dlg.exec():
            try:
                ts = parse_clock(dlg.clock.text(), race.gun_time_ms)
                reason, operator = dlg.values()
                repo.set_gun(self.s.conn, race.id, ts, reason, operator)
            except ValueError as exc:
                self._err(exc)
                continue
            self.s.operator = operator
            self.s.notify_changed()
            return

    def toggle_void(self) -> None:
        row = self.reads.current_row()
        if row is None:
            return self._err(ValueError("Seleccione una lectura de la lista."))
        op = require_operator(self.s, self)
        if not op:
            return
        is_void = row[5] == "ANULADA"
        verb = "Restaurar" if is_void else "Anular"
        dlg = ReasonDialog(f"{verb} lectura", f"{verb} la lectura {row[3]} en «{row[2]}» a las {format_clock(row[1])}.", op, self)
        while dlg.exec():
            try:
                reason, operator = dlg.values()
                (repo.unvoid_read if is_void else repo.void_read)(self.s.conn, row[0], reason, operator)
            except ValueError as exc:
                self._err(exc)
                continue
            self.s.operator = operator
            self.s.notify_changed()
            return
