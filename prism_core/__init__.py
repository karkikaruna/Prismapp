"""PRISM core - pure-Python benchmark engine (no Qt).

Houses the validated PRISM methodology (prompt rendering, response parsing,
consistency scoring, reporting) together with the orchestration, Ollama
integration, reproducibility fingerprint, and local run store that the desktop
GUI calls into.

Design rule: this package MUST NOT import PySide6/Qt. Keeping it Qt-free makes
the methodology headless-testable and reusable by future non-GUI consumers
(CI, a submission backend). The GUI layer lives in ``prism_app`` and depends on
this package, never the other way around.
"""

__version__ = "0.1.0"