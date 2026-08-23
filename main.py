"""
PRISM desktop app entry point.

    python main.py

Launches the PySide6 GUI. The GUI (app/) is a thin presentation layer over
prism_core - a Qt-free benchmark engine (inference, parsing, consistency
scoring, reporting, local run index) that talks to Ollama directly and holds
no GUI code. app/ depends on prism_core; prism_core never imports app/.

Crash safety: an app-wide exception hook is installed so that any uncaught
error anywhere in the GUI shows a dialog instead of silently killing the
process. Inference-specific errors (OOM, lost Ollama connection, crashes
during a benchmark run) are handled even more locally, inside
prism_core.inference / app.services.backend.BenchmarkWorker, so they never
reach this hook at all - they halt just that run and let the person retry,
continue, or stop from the Benchmark screen.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon


def _install_crash_guard(app: QApplication) -> None:
    """Catch any exception that escapes a Qt event handler and show a
    dialog instead of letting Qt/Python tear the process down. This is a
    last-resort net around whatever the benchmark-specific handling in
    inference.py / backend.py doesn't already catch closer to the source.
    """

    def handle(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(details, file=sys.stderr)
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("PRISM - unexpected error")
            box.setText(
                "Something went wrong, but PRISM is still running.\n\n"
                f"{exc_type.__name__}: {exc_value}"
            )
            box.setDetailedText(details)
            box.exec()
        except Exception:
            # If even the dialog fails, at least we already logged to stderr
            # above and the process keeps running rather than dying.
            pass

    sys.excepthook = handle


def main() -> None:
    app = QApplication(sys.argv)
    _install_crash_guard(app)
    # Fusion is Qt's own native-feeling cross-platform style (proper focus
    # rects, real system-ish widgets) - a much better base for the QSS in
    # theme.py to sit on top of than whatever the platform default is.
    app.setStyle("Fusion")
    app.setApplicationName("PRISM")
    icon_path = PROJECT_ROOT / "app" / "resources" / "prism_logo.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        from app.gui.main_window import MainWindow
        window = MainWindow()
    except Exception as exc:  # noqa: BLE001 - a launch failure must not vanish silently
        QMessageBox.critical(
            None,
            "PRISM failed to start",
            f"PRISM could not start up:\n\n{type(exc).__name__}: {exc}",
        )
        sys.exit(1)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()