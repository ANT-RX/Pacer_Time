"""Resultados: clasificación general, por género y categoría, con parciales; exportación a CSV/HTML."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import exporters
from ..models import GENDER_LABELS, STATUS_LABELS, Computation
from ..timeutil import format_duration, format_pace
from .session import Session
from .widgets import COLOR_ERROR, COLOR_MANUAL, COLOR_MUTED, DataTable

BASE_COLS = ["Pos", "Dorsal", "Nombre", "Género", "Categoría", "Pos género", "Pos cat.", "Pistola", "Neto", "Ritmo/km"]


class ResultsTab(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self._comp: Computation | None = None
        self._headers: list[str] = []
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre o dorsal…")
        self.search.setClearButtonEnabled(True)
        self.cat = QComboBox()
        self.gender = QComboBox()
        self.gender.addItem("Todos los géneros", None)
        for g in ("M", "F", "X"):
            self.gender.addItem(GENDER_LABELS[g], g)
        self.only_fin = QCheckBox("Solo finalizados")
        b_csv = QPushButton("Exportar CSV…")
        b_html = QPushButton("Exportar HTML…")
        bar.addWidget(self.search, 1)
        bar.addWidget(self.cat)
        bar.addWidget(self.gender)
        bar.addWidget(self.only_fin)
        bar.addWidget(b_csv)
        bar.addWidget(b_html)
        lay.addLayout(bar)
        self.table = DataTable(BASE_COLS + ["Estado"])
        m = self.table.source
        m.set_row_color(self._color_row)
        m.set_right_aligned([0, 1, 5, 6, 7, 8, 9])
        lay.addWidget(self.table, 1)
        self.info = QLabel("Sin resultados todavía.")
        lay.addWidget(self.info)

        self.search.textChanged.connect(self.table.set_filter)
        self.cat.currentIndexChanged.connect(self.render)
        self.gender.currentIndexChanged.connect(self.render)
        self.only_fin.toggled.connect(self.render)
        b_csv.clicked.connect(lambda: self.export("csv"))
        b_html.clicked.connect(lambda: self.export("html"))
        self.s.results_ready.connect(self.on_results)
        self.s.race_changed.connect(self._reset)
        if self.s.last_computation:
            self.on_results(self.s.last_computation)
        else:
            self.s.request_results()

    def _reset(self) -> None:
        self._comp = None
        self.table.set_rows([])

    def on_results(self, comp: Computation) -> None:
        self._comp = comp
        names = sorted({r.category_name for r in comp.results if r.category_name})
        cur = self.cat.currentData()
        self.cat.blockSignals(True)
        self.cat.clear()
        self.cat.addItem("Todas las categorías", None)
        for n in names:
            self.cat.addItem(n, n)
        if cur is not None:
            self.cat.setCurrentIndex(max(0, self.cat.findData(cur)))
        self.cat.blockSignals(False)
        self.render()

    def render(self) -> None:
        comp = self._comp
        if comp is None:
            return
        pts = self.s.points()
        splits = [p for p in pts if p.kind == "split"]
        headers = BASE_COLS + [p.name for p in splits] + ["Estado", "flags", "status"]
        if headers != self._headers:
            self.table.set_headers(headers)
            self._headers = headers
            n_vis = len(headers) - 2
            for c in range(n_vis, len(headers)):
                self.table.setColumnHidden(c, True)
            m = self.table.source
            m.set_formatter(3, lambda v: GENDER_LABELS.get(v, v))
            for c in (7, 8):
                m.set_formatter(c, format_duration)
            m.set_formatter(9, format_pace)
            for i in range(len(splits)):
                m.set_formatter(10 + i, format_duration)
            m.set_formatter(10 + len(splits), lambda v: STATUS_LABELS.get(v, v))
            m.set_right_aligned([0, 1, 5, 6, 7, 8, 9] + list(range(10, 10 + len(splits))))
            for c, w in enumerate([50, 70, 220, 80, 150, 80, 70, 90, 90, 80]):
                self.table.setColumnWidth(c, w)
        cat, gender, only = self.cat.currentData(), self.gender.currentData(), self.only_fin.isChecked()
        rows = []
        for r in comp.results:
            if cat and r.category_name != cat:
                continue
            if gender and r.gender != gender:
                continue
            if only and r.status != "FIN":
                continue
            rows.append(
                (r.position, r.bib, r.name, r.gender, r.category_name, r.gender_pos, r.category_pos, r.gun_ms, r.net_ms, r.pace_ms_km)
                + tuple(r.splits_ms.get(p.id) for p in splits)
                + (r.status, r.flags, r.status)
            )
        self.table.set_rows(rows, key_col=1)
        fin = sum(1 for r in comp.results if r.status == "FIN")
        self.info.setText(
            f"{len(rows)} filas mostradas · {fin} finalizaron de {len(comp.results)} · * fila amarilla = incluye tiempo corregido a mano"
        )

    @staticmethod
    def _color_row(row):
        flags, status = row[-2], row[-1]
        if "manual" in (flags or ()):
            return COLOR_MANUAL
        if status == "DQ":
            return COLOR_ERROR
        if status != "FIN":
            return COLOR_MUTED
        return None

    def export(self, kind: str) -> None:
        if self._comp is None:
            return
        race = self.s.race()
        suggested = f"resultados-{race.name}.{kind}".replace(" ", "_")
        flt = "CSV (*.csv)" if kind == "csv" else "HTML (*.html)"
        path, _ = QFileDialog.getSaveFileName(self, "Exportar resultados", suggested, flt)
        if not path:
            return
        try:
            if kind == "csv":
                exporters.write_results_csv(path, self._comp, race, self.s.points())
            else:
                exporters.write_results_html(path, self._comp, race, self.s.points(), title=f"{self.s.event_row()['name']} — {race.name}")
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo exportar", str(exc))
            return
        QMessageBox.information(self, "Exportado", f"Resultados guardados en:\n{path}")
