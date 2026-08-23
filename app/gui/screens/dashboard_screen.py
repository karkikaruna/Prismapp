"""Dashboard screen - PRISM's main analytical surface.

This is prompt-reliability's dashboard (KPI cards, filter bar, four bar
charts, and a sortable detail table) rewired onto the merged app's own
data layer (``app.services.backend`` / the local SQLite run index +
bundled seed results) instead of prism_app's ``appdata``/``resources``
modules, plus an added **comparison-model** selector so two models can be
viewed side by side without leaving the screen.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QStandardPaths, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from prism_core import config
from app.gui.theme import DUSK, PAPER
from app.services import backend, pdf_report, app_state

try:
    from PySide6.QtCharts import (
        QBarCategoryAxis,
        QBarSeries,
        QBarSet,
        QChart,
        QChartView,
        QLineSeries,
        QValueAxis,
    )
    HAS_CHARTS = True
except Exception:
    HAS_CHARTS = False


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value: Any) -> str:
    return f"{_f(value) * 100.0:.1f}%"


def _clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class KPICard(QFrame):
    """Compact summary metric card."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("KpiCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("KpiTitle")
        layout.addWidget(self._title_lbl)

        self._value_lbl = QLabel("-")
        self._value_lbl.setObjectName("KpiValue")
        layout.addWidget(self._value_lbl)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setObjectName("Faint")
        layout.addWidget(self._sub_lbl)

    def set_data(self, value: str, subtext: str = "") -> None:
        self._value_lbl.setText(value)
        self._sub_lbl.setText(subtext)


def _active_palette():
    """Resolve the palette (paper/dusk) currently selected in Settings, so
    charts are drawn to match - instead of always rendering in "paper"
    colors even when the rest of the app is in dark ("dusk") mode."""
    return DUSK if app_state.get_theme() == "dusk" else PAPER


def _chart_series(p) -> list[str]:
    return [p.accent, p.good, p.warn, p.bad, p.text_dim]


class DashboardScreen(QWidget):
    """Comprehensive analytical dashboard for research and user benchmark results."""

    benchmark_requested = Signal(str)  # model_tag the user wants to download+run to compare

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._ds_rows: list[dict[str, Any]] = []
        self._pr_rows: list[dict[str, Any]] = []

        self._build_ui()
        self.reload_data()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        # Outer widget fills the scroll area and centers a max-width
        # content column, so the dashboard reads as a centered page with
        # generous breathing room instead of stretching edge-to-edge on
        # wide windows.
        outer_container = QWidget()
        outer_container.setObjectName("DashboardContainer")
        outer_row = QHBoxLayout(outer_container)
        outer_row.setContentsMargins(0, 0, 0, 0)
        outer_row.setSpacing(0)
        outer_row.addStretch(1)

        container = QWidget()
        container.setObjectName("DashboardContent")
        container.setMaximumWidth(1180)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(40, 36, 40, 44)
        layout.setSpacing(28)

        outer_row.addWidget(container, 10)
        outer_row.addStretch(1)

        # 1. Header -----------------------------------------------------
        header_box = QVBoxLayout()
        header_box.setSpacing(2)
        title = QLabel("Dashboard")
        title.setObjectName("H1")
        header_box.addWidget(title)
        subtitle = QLabel(
            "Explore PRISM benchmark results across models, datasets, and prompting conditions."
        )
        subtitle.setObjectName("Body")
        header_box.addWidget(subtitle)
        layout.addLayout(header_box)

        # 2. Filter bar (model, comparison model, dataset, source) ------
        # Two rows instead of one long strip: filters on top, actions
        # below - so the action buttons never get squeezed/clipped when
        # the window is narrower than all four combos + both buttons.
        filter_frame = QFrame()
        filter_frame.setObjectName("FilterBar")
        filter_outer = QVBoxLayout(filter_frame)
        filter_outer.setContentsMargins(20, 16, 20, 16)
        filter_outer.setSpacing(14)

        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(18)

        filter_layout.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(160)
        self._model_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._model_combo)

        # --- Comparison model selector -----------------------------------
        compare_lbl = QLabel("Compare with:")
        filter_layout.addWidget(compare_lbl)
        self._compare_combo = QComboBox()
        self._compare_combo.setMinimumWidth(160)
        self._compare_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._compare_combo)
        # Kept in sync by _refresh_compare_options(): rebuilt any time the
        # primary "Model:" selection changes, since which models are valid
        # to compare against depends on it (see that method for why).
        self._model_combo.currentIndexChanged.connect(self._refresh_compare_options)

        filter_layout.addWidget(QLabel("Dataset:"))
        self._dataset_combo = QComboBox()
        self._dataset_combo.setMinimumWidth(150)
        self._dataset_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._dataset_combo)

        filter_layout.addWidget(QLabel("Source:"))
        self._source_combo = QComboBox()
        self._source_combo.setMinimumWidth(150)
        self._source_combo.addItems(["All Sources", "Research Results", "User Runs"])
        self._source_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._source_combo)

        filter_layout.addStretch(1)
        filter_outer.addLayout(filter_layout)

        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(10)
        actions_layout.addStretch(1)

        refresh_btn = QPushButton("Refresh Data")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        refresh_btn.clicked.connect(self.reload_data)
        actions_layout.addWidget(refresh_btn)

        download_pdf_btn = QPushButton("Download PDF")
        download_pdf_btn.setObjectName("Primary")
        download_pdf_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        download_pdf_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        download_pdf_btn.clicked.connect(self._on_download_pdf)
        actions_layout.addWidget(download_pdf_btn)

        filter_outer.addLayout(actions_layout)

        layout.addWidget(filter_frame)

        # 3. KPI cards ----------------------------------------------------
        kpi_grid = QGridLayout()
        kpi_grid.setHorizontalSpacing(18)
        kpi_grid.setVerticalSpacing(18)

        self._kpi_acc = KPICard("Accuracy", self)
        self._kpi_agree = KPICard("Mean Agreement", self)
        self._kpi_sens = KPICard("Prompt Sensitivity", self)
        self._kpi_comp = KPICard("Instruction Compliance", self)
        self._kpi_rec = KPICard("Answer Recovery", self)
        self._kpi_unk = KPICard("Unknown Rate", self)

        kpi_grid.addWidget(self._kpi_acc, 0, 0)
        kpi_grid.addWidget(self._kpi_agree, 0, 1)
        kpi_grid.addWidget(self._kpi_sens, 0, 2)
        kpi_grid.addWidget(self._kpi_comp, 1, 0)
        kpi_grid.addWidget(self._kpi_rec, 1, 1)
        kpi_grid.addWidget(self._kpi_unk, 1, 2)

        layout.addLayout(kpi_grid)

        # 4. Charts ---------------------------------------------------------
        if HAS_CHARTS:
            self._charts_grid = QGridLayout()
            self._charts_grid.setHorizontalSpacing(22)
            self._charts_grid.setVerticalSpacing(22)
            self._charts_grid.setColumnStretch(0, 1)
            self._charts_grid.setColumnStretch(1, 1)
            layout.addLayout(self._charts_grid)
        else:
            no_charts = QLabel(
                "Note: PySide6.QtCharts is not available in the current environment. "
                "All analytical data is displayed in the table below."
            )
            no_charts.setObjectName("Faint")
            layout.addWidget(no_charts)

        # 5. Detail table -----------------------------------------------
        table_box = QVBoxLayout()
        table_box.setSpacing(12)
        table_header_row = QHBoxLayout()
        table_title = QLabel("Detailed Results")
        table_title.setObjectName("H2")
        table_header_row.addWidget(table_title)
        table_header_row.addStretch(1)
        table_box.addLayout(table_header_row)

        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSortingEnabled(True)
        self._table.setMinimumHeight(320)
        table_box.addWidget(self._table)

        layout.addLayout(table_box)

        scroll.setWidget(outer_container)
        main_layout.addWidget(scroll)

    # --------------------------------------------------------- Data loading
    def reload_data(self) -> None:
        """Load bundled research data + any local user runs from the merged
        app's own SQLite run index (app.services.backend), instead of
        prism_app's file-based appdata/resources lookup."""
        self._ds_rows = []
        self._pr_rows = []

        conn: sqlite3.Connection = backend.get_conn()
        try:
            for tag, run in backend.models_with_data(conn).items():
                is_seed = str(run["app_version"] or "").startswith("bundled-seed")
                source = "Research" if is_seed else "User Run"

                for row in backend.model_dataset_rows(conn, tag):
                    row = dict(row)
                    row["source"] = source
                    row["run_id"] = run["benchmark_run_id"]
                    self._ds_rows.append(row)

                for row in backend.model_prompt_rows(conn, tag):
                    row = dict(row)
                    row["model"] = tag
                    row["source"] = source
                    row["run_id"] = run["benchmark_run_id"]
                    self._pr_rows.append(row)
        finally:
            conn.close()

        self._populate_filter_options()
        self._update_views()

    def _populate_filter_options(self) -> None:
        cur_model = self._model_combo.currentText()
        cur_compare = self._compare_combo.currentText()
        cur_ds = self._dataset_combo.currentText()

        models = sorted({r.get("model", "") for r in self._ds_rows if r.get("model")})
        datasets = sorted({r.get("dataset", "") for r in self._ds_rows if r.get("dataset")})
        self._all_models_cache = models

        for combo in (self._model_combo, self._compare_combo, self._dataset_combo):
            combo.blockSignals(True)

        self._model_combo.clear()
        self._model_combo.addItem("All Models")
        for m in models:
            self._model_combo.addItem(m)

        self._dataset_combo.clear()
        self._dataset_combo.addItem("All Datasets")
        for d in datasets:
            self._dataset_combo.addItem(d)

        idx = self._model_combo.findText(cur_model)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        idx = self._dataset_combo.findText(cur_ds)
        if idx >= 0:
            self._dataset_combo.setCurrentIndex(idx)

        for combo in (self._model_combo, self._dataset_combo):
            combo.blockSignals(False)

        # Compare combo depends on the (now possibly-restored) primary
        # model selection, so build it after restoring cur_model above.
        self._refresh_compare_options(preferred=cur_compare)

    def _refresh_compare_options(self, *_args, preferred: Optional[str] = None) -> None:
        """Rebuild the "Compare with" list so it can never offer a
        self-comparison, and disable it entirely when the primary "Model:"
        selector is "All Models" - comparing "all models" against one
        specific model isn't a coherent view, so the option shouldn't be
        there to pick in the first place."""
        primary = self._model_combo.currentText()
        preferred = self._compare_combo.currentText() if preferred is None else preferred

        self._compare_combo.blockSignals(True)
        self._compare_combo.clear()
        self._compare_combo.addItem("None")

        if primary == "All Models":
            self._compare_combo.setEnabled(False)
            self._compare_combo.setToolTip(
                "Pick a specific model above to compare it against another."
            )
        else:
            self._compare_combo.setEnabled(True)
            self._compare_combo.setToolTip("")
            models = getattr(self, "_all_models_cache", [])
            for m in models:
                if m != primary:  # never offer comparing a model to itself
                    self._compare_combo.addItem(m)

        idx = self._compare_combo.findText(preferred)
        self._compare_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._compare_combo.blockSignals(False)
        self._on_filter_changed()

    def _on_filter_changed(self) -> None:
        self._update_views()

    # ------------------------------------------------------ view filtering
    def _selected_models(self) -> Optional[list[str]]:
        """None means 'all models'; otherwise the exact set of model tags
        to include (primary + comparison model, when one is chosen)."""
        primary = self._model_combo.currentText()
        compare = self._compare_combo.currentText()

        selected: list[str] = []
        if primary != "All Models":
            selected.append(primary)
        if compare != "None" and compare not in selected:
            selected.append(compare)

        return selected or None

    def _apply_common_filters(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        models = self._selected_models()
        sel_ds = self._dataset_combo.currentText()
        sel_src = self._source_combo.currentText()

        out = []
        for r in rows:
            if models is not None and r.get("model") not in models:
                continue
            if sel_ds != "All Datasets" and r.get("dataset") != sel_ds:
                continue
            if sel_src == "Research Results" and r.get("source") != "Research":
                continue
            if sel_src == "User Runs" and r.get("source") != "User Run":
                continue
            out.append(r)
        return out

    def _filtered_ds_rows(self) -> list[dict[str, Any]]:
        return self._apply_common_filters(self._ds_rows)

    def _filtered_pr_rows(self) -> list[dict[str, Any]]:
        return self._apply_common_filters(self._pr_rows)

    def _update_views(self) -> None:
        ds_filtered = self._filtered_ds_rows()
        pr_filtered = self._filtered_pr_rows()

        self._update_kpi_cards(ds_filtered)
        if HAS_CHARTS:
            self._update_charts(ds_filtered, pr_filtered)
        self._update_table(ds_filtered)

    def _update_kpi_cards(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            for kpi in (self._kpi_acc, self._kpi_agree, self._kpi_sens,
                        self._kpi_comp, self._kpi_rec, self._kpi_unk):
                kpi.set_data("-", "No data matching filter")
            return

        n = len(rows)
        avg_acc = sum(_f(r.get("prompt_response_accuracy")) for r in rows) / n
        avg_agree = sum(_f(r.get("mean_agreement")) for r in rows) / n
        avg_sens = sum(_f(r.get("mean_prompt_sensitivity")) for r in rows) / n
        avg_comp = sum(_f(r.get("instruction_compliance_rate")) for r in rows) / n
        avg_rec = sum(_f(r.get("answer_recovery_rate")) for r in rows) / n
        avg_unk = sum(_f(r.get("unknown_rate")) for r in rows) / n

        compare_active = self._compare_combo.currentText() != "None"
        context_str = (
            f"Comparing {n} evaluation slice(s)" if compare_active
            else f"Across {n} evaluation slice(s)"
        )
        self._kpi_acc.set_data(_pct(avg_acc), context_str)
        self._kpi_agree.set_data(_pct(avg_agree), context_str)
        self._kpi_sens.set_data(_pct(avg_sens), context_str)
        self._kpi_comp.set_data(_pct(avg_comp), context_str)
        self._kpi_rec.set_data(_pct(avg_rec), context_str)
        self._kpi_unk.set_data(_pct(avg_unk), context_str)

    def _update_charts(self, ds_rows: list[dict[str, Any]], pr_rows: list[dict[str, Any]]) -> None:
        _clear_layout(self._charts_grid)
        if not ds_rows:
            return

        datasets = sorted({r.get("dataset", "") for r in ds_rows})
        models = sorted({r.get("model", "") for r in ds_rows})

        if models and datasets:
            series_acc = {}
            for ds in datasets:
                ds_map = {r.get("model"): _f(r.get("prompt_response_accuracy")) for r in ds_rows if r.get("dataset") == ds}
                series_acc[ds] = [ds_map.get(m, 0.0) for m in models]

            self._charts_grid.addWidget(
                self._create_bar_chart("Model Accuracy Comparison", models, series_acc), 0, 0,
            )

            series_outcome = {
                "Unanimous Correct": [],
                "Invariant Incorrect": [],
                "Prompt-Variable": [],
            }
            for ds in datasets:
                d_rows = [r for r in ds_rows if r.get("dataset") == ds]
                if d_rows:
                    unanimous = sum(_f(r.get("answer_unanimous_rate")) for r in d_rows) / len(d_rows)
                    invariant_wrong = sum(_f(r.get("prompt_invariant_incorrect_rate")) for r in d_rows) / len(d_rows)
                    variable = max(0.0, 1.0 - unanimous - invariant_wrong)
                else:
                    unanimous = invariant_wrong = variable = 0.0
                series_outcome["Unanimous Correct"].append(unanimous)
                series_outcome["Invariant Incorrect"].append(invariant_wrong)
                series_outcome["Prompt-Variable"].append(variable)

            self._charts_grid.addWidget(
                self._create_bar_chart("Question Outcome Profile", datasets, series_outcome), 0, 1,
            )

        if pr_rows:
            conditions = list(config.PROMPT_CONDITIONS)
            pr_by_model: dict[str, list[float]] = {}
            for m in models:
                m_pr = [r for r in pr_rows if r.get("model") == m]
                if m_pr:
                    cond_accs = []
                    for c in conditions:
                        c_rows = [r for r in m_pr if r.get("prompt_condition") == c]
                        val = sum(_f(r.get("accuracy")) for r in c_rows) / len(c_rows) if c_rows else 0.0
                        cond_accs.append(val)
                    pr_by_model[m] = cond_accs

            if pr_by_model:
                self._charts_grid.addWidget(
                    self._create_line_chart("Accuracy by Prompt Condition (P0-P4)", conditions, pr_by_model), 1, 0,
                )

            comp_series = {}
            for ds in datasets:
                ds_map = {r.get("model"): _f(r.get("instruction_compliance_rate")) for r in ds_rows if r.get("dataset") == ds}
                comp_series[ds] = [ds_map.get(m, 0.0) for m in models]

            self._charts_grid.addWidget(
                self._create_bar_chart("Instruction Compliance Rate", models, comp_series), 1, 1,
            )

    def _create_bar_chart(
        self, title: str, categories: list[str], series_map: dict[str, list[float]]
    ) -> "QChartView":
        p = _active_palette()
        chart_series = _chart_series(p)
        chart = QChart()
        chart.setTitle(title)
        chart.setBackgroundBrush(QBrush(QColor(p.surface_raised)))
        chart.setBackgroundPen(QPen(Qt.PenStyle.NoPen))
        chart.setPlotAreaBackgroundVisible(False)
        chart.setTitleBrush(QBrush(QColor(p.text)))

        series = QBarSeries()
        for i, (name, values) in enumerate(series_map.items()):
            bar_set = QBarSet(name)
            for val in values:
                bar_set.append(val)
            bar_set.setColor(QColor(chart_series[i % len(chart_series)]))
            bar_set.setBorderColor(QColor(p.surface_raised))
            series.append(bar_set)
        chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        axis_x.setLabelsColor(QColor(p.text_dim))
        axis_x.setLinePen(QPen(QColor(p.hairline)))
        axis_x.setGridLineVisible(False)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setRange(0.0, 1.0)
        axis_y.setLabelFormat("%.2f")
        axis_y.setLabelsColor(QColor(p.text_dim))
        axis_y.setLinePen(QPen(QColor(p.hairline)))
        axis_y.setGridLinePen(QPen(QColor(p.hairline)))
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setLabelColor(QColor(p.text))

        view = QChartView(chart)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setBackgroundBrush(QBrush(QColor(p.surface_raised)))
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        view.setMinimumHeight(270)
        return view

    def _create_line_chart(
        self, title: str, categories: list[str], series_map: dict[str, list[float]]
    ) -> "QChartView":
        """Line-chart variant of ``_create_bar_chart``, used for the
        accuracy-by-prompt-condition view: one line per model, with a
        marker on every prompt condition, so it's easy to see whether a
        model rises, falls, or stays flat across P0-P4."""
        p = _active_palette()
        chart_series = _chart_series(p)
        chart = QChart()
        chart.setTitle(title)
        chart.setBackgroundBrush(QBrush(QColor(p.surface_raised)))
        chart.setBackgroundPen(QPen(Qt.PenStyle.NoPen))
        chart.setPlotAreaBackgroundVisible(False)
        chart.setTitleBrush(QBrush(QColor(p.text)))

        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        axis_x.setLabelsColor(QColor(p.text_dim))
        axis_x.setLinePen(QPen(QColor(p.hairline)))
        axis_x.setGridLineVisible(False)

        axis_y = QValueAxis()
        axis_y.setRange(0.0, 1.0)
        axis_y.setLabelFormat("%.2f")
        axis_y.setLabelsColor(QColor(p.text_dim))
        axis_y.setLinePen(QPen(QColor(p.hairline)))
        axis_y.setGridLinePen(QPen(QColor(p.hairline)))
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)

        for i, (name, values) in enumerate(series_map.items()):
            line = QLineSeries()
            line.setName(name)
            color = QColor(chart_series[i % len(chart_series)])
            pen = QPen(color)
            pen.setWidth(2)
            line.setPen(pen)
            line.setPointsVisible(True)  # mark each prompt condition with a point
            for x, val in enumerate(values):
                line.append(float(x), val)
            chart.addSeries(line)
            line.attachAxis(axis_x)
            line.attachAxis(axis_y)

        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setLabelColor(QColor(p.text))

        view = QChartView(chart)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setBackgroundBrush(QBrush(QColor(p.surface_raised)))
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        view.setMinimumHeight(270)
        return view

    def _num_item(self, val: float) -> QTableWidgetItem:
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.DisplayRole, round(val, 3))
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _update_table(self, rows: list[dict[str, Any]]) -> None:
        headers = [
            "Source", "Model", "Dataset", "Questions", "Accuracy", "Cond. Acc",
            "Agreement", "Sensitivity", "Compliance", "Recovery", "Unknown", "Invariant Wrong",
        ]

        table = self._table
        table.setSortingEnabled(False)
        table.clear()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            col = 0
            table.setItem(r, col, QTableWidgetItem(str(row.get("source", "")))); col += 1
            table.setItem(r, col, QTableWidgetItem(str(row.get("model", "")))); col += 1
            table.setItem(r, col, QTableWidgetItem(str(row.get("dataset", "")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("n_questions")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("prompt_response_accuracy")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("conditional_accuracy")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("mean_agreement")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("mean_prompt_sensitivity")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("instruction_compliance_rate")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("answer_recovery_rate")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("unknown_rate")))); col += 1
            table.setItem(r, col, self._num_item(_f(row.get("prompt_invariant_incorrect_rate")))); col += 1

        table.setSortingEnabled(True)

    # --------------------------------------------------------- PDF export
    def _chart_pixmaps(self) -> list[tuple[str, Any]]:
        """Grab the currently rendered chart views as (title, QPixmap) pairs,
        in the same order they're laid out on screen, so the exported PDF
        matches what's on screen when the user clicks the button."""
        if not HAS_CHARTS:
            return []
        grabs: list[tuple[str, Any]] = []
        for i in range(self._charts_grid.count()):
            item = self._charts_grid.itemAt(i)
            view = item.widget() if item is not None else None
            if isinstance(view, QChartView):
                title = view.chart().title() or "Chart"
                grabs.append((title, view.grab()))
        return grabs

    def _leaderboard(self, conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
        """Average dataset-level metrics per model, across ALL locally
        available data (not the current filter), for the report's
        cross-model leaderboard page."""
        board: dict[str, dict[str, float]] = {}
        for tag, rows in backend.all_models_dataset_rows(conn).items():
            if not rows:
                continue
            n = len(rows)
            board[tag] = {
                "n_questions": sum(_f(r.get("n_questions")) for r in rows),
                "prompt_response_accuracy": sum(_f(r.get("prompt_response_accuracy")) for r in rows) / n,
                "mean_agreement": sum(_f(r.get("mean_agreement")) for r in rows) / n,
                "instruction_compliance_rate": sum(_f(r.get("instruction_compliance_rate")) for r in rows) / n,
                "prompt_invariant_incorrect_rate": sum(_f(r.get("prompt_invariant_incorrect_rate")) for r in rows) / n,
            }
        return board

    @staticmethod
    def _pdf_filename_part(text: str) -> str:
        """Sanitize a model label/tag for use inside a PDF filename."""
        return text.replace(':', '_').replace('/', '_').replace(' ', '_')

    def _on_download_pdf(self) -> None:
        primary = self._model_combo.currentText()
        if not primary:
            return

        if primary == "All Models":
            self._download_all_models_pdf()
            return

        compare = self._compare_combo.currentText()
        compare_active = compare not in ("None", "")

        ds_rows = [r for r in self._filtered_ds_rows() if r.get("model") == primary]
        pr_rows = [r for r in self._filtered_pr_rows() if r.get("model") == primary]
        compare_rows = (
            [r for r in self._filtered_ds_rows() if r.get("model") == compare]
            if compare_active else []
        )

        # Comparison reports get their own naming pattern ("X_vs_Y_comparison")
        # rather than "X_summary", so a folder full of downloaded PDFs makes
        # it obvious at a glance which files are single-model summaries and
        # which are head-to-head comparisons.
        if compare_active:
            default_name = (
                f"PRISM_{self._pdf_filename_part(primary)}"
                f"_vs_{self._pdf_filename_part(compare)}_comparison.pdf"
            )
        else:
            default_name = f"PRISM_{self._pdf_filename_part(primary)}_summary.pdf"

        # QFileDialog's "dir" argument needs an actual directory, not a bare
        # filename - see ``_default_save_path`` for why this can't just be
        # a bare filename passed to ``QFileDialog``.
        default_path = self._default_save_path(default_name)

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Download PDF summary", str(default_path), "PDF files (*.pdf)",
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pdf"):
            out_path += ".pdf"

        conn = backend.get_conn()
        try:
            info = backend.run_info(conn, primary) or {}
            leaderboard = self._leaderboard(conn)
        finally:
            conn.close()

        try:
            pdf_report.build_model_summary_pdf(
                out_path,
                model_label=backend.model_label(primary),
                model_tag=primary,
                run_info=info,
                dataset_rows=ds_rows,
                prompt_rows=pr_rows,
                compare_label=backend.model_label(compare) if compare_active else None,
                compare_rows=compare_rows,
                leaderboard=leaderboard,
                chart_images=self._chart_pixmaps(),
            )
        except Exception as exc:  # noqa: BLE001 - surface any export failure to the user
            QMessageBox.critical(self, "Export failed", f"Couldn't write the PDF:\n{exc}")
            return

        QMessageBox.information(self, "PDF saved", f"Summary saved to:\n{out_path}")

    def _download_all_models_pdf(self) -> None:
        """Separate "All Models" PDF, only reachable when the "Model"
        filter is set to "All Models" - the full per-model/per-dataset
        table lives here and nowhere else, so a single-model summary PDF
        never carries every other model's data along with it."""
        default_path = self._default_save_path("PRISM_All_Models_summary.pdf")

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Download PDF summary", str(default_path), "PDF files (*.pdf)",
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pdf"):
            out_path += ".pdf"

        conn = backend.get_conn()
        try:
            leaderboard = self._leaderboard(conn)
            all_rows: list[dict[str, Any]] = []
            for tag, rows in backend.all_models_dataset_rows(conn).items():
                label = backend.model_label(tag)
                all_rows.extend({**r, "_model_label": label} for r in rows)
            all_rows.sort(key=lambda r: (str(r.get("_model_label", "")), str(r.get("dataset", ""))))
        finally:
            conn.close()

        try:
            pdf_report.build_all_models_summary_pdf(
                out_path,
                all_rows=all_rows,
                leaderboard=leaderboard,
                chart_images=self._chart_pixmaps(),
            )
        except Exception as exc:  # noqa: BLE001 - surface any export failure to the user
            QMessageBox.critical(self, "Export failed", f"Couldn't write the PDF:\n{exc}")
            return

        QMessageBox.information(self, "PDF saved", f"Summary saved to:\n{out_path}")

    def _default_save_path(self, default_name: str) -> Path:
        """Documents folder (falling back to home) + the given filename -
        factored out of ``_on_download_pdf`` so both the single/comparison
        and All Models save flows anchor to the same place. See that
        method's original comment for why this can't just be a bare
        filename passed to ``QFileDialog``."""
        docs_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default_dir = Path(docs_dir) if docs_dir else Path.home()
        return default_dir / default_name

    # ------------------------------------------------- external hooks
    def set_active_model(self, model_tag: str) -> None:
        """Called by MainWindow when the active model changes elsewhere in
        the app (e.g. a different model chosen on the startup screen, or
        after a benchmark run finishes) so the dashboard's primary filter
        follows it.

        Reloads data first: this can be called before the dashboard has
        ever been shown (e.g. right after picking a model on the startup
        screen, well before the person has visited the Dashboard tab), in
        which case the "Model:" combo may still only hold whatever it was
        built with initially and won't yet have ``model_tag`` as an option
        at all - so the requested model would silently fail to apply here
        and the dashboard would keep showing its previous/default
        selection instead of the model the person just picked."""
        self.reload_data()
        idx = self._model_combo.findText(model_tag)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)

    def focus_on_latest_run(self, model_tag: str) -> None:
        """Called by MainWindow right after a benchmark run finishes so the
        person lands on *just* that run's data, not whatever filters were
        left over from a previous dashboard visit. Scopes the view to this
        model alone (no lingering "Compare with" selection - possibly for a
        model that no longer has any run at all) and to "User Runs" only,
        so a freshly completed run isn't diluted by bundled Research
        Results baseline rows for the same model/dataset. Call reload_data()/
        refresh() afterwards so the filter combos have this run's data to
        select from before this is applied.
        """
        idx = self._model_combo.findText(model_tag)
        if idx >= 0:
            self._model_combo.blockSignals(True)
            self._model_combo.setCurrentIndex(idx)
            self._model_combo.blockSignals(False)

        idx = self._source_combo.findText("User Runs")
        if idx >= 0:
            self._source_combo.blockSignals(True)
            self._source_combo.setCurrentIndex(idx)
            self._source_combo.blockSignals(False)

        # Rebuilds "Compare with" for the new primary model and resets it
        # to "None" (also triggers the one view update this method needs).
        self._refresh_compare_options(preferred="None")

    # Aliases matching the method names MainWindow calls.
    def refresh(self) -> None:
        self.reload_data()

    def set_model(self, model_tag: str) -> None:
        self.set_active_model(model_tag)