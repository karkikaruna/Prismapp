from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton, QCheckBox,
    QSpinBox, QProgressBar, QPlainTextEdit, QScrollArea, QGridLayout, QMessageBox,
)

from app.gui.widgets import card, h2, body, faint, divider, SectionHeader, MetricTile, kicker, centered_column
from app.services import backend
from app.services.backend import BenchmarkWorker
from prism_core import fingerprint


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


class RunScreen(QWidget):
    run_completed = Signal(str)          # model_tag that just finished
    continue_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: BenchmarkWorker | None = None
        self._current_run_id: str | None = None
        self.model_tag: str | None = None
        self._success_count = 0
        self._error_count = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        # prompt-reliability's centred, max-width composition, applied on
        # top of prism-build's own screen/card structure below.
        root = centered_column(content, max_width=900, margins=(28, 24, 28, 28), spacing=16)

        # ---- header row: title/subtitle on the left, primary actions on
        # the right - mirrors Nabin's compact top bar instead of stacking
        # the Start/Cancel controls under a full-width config card. ----
        header_row = QHBoxLayout()
        header_row.setSpacing(16)

        header_col = QVBoxLayout()
        header_col.setSpacing(2)
        header_col.addWidget(kicker("Benchmark"))
        self.title_lbl = QLabel("Run benchmark")
        self.title_lbl.setObjectName("H1")
        self.title_lbl.setWordWrap(True)
        header_col.addWidget(self.title_lbl)
        self.sub_lbl = body(
            "Runs the full PRISM pipeline for this model."
        )
        header_col.addWidget(self.sub_lbl)
        header_row.addLayout(header_col, 1)

        action_col = QVBoxLayout()
        action_col.setSpacing(8)
        action_col.addStretch(1)
        self.start_btn = QPushButton("Start benchmark  \u25b6")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setFixedHeight(40)
        self.start_btn.setMinimumWidth(150)
        self.start_btn.clicked.connect(self.start_run)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.setMinimumWidth(150)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_run)

        action_col.addWidget(self.start_btn)
        action_col.addWidget(self.cancel_btn)
        header_row.addLayout(action_col, 0)
        root.addLayout(header_row)

        # ---- Datasets & Options: two clearly-separated section cards, in
        # the spirit of Nabin's individually-boxed "Datasets" / "Sampling &
        # Protocol" sections rather than one card holding both in an inline
        # grid. Stacked vertically (not side by side) so neither card's
        # minimum width can force the row wider than the window - that
        # forced width was what clipped the "Options" card at the edges on
        # narrower windows instead of letting the layout reflow. ----
        sections_row = QVBoxLayout()
        sections_row.setSpacing(16)

        dataset_card = card("Card")
        ds_lay = QVBoxLayout(dataset_card)
        ds_lay.setContentsMargins(22, 18, 22, 20)
        ds_lay.setSpacing(10)
        ds_lay.addWidget(h2("Datasets"))
        ds_lay.addWidget(faint("Choose which benchmark datasets to run."))
        self.dataset_checks: dict[str, QCheckBox] = {}
        for name in backend.datasets_catalog():
            cb = QCheckBox(name.replace("_", " ").title())
            cb.setChecked(True)
            self.dataset_checks[name] = cb
            ds_lay.addWidget(cb)
        ds_lay.addStretch(1)
        sections_row.addWidget(dataset_card)

        options_card = card("Card")
        opts_lay = QVBoxLayout(options_card)
        opts_lay.setContentsMargins(22, 18, 22, 20)
        opts_lay.setSpacing(10)
        opts_lay.addWidget(h2("Options"))
        limit_row = QHBoxLayout()
        limit_row.addWidget(faint("Max questions"))
        self.max_q_spin = QSpinBox()
        self.max_q_spin.setRange(0, 1000)
        self.max_q_spin.setValue(0)
        self.max_q_spin.setSpecialValueText("All")
        limit_row.addWidget(self.max_q_spin)
        limit_row.addStretch(1)
        opts_lay.addLayout(limit_row)
        self.overwrite_cb = QCheckBox("Overwrite completed requests")
        opts_lay.addWidget(self.overwrite_cb)
        opts_lay.addStretch(1)
        sections_row.addWidget(options_card)

        root.addLayout(sections_row)

        # ---- Live progress ----
        progress_card = card("CardRaised")
        pl = QVBoxLayout(progress_card)
        pl.setContentsMargins(24, 22, 24, 22)
        pl.setSpacing(12)

        status_head = QHBoxLayout()
        status_head.setSpacing(8)
        self.status_dot = QLabel("\u25cf")
        self.status_dot.setObjectName("Faint")
        status_head.addWidget(self.status_dot)
        self.status_title = QLabel("Idle")
        self.status_title.setObjectName("H2")
        status_head.addWidget(self.status_title)
        status_head.addStretch(1)
        pl.addLayout(status_head)
        self.status_sub = QLabel("Configure a run above and press Start.")
        self.status_sub.setObjectName("Body")
        self.status_sub.setWordWrap(True)
        pl.addWidget(self.status_sub)

        overall_lbl_row = QHBoxLayout()
        overall_lbl_row.addWidget(faint("Overall progress"))
        overall_lbl_row.addStretch(1)
        self.overall_pct_lbl = QLabel("0%")
        self.overall_pct_lbl.setObjectName("Faint")
        overall_lbl_row.addWidget(self.overall_pct_lbl)
        pl.addLayout(overall_lbl_row)

        self._stage_index = 0
        self._stage_count = 0
        self.overall_bar = QProgressBar()
        self.overall_bar.setObjectName("OverallBar")
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setFixedHeight(14)
        pl.addWidget(self.overall_bar)

        dataset_lbl_row = QHBoxLayout()
        dataset_lbl_row.addWidget(faint("Current dataset"))
        dataset_lbl_row.addStretch(1)
        self.dataset_pct_lbl = QLabel("0%")
        self.dataset_pct_lbl.setObjectName("Faint")
        dataset_lbl_row.addWidget(self.dataset_pct_lbl)
        pl.addLayout(dataset_lbl_row)

        self.dataset_bar = QProgressBar()
        self.dataset_bar.setObjectName("DatasetBar")
        self.dataset_bar.setRange(0, 100)
        self.dataset_bar.setValue(0)
        self.dataset_bar.setTextVisible(False)
        self.dataset_bar.setFixedHeight(10)
        pl.addWidget(self.dataset_bar)

        # Dense 3-column stat grid (2 rows) instead of two separate full
        # rows - a more compact, desktop-panel-like read-out.
        stats_grid = QGridLayout()
        stats_grid.setHorizontalSpacing(12)
        stats_grid.setVerticalSpacing(12)
        self.stat_dataset = MetricTile("Dataset", "-")
        self.stat_requests = MetricTile("Requests", "0 / 0")
        self.stat_latency = MetricTile("Avg latency", "-")
        self.stat_eta = MetricTile("Est. time left", "-")
        # Successful vs. errored requests, tallied live from the same
        # request_progress stream that drives stat_requests above - a
        # quick at-a-glance health check without having to read the log.
        self.stat_success = MetricTile("Successful", "0")
        self.stat_errors = MetricTile("Errors", "0")
        tiles = (
            self.stat_dataset, self.stat_requests, self.stat_latency,
            self.stat_eta, self.stat_success, self.stat_errors,
        )
        for i, tile in enumerate(tiles):
            stats_grid.addWidget(tile, i // 3, i % 3)
        pl.addLayout(stats_grid)

        root.addWidget(progress_card)

        # ---- Run log ----
        # Plain-text, append-only feed of stage transitions and per-request
        # errors, driven by _on_stage/_on_progress/_on_finished/
        # _on_fatal_halt below. Read-only so it can't be edited, monospace
        # so timestamps/markers line up.
        #
        # UI-hidden by design: log_card is built but never added to `root`,
        # so nothing renders it. self.log itself is left fully intact and
        # keeps receiving every appendPlainText()/clear() call below exactly
        # as before - only its visibility changed, not its behavior.
        #
        # IMPORTANT: log_card has no C++ parent (it's deliberately not
        # added to any layout) and previously wasn't stored anywhere in
        # Python either - once __init__ returned, nothing referenced it,
        # so it (and everything it owns, including self.log) got garbage
        # collected almost immediately. Any later signal handler that
        # touched self.log - e.g. _on_fatal_halt firing because Ollama's
        # connection dropped - then crashed with a shiboken "Internal C++
        # object already deleted" RuntimeError. Keeping a reference on
        # self is what keeps it (and self.log) alive for the screen's
        # actual lifetime.
        self._log_card = log_card = card("Card")
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(22, 18, 22, 18)
        ll.setSpacing(8)
        ll.addWidget(h2("Run log"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("RunLog")
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Log output will appear here once a run starts.")
        self.log.setMinimumHeight(160)
        self.log.setMaximumBlockCount(2000)  # cap growth on very long runs
        log_font = self.log.font()
        log_font.setFamily("Menlo, Consolas, monospace")
        self.log.setFont(log_font)
        ll.addWidget(self.log)
        # (intentionally not added to `root` - see note above)

        # ---- Crash-safe error banner (OOM / crash / lost connection) ----
        # Shown only when inference halts on a fatal condition. The app
        # itself never closes when this happens - the worker thread stops
        # cleanly, everything gathered so far is already saved to disk, and
        # the person chooses how to proceed.
        self.error_banner = card("ErrorBanner")
        self.error_banner.setVisible(False)
        eb = QVBoxLayout(self.error_banner)
        eb.setContentsMargins(20, 16, 20, 16)
        eb.setSpacing(10)

        eb_title_row = QHBoxLayout()
        self.error_title = QLabel("Inference halted")
        self.error_title.setObjectName("H2")
        eb_title_row.addWidget(self.error_title)
        eb_title_row.addStretch(1)
        eb.addLayout(eb_title_row)

        self.error_message = QLabel("")
        self.error_message.setObjectName("Body")
        self.error_message.setWordWrap(True)
        eb.addWidget(self.error_message)

        self.error_note = body(
            "Everything completed before this point has been saved. The app "
            "is still running - choose how to proceed."
        )
        eb.addWidget(self.error_note)

        eb_actions = QHBoxLayout()
        self.continue_run_btn = QPushButton("Continue")
        self.continue_run_btn.setObjectName("Primary")
        self.continue_run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_run_btn.setToolTip(
            "Resume from where it halted - already-completed requests are kept."
        )
        self.continue_run_btn.clicked.connect(self._on_continue_after_halt)
        eb_actions.addWidget(self.continue_run_btn)

        self.rerun_btn = QPushButton("Rerun from scratch")
        self.rerun_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rerun_btn.setToolTip(
            "Restart this dataset from the beginning, discarding partial results."
        )
        self.rerun_btn.clicked.connect(self._on_rerun)
        eb_actions.addWidget(self.rerun_btn)

        self.stop_run_btn = QPushButton("Stop")
        self.stop_run_btn.setObjectName("Danger")
        self.stop_run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_run_btn.clicked.connect(self._on_stop_after_halt)
        eb_actions.addWidget(self.stop_run_btn)
        eb_actions.addStretch(1)
        eb.addLayout(eb_actions)

        root.addWidget(self.error_banner)
        root.addStretch(1)

        continue_row = QHBoxLayout()
        continue_row.addStretch(1)
        self.continue_btn = QPushButton("View dashboard  \u2192")
        self.continue_btn.setObjectName("Primary")
        self.continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_btn.setFixedHeight(40)
        self.continue_btn.setVisible(False)
        self.continue_btn.clicked.connect(self._on_view_dashboard_clicked)
        continue_row.addWidget(self.continue_btn)
        root.addLayout(continue_row)

    def _on_view_dashboard_clicked(self) -> None:
        if self.model_tag and not backend.has_data(backend.get_conn(), self.model_tag):
            if backend.has_public_result(self.model_tag):
                backend.import_public_result(self.model_tag)
        self.continue_requested.emit()

    def preselect_model(self, model_tag: str) -> None:
        self.model_tag = model_tag
        self.title_lbl.setText(f"Run benchmark - {backend.model_label(model_tag)}")
        self.continue_btn.setVisible(False)
        self.continue_btn.setText("View dashboard  \u2192")
        if backend.has_data(backend.get_conn(), model_tag):
            self.status_title.setText("Existing results found")
            self.status_sub.setText(
                "This model already has benchmark data on this device - continue to the "
                "dashboard, or start a fresh run above."
            )
            self.continue_btn.setVisible(True)
        elif backend.has_public_result(model_tag):
            self.status_title.setText("Verified public results available")
            self.status_sub.setText(
                "A verified PRISM benchmark result from GitHub is available for this model - "
                "view the verified results on the dashboard, or run a fresh benchmark locally."
            )
            self.continue_btn.setText("View verified result  \u2192")
            self.continue_btn.setVisible(True)
        else:
            self.status_title.setText("Idle")
            self.status_sub.setText("Configure a run above and press Start.")

    # ---------------- run control ----------------

    def start_run(self) -> None:
        if not self.model_tag:
            self.status_title.setText("No model selected")
            self.status_sub.setText("Pick a model on the startup screen first.")
            return

        if not backend.ollama_available():
            # is_installed() below silently returns False whenever Ollama
            # can't be reached at all (it swallows OllamaError), which used
            # to surface as the misleading "Model not installed" message
            # even when the model *was* pulled and the real problem was
            # that the Ollama server had stopped/crashed. Check reachability
            # first so the error the user sees matches the real cause.
            QMessageBox.critical(
                self,
                "Ollama isn't running",
                (
                    "Can't reach Ollama at all right now, so inference can't "
                    "run even though the model may already be pulled.\n\n"
                    "Make sure the Ollama server is running, then try Start "
                    "again."
                ),
            )
            self.status_title.setText("Ollama not reachable")
            self.status_sub.setText(
                "Start (or restart) the Ollama server, then press Start again."
            )
            return

        if not backend.is_installed(self.model_tag):
            # Hard gate: a benchmark run against a model that isn't
            # actually pulled onto this device would just fail against
            # Ollama request by request - stop it here with a clear error
            # instead.
            QMessageBox.critical(
                self,
                "Model not installed",
                (
                    f"\u201c{self.model_tag}\u201d isn't pulled onto this device yet, "
                    "so a benchmark can't run against it.\n\n"
                    "Go back to model selection (\u201cChange\u201d in the toolbar) and "
                    "download it first."
                ),
            )
            self.status_title.setText("Model not installed")
            self.status_sub.setText(
                f"\u201c{self.model_tag}\u201d needs to be pulled before it can be benchmarked."
            )
            return

        datasets = [n for n, cb in self.dataset_checks.items() if cb.isChecked()]
        if not datasets:
            self.status_title.setText("Select at least one dataset")
            return

        max_q = None if self.max_q_spin.value() == 0 else self.max_q_spin.value()

        self.log.clear()
        self.overall_bar.setValue(0)
        self.overall_pct_lbl.setText("0%")
        self.dataset_bar.setValue(0)
        self.dataset_pct_lbl.setText("0%")
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.continue_btn.setVisible(False)
        self.status_title.setText("Starting\u2026")
        self.status_sub.setText(f"Warming up inference for {backend.model_label(self.model_tag)}")
        self.stat_dataset.set_value("-")
        self.stat_requests.set_value("0 / 0")
        self.stat_latency.set_value("-")
        self.stat_eta.set_value("-")
        self._success_count = 0
        self._error_count = 0
        self.stat_success.set_value("0")
        self.stat_errors.set_value("0")
        self.stat_errors.value_lbl.setStyleSheet("")

        self.error_banner.setVisible(False)
        self._last_datasets = datasets
        self._last_max_q = max_q
        self._current_run_id = fingerprint.new_benchmark_run_id()

        self.worker = BenchmarkWorker(
            self.model_tag, datasets,
            max_questions=max_q, overwrite=self.overwrite_cb.isChecked(),
            benchmark_run_id=self._current_run_id,
        )
        self.worker.stage.connect(self._on_stage)
        self.worker.request_progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.fatal_halt.connect(self._on_fatal_halt)
        self.worker.start()

    def resume_run(self, model_tag: str, datasets: list[str], benchmark_run_id: str) -> None:
        """Entry point for resuming a run that was left ``status="running"``
        in the run index from a previous app session (see MainWindow._maybe_
        resume_interrupted_run). Reuses the same benchmark_run_id so the
        engine finds and reuses the interrupted run's existing raw records
        instead of starting a new run directory from scratch."""
        self.model_tag = model_tag
        self._last_datasets = datasets
        self._last_max_q = None
        self._current_run_id = benchmark_run_id
        self._restart(overwrite=False)

    def cancel_run(self) -> None:        
        if self.worker:
            self.worker.cancel()
            self.status_sub.setText("Cancelling after the current request\u2026")
            self.cancel_btn.setEnabled(False)

    def _on_fatal_halt(self, reason: str) -> None:
        """A fatal condition (OOM, lost Ollama connection, repeated
        failures) stopped inference. The worker thread is already winding
        down cleanly on its own - this never touches the app's own event
        loop or main thread, so the window stays fully responsive. Show the
        banner so the person can decide what happens next."""
        self.error_message.setText(reason)
        self.error_banner.setVisible(True)
        self.status_title.setText("Inference halted")
        self.status_sub.setText("Waiting for you to continue, rerun, or stop.")
        self.log.appendPlainText(f"\u26a0 HALTED: {reason}")

    def _restart(self, *, overwrite: bool) -> None:
        if not backend.ollama_available():
            # A fatal halt is often caused by exactly this (lost connection
            # to Ollama) - re-check before blindly retrying so the person
            # gets a clear message instead of watching it halt again.
            QMessageBox.critical(
                self,
                "Ollama isn't running",
                "Still can't reach Ollama. Start (or restart) the server, "
                "then try again.",
            )
            return
        self.error_banner.setVisible(False)
        datasets = getattr(self, "_last_datasets", None) or [
            n for n, cb in self.dataset_checks.items() if cb.isChecked()
        ]
        max_q = getattr(self, "_last_max_q", None)
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_title.setText("Resuming\u2026" if not overwrite else "Restarting dataset\u2026")
        self.status_sub.setText(f"{'Continuing' if not overwrite else 'Rerunning'} for {backend.model_label(self.model_tag)}")
        self.worker = BenchmarkWorker(
            self.model_tag, datasets,
            max_questions=max_q, overwrite=overwrite,
            benchmark_run_id=self._current_run_id,
        )
        self.worker.stage.connect(self._on_stage)
        self.worker.request_progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.fatal_halt.connect(self._on_fatal_halt)
        self.worker.start()

    def _on_continue_after_halt(self) -> None:
        # Resume: already-completed (success) requests are reused as-is by
        # the engine, only the remaining/failed ones run again.
        self._restart(overwrite=False)

    def _on_rerun(self) -> None:
        # Restart the selected datasets for this model from scratch.
        self._restart(overwrite=True)

    def _on_stop_after_halt(self) -> None:
        self.error_banner.setVisible(False)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_title.setText("Stopped")
        self.status_sub.setText(
            "Run stopped after a halted inference. Partial results are saved "
            " - press Start to try again whenever you're ready."
        )

    def _on_stage(self, stage: str, message: str, dataset, index: int, count: int) -> None:
        self.status_title.setText(message)
        if dataset:
            self.stat_dataset.set_value(str(dataset).replace("_", " ").title())
        self.log.appendPlainText(f"\u25b6 [{stage}] {message}")
        # Remember where we are in the dataset list so _on_progress can
        # blend in-dataset request progress into the overall bar instead of
        # only advancing overall progress once an entire dataset finishes.
        self._stage_index = index
        self._stage_count = count
        if count:
            pct = int(index / count * 100)
            self.overall_bar.setValue(pct)
            self.overall_pct_lbl.setText(f"{pct}%  \u00b7  dataset {min(index + 1, count)} of {count}")
        # a new dataset stage starting means its own sub-progress resets
        self.dataset_bar.setValue(0)
        self.dataset_pct_lbl.setText("0%")

    def _on_progress(self, dataset: str, status: str, request_number: int, total: int,
                      percent: float, avg_latency: float, eta_seconds: float | None = None) -> None:
        self.stat_requests.set_value(f"{request_number} / {total}")
        if avg_latency:
            self.stat_latency.set_value(f"{avg_latency:.2f}s")
        if eta_seconds is not None:
            self.stat_eta.set_value(_format_eta(eta_seconds))

        pct = int(percent) if percent else (int(request_number / total * 100) if total else 0)
        pct = max(0, min(100, pct))
        self.dataset_bar.setValue(pct)
        self.dataset_pct_lbl.setText(f"{pct}%")

        # Blend this dataset's fractional completion into the overall bar so
        # it advances continuously during inference, rather than staying
        # frozen until the whole dataset finishes and only jumping between
        # datasets (which is what made overall progress look "stuck").
        count = getattr(self, "_stage_count", 0)
        index = getattr(self, "_stage_index", 0)
        if count:
            overall_pct = int(((index + pct / 100.0) / count) * 100)
            overall_pct = max(0, min(100, overall_pct))
            self.overall_bar.setValue(overall_pct)
            self.overall_pct_lbl.setText(
                f"{overall_pct}%  \u00b7  dataset {min(index + 1, count)} of {count}"
            )

        # Tally running success/error counts. "reused" (a previously
        # completed request loaded from cache rather than re-run) counts as
        # successful for display purposes; only "error" increments the
        # error tile. These counters were being reset to 0 before every run
        # but never actually incremented here, so the tiles sat frozen at
        # "0" for the whole run regardless of real progress.
        if status == "error":
            self._error_count = getattr(self, "_error_count", 0) + 1
            self.stat_errors.set_value(str(self._error_count))
            self.log.appendPlainText(f"  \u2715 error on {dataset} request {request_number}")
        elif status in ("success", "reused"):
            self._success_count = getattr(self, "_success_count", 0) + 1
            self.stat_success.set_value(str(self._success_count))

    def _on_finished(self, success: bool, message: str, result) -> None:
        self.cancel_btn.setEnabled(False)
        self.start_btn.setEnabled(True)

        if not success:
            if self.error_banner.isVisible():
                # The fatal_halt banner is already showing the real reason
                # and the Continue/Rerun/Stop choices - don't stomp on it.
                return
            self.status_title.setText("Run stopped")
            self.status_sub.setText(message or "Benchmark did not complete.")
            self.log.appendPlainText(f"\u26a0 {message}")
            return

        self.status_title.setText("Benchmark complete")
        self.status_sub.setText("Results are ready on the Dashboard.")
        self.log.appendPlainText("\u2713 Benchmark finished, summary written.")
        self.continue_btn.setVisible(True)
        self.overall_bar.setValue(100)
        self.overall_pct_lbl.setText("100%")
        self.dataset_bar.setValue(100)
        self.dataset_pct_lbl.setText("100%")
        self.stat_eta.set_value("Done")
        if self.model_tag:
            self.run_completed.emit(self.model_tag)