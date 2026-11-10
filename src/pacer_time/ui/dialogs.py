"""Diálogos de captura de datos."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from ..models import AWARD_LABELS, AWARD_MODES, Category, TimingPoint
from ..timeutil import format_clock, now_ms


class _FormDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.layout_ = QVBoxLayout(self)
        self.form = QFormLayout()
        self.layout_.addLayout(self.form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._try_accept)
        self.buttons.rejected.connect(self.reject)
        self.layout_.addWidget(self.buttons)

    def _try_accept(self) -> None:
        try:
            self.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Revise los datos", str(exc))
            return
        self.accept()

    def validate(self) -> None:  # sobreescribir
        pass


class NewEventDialog(_FormDialog):
    def __init__(self, parent=None):
        super().__init__("Nuevo evento", parent)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Ej. Carrera de la Ciudad 2026")
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("dd/MM/yyyy")
        self.city = QLineEdit()
        self.race_name = QLineEdit("5K")
        self.distance = QDoubleSpinBox()
        self.distance.setRange(0, 400)
        self.distance.setDecimals(3)
        self.distance.setValue(5)
        self.distance.setSuffix(" km")
        self.mode = QComboBox()
        for m in AWARD_MODES:
            self.mode.addItem(AWARD_LABELS[m], m)
        self.mode.setCurrentIndex(AWARD_MODES.index("hybrid"))
        self.splits = QSpinBox()
        self.splits.setRange(0, 9)
        self.splits.setToolTip("Puntos intermedios con tapete/antena además de salida y meta")
        for label, w in [
            ("Evento", self.name), ("Fecha", self.date), ("Ciudad", self.city),
            ("Nombre de la carrera", self.race_name), ("Distancia", self.distance),
            ("Premiación", self.mode), ("Puntos intermedios", self.splits),
        ]:
            self.form.addRow(label, w)

    def validate(self) -> None:
        if not self.name.text().strip():
            raise ValueError("Escriba el nombre del evento.")
        if not self.race_name.text().strip():
            raise ValueError("Escriba el nombre de la carrera.")

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(),
            "date": self.date.date().toString("yyyy-MM-dd"),
            "city": self.city.text().strip(),
            "race_name": self.race_name.text().strip(),
            "distance_m": round(self.distance.value() * 1000),
            "award_mode": self.mode.currentData(),
            "splits": self.splits.value(),
        }


class ParticipantDialog(_FormDialog):
    def __init__(self, categories: list[Category], data: dict | None = None, parent=None):
        super().__init__("Participante" if data else "Nuevo participante", parent)
        self.editing = data is not None
        d = data or {}
        self.first = QLineEdit(d.get("first_name", ""))
        self.last = QLineEdit(d.get("last_name", ""))
        self.document = QLineEdit(d.get("document") or "")
        self.document.setPlaceholderText("CURP, INE, pasaporte…")
        self.birth = QLineEdit(d.get("birth_date") or "")
        self.birth.setPlaceholderText("AAAA-MM-DD o DD/MM/AAAA")
        self.gender = QComboBox()
        for code, label in (("M", "Varonil"), ("F", "Femenil"), ("X", "Otro / no especifica")):
            self.gender.addItem(label, code)
        self.gender.setCurrentIndex(max(0, self.gender.findData(d.get("gender", "M"))))
        self.club = QLineEdit(d.get("club", ""))
        self.email = QLineEdit(d.get("email", ""))
        self.phone = QLineEdit(d.get("phone", ""))
        self.emerg = QLineEdit(d.get("emergency_contact", ""))
        self.emerg_phone = QLineEdit(d.get("emergency_phone", ""))
        self.bib = QSpinBox()
        self.bib.setRange(0, 999999)
        self.bib.setSpecialValueText("Automático")
        self.bib.setValue(d.get("bib") or 0)
        self.category = QComboBox()
        self.category.addItem("Automática (por edad y género)", None)
        for c in categories:
            self.category.addItem(c.name, c.id)
        if d.get("category_id") is not None:
            self.category.setCurrentIndex(max(0, self.category.findData(d["category_id"])))
        self.chip = QLineEdit(d.get("chips", ""))
        self.chip.setPlaceholderText("Código del chip (escanee o escriba)")
        self.paid = QCheckBox("Pagado")
        self.paid.setChecked(bool(d.get("paid")))
        self.kit = QCheckBox("Kit entregado")
        self.kit.setChecked(bool(d.get("kit_delivered")))
        rows = [
            ("Nombre(s)*", self.first), ("Apellidos", self.last), ("Documento", self.document),
            ("Fecha de nacimiento", self.birth), ("Género", self.gender), ("Club / equipo", self.club),
            ("Correo", self.email), ("Teléfono", self.phone), ("Contacto de emergencia", self.emerg),
            ("Tel. de emergencia", self.emerg_phone), ("Dorsal", self.bib), ("Categoría", self.category),
            ("Chip", self.chip),
        ]
        for label, w in rows:
            self.form.addRow(label, w)
        self.form.addRow("", self.paid)
        self.form.addRow("", self.kit)

    def validate(self) -> None:
        if not (self.first.text().strip() or self.last.text().strip()):
            raise ValueError("Falta el nombre del participante.")

    def values(self) -> dict:
        return {
            "first_name": self.first.text(), "last_name": self.last.text(), "document": self.document.text(),
            "birth_date": self.birth.text(), "gender": self.gender.currentData(), "club": self.club.text(),
            "email": self.email.text(), "phone": self.phone.text(), "emergency_contact": self.emerg.text(),
            "emergency_phone": self.emerg_phone.text(),
        }

    def registration_values(self) -> dict:
        return {
            "bib": self.bib.value() or None,
            "category_id": self.category.currentData(),
            "chip": self.chip.text().strip() or None,
            "paid": self.paid.isChecked(),
            "kit_delivered": self.kit.isChecked(),
        }


class ReasonDialog(_FormDialog):
    """Toda corrección exige motivo y operador (quedan en el historial)."""

    def __init__(self, title: str, description: str, operator: str, parent=None):
        super().__init__(title, parent)
        desc = QLabel(description)
        desc.setWordWrap(True)
        self.form.addRow(desc)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Ej. «Lectura perdida; tiempo tomado del video»")
        self.operator = QLineEdit(operator)
        self.form.addRow("Motivo*", self.reason)
        self.form.addRow("Operador*", self.operator)

    def validate(self) -> None:
        if not self.reason.text().strip():
            raise ValueError("Escriba el motivo de la corrección.")
        if not self.operator.text().strip():
            raise ValueError("Escriba su nombre (operador).")

    def values(self) -> tuple[str, str]:
        return self.reason.text().strip(), self.operator.text().strip()


class ManualTimeDialog(ReasonDialog):
    def __init__(self, points: list[TimingPoint], bib: int | None, operator: str, point_id: int | None = None, parent=None):
        super().__init__(
            "Fijar tiempo de paso",
            "Agrega un tiempo perdido o reemplaza el leído. Use la hora del reloj del día (la misma que muestra el sistema).",
            operator,
            parent,
        )
        self.bib = QSpinBox()
        self.bib.setRange(1, 999999)
        self.bib.setValue(bib or 1)
        self.point = QComboBox()
        for p in points:
            self.point.addItem(p.name, p.id)
        if point_id is not None:
            self.point.setCurrentIndex(max(0, self.point.findData(point_id)))
        else:
            self.point.setCurrentIndex(self.point.count() - 1)  # por defecto: meta
        self.clock = QLineEdit(format_clock(now_ms()))
        self.clock.setPlaceholderText("HH:MM:SS.mmm")
        self.form.insertRow(1, "Dorsal", self.bib)
        self.form.insertRow(2, "Punto", self.point)
        self.form.insertRow(3, "Hora del paso*", self.clock)


class ChipDialog(_FormDialog):
    def __init__(self, bib: int, parent=None):
        super().__init__(f"Asignar chip al dorsal {bib}", parent)
        self.code = QLineEdit()
        self.code.setPlaceholderText("Escanee o escriba el código del chip")
        self.form.addRow("Chip", self.code)

    def validate(self) -> None:
        if not self.code.text().strip():
            raise ValueError("Escriba el código del chip.")


def today_iso() -> str:
    return date.today().isoformat()


class ClockDialog(ReasonDialog):
    """Ajuste del instante del disparo de salida."""

    def __init__(self, current_ms: int | None, operator: str, parent=None):
        super().__init__(
            "Ajustar hora del disparo",
            "Cambia la hora oficial de salida (gun time) de la carrera. Todos los tiempos de pistola se recalculan; las lecturas no se tocan.",
            operator,
            parent,
        )
        self.clock = QLineEdit(format_clock(current_ms if current_ms is not None else now_ms()))
        self.clock.setPlaceholderText("HH:MM:SS.mmm")
        self.form.insertRow(1, "Hora del disparo*", self.clock)
