from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QLineEdit, QProgressBar, QMessageBox, QScrollArea, QInputDialog,
)

from app.gui.widgets import pill, card, h2, body, faint, SectionHeader, centered_column
from app.services import backend, app_state, supabase_sync, public_results
from app.services.backend import PullWorker


class ModelRow(QFrame):
    """One row in the model list. The row itself *is* the selector - click
    anywhere on it (outside the pull button) to make it the active model.
    A selected row gets an accent-colored fill; the pull button still does
    its own separate job of downloading the model."""

    pull_requested = Signal(str)
    stop_requested = Signal(str)
    selected = Signal(str)

    def __init__(self, model_tag: str, label: str, parent=None, display_tag: str | None = None):
        super().__init__(parent)
        self.model_tag = model_tag
        # The tag actually shown to the person. Defaults to model_tag (the
        # curated catalog always passes an explicit, already-unambiguous
        # tag), but a custom typed-in model passes what the person actually
        # typed here, so e.g. entering "phi4-mini" never grows a ":latest"
        # in front of them - model_tag itself still gets normalized to
        # ":latest" internally (row key, has_data/is_installed lookups,
        # Supabase storage), since those need one canonical form.
        self.display_tag = display_tag or model_tag
        self.setObjectName("ModelListRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)

        self.star_lbl = QLabel("\u2606")
        self.star_lbl.setObjectName("ModelRowStar")
        top.addWidget(self.star_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_lbl = QLabel(label)
        name_lbl.setObjectName("H2")
        name_lbl.setWordWrap(True)
        tag_lbl = QLabel(self.display_tag)
        tag_lbl.setObjectName("Faint")
        tag_lbl.setWordWrap(True)
        text_col.addWidget(name_lbl)
        text_col.addWidget(tag_lbl)
        top.addLayout(text_col, 1)
        top.addStretch(0)

        self.public_pill = pill("VERIFIED", "#38bdf8", "rgba(56, 189, 248, 0.12)")
        self.public_pill.setToolTip("Verified public benchmark results are published on GitHub.")
        self.public_pill.setVisible(public_results.has_published_result(model_tag))
        top.addWidget(self.public_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        # Shown proactively as soon as the row is populated (see
        # set_ram_status below) - so a person scanning the list at launch
        # can see which models their device likely can't handle *before*
        # clicking anything, not only after they've already selected one.
        self.ram_pill = pill("LOW RAM", "#ea6b6b", "rgba(234, 107, 107, 0.12)")
        self.ram_pill.setToolTip(
            "This device may not have enough RAM to run this model well."
        )
        self.ram_pill.setVisible(False)
        top.addWidget(self.ram_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        self.status_pill = pill("CHECKING\u2026", "#9c99a8", "transparent")
        top.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        self.action_btn = QPushButton("Pull model")
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_btn.setFixedWidth(110)
        self.action_btn.clicked.connect(self._on_pull_clicked)
        top.addWidget(self.action_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setFixedWidth(70)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(lambda: self.stop_requested.emit(self.model_tag))
        top.addWidget(self.stop_btn)

        root.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setObjectName("OllamaTaskBar")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Faint")
        self.progress_label.setVisible(False)
        root.addWidget(self.progress_label)

    def _on_pull_clicked(self) -> None:
        # Pulling a model doesn't itself change the selection, so this is
        # kept separate from mousePressEvent's row-click selection.
        self.pull_requested.emit(self.model_tag)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.action_btn.geometry().contains(event.pos()) or self.stop_btn.geometry().contains(event.pos()):
            super().mousePressEvent(event)
            return
        self.selected.emit(self.model_tag)
        super().mousePressEvent(event)

    def set_selected(self, is_selected: bool) -> None:
        self._selected = is_selected
        self.setProperty("selected", "true" if is_selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_installed(self, installed: bool) -> None:
        self.star_lbl.setText("\u2605" if installed else "\u2606")
        if installed:
            self.status_pill.setText("INSTALLED")
            self.status_pill.setStyleSheet("background: rgba(95,209,142,0.15); color:#5fd18e;")
            self.action_btn.setText("Re-pull")
        else:
            self.status_pill.setText("NOT INSTALLED")
            self.status_pill.setStyleSheet("background: rgba(232,180,92,0.15); color:#e8b45c;")
            self.action_btn.setText("Pull model")
        self.action_btn.setEnabled(True)
        self.action_btn.setVisible(True)
        self.stop_btn.setVisible(False)

    def set_ram_status(self, shortfall: tuple[float, float] | None) -> None:
        """Proactive per-row RAM badge, independent of selection - set the
        instant the list is populated/refreshed so it's visible on launch
        for every not-yet-installed row, not just whichever one the person
        happens to click. ``shortfall`` is ``(required_gb, available_gb)``
        from backend.check_ram_for_model(), or None if this device looks
        fine for this model (or it's already installed - see
        StartupScreen._refresh_model_rows, which skips the check there)."""
        if shortfall is None:
            self.ram_pill.setVisible(False)
            self.setToolTip("")
            return
        required_gb, available_gb = shortfall
        self.ram_pill.setVisible(True)
        self.setToolTip(
            f"Recommends about {required_gb:.0f} GB of RAM; this device has "
            f"about {available_gb:.1f} GB. It may run very slowly, swap "
            "heavily, or fail to load."
        )

    def set_not_found(self, message: str) -> None:
        """Pin this row to a terminal "doesn't exist" state: no pull/
        re-pull button (retrying an invalid name would just fail the same
        way), just the tag and the error. Distinct from the transient
        progress_label text set elsewhere - this replaces the row's
        actionable controls entirely rather than sitting alongside them."""
        self.star_lbl.setText("\u2606")
        self.status_pill.setText("NOT FOUND")
        self.status_pill.setStyleSheet("background: rgba(234,107,107,0.15); color:#ea6b6b;")
        self.action_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self.progress.setVisible(False)
        self.progress_label.setText(message)
        self.progress_label.setVisible(True)

    def set_pulling(self, active: bool) -> None:
        self.action_btn.setVisible(not active)
        self.stop_btn.setVisible(active)
        self.stop_btn.setEnabled(active)
        self.progress.setVisible(active)
        self.progress_label.setVisible(active)
        if active:
            self.status_pill.setText("DOWNLOADING")
            self.status_pill.setStyleSheet("background: rgba(143,124,242,0.18); color:#8f7cf2;")

    def update_progress(self, status: str, completed: float, total: float) -> None:
        if total > 0:
            pct = int(completed / total * 100)
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            mb_done = completed / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self.progress_label.setText(f"{status} - {mb_done:,.0f} / {mb_total:,.0f} MB")
        else:
            self.progress.setRange(0, 0)
            self.progress_label.setText(status)


class StartupScreen(QWidget):
    """
    Setup screen: a full-width page (matching the rest of the app's
    Settings-style screens) with an Ollama connection status card up top
    and a "Model Selection" card below it containing the model list, the
    "add another model" field, and the continue action.
    """

    model_chosen = Signal(str)
    # Carries whichever model row is currently highlighted in the picker
    # (may be "" if none is) - so that skipping straight to the Dashboard
    # while a specific installed model is selected lands on that model's
    # data, not on whatever was last remembered/used.
    skip_to_dashboard = Signal(str)
    # Emitted when the person picks "View existing data" for a searched
    # model tag that already has verified/published results on GitHub -
    # the model is deliberately NOT pulled/installed for this path (see
    # _pull_custom_model), so MainWindow must route straight to the
    # Dashboard rather than the Benchmark screen.
    view_data_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pull_workers: dict[str, PullWorker] = {}
        self.model_rows: dict[str, ModelRow] = {}
        self.selected_model_tag: str | None = None
        # Tags a pull has confirmed don't exist (ModelNotFoundError) - kept
        # so their rows stay pinned to the "not found" state (no pull/
        # re-pull button, just the error) instead of _refresh_model_rows()
        # reverting them back to a plain "Pull model" row, and so Continue/
        # Skip know to hide themselves while one of these is selected.
        self._invalid_tags: set[str] = set()
        # Set while a pull was kicked off from the "Continue" button, so
        # that a *successful* pull automatically advances into the app
        # instead of leaving the user stranded on this screen.
        self._auto_continue_tag: str | None = None
        # The app now stops Ollama on close (see MainWindow.closeEvent /
        # backend.ollama_stop), so it should be the one that starts it
        # again too, on an explicit click - never silently auto-connect
        # just because *something* answered on the port at launch (e.g.
        # the person started it manually, or the previous stop failed).
        # Only the very first check of a session is held to this; a
        # Refresh click afterward behaves normally.
        self._launch_check_pending = True
        self.setObjectName("StartupRoot")
        self._existing_results = False
        self._connected = False
        self._install_worker = None  # backend.OllamaInstallWorker | None
        self._ollama_check_worker = None  # backend.OllamaCheckWorker | None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # A real scroll area (matching Dashboard/Benchmark) so this screen
        # never clips its model list or buttons below a window's visible
        # height on small displays/laptops - it scrolls instead of hiding
        # content or forcing the window to grow past the screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(scroll)

        # Centered, width-capped content column (same pattern as the
        # Benchmark screen) so the page reads as a focused setup panel
        # instead of stretching full-bleed across a wide window, while
        # still shrinking gracefully on narrow windows.
        content = QWidget()
        scroll.setWidget(content)
        inner = centered_column(content, max_width=760, margins=(28, 36, 28, 36), spacing=22)

        inner.addWidget(SectionHeader(
            "Setup Benchmark",
        ))

        # ---- Ollama connection status card ----
        conn_card = card("CardFlat")
        conn_lay = QVBoxLayout(conn_card)
        conn_lay.setContentsMargins(24, 22, 24, 22)
        conn_lay.setSpacing(14)

        conn_top = QHBoxLayout()
        conn_top.setSpacing(12)
        self.status_dot = QLabel("\u25cf")
        self.status_dot.setObjectName("StatusDotPending")
        conn_top.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        ollama_lbl = QLabel("Ollama")
        ollama_lbl.setObjectName("H2")
        conn_top.addWidget(ollama_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_pill = pill("CHECKING\u2026", "#9c99a8", "transparent")
        conn_top.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        conn_top.addStretch(1)
        self.install_btn = QPushButton("Install Ollama")
        self.install_btn.setObjectName("Primary")
        self.install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.install_btn.setVisible(False)
        self.install_btn.setFixedHeight(38)
        self.install_btn.clicked.connect(self._on_install_clicked)
        conn_top.addWidget(self.install_btn)

        self.refresh_btn = QPushButton("Refresh Status")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setFixedHeight(38)
        self.refresh_btn.clicked.connect(self._check_ollama)
        conn_top.addWidget(self.refresh_btn)
        conn_lay.addLayout(conn_top)

        self.status_detail = faint("Checking localhost:11434 for a running Ollama instance\u2026")
        self.status_detail.setMinimumHeight(20)
        conn_lay.addWidget(self.status_detail)

        # Progress bar for the Ollama install itself (download % when known,
        # a busy/indeterminate bar for steps like "running the installer"
        # or "starting the service" where there's no percentage to show).
        self.install_progress = QProgressBar()
        self.install_progress.setObjectName("OllamaTaskBar")
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(0)
        self.install_progress.setFixedHeight(16)
        self.install_progress.setTextVisible(True)
        self.install_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.install_progress.setVisible(False)
        conn_lay.addWidget(self.install_progress)

        inner.addWidget(conn_card)

        # ---- Model Selection card ----
        sel_label = QLabel("Model Selection")
        sel_label.setObjectName("SectionLabel")
        inner.addWidget(sel_label)

        sel_card = card("CardFlat")
        sel_lay = QVBoxLayout(sel_card)
        sel_lay.setContentsMargins(20, 18, 20, 18)
        sel_lay.setSpacing(14)

        sel_lay.addWidget(body(
            "Select an installed model to benchmark, or add another model from Ollama."
        ))

        # ---- model rows - laid out directly (no scroll) so all four
        # validated models are visible at once without scrolling; any
        # custom/cloud-saved tags added later simply grow the card. ----
        self.rows_host = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_host)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(8)

        for tag, meta in backend.models_catalog().items():
            row = ModelRow(tag, meta["label"])
            row.pull_requested.connect(self._start_pull)
            row.stop_requested.connect(self._stop_pull)
            row.selected.connect(self._select_model)
            self.model_rows[tag] = row
            self.rows_lay.addWidget(row)

        sel_lay.addWidget(self.rows_host)

        # ---- "Add other models" - a labeled input row so pulling
        # something outside the four validated models reads as one simple
        # action (type a tag, hit the button). Anything pulled here is
        # also saved to Supabase (see supabase_sync.save_custom_model) so
        # it's remembered on the next launch and on any other device
        # synced to the same project.
        custom_row = QHBoxLayout()
        custom_row.setSpacing(8)
        self.custom_model_input = QLineEdit()
        self.custom_model_input.setObjectName("StartupCombo")
        self.custom_model_input.setPlaceholderText("Enter Ollama model tag, e.g. qwen2.5:3b")
        self.custom_model_input.setFixedHeight(40)
        self.custom_model_input.returnPressed.connect(self._pull_custom_model)
        custom_row.addWidget(self.custom_model_input, 1)
        self.custom_pull_btn = QPushButton("Add Model")
        self.custom_pull_btn.setObjectName("Primary")
        self.custom_pull_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.custom_pull_btn.setFixedHeight(40)
        self.custom_pull_btn.clicked.connect(self._pull_custom_model)
        custom_row.addWidget(self.custom_pull_btn)
        sel_lay.addLayout(custom_row)

        add_sub = faint(
            "Note: Enter the exact model tag from the Ollama library. If not "
            "installed, it will be prepared automatically."
        )
        sel_lay.addWidget(add_sub)

        self.device_label = faint("")
        sel_lay.addWidget(self.device_label)

        # Shown as soon as a too-big-for-this-device model is selected,
        # *before* the user tries to pull or continue - an up-front heads
        # up rather than a surprise at click time.
        self.ram_warning_label = QLabel("")
        self.ram_warning_label.setObjectName("Faint")
        self.ram_warning_label.setStyleSheet("color:#ea6b6b;")
        self.ram_warning_label.setWordWrap(True)
        self.ram_warning_label.setVisible(False)
        sel_lay.addWidget(self.ram_warning_label)

        self.continue_btn = QPushButton("Select a model")
        self.continue_btn.setObjectName("StartupPrimaryBtnReady")
        self.continue_btn.setFixedHeight(48)
        self.continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self._on_continue_clicked)
        sel_lay.addWidget(self.continue_btn)

        inner.addWidget(sel_card)

        # ---- "existing results" skip link - the very last thing on the
        # screen, below the model selection card. ----
        self.skip_link = QPushButton(" Skip to dashboard  \u2192")
        self.skip_link.setObjectName("StartupSkipLink")
        self.skip_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_link.setFlat(True)
        self.skip_link.setVisible(False)
        inner.addWidget(self.skip_link, 0, Qt.AlignmentFlag.AlignHCenter)
        # NOTE: connect to a real slot, not directly to skip_to_dashboard.emit.
        # QPushButton.clicked emits a bool ("checked"); binding that straight
        # to emit() would pass that bool as the signal's str argument instead
        # of the selected model tag (same pitfall as HomeScreen's Get
        # Started button - see its comment for details).
        self.skip_link.clicked.connect(self._on_skip_clicked)

        inner.addStretch(1)

        QTimer.singleShot(250, self._check_ollama)

    # ---------------- logic ----------------

    def set_existing_results_available(self, available: bool) -> None:
        self._existing_results = available
        self._refresh_skip_link_visibility()

    def _refresh_skip_link_visibility(self) -> None:
        # Never offer a way past this screen - not even to view old results
        # - unless Ollama is actually installed and running right now. A
        # user with existing local data but no working Ollama still can't
        # benchmark or use the rest of the app, so they must not be able to
        # slip past this gate. Also hide it while a confirmed-nonexistent
        # tag is the highlighted selection - skip_to_dashboard honors that
        # selection first (see MainWindow._on_skip_to_dashboard), so
        # leaving the link up here would just be a second way to run into
        # the same "no data for this model" dead end Continue already
        # hides for.
        selected_is_invalid = self.selected_model_tag in self._invalid_tags
        self.skip_link.setVisible(self._existing_results and self._connected and not selected_is_invalid)

    def _on_skip_clicked(self, checked: bool = False) -> None:
        # Pass along whichever model is currently highlighted in the
        # picker (e.g. an installed model the person just selected but
        # hasn't pressed Continue for yet), so MainWindow can send them
        # straight to that model's Dashboard instead of falling back to
        # a remembered/first-available model.
        self.skip_to_dashboard.emit(self.selected_model_tag or "")

    def _check_ollama(self) -> None:
        self.status_dot.setObjectName("StatusDotPending")
        self._refresh_dot_style()
        self.status_pill.setText("CHECKING\u2026")
        self.status_pill.setStyleSheet("background: rgba(156,153,168,0.15); color:#9c99a8;")
        self.status_detail.setText("Checking localhost:11434 for a running Ollama instance\u2026")

        # The actual reachability check (an HTTP call, plus a possible
        # process kill/restart if Ollama is up without OLLAMA_NO_CLOUD) is
        # done off the GUI thread - see backend.OllamaCheckWorker. Left
        # inline this used to freeze the window for several seconds on
        # every launch and every Refresh click. Guard against a second
        # check starting while one is already in flight (e.g. rapid
        # Refresh clicks).
        if self._ollama_check_worker is not None and self._ollama_check_worker.isRunning():
            return
        worker = backend.OllamaCheckWorker(self)
        worker.checked.connect(self._on_ollama_checked)
        self._ollama_check_worker = worker
        worker.start()

    def _on_ollama_checked(self, reachable: bool) -> None:
        is_launch_check = self._launch_check_pending
        self._launch_check_pending = False
        if is_launch_check and reachable:
            # Something is answering on the port right at launch, but
            # don't auto-connect to it - require the explicit Start Ollama
            # click every session (see the note in __init__). Present this
            # exactly like the "installed but not running" state so the
            # person just clicks the same button either way.
            self._connected = False
            self._refresh_skip_link_visibility()
            self.status_dot.setObjectName("StatusDotBad")
            self._refresh_dot_style()
            self.status_pill.setText("NOT CONNECTED")
            self.status_pill.setStyleSheet("background: rgba(234,107,107,0.15); color:#ea6b6b;")
            self.status_detail.setText(
                "Click Start Ollama to begin this session."
            )
            for tag, row in self.model_rows.items():
                if tag not in self.pull_workers:
                    row.action_btn.setEnabled(False)
            self.install_btn.setText("Start Ollama")
            self.install_btn.setVisible(True)
            self._refresh_model_rows()
            self._update_continue_btn()
            return
        if reachable:
            self._connected = True
            self.status_dot.setObjectName("StatusDotGood")
            self._refresh_dot_style()
            self.status_pill.setText("CONNECTED")
            self.status_pill.setStyleSheet("background: rgba(95,209,142,0.15); color:#5fd18e;")
            # The install button is only ever driven by the "not reachable"
            # branch below (Install Ollama / Start Ollama / Working...). If
            # an install/start was ever kicked off and Ollama then came up
            # (including just because the user started it themselves and
            # hit Refresh), nothing was resetting this button - it would
            # stay stuck on "Working..." forever even though everything is
            # now fine. Hide it once we're actually connected.
            if self._install_worker is None:
                self.install_btn.setEnabled(True)
                self.install_btn.setVisible(False)
                self.install_progress.setVisible(False)
            self._refresh_skip_link_visibility()
            self._show_model_picker()
            self._resume_pending_work()
        else:
            self._connected = False
            self._refresh_skip_link_visibility()
            self.status_dot.setObjectName("StatusDotBad")
            self._refresh_dot_style()

            already_installed = backend.ollama_installed_on_device()
            # The pill previously always said "NOT FOUND" here, even when
            # Ollama IS installed on this device and just isn't running -
            # confusing, since the detail text right below already draws
            # that distinction correctly. Match the pill to it: "installed
            # but not running" reads very differently from "not present on
            # this device at all".
            if already_installed:
                self.status_pill.setText("NOT RUNNING")
            else:
                self.status_pill.setText("NOT FOUND")
            self.status_pill.setStyleSheet("background: rgba(234,107,107,0.15); color:#ea6b6b;")

            for tag, row in self.model_rows.items():
                if tag not in self.pull_workers:
                    row.action_btn.setEnabled(False)
            if already_installed:
                self.status_detail.setText(
                    "Ollama is installed on this device but isn't running. "
                    "Click Start Ollama, or refresh status if you've "
                    "started it yourself."
                )
                self.install_btn.setText("Start Ollama")
                self.install_btn.setVisible(True)
                # The binary is present, so whatever earlier install
                # attempt was pending is effectively resolved - starting
                # it from here is a plain "Start", not a fresh install,
                # so don't leave a stale pending_install flag around to
                # cause a confusing "previous install didn't finish"
                # message on some future launch.
                app_state.set_pending_install(False)
            elif backend.ollama_auto_install_supported():
                if app_state.get_pending_install():
                    # A previous install was actually kicked off (by a
                    # click) and never finished - the app was closed or
                    # crashed mid-install. Say so, but still require a
                    # fresh click rather than auto-firing.
                    self.status_detail.setText(
                        "A previous Ollama install didn't finish (the app "
                        "was closed or crashed partway through). Click "
                        "Install Ollama to try again."
                    )
                else:
                    self.status_detail.setText(
                        "Ollama isn't installed on this device. PRISM can "
                        "install the latest stable release automatically."
                    )
                self.install_btn.setText("Install Ollama")
                self.install_btn.setVisible(True)
                # Deliberately NOT auto-clicking Install here even if
                # pending_install is set - the very first install must
                # always be a person's explicit click, never something
                # that fires the moment the app opens. pending_install is
                # still used (see below) to distinguish "never tried" from
                # "an install was actually in flight and got cut off", and
                # to show a clear one-line explanation in that second case
                # instead of silently starting.
            else:
                self.status_detail.setText(
                    "Ollama isn't reachable at localhost:11434, and this "
                    "operating system isn't supported for automatic install. "
                    "Install it manually from ollama.com, run it, then "
                    "refresh status."
                )
                self.install_btn.setVisible(False)
            # Whether or not Ollama itself is reachable, the model rows
            # should never be left showing their initial "CHECKING..."
            # placeholder forever - reflect what's actually on disk (or
            # "NOT INSTALLED" if nothing is, since nothing can be pulled
            # without a running server anyway).
            self._refresh_model_rows()
            self._update_continue_btn()

    def _on_install_clicked(self) -> None:
        if self._install_worker is not None:
            return
        app_state.set_pending_install(True)
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Working\u2026")
        self.refresh_btn.setEnabled(False)
        self.status_detail.setText("Setting up Ollama\u2026")
        self.install_progress.setVisible(True)
        self.install_progress.setRange(0, 0)  # indeterminate until we know a %

        worker = backend.OllamaInstallWorker(self)
        worker.progress.connect(self._on_install_progress)
        worker.password_needed.connect(self._on_sudo_password_needed)
        worker.finished_ok.connect(self._on_install_finished)
        self._install_worker = worker
        worker.start()

    def _on_install_progress(self, stage: str, message: str, percent: float) -> None:
        self.status_detail.setText(message)
        if percent is not None and percent >= 0:
            self.install_progress.setRange(0, 100)
            self.install_progress.setValue(int(percent))
        else:
            # No concrete percentage for this step (installing/starting/
            # detecting) - a busy bar still shows something is happening
            # instead of the UI looking frozen.
            self.install_progress.setRange(0, 0)

    def _on_sudo_password_needed(self) -> None:
        """Linux only: the install script needs sudo and this device isn't
        already root / passworless-sudo, so ask right here in the app
        instead of leaving the user to go type it into a terminal."""
        worker = self._install_worker
        if worker is None:
            return
        password, accepted = QInputDialog.getText(
            self,
            "Administrator password required",
            "Installing Ollama needs administrator (sudo) access on Linux.\n"
            "Enter your password to continue:",
            QLineEdit.EchoMode.Password,
        )
        worker.supply_password(password if accepted and password else None)

    def _on_install_finished(self, ok: bool, error_message: str) -> None:
        self._install_worker = None
        self.install_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.install_progress.setVisible(False)
        if ok:
            app_state.set_pending_install(False)
            self._check_ollama()
            return
        # Window exception dialog: never leave the user staring at a stuck
        # status line - surface exactly what went wrong and how to recover.
        # The install script can fail on a *later* step (e.g. enabling the
        # systemd service) after the ollama binary is already in place -
        # check for that rather than always telling the person to go
        # install it manually when a plain "Start Ollama" click would
        # actually work fine from here.
        from prism_core import ollama_installer
        if ollama_installer.find_binary() is not None:
            QMessageBox.warning(
                self,
                "Ollama installed, but setup didn't fully finish",
                (
                    (error_message or "The install script reported an error.")
                    + "\n\nThe Ollama program itself was installed successfully "
                    "though - click \u201cStart Ollama\u201d below to finish up."
                ),
            )
        else:
            QMessageBox.critical(
                self,
                "Couldn't set up Ollama",
                error_message or "An unknown error occurred while setting up Ollama.",
            )
        self._check_ollama()

    def _refresh_dot_style(self) -> None:
        colors = {
            "StatusDotPending": "#e8b45c",
            "StatusDotGood": "#5fd18e",
            "StatusDotBad": "#ea6b6b",
        }
        name = self.status_dot.objectName()
        self.status_dot.setStyleSheet(f"color:{colors.get(name, '#9c99a8')}; font-size:11px;")

    def _show_model_picker(self) -> None:
        self._load_cloud_models()
        self._load_locally_installed_models()
        self._refresh_model_rows()

        # Nothing is ever restored here - the model choice isn't persisted
        # across launches at all (see app_state.py). Every session starts
        # with no model selected, and _update_continue_btn() shows "Select
        # a model" (disabled) until the person actually clicks one. This
        # also means the app never needs to reconcile a stale remembered
        # tag against whatever's actually still installed - one less thing
        # that can go wrong or drift.
        self._update_continue_btn()

    def _select_model(self, tag: str) -> None:
        if tag not in self.model_rows:
            return
        self.selected_model_tag = tag
        for row_tag, row in self.model_rows.items():
            row.set_selected(row_tag == tag)
        self._update_ram_warning(tag)
        self._update_continue_btn()
        self._refresh_skip_link_visibility()

    def _display_tag(self, tag: str) -> str:
        """The text to show the person for ``tag`` - the row's own
        display_tag (what they actually typed, for a custom model) when a
        row exists, else the tag itself (curated models are already an
        explicit, unambiguous tag with nothing to normalize away)."""
        row = self.model_rows.get(tag)
        return row.display_tag if row is not None else tag

    def _update_ram_warning(self, tag: str) -> None:
        """Surface a soft, non-blocking RAM warning inline, right away, as
        soon as a model is picked - before the user ever clicks Pull/
        Continue. Purely advisory: it never disables the row or Continue,
        it just lets the person know this device may struggle with the
        model they picked (the hard block, for devices far too small to
        even load the model, lives separately in _confirm_ram_ok, which
        fires at the moment Pull is actually clicked)."""
        installed = backend.is_installed(tag)
        if installed:
            self.ram_warning_label.setVisible(False)
            self.ram_warning_label.setText("")
            return

        shortfall = backend.check_ram_for_model(tag)
        if shortfall is None:
            self.ram_warning_label.setVisible(False)
            self.ram_warning_label.setText("")
            return

        required_gb, available_gb = shortfall
        self.ram_warning_label.setStyleSheet(
            "color:#e8b45c; background: rgba(232,180,92,0.10); "
            "border: 1px solid rgba(232,180,92,0.35); border-radius: 6px; "
            "padding: 8px 10px;"
        )
        self.ram_warning_label.setText(
            f"\u26a0 Limited RAM: \u201c{self._display_tag(tag)}\u201d recommends about "
            f"{required_gb:.0f} GB of RAM, but this device has about "
            f"{available_gb:.1f} GB. It may run slowly or struggle to load. "
            "You can still continue, or pick a smaller model."
        )
        self.ram_warning_label.setVisible(True)
    def _update_continue_btn(self) -> None:
        tag = self.selected_model_tag
        if not tag:
            self.continue_btn.setVisible(True)
            self.continue_btn.setText("Select a model")
            self.continue_btn.setEnabled(False)
            return
        if tag in self._invalid_tags:
            # Confirmed nonexistent - there is nothing Continue could
            # sensibly do (it would only land on a misleading empty/
            # wrong Dashboard), so remove it entirely rather than leave
            # it disabled with a label that no longer applies.
            self.continue_btn.setVisible(False)
            return
        self.continue_btn.setVisible(True)
        if not self._connected:
            self.continue_btn.setText("Waiting for Ollama\u2026")
            self.continue_btn.setEnabled(False)
            return
        if tag in self.pull_workers:
            self.continue_btn.setEnabled(False)
            self.continue_btn.setText(f"Downloading {self._display_tag(tag)}\u2026")
            return
        self.continue_btn.setEnabled(True)
        if backend.is_installed(tag):
            self.continue_btn.setText(f"Continue with {self._display_tag(tag)}  \u2192")
        else:
            # Not installed is no longer a hard block on Continue - it
            # just means there's nothing to benchmark yet, so this takes
            # the person straight to the Dashboard (see _on_continue_
            # clicked) instead of forcing a pull they may not want right
            # now. Pulling remains available any time from this row's own
            # "Pull model" button.
            self.continue_btn.setText(f"View dashboard for {self._display_tag(tag)}  \u2192")

    def _on_continue_clicked(self) -> None:
        tag = self.selected_model_tag
        if not tag or not self._connected:
            return

        if not backend.is_installed(tag):
            # Let the person proceed with a not-yet-installed model - they
            # just can't run inference against it (run_screen.start_run()
            # still hard-blocks that with a clear error). Route to the
            # Dashboard rather than Benchmark, since there's nothing to
            # configure/start for a model that isn't on this device yet;
            # a "Pull model" button on this screen (and the toolbar's
            # "Change" link back here) is always available if they decide
            # to install it later.
            self.view_data_requested.emit(tag)
            return

        self.model_chosen.emit(tag)

    def _confirm_ram_ok(self, model_tag: str) -> bool:
        """Show a blocking error dialog (and let the user back out) if
        this device's RAM looks too small for ``model_tag``. Returns True
        to proceed anyway, False to cancel. A model already installed
        skips the check entirely -- if it runs today, it'll keep running."""
        # Hard block first: a device far below the model's requirement will
        # not run it at all, so this is a blocking error, not a choice.
        hard_error = backend.model_compatibility_error(model_tag)
        if hard_error is not None:
            QMessageBox.critical(self, "Model not supported on this device", hard_error)
            return False

        shortfall = backend.check_ram_for_model(model_tag)
        if shortfall is None:
            return True
        required_gb, available_gb = shortfall
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Insufficient RAM for this model")
        box.setText(f"\u201c{self._display_tag(model_tag)}\u201d likely will not run well on this device.")
        box.setInformativeText(
            f"This model recommends at least {required_gb:.0f} GB of RAM, "
            f"but this device has about {available_gb:.1f} GB available.\n\n"
            "Downloading and running it anyway may cause Ollama to run very "
            "slowly, swap heavily, or fail to load the model entirely.\n\n"
            "Continue anyway?"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        choice = box.exec()
        return choice == QMessageBox.StandardButton.Yes


    # ---------------- model list ----------------

    def _refresh_model_rows(self) -> None:
        for tag, row in self.model_rows.items():
            if tag in self.pull_workers:
                # A pull is actively running for this row - set_installed()
                # unconditionally resets the action button/pill (that's
                # what "not installed yet" and "already installed" both
                # need), which would revert an in-progress pull back to a
                # plain "Pull model" button while the progress bar
                # underneath kept showing stale progress. Leave pulling
                # rows alone; _pull_finished() refreshes them once done.
                continue
            if tag in self._invalid_tags:
                # Confirmed not to exist - stay pinned in set_not_found()'s
                # state (no pull/re-pull button) instead of being reset
                # back to a plain "Pull model" row on every refresh.
                continue
            # Route through backend.is_installed() (not a local re-check
            # against installed_model_tags()) so every "is this installed"
            # decision in the app shares one normalization rule - a
            # previous local copy of this check here matched on bare base
            # name only, which falsely marked e.g. qwen2.5:0.5b as
            # installed whenever any other qwen2.5:* tag was actually on
            # disk.
            installed = backend.is_installed(tag)
            row.set_installed(installed)
            # Proactive RAM badge, independent of selection - see
            # ModelRow.set_ram_status. Only meaningful for not-yet-installed
            # models: one already installed and running today doesn't need
            # a warning about running it (mirrors _confirm_ram_ok's own
            # "already installed skips the check" rule at pull time).
            row.set_ram_status(None if installed else backend.check_ram_for_model(tag))
        if self.selected_model_tag:
            self._update_ram_warning(self.selected_model_tag)
        self._update_continue_btn()


    def _load_cloud_models(self) -> None:
        """Add a row for any "other model" tag previously saved to
        Supabase (from this device or another one) that is *also* actually
        installed on this device. Only the four curated catalog models are
        shown regardless of install state (so they can be pulled); any
        "other model" tag - which exists purely so a pull can be resumed/
        retried across launches - has no reason to keep occupying a row
        once its pull was abandoned or never finished, since there's
        nothing useful to do with it here except pull it again (which
        "Add other models" already covers)."""
        if not supabase_sync.is_configured():
            return
        for entry in supabase_sync.fetch_custom_models():
            tag = (entry.get("model_tag") or "").strip()
            if not tag or tag in self.model_rows:
                continue
            if not backend.is_installed(tag):
                continue
            label = entry.get("label") or tag
            row = ModelRow(tag, label)
            row.pull_requested.connect(self._start_pull)
            row.stop_requested.connect(self._stop_pull)
            row.selected.connect(self._select_model)
            self.model_rows[tag] = row
            self.rows_lay.addWidget(row)
            row.set_installed(True)

    def _load_locally_installed_models(self) -> None:
        """Add a row for every model Ollama already has installed on this
        device that isn't one of the four curated models and isn't already
        shown (e.g. from Supabase above). This is what makes "other" models
        show up here at all - and survive a restart - without depending on
        Supabase being configured: Ollama itself is the persistence layer
        (a pulled model stays on disk and in ``ollama list`` regardless of
        this app), so simply asking it what's installed, every time this
        screen is shown, is enough. Previously only the four built-in
        models plus whatever had separately been saved to Supabase's
        ``custom_models`` table ever got a row - a model pulled directly via
        ``ollama pull`` in a terminal, or through this app while Supabase
        wasn't configured, was fully installed and usable but invisible
        here on the very next launch.
        """
        for model in backend.installed_models():
            tag = model.name
            if not tag or tag in self.model_rows:
                continue
            row = ModelRow(tag, backend.model_label(tag))
            row.pull_requested.connect(self._start_pull)
            row.stop_requested.connect(self._stop_pull)
            row.selected.connect(self._select_model)
            self.model_rows[tag] = row
            self.rows_lay.addWidget(row)
            row.set_installed(True)

    def _resume_pending_work(self) -> None:
        """Called once Ollama is confirmed connected. Any model pull that
        was left marked pending (started, never finished successfully -
        e.g. the app was closed or crashed mid-download) gets restarted
        automatically here. Ollama itself resumes the actual byte range
        from wherever the partial blob on disk left off, so this doesn't
        redownload from zero.

        A pending marker can also outlive a pull that actually *succeeded*
        - e.g. the app was closed in the gap between the download finishing
        and ``_pull_finished`` running, or the process was killed right as
        the marker would have been cleared. Blindly restarting the pull in
        that case redoes the whole "pulling manifest" handshake against a
        model Ollama already has fully installed. Check installed-state
        first and just clear the stale marker instead of re-pulling."""
        for tag in app_state.get_pending_pull_tags():
            if tag in self.pull_workers:
                continue
            if backend.is_installed(tag):
                app_state.remove_pending_pull(tag)
                continue
            if tag in self.model_rows:
                self._start_pull(tag)
            else:
                # A custom/cloud tag not currently rendered as a row -
                # nothing to attach progress to, so just drop the marker
                # rather than pulling silently with no UI.
                app_state.remove_pending_pull(tag)

    def _pull_custom_model(self) -> None:
        raw_tag = self.custom_model_input.text().strip()
        if not raw_tag:
            return
        # Normalize a bare tag (no ":size"/":variant") to its explicit
        # ":latest" form for the *internal* identity only - row dict key,
        # Supabase custom_models, has_data/is_installed lookups all need
        # one canonical form so a bare
        # "qwen2.5" and an explicit "qwen2.5:latest" are recognized as the
        # same model. Never shown to the person, though - every label and
        # message below uses ``raw_tag`` (what they actually typed) so
        # they don't see a ":latest" they never wrote.
        tag = backend.normalize_tag(raw_tag)

        # Searching for a tag that isn't installed here yet but already has
        # verified results published on GitHub is ambiguous: does the person
        # want to see that existing data, or generate their own run? Ask,
        # rather than silently kicking off a (possibly large, possibly
        # unnecessary) pull. Only ask once per tag - if it's already a row
        # here (already installed, or already decided on "run it myself"
        # earlier this session), just fall through to the normal pull flow.
        if tag not in self.model_rows and not backend.is_installed(tag) \
                and public_results.has_published_result(tag):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Verified results already available")
            box.setText(f"\u201c{raw_tag}\u201d already has verified benchmark results published on GitHub.")
            box.setInformativeText(
                "Would you like to view that existing data, or download this "
                "model and run the benchmark yourself on this device?"
            )
            view_btn = box.addButton("View existing data", QMessageBox.ButtonRole.AcceptRole)
            run_btn = box.addButton("Run benchmark myself", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(view_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is view_btn:
                self.custom_model_input.clear()
                self.view_data_requested.emit(tag)
                return
            if clicked is not run_btn:
                return  # Cancel - leave the input text as-is, do nothing

        is_new = tag not in self.model_rows
        if is_new:
            row = ModelRow(tag, raw_tag, display_tag=raw_tag)
            row.pull_requested.connect(self._start_pull)
            row.stop_requested.connect(self._stop_pull)
            row.selected.connect(self._select_model)
            self.model_rows[tag] = row
            self.rows_lay.addWidget(row)
            row.set_installed(backend.is_installed(tag))
        self.custom_model_input.clear()
        # Remember it in Supabase right away (not gated on the pull actually
        # succeeding) so it reappears in "Add other models" on next launch
        # even if this pull is retried later.
        if is_new:
            supabase_sync.save_custom_model(tag)
        self._select_model(tag)
        self._start_pull(tag)

    def _start_pull(self, model_tag: str) -> None:
        if model_tag in self.pull_workers:
            return
        row = self.model_rows.get(model_tag)
        if row is None:
            return
        if not backend.ollama_available():
            # Pull buttons are created up front for every catalog model
            # regardless of whether Ollama is actually installed/running,
            # so nothing previously stopped a click here from firing a
            # doomed /api/pull request at a server that isn't there.
            QMessageBox.critical(
                self,
                "Ollama isn't running",
                "Ollama needs to be installed and running before a model "
                "can be pulled. Install/start it above, then try again.",
            )
            return
        if not backend.is_installed(model_tag) and not self._confirm_ram_ok(model_tag):
            return
        row.set_pulling(True)
        app_state.add_pending_pull(model_tag)

        worker = PullWorker(model_tag)
        worker.progress.connect(row.update_progress)
        worker.finished_ok.connect(
            lambda ok, msg, not_found, t=model_tag: self._pull_finished(t, ok, msg, not_found)
        )
        self.pull_workers[model_tag] = worker
        worker.start()

    def _stop_pull(self, model_tag: str) -> None:
        worker = self.pull_workers.get(model_tag)
        if worker is None:
            return
        row = self.model_rows.get(model_tag)
        if row is not None:
            row.stop_btn.setEnabled(False)
            row.progress_label.setVisible(True)
            row.progress_label.setText("Stopping\u2026")
        # A user-requested Stop is a deliberate "not now", not an
        # interruption - drop the pending-pull marker right away so this
        # doesn't look identical to an app-closed-mid-download and get
        # silently auto-resumed the next time PRISM launches. (An actual
        # crash/close mid-pull never reaches this method at all, so it
        # still leaves the marker in place and still resumes, same as
        # before.)
        app_state.remove_pending_pull(model_tag)
        worker.cancel()
        if self._auto_continue_tag == model_tag:
            self._auto_continue_tag = None

    def _pull_finished(self, model_tag: str, ok: bool, msg: str, not_found: bool = False) -> None:
        row = self.model_rows.get(model_tag)
        self.pull_workers.pop(model_tag, None)
        cancelled = msg == "Cancelled"
        if ok or cancelled:
            # Success clears the marker because the pull is done. A user
            # cancel already cleared it in _stop_pull, but remove it here
            # too (idempotent) in case _pull_finished is ever reached via
            # some other path that sets msg == "Cancelled" without going
            # through _stop_pull first.
            app_state.remove_pending_pull(model_tag)
        if not_found:
            self._invalid_tags.add(model_tag)
        elif ok:
            # A later successful pull of a tag that previously failed as
            # "not found" (e.g. the person fixed a typo and re-added it as
            # a fresh row under the corrected tag - see also the dedup on
            # tag in _pull_custom_model) should no longer be pinned.
            self._invalid_tags.discard(model_tag)
        if row is not None:
            row.set_pulling(False)
            if not_found:
                row.set_not_found(
                    f"Sorry, \u201c{row.display_tag}\u201d doesn\u2019t exist. Double-check the model name."
                )
            elif not ok and msg:
                row.progress_label.setVisible(True)
                row.progress_label.setText("Stopped" if cancelled else f"Couldn't finish: {msg}")
        self._refresh_model_rows()
        if self.selected_model_tag == model_tag:
            self._update_continue_btn()
            self._refresh_skip_link_visibility()

        # A bad model name gets its own dialog regardless of how the pull
        # was kicked off (typed into "Add other models", or via Continue
        # auto-pulling an uninstalled model) - the row's own state (no
        # pull/re-pull button, Continue/Skip hidden - see set_not_found()
        # and _update_continue_btn()/_refresh_skip_link_visibility() above)
        # makes this permanent and visible at a glance, but a dialog still
        # calls it out immediately in case the row has scrolled out of view.
        if not ok and not_found:
            QMessageBox.warning(
                self,
                "Model not found",
                f"Sorry, the model \u201c{self._display_tag(model_tag)}\u201d doesn\u2019t exist. "
                "Double-check the model name and try again.",
            )

        if self._auto_continue_tag == model_tag:
            self._auto_continue_tag = None
            if ok and backend.is_installed(model_tag):
                self.model_chosen.emit(model_tag)
            elif not cancelled and not not_found:
                QMessageBox.critical(
                    self,
                    "Download didn't finish",
                    (
                        f"\u201c{self._display_tag(model_tag)}\u201d didn't finish downloading"
                        + (f" ({msg})." if msg else ".")
                        + "\n\nIt needs to be on this device before you can benchmark or "
                        "view results for it - give it another try when you're ready."
                    ),
                )
