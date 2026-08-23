"""Locate the bundled, read-only benchmark data shipped inside the package.

The frozen dataset samples, precomputed prompts, and the five prompt templates
travel with the app so no HuggingFace download is needed at runtime. This module
returns real :class:`pathlib.Path` objects (the ported methodology code uses
``Path.glob`` / ``read_text``), resolving correctly in three environments:

* running from a source checkout,
* running from an installed wheel,
* running from a PyInstaller-frozen executable (data added so it lands at
  ``<_MEIPASS>/prism_core/resources/data``).
"""
from __future__ import annotations

import sys
from pathlib import Path


def _base_data_dir() -> Path:
    """Resolve the root of the bundled ``resources/data`` tree."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller unpacks bundled data under _MEIPASS mirroring the source
        # package layout (see the .spec / --add-data mapping).
        return Path(sys._MEIPASS) / "prism_core" / "resources" / "data"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent / "resources" / "data"


DATA_DIR: Path = _base_data_dir()
TEMPLATES_DIR: Path = DATA_DIR / "templates"
PROCESSED_DIR: Path = DATA_DIR / "processed"
PROMPTS_DIR: Path = DATA_DIR / "prompts"


def sample_path(dataset_name: str) -> Path:
    """Path to a frozen dataset sample, e.g. ``arc_challenge_sample.json``."""
    return PROCESSED_DIR / f"{dataset_name}_sample.json"


def prompts_path(dataset_name: str) -> Path:
    """Path to a precomputed prompt artifact, e.g. ``sciq_prompts.json``."""
    return PROMPTS_DIR / f"{dataset_name}_prompts.json"
