from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QActionGroup, QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QStackedWidget, QToolBar, QStatusBar,
    QLabel, QPushButton, QMessageBox,
)

from app.gui.theme import DUSK, PAPER, build_qss
from app.gui.widgets import ModelBadge, FadeStackedWidget
from app.gui.screens.startup_screen import StartupScreen
from app.gui.screens.run_screen import RunScreen
from app.gui.screens.dashboard_screen import DashboardScreen
from app.gui.screens.settings_screen import SettingsScreen
from app.gui.screens.home_screen import HomeScreen
from app.services import backend, app_state, supabase_sync, public_results

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources"
LOGO_ICON_PATH = RESOURCES_DIR / "prism_logo.png"      # mark only, used as window/app icon
LOGO_WORDMARK_PATH = RESOURCES_DIR / "prism_text.png"  # mark + "PRISM" wordmark, used in the toolbar


class MainWindow(QMainWindow):
    """
    PRISM opens on a full-window Home landing screen (mark, tagline, "Get
    Started") with no toolbar/status bar chrome at all. "Get Started"
    hands off to the existing full-window startup screen (connect to
    Ollama, choose the active model). Once a model is chosen the real app
    shell takes over: a native menu bar + toolbar + status bar (genuine
    OS-style desktop chrome, not a sidebar-in-a-webpage), with
    Dashboard / Benchmark / Settings as toolbar destinations. The
    Dashboard and Benchmark screens are always scoped to whichever single
    model is currently active.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PRISM - Prompt-Reliability Benchmark")
        if LOGO_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_ICON_PATH)))
        self.resize(1220, 780)
        # Low enough to still be usable on a 13" laptop panel (1280x800
        # native, minus OS chrome) or a split-screen/tiled window, while
        # every screen's own QScrollArea (dashboard/benchmark/startup)
        # takes over instead of clipping content once a screen's natural
        # content is taller than the available space.
        self.setMinimumSize(860, 560)

        self.active_model: str | None = None
        self._theme = app_state.get_theme()

        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---------- outer switcher: home <-> startup <-> app shell ----------
        self.outer_stack = FadeStackedWidget()
        root_layout.addWidget(self.outer_stack)

        self.home_screen = HomeScreen()
        self.outer_stack.addWidget(self.home_screen)

        self.startup_screen = StartupScreen()
        self.outer_stack.addWidget(self.startup_screen)

        self.app_shell = QWidget()
        shell_lay = QVBoxLayout(self.app_shell)
        shell_lay.setContentsMargins(0, 0, 0, 0)
        shell_lay.setSpacing(0)

        self.content_stack = FadeStackedWidget()
        self.dashboard_screen = DashboardScreen()
        self.run_screen = RunScreen()
        self.settings_screen = SettingsScreen()
        self.content_stack.addWidget(self.dashboard_screen)
        self.content_stack.addWidget(self.run_screen)
        self.content_stack.addWidget(self.settings_screen)
        shell_lay.addWidget(self.content_stack)

        self.outer_stack.addWidget(self.app_shell)

        # ---------- native chrome: toolbar / status bar (menu bar removed) ----------
        self._build_toolbar()
        self._build_status_bar()

        # ---------- wiring ----------
        self.home_screen.get_started_clicked.connect(self._on_home_get_started)
        self.startup_screen.model_chosen.connect(self._on_model_chosen)
        self.startup_screen.skip_to_dashboard.connect(self._on_skip_to_dashboard)
        self.startup_screen.view_data_requested.connect(self._on_view_data_requested)
        self.run_screen.run_completed.connect(self._on_run_completed)
        self.run_screen.continue_requested.connect(lambda: self._show_tab("dashboard"))
        self.dashboard_screen.benchmark_requested.connect(self._on_benchmark_requested)
        self.settings_screen.theme_changed.connect(self.apply_theme)

        self.settings_screen.set_active(self._theme)
        self.apply_theme(self._theme)

        backend.ensure_bundled_seed()
        has_existing = bool(backend.models_with_data(backend.get_conn()))
        self.startup_screen.set_existing_results_available(has_existing)
        self._show_startup_chrome()
        self.outer_stack.setCurrentWidget(self.home_screen)

        self._ollama_timer = QTimer(self)
        self._ollama_timer.timeout.connect(self._refresh_ollama_status)
        self._ollama_timer.start(8000)
        self._refresh_ollama_status()

        # Flush any runs left in the local sync outbox from a previous
        # offline/failed session, then keep flushing periodically - both
        # off the GUI thread, so a slow/unreachable Supabase host never
        # stalls the app.
        self._sync_pending_outbox()
        self._auto_sync_timer = QTimer(self)
        self._auto_sync_timer.setInterval(120_000)  # 2 minutes
        self._auto_sync_timer.timeout.connect(self._sync_pending_outbox)
        self._auto_sync_timer.start()

    def _sync_pending_outbox(self) -> None:
        """Best-effort background flush of the local Supabase sync outbox.
        Silently does nothing if Supabase isn't configured or nothing is
        pending - safe to call on every launch and on a recurring timer."""
        if not supabase_sync.is_configured():
            return
        supabase_sync.trigger_background_sync(lambda: backend.store.connect(backend.DB_PATH))

    # ---------------- chrome construction ----------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setObjectName("AppToolBar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(toolbar)

        # Brand mark (leading, like a native app's toolbar icon) - the
        # icon-only mark, with the "PRISM" name set as real text beside it
        # rather than baked into a wordmark image.
        brand_icon = QLabel()
        brand_icon.setObjectName("BrandIcon")
        if LOGO_ICON_PATH.exists():
            pix = QPixmap(str(LOGO_ICON_PATH))
            if not pix.isNull():
                brand_icon.setPixmap(pix.scaledToHeight(32, Qt.TransformationMode.SmoothTransformation))
        toolbar.addWidget(brand_icon)

        brand_text = QLabel("PRISM")
        brand_text.setObjectName("BrandMark")
        toolbar.addWidget(brand_text)
        self.toolbar_separator = toolbar.addSeparator()

        nav_group = QActionGroup(self)
        nav_group.setExclusive(True)

        self.nav_dashboard = QAction("Dashboard", self, checkable=True)
        self.nav_dashboard.triggered.connect(lambda: self._show_tab("dashboard"))
        self.nav_benchmark = QAction("Benchmark", self, checkable=True)
        self.nav_benchmark.triggered.connect(lambda: self._show_tab("benchmark"))
        self.nav_settings = QAction("Settings", self, checkable=True)
        self.nav_settings.triggered.connect(lambda: self._show_tab("settings"))

        for a in (self.nav_dashboard, self.nav_benchmark, self.nav_settings):
            nav_group.addAction(a)
            toolbar.addAction(a)

        # expanding spacer pushes the model badge / change button to the
        # right edge of the toolbar, like a native app's trailing toolbar items
        from PySide6.QtWidgets import QSizePolicy
        self.toolbar_spacer = QWidget()
        self.toolbar_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar_spacer_action = toolbar.addWidget(self.toolbar_spacer)

        self.model_badge = ModelBadge()
        self.model_badge_action = toolbar.addWidget(self.model_badge)

        self.change_model_btn = QPushButton("Change")
        self.change_model_btn.setObjectName("ChangeModelBtn")
        self.change_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_model_btn.clicked.connect(self._on_change_model_requested)
        self.change_model_btn_action = toolbar.addWidget(self.change_model_btn)

        # Widgets added to a QToolBar are wrapped in a QAction under the
        # hood; the toolbar's own layout re-shows the wrapped widget based
        # on THAT action's visibility whenever the toolbar is (re)shown or
        # polished, regardless of what setVisible() was called directly on
        # the widget. So hide/show must go through these actions, not the
        # widgets themselves, or the widgets flash back to visible the
        # moment the window is first shown.
        self.model_badge_action.setVisible(False)
        self.change_model_btn_action.setVisible(False)

        self.nav_dashboard.setChecked(True)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setObjectName("AppStatusBar")
        self.setStatusBar(bar)
        self.ollama_status_lbl = QLabel("Ollama: checking\u2026")
        bar.addPermanentWidget(self.ollama_status_lbl)
        self.data_path_lbl = QLabel(f"Local data: {backend.APP_DIR}")
        bar.addWidget(self.data_path_lbl)

    # ------------------------------------------------------------ Qt events

    def showEvent(self, event) -> None:
        """Qt re-reveals explicitly-hidden child widgets the first time a
        top-level window is shown, which would otherwise undo the
        model-badge/Change-button hide() applied in __init__ (before
        main.py's window.show() ever runs). Re-apply whichever chrome
        state is correct for the screen currently on top once the window
        is actually on screen, so neither the Home landing screen nor the
        startup screen ever flashes the full app-shell toolbar.
        """
        super().showEvent(event)
        if self.outer_stack.currentWidget() in (self.home_screen, self.startup_screen):
            self._show_startup_chrome()
        else:
            self._show_app_chrome()

    def closeEvent(self, event) -> None:
        """Warn before closing if something's actively in-flight that would
        be silently killed - a running benchmark, an Ollama install, or a
        model pull - rather than letting the window vanish mid-task.

        Either way the window actually closes, stop the local Ollama
        server first (see backend.ollama_stop) - the app owns Ollama's
        lifecycle: it starts it, so it also stops it, rather than leaving
        it running in the background after the app is gone. This also
        guarantees the next launch never inherits a leftover process that
        might be missing OLLAMA_NO_CLOUD (see
        ollama_installer._serve_env), which is what causes pulls to hang
        forever on "pulling manifest".
        """
        reasons = self._in_progress_reasons()
        if not reasons:
            backend.ollama_stop()
            super().closeEvent(event)
            return

        if len(reasons) == 1:
            detail = f"{reasons[0]} is still running."
        else:
            detail = "The following are still running:\n\u2022 " + "\n\u2022 ".join(reasons)

        choice = QMessageBox.warning(
            self,
            "Work in progress",
            f"{detail}\n\nClosing PRISM now will stop it before it finishes. "
            "Are you sure you want to quit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            # Actually stop everything in flight before letting the window
            # close - previously "Yes" just let the window vanish while the
            # pull/install/benchmark kept running on its QThread in the
            # background. For a pull, that background thread (and Ollama's
            # own in-progress download) could easily outlive the visible
            # app, which is what made a *second* launch's pull of the same
            # model appear to refuse to start or progress.
            self._cancel_in_progress_work()
            backend.ollama_stop()
            super().closeEvent(event)
        else:
            event.ignore()

    def _cancel_in_progress_work(self) -> None:
        """Best-effort cancel of every worker thread still running, and a
        short wait for each to actually finish, so nothing is left running
        against Ollama after this window is gone."""
        workers = []

        worker = getattr(self.run_screen, "worker", None)
        interrupted_run_id = None
        if worker is not None and worker.isRunning():
            # cancel() makes the engine finalize the run's DB status as
            # "cancelled" - the right outcome for the Cancel button, but
            # wrong here: this is the app closing mid-run, not someone
            # deliberately abandoning it, and a "cancelled" run is never
            # auto-resumed on the next launch. Remember its run_id so we
            # can put it back to "running" (resumable) after it stops.
            interrupted_run_id = getattr(self.run_screen, "_current_run_id", None)
            worker.cancel()
            workers.append(worker)

        install_worker = getattr(self.startup_screen, "_install_worker", None)
        if install_worker is not None and install_worker.isRunning():
            # No cancel() on the install worker (installing/starting Ollama
            # isn't safely interruptible mid-step) - just don't block on it.
            pass

        for tag, pull_worker in list(getattr(self.startup_screen, "pull_workers", {}).items()):
            if pull_worker.isRunning():
                pull_worker.cancel()
                workers.append(pull_worker)

        for w in workers:
            w.wait(5000)  # give each up to 5s to unwind before we move on

        if interrupted_run_id:
            try:
                backend.store.set_run_status(backend.get_conn(), interrupted_run_id, "running")
            except Exception:
                pass

    def _in_progress_reasons(self) -> list[str]:
        reasons: list[str] = []
        worker = getattr(self.run_screen, "worker", None)
        if worker is not None and worker.isRunning():
            reasons.append("A benchmark run")
        install_worker = getattr(self.startup_screen, "_install_worker", None)
        if install_worker is not None and install_worker.isRunning():
            reasons.append("Installing/starting Ollama")
        pulling = [
            tag for tag, w in getattr(self.startup_screen, "pull_workers", {}).items()
            if w.isRunning()
        ]
        if pulling:
            label = "A model download" if len(pulling) == 1 else f"{len(pulling)} model downloads"
            reasons.append(f"{label} ({', '.join(pulling)})")
        return reasons

    # ---------------- navigation ----------------

    def _show_startup_chrome(self) -> None:
        """On the startup screen, the toolbar shows only the brand mark - no Dashboard/Benchmark/Settings nav, no model badge, no status bar - since there's no active model or app state yet to navigate
        around."""
        for action in (self.nav_dashboard, self.nav_benchmark, self.nav_settings):
            action.setVisible(False)
        self.toolbar_separator.setVisible(False)
        self.toolbar_spacer_action.setVisible(False)
        self.model_badge_action.setVisible(False)
        self.change_model_btn_action.setVisible(False)
        self.statusBar().setVisible(False)

    def _show_app_chrome(self) -> None:
        for action in (self.nav_dashboard, self.nav_benchmark, self.nav_settings):
            action.setVisible(True)
        self.toolbar_separator.setVisible(True)
        self.toolbar_spacer_action.setVisible(True)
        self.model_badge_action.setVisible(True)
        self.change_model_btn_action.setVisible(True)
        self.statusBar().setVisible(True)

    def _show_tab(self, key: str) -> None:
        mapping = {
            "dashboard": (self.dashboard_screen, self.nav_dashboard),
            "benchmark": (self.run_screen, self.nav_benchmark),
            "settings": (self.settings_screen, self.nav_settings),
        }
        widget, action = mapping[key]
        if key == "dashboard":
            self.dashboard_screen.refresh()
        action.setChecked(True)
        self.content_stack.fade_to(widget)

    def _on_model_chosen(self, model_tag: str) -> None:
        # Same defensive re-check as _on_skip_to_dashboard - this signal is
        # only emitted once StartupScreen has verified Ollama is connected
        # and the model is pulled, but navigation off the startup screen
        # must never rely on a single code path getting that right.
        if not backend.ollama_available():
            QMessageBox.warning(
                self,
                "Ollama isn't running",
                "Ollama needs to be installed and running before you can use "
                "PRISM. Please finish setup on the previous screen first.",
            )
            return
        self._show_app_chrome()
        self.active_model = model_tag
        app_state.set_selected_model(model_tag)
        self.model_badge.set_model(backend.model_label(model_tag))
        self.dashboard_screen.set_model(model_tag)
        self.run_screen.preselect_model(model_tag)
        self.outer_stack.fade_to(self.app_shell)

        # Always land on the Benchmark screen after startup - if this model
        # already has local results, preselect_model() above has already
        # surfaced its own "Existing results found - View dashboard" prompt
        # there, so the user decides rather than being auto-redirected.
        self._show_tab("benchmark")
        self._maybe_resume_interrupted_run(model_tag)

    def _on_view_data_requested(self, model_tag: str) -> None:
        """Routes to the Dashboard without requiring the model to be
        installed. Two callers share this path:
          1. StartupScreen's "View existing data" choice for a searched
             tag known to have verified GitHub results.
          2. Continue on a not-yet-installed curated model, which has no
             promised data at all - going to the Dashboard should just
             show its (possibly empty) state, not an error.
        Only case 1 is an error if the fetch fails - has_published_result()
        distinguishes the two, since case 2 was never promised any data to
        begin with. Neither case marks the model installed; if the person
        later tries to start a fresh run against this tag anyway,
        run_screen.start_run()'s existing "Model not installed" hard gate
        still applies exactly as it does for any other not-installed tag -
        this path never bypasses that check.

        A *third*, unpromised case can also reach here: a custom tag the
        person typed in (Add other models) that has no local data and no
        published result, and isn't one of the curated catalog models
        either - e.g. a misspelled/nonexistent tag whose pull failed.
        Unlike case 2, the Dashboard has literally nothing to key off for
        this tag: its model dropdown is only ever populated from tags that
        already have rows in the results tables, so silently continuing
        here doesn't show *this* model's (empty) state - it leaves
        whatever model was previously showing, which looks like being
        redirected to a random model's dashboard. Refuse instead."""
        conn = backend.get_conn()
        has_data = backend.has_data(conn, model_tag)
        if not has_data and public_results.has_published_result(model_tag):
            imported = backend.import_public_result(model_tag)
            if not imported:
                QMessageBox.critical(
                    self,
                    "Couldn't load verified results",
                    f"\u201c{model_tag}\u201d was expected to have published results, "
                    "but they couldn't be downloaded right now. Check your "
                    "connection and try again.",
                )
                return
            has_data = True
        if not has_data and model_tag not in backend.models_catalog():
            QMessageBox.information(
                self,
                "No data for this model yet",
                f"Sorry, data isn\u2019t available for \u201c{model_tag}\u201d. "
                "Run inference to view the dashboard for this model.",
            )
            return
        self._show_app_chrome()
        self.active_model = model_tag
        app_state.set_selected_model(model_tag)
        self.model_badge.set_model(backend.model_label(model_tag))
        self.dashboard_screen.set_model(model_tag)
        self.run_screen.preselect_model(model_tag)
        self.outer_stack.fade_to(self.app_shell)
        self._show_tab("dashboard")

    def _on_skip_to_dashboard(self, picked_tag: str = "") -> None:
        # Belt-and-suspenders: the skip link itself is only shown when
        # Ollama is connected (see StartupScreen._refresh_skip_link_visibility),
        # but never trust a signal alone to gate navigation - re-check here
        # too, since this is the one place that can move the user off the
        # startup screen.
        if not backend.ollama_available():
            QMessageBox.warning(
                self,
                "Ollama isn't running",
                "Ollama needs to be installed and running before you can use "
                "PRISM. Please finish setup on the previous screen first.",
            )
            return
        conn = backend.get_conn()
        # ``picked_tag`` is whichever model row was highlighted in the
        # picker at the moment "Skip to dashboard" was clicked (e.g. the
        # person selected an installed model but never pressed Continue) -
        # honor that choice first, as long as it's actually installed and
        # has data. A model with a verified GitHub result is treated the
        # same as "has data" - it just needs to be imported first (mirrors
        # _on_view_data_requested).
        if picked_tag and not backend.has_data(conn, picked_tag) and public_results.has_published_result(picked_tag):
            imported = backend.import_public_result(picked_tag)
            if not imported:
                QMessageBox.critical(
                    self,
                    "Couldn't load verified results",
                    f"\u201c{picked_tag}\u201d was expected to have published results, "
                    "but they couldn't be downloaded right now. Check your "
                    "connection and try again.",
                )
                return
        if picked_tag and not backend.has_data(conn, picked_tag):
            # The person actively picked/added this exact model in the
            # picker and then hit "Skip to dashboard" - if it has no local
            # results and nothing published on GitHub either, silently
            # showing a *different* model's dashboard (the old behavior)
            # is misleading. Tell them plainly instead of guessing what
            # they meant.
            QMessageBox.information(
                self,
                "No data for this model yet",
                f"Sorry, data isn\u2019t available for \u201c{picked_tag}\u201d. "
                "Run inference to view the dashboard for this model.",
            )
            return
        self._show_app_chrome()
        # By this point picked_tag, if given, is guaranteed to have data
        # (either it already did, or it was just imported above) - the
        # "no data" case returned early instead of falling through here.
        # Only fall back to the remembered/first-available model when
        # nothing was actively selected.
        if picked_tag and backend.has_data(conn, picked_tag):
            model_tag = picked_tag
        else:
            model_tag = app_state.get_selected_model()
            if not model_tag or not backend.has_data(conn, model_tag):
                data = backend.models_with_data(conn)
                model_tag = next(iter(data), None)
        if model_tag:
            self.active_model = model_tag
            app_state.set_selected_model(model_tag)
            self.model_badge.set_model(backend.model_label(model_tag))
            self.dashboard_screen.set_model(model_tag)
            self.run_screen.preselect_model(model_tag)
        self.outer_stack.fade_to(self.app_shell)
        self._show_tab("dashboard")

    def _on_change_model_requested(self) -> None:
        """'Change' button on the toolbar model badge - drops the user back
        onto the startup screen's model picker so they can switch the
        active model. The badge (and this button) are hidden on the
        startup screen itself, so this is only reachable from the other
        screens."""
        has_existing = bool(backend.models_with_data(backend.get_conn()))
        self.startup_screen.set_existing_results_available(has_existing)
        self._show_startup_chrome()
        self.outer_stack.fade_to(self.startup_screen)

    def _on_home_get_started(self) -> None:
        """Home landing screen's "Get Started" - hands off to the existing
        startup screen (connect to Ollama, choose the active model); from
        there everything continues exactly as before."""
        has_existing = bool(backend.models_with_data(backend.get_conn()))
        self.startup_screen.set_existing_results_available(has_existing)
        self._show_startup_chrome()
        self.outer_stack.fade_to(self.startup_screen)

    def _on_run_completed(self, model_tag: str) -> None:
        # Refresh regardless of whether this was the active model (a re-run)
        # or a "compare" target model run from the dashboard's compare drawer.
        # run_completed only fires from RunScreen._on_finished on a *clean*
        # success (never on a cancelled or fatally-halted run - see
        # RunScreen._on_finished), so jumping straight to the dashboard here
        # never hides an error banner or a still-running benchmark - the
        # person always lands on the freshly-refreshed results for the run
        # that just finished, instead of having to press "View dashboard"
        # themselves.
        self.dashboard_screen.refresh()
        self.dashboard_screen.focus_on_latest_run(model_tag)
        self._maybe_auto_sync(model_tag)
        self._show_tab("dashboard")

    def _maybe_resume_interrupted_run(self, model_tag: str) -> None:
        """If the app was closed (or crashed) while a benchmark run for
        this model was mid-flight, its row in the run index was left with
        status="running" (closeEvent's cancel just stops the worker - it
        never marks the run finished/failed). Detect that here and resume
        automatically rather than making the person notice and click
        Continue themselves; the engine already reuses every completed
        request and only re-runs what's left (see inference.run_dataset_model),
        so this picks up where it left off instead of starting over.
        """
        conn = backend.get_conn()
        row = conn.execute(
            "SELECT benchmark_run_id, datasets FROM runs "
            "WHERE model = ? AND status = 'running' "
            "ORDER BY created_utc DESC LIMIT 1",
            (model_tag,),
        ).fetchone()
        if row is None:
            return
        import json as _json
        try:
            datasets = _json.loads(row["datasets"]) if row["datasets"] else []
        except Exception:
            datasets = []
        if not datasets:
            return
        self.statusBar().showMessage(
            f"Resuming the interrupted benchmark run for {backend.model_label(model_tag)}\u2026",
            6000,
        )
        self.run_screen.resume_run(model_tag, datasets, row["benchmark_run_id"])

    def _on_benchmark_requested(self, model_tag: str) -> None:
        if not model_tag:
            return
        self.run_screen.preselect_model(model_tag)
        self._show_tab("benchmark")

    def _refresh_ollama_status(self) -> None:
        # Runs the (potentially blocking, up to ~5s) reachability check on a
        # worker thread so a slow/offline Ollama never freezes the GUI.
        # Guard against overlap: if a previous check is still running (e.g.
        # Ollama is timing out), skip this tick rather than piling up threads.
        if getattr(self, "_ollama_status_worker", None) is not None and self._ollama_status_worker.isRunning():
            return
        worker = backend.OllamaStatusWorker(self)
        worker.status_checked.connect(self._on_ollama_status_checked)
        self._ollama_status_worker = worker
        worker.start()

    def _on_ollama_status_checked(self, available: bool) -> None:
        if available:
            self.ollama_status_lbl.setText("\u25cf Ollama online")
        else:
            self.ollama_status_lbl.setText("\u25cb Ollama offline")

    # ---------------- cloud sync ----------------

    def _maybe_auto_sync(self, model_tag: str) -> None:
        """Silently mirrors the run that just finished to the project's
        shared Supabase store (hardcoded in prism_core/config.py). No-op if
        that project isn't configured; failures show briefly in the status
        bar but never interrupt the app."""
        if not supabase_sync.is_configured():
            self.statusBar().showMessage(
                "Not synced to Supabase - PRISM_SUPABASE_URL / "
                "PRISM_SUPABASE_PUBLISHABLE_KEY aren't set.", 6000
            )
            return
        conn = backend.get_conn()
        info = backend.run_info(conn, model_tag)
        if not info:
            return
        worker = backend.SyncWorker(mode="single", benchmark_run_id=info["benchmark_run_id"], parent=self)
        worker.finished_ok.connect(self._on_sync_finished)
        self._sync_worker = worker  # keep a reference so it isn't GC'd mid-run
        worker.start()

    def _on_sync_finished(self, ok: bool, message: str) -> None:
        if ok:
            self.statusBar().showMessage("Synced to Supabase.", 4000)
        else:
            self.statusBar().showMessage(f"Supabase sync failed: {message}", 6000)

    # ---------------- theme ----------------

    def apply_theme(self, name: str) -> None:
        self._theme = name
        app_state.set_theme(name)
        palette = DUSK if name == "dusk" else PAPER
        self.setStyleSheet(build_qss(palette))
        self.settings_screen.set_active(name)
        # chart title/legend/axis colors are picked per-theme at draw time,
        # so a live theme switch needs a repaint to actually show the fix
        if hasattr(self, "dashboard_screen"
                   ):
            self.dashboard_screen.refresh()