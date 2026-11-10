"""Widgets reutilizables: tabla ordenable/filtrable sobre un modelo simple basado en listas."""

from __future__ import annotations

from typing import Any, Callable, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableView

ROLE_RAW = Qt.ItemDataRole.UserRole

COLOR_ERROR = QColor("#fde8e8")
COLOR_WARN = QColor("#fff4d6")
COLOR_OK = QColor("#e6f6ea")
COLOR_MUTED = QColor("#f0f1f3")
COLOR_MANUAL = QColor("#fff9c4")


class TableModel(QAbstractTableModel):
    def __init__(self, headers: Sequence[str], parent=None):
        super().__init__(parent)
        self._headers = list(headers)
        self._rows: list[Sequence[Any]] = []
        self._fmt: dict[int, Callable[[Any], str]] = {}
        self._right: set[int] = set()
        self._row_color: Callable[[Sequence[Any]], QColor | None] | None = None

    def set_headers(self, headers: Sequence[str]) -> None:
        self.beginResetModel()
        self._headers = list(headers)
        self.endResetModel()

    def set_rows(self, rows: list[Sequence[Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def set_formatter(self, col: int, fn: Callable[[Any], str]) -> None:
        self._fmt[col] = fn

    def set_right_aligned(self, cols: Sequence[int]) -> None:
        self._right = set(cols)

    def set_row_color(self, fn: Callable[[Sequence[Any]], QColor | None] | None) -> None:
        self._row_color = fn

    def row(self, i: int) -> Sequence[Any]:
        return self._rows[i]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        value = row[col] if col < len(row) else None
        if role == Qt.ItemDataRole.DisplayRole:
            fmt = self._fmt.get(col)
            if fmt:
                return fmt(value)
            return "" if value is None else str(value)
        if role == ROLE_RAW:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and col in self._right:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole and self._row_color:
            c = self._row_color(row)
            return QBrush(c) if c else None
        return None


class DataTable(QTableView):
    """Tabla con orden por columna (usando el valor crudo), filtro de texto y conservación de la selección."""

    def __init__(self, headers: Sequence[str], parent=None):
        super().__init__(parent)
        self.source = TableModel(headers, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.source)
        self.proxy.setSortRole(ROLE_RAW)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # orden original hasta que el usuario elija una columna
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    def set_headers(self, headers: Sequence[str]) -> None:
        self.source.set_headers(headers)

    def set_rows(self, rows: list[Sequence[Any]], key_col: int | None = None, fit: bool = False) -> None:
        sel_key = None
        if key_col is not None:
            cur = self.current_row()
            sel_key = cur[key_col] if cur is not None else None
        scroll = self.verticalScrollBar().value()
        self.source.set_rows(rows)
        self.verticalScrollBar().setValue(scroll)
        if fit:
            self.resizeColumnsToContents()
        if sel_key is not None and key_col is not None:
            for i, r in enumerate(rows):
                if r[key_col] == sel_key:
                    idx = self.proxy.mapFromSource(self.source.index(i, 0))
                    if idx.isValid():
                        self.selectRow(idx.row())
                    break

    def set_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)

    def current_row(self) -> Sequence[Any] | None:
        sel = self.selectionModel().selectedRows()
        idx = sel[0] if sel else self.currentIndex()
        if not idx.isValid():
            return None
        return self.source.row(self.proxy.mapToSource(idx).row())

    def visible_row_count(self) -> int:
        return self.proxy.rowCount()


class Badge(QLabel):
    """Etiqueta con color de fondo (estado de carrera, de lector, etc.)."""

    STYLES = {
        "ok": "background:#d8f3dc;color:#1b4332;",
        "warn": "background:#fff0c2;color:#7a5200;",
        "error": "background:#fcd9d9;color:#7a1f1f;",
        "muted": "background:#e9ecef;color:#495057;",
        "info": "background:#dbeafe;color:#1e3a8a;",
    }

    def __init__(self, text: str = "", kind: str = "muted", parent=None):
        super().__init__(parent)
        self.set(text, kind)

    def set(self, text: str, kind: str = "muted") -> None:
        self.setText(text)
        self.setStyleSheet(f"{self.STYLES.get(kind, self.STYLES['muted'])}border-radius:9px;padding:2px 10px;font-weight:600;")


def require_operator(session, parent=None) -> str | None:
    """Devuelve el operador activo; si no hay, lo pide. None si se cancela."""
    from PySide6.QtWidgets import QInputDialog, QLineEdit

    if session.operator.strip():
        return session.operator.strip()
    text, ok = QInputDialog.getText(parent, "Operador", "Escriba su nombre (queda en el historial de cambios):", QLineEdit.EchoMode.Normal)
    if ok and text.strip():
        session.operator = text.strip()
        return session.operator
    return None
