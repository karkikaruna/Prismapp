"""
Small persisted app-state file (theme, pending install/pull markers) -
separate from the prism_core run index (SQLite) because it's UI state, not
benchmark data. Lives at ~/.prism/app_state.json next to the run index and
run outputs.

Deliberately does NOT persist the selected model - the person picks a
model fresh every launch (see StartupScreen._show_model_picker). The
active model is only ever tracked in memory for the lifetime of the running
app (MainWindow.active_model).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".prism"
STATE_PATH = APP_DIR / "app_state.json"

_DEFAULTS: dict[str, Any] = {
    "theme": "paper",
    "pending_install": False,
    "pending_pull_tags": [],
}


def _read() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def _write(state: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_theme() -> str:
    return _read().get("theme", "paper")


def set_theme(theme: str) -> None:
    state = _read()
    state["theme"] = theme
    _write(state)


# --- resume-on-relaunch markers ---------------------------------------------
# Set the moment an install/pull actually starts, cleared only on a
# *successful* finish - so if the app is closed (or crashes) mid-task, the
# marker survives and the next launch knows to pick it back up
# automatically instead of silently forgetting an interrupted setup step.

def set_pending_install(pending: bool) -> None:
    state = _read()
    state["pending_install"] = pending
    _write(state)


def get_pending_install() -> bool:
    return bool(_read().get("pending_install", False))


def add_pending_pull(model_tag: str) -> None:
    state = _read()
    tags = set(state.get("pending_pull_tags") or [])
    tags.add(model_tag)
    state["pending_pull_tags"] = sorted(tags)
    _write(state)


def remove_pending_pull(model_tag: str) -> None:
    state = _read()
    tags = set(state.get("pending_pull_tags") or [])
    tags.discard(model_tag)
    state["pending_pull_tags"] = sorted(tags)
    _write(state)


def get_pending_pull_tags() -> list[str]:
    return list(_read().get("pending_pull_tags") or [])