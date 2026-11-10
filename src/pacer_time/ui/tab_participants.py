"""Pestañas de Participantes (alta/edición/importación) y Check-in (entrega de kit + chip)."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import categories as cats
from .. import importers, repo
from ..models import GENDER_LABELS, STATUS_LABELS
from .dialogs import ParticipantDialog, ReasonDialog
from .session import Session
from .widgets import COLOR_ERROR, COLOR_MUTED, DataTable, require_operator

COLS = ["Dorsal", "Nombre", "Género", "Edad", "Categoría", "Documento", "Club", "Chip", "Kit", "Pagado", "Estado", "reg_id"]


class ParticipantsTab(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre, dorsal, documento, club, chip…")
        self.search.setClearButtonEnabled(True)
        self.count = QLabel()
        b_add = QPushButton("Agregar participante")
        b_edit = QPushButton("Editar")
        b_import = QPushButton("Importar CSV…")
        b_kit = QPushButton("Marcar kit entregado")
        b_status = QPushButton("Estado ▾")
        for b in (b_add, b_edit, b_import, b_kit, b_status):
            b.setAutoDefault(False)
        bar.addWidget(self.search, 1)
        for w in (b_add, b_edit, b_kit, b_status, b_import):
            bar.addWidget(w)
        lay.addLayout(bar)
        self.table = DataTable(COLS)
        self.table.setColumnHidden(len(COLS) - 1, True)
        m = self.table.source
        m.set_formatter(2, lambda v: GENDER_LABELS.get(v, v))
        m.set_formatter(8, lambda v: "✔" if v else "")
        m.set_formatter(9, lambda v: "✔" if v else "")
        m.set_formatter(10, lambda v: STATUS_LABELS.get(v, v) if v != "REG" else "Inscrito")
        m.set_row_color(lambda r: COLOR_MUTED if r[10] in ("DNS", "DNF") else (COLOR_ERROR if r[10] == "DQ" else None))
        m.set_right_aligned([0, 3])
        lay.addWidget(self.table, 1)
        lay.addWidget(self.count)

        self.search.textChanged.connect(self._filter)
        b_add.clicked.connect(self.add)
        b_edit.clicked.connect(self.edit)
        b_import.clicked.connect(self.import_csv)
        b_kit.clicked.connect(self.toggle_kit)
        self.table.doubleClicked.connect(lambda *_: self.edit())
        menu = QMenu(self)
        for code, label in (("REG", "Inscrito (normal)"), ("DNS", "No salió (DNS)"), ("DNF", "No terminó (DNF)"), ("DQ", "Descalificado (DQ)")):
            menu.addAction(label, lambda c=code: self.change_status(c))
        b_status.setMenu(menu)
        self.s.changed.connect(self.refresh)
        self.s.race_changed.connect(self.refresh)
        self.refresh()

    # ---------------------------------------------------------------- datos
    def refresh(self) -> None:
        conn = self.s.conn
        event_date = date.fromisoformat(self.s.event_row()["event_date"])
        rows = []
        for r in repo.list_registrations(conn, self.s.race_id):
            bd = cats.parse_date(r["birth_date"]) if r["birth_date"] else None
            rows.append((
                r["bib"], f"{r['first_name']} {r['last_name']}".strip(), r["gender"], cats.age_on(bd, event_date),
                r["category_name"], r["document"] or "", r["club"], r["chips"], r["kit_delivered"], r["paid"], r["status"], r["reg_id"],
            ))
        self.table.set_rows(rows, key_col=11, fit=False)
        self.count.setText(f"{len(rows)} inscritos · {sum(1 for r in rows if r[8])} con kit entregado · {sum(1 for r in rows if r[7])} con chip")
        if self.table.columnWidth(1) == 100 or self.table.columnWidth(1) == 0:
            for c, w in enumerate([70, 220, 80, 50, 150, 130, 140, 150, 50, 60, 110]):
                self.table.setColumnWidth(c, w)

    def _filter(self, text: str) -> None:
        self.table.set_filter(text)

    def _selected_reg(self):
        row = self.table.current_row()
        if row is None:
            QMessageBox.information(self, "Participantes", "Seleccione un participante de la lista.")
            return None
        return next((r for r in repo.list_registrations(self.s.conn, self.s.race_id) if r["reg_id"] == row[11]), None)

    def _error(self, exc: Exception) -> None:
        QMessageBox.warning(self, "No se pudo completar", str(exc))

    # ---------------------------------------------------------------- acciones
    def add(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        dlg = ParticipantDialog(repo.list_categories(self.s.conn, self.s.race_id), parent=self)
        while dlg.exec():  # si hay error se reabre el mismo diálogo con lo ya escrito
            try:
                repo.register(self.s.conn, self.s.race_id, dlg.values(), operator=op, **dlg.registration_values())
            except ValueError as exc:
                self._error(exc)
                continue
            self.s.notify_changed()
            return

    def edit(self) -> None:
        reg = self._selected_reg()
        if reg is None:
            return
        op = require_operator(self.s, self)
        if not op:
            return
        data = dict(reg)
        data["bib"] = reg["bib"]
        dlg = ParticipantDialog(repo.list_categories(self.s.conn, self.s.race_id), data, parent=self)
        from ..db import tx

        while dlg.exec():
            try:
                conn = self.s.conn
                with tx(conn):
                    repo.update_participant(conn, reg["participant_id"], dlg.values(), op)
                    rv = dlg.registration_values()
                    category_id = rv["category_id"]
                    if category_id is None:
                        v = dlg.values()
                        category_id = repo.suggest_category(conn, self.s.race_id, v["gender"], v["birth_date"].strip() or None)
                    repo.update_registration(
                        conn, reg["reg_id"], op, bib=rv["bib"] or reg["bib"], category_id=category_id,
                        paid=rv["paid"], kit_delivered=rv["kit_delivered"],
                    )
                    old = {c for c in reg["chips"].split() if c}
                    new = {repo.normalize_chip(c) for c in (rv["chip"] or "").split() if c}
                    for code in old - new:
                        repo.remove_chip(conn, self.s.race_id, code, op)
                    for code in new - old:
                        repo.assign_chip(conn, reg["reg_id"], code, op)
            except ValueError as exc:
                self._error(exc)
                continue
            self.s.notify_changed()
            return

    def toggle_kit(self) -> None:
        reg = self._selected_reg()
        if reg is None:
            return
        op = require_operator(self.s, self)
        if not op:
            return
        repo.update_registration(self.s.conn, reg["reg_id"], op, kit_delivered=not reg["kit_delivered"])
        self.s.notify_changed()

    def change_status(self, new_status: str) -> None:
        reg = self._selected_reg()
        if reg is None or reg["status"] == new_status:
            return
        op = require_operator(self.s, self)
        if not op:
            return
        dlg = ReasonDialog(
            "Cambiar estado",
            f"Dorsal {reg['bib']} ({reg['first_name']} {reg['last_name']}): {reg['status']} → {new_status}",
            op, self,
        )
        if dlg.exec():
            reason, operator = dlg.values()
            try:
                repo.set_status(self.s.conn, reg["reg_id"], new_status, reason, operator)
            except ValueError as exc:
                return self._error(exc)
            self.s.operator = operator
            self.s.notify_changed()

    def import_csv(self) -> None:
        op = require_operator(self.s, self)
        if not op:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Importar inscripciones", "", "CSV (*.csv *.txt);;Todos (*)")
        if not path:
            return
        try:
            rep = importers.import_participants_csv(self.s.conn, self.s.race_id, path, op)
        except Exception as exc:  # archivo ilegible, codificación, etc.
            return self._error(exc)
        lines = [rep.summary()]
        if rep.unknown_columns:
            lines.append("Columnas ignoradas: " + ", ".join(rep.unknown_columns))
        for ln, msg in rep.errors[:15]:
            lines.append(f"  Línea {ln}: {msg}")
        if len(rep.errors) > 15:
            lines.append(f"  … y {len(rep.errors) - 15} errores más")
        QMessageBox.information(self, "Importación terminada", "\n".join(lines))
        self.s.notify_changed()


class CheckInTab(QWidget):
    """Mostrador de entrega de kits: busca al corredor, lee/teclea el chip y marca el kit como entregado."""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.s = session
        self._reg = None
        lay = QVBoxLayout(self)
        title = QLabel("Check-in / entrega de kit")
        title.setStyleSheet("font-size:20px;font-weight:700;")
        lay.addWidget(title)
        form = QFormLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Dorsal, documento o código de chip — Enter para buscar")
        self.search.setStyleSheet("font-size:18px;padding:6px;")
        self.chip = QLineEdit()
        self.chip.setPlaceholderText("Pase el chip por el lector USB o escriba el código — Enter para confirmar")
        self.chip.setStyleSheet("font-size:18px;padding:6px;")
        form.addRow("Buscar corredor", self.search)
        form.addRow("Chip", self.chip)
        lay.addLayout(form)
        self.card = QLabel("Busque a un corredor para comenzar.")
        self.card.setWordWrap(True)
        self.card.setTextFormat(Qt.TextFormat.RichText)
        self.card.setFrameShape(QFrame.Shape.StyledPanel)
        self.card.setStyleSheet("padding:14px;font-size:16px;background:#f6f8fa;border-radius:8px;")
        self.card.setMinimumHeight(170)
        self.card.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.card)
        self.msg = QLabel()
        self.msg.setStyleSheet("font-size:15px;font-weight:600;")
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        self.btn = QPushButton("Confirmar entrega de kit")
        self.btn.setEnabled(False)
        self.btn.setStyleSheet("font-size:16px;padding:8px 18px;")
        row.addWidget(self.btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.search.returnPressed.connect(self.lookup)
        self.chip.returnPressed.connect(self.confirm)
        self.btn.clicked.connect(self.confirm)

    def _say(self, text: str, ok: bool = True) -> None:
        self.msg.setStyleSheet(f"font-size:15px;font-weight:600;color:{'#1b7f3b' if ok else '#b42318'};")
        self.msg.setText(text)

    def lookup(self) -> None:
        text = self.search.text().strip()
        conn, rid = self.s.conn, self.s.race_id
        reg = None
        if text.isdigit():
            reg = repo.find_registration(conn, rid, bib=int(text))
        if reg is None and text:
            reg = repo.find_registration(conn, rid, document=text) or repo.find_registration(conn, rid, chip=text)
        if reg is None:
            self._reg = None
            self.btn.setEnabled(False)
            self.card.setText("")
            return self._say(f"No se encontró «{text}».", ok=False)
        full = next(r for r in repo.list_registrations(conn, rid) if r["reg_id"] == reg["id"])
        self._reg = full
        self.btn.setEnabled(True)
        warn = []
        if full["status"] != "REG":
            warn.append(f"Estado: {STATUS_LABELS.get(full['status'], full['status'])}")
        if not full["paid"]:
            warn.append("PAGO PENDIENTE")
        if full["kit_delivered"]:
            warn.append("el kit ya fue entregado")
        self.card.setText(
            f"<div style='font-size:30px;font-weight:800'>#{full['bib']} &nbsp; {full['first_name']} {full['last_name']}</div>"
            f"<div>{GENDER_LABELS.get(full['gender'], '')} · {full['category_name'] or 'sin categoría'} · {full['club'] or 'sin club'}</div>"
            f"<div>Documento: {full['document'] or '—'} &nbsp; Chip actual: <b>{full['chips'] or '—'}</b></div>"
            f"<div>Emergencia: {full['emergency_contact'] or '—'} {full['emergency_phone']}</div>"
            + (f"<div style='color:#b42318;font-weight:700;margin-top:6px'>⚠ {' · '.join(warn)}</div>" if warn else "")
        )
        self._say("Corredor encontrado. Escanee el chip.")
        self.chip.setFocus()

    def confirm(self) -> None:
        if self._reg is None:
            return self._say("Primero busque a un corredor.", ok=False)
        op = require_operator(self.s, self)
        if not op:
            return
        code = self.chip.text().strip()
        try:
            if code:
                repo.assign_chip(self.s.conn, self._reg["reg_id"], code, op)
            repo.update_registration(self.s.conn, self._reg["reg_id"], op, kit_delivered=True)
        except ValueError as exc:
            return self._say(str(exc), ok=False)
        bib = self._reg["bib"]
        self._reg = None
        self.btn.setEnabled(False)
        self.search.clear()
        self.chip.clear()
        self.card.setText("")
        self._say(f"✔ Dorsal {bib}: kit entregado" + (f", chip {repo.normalize_chip(code)} asignado." if code else "."))
        self.search.setFocus()
        self.s.notify_changed()
