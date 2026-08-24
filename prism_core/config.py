from __future__ import annotations

import os
from typing import Optional

try:  # optional dependency - only needed to read a local .env in dev/packaging
    from dotenv import load_dotenv, find_dotenv

    _DOTENV_AVAILABLE = True

    # Plain load_dotenv() with no path only finds a `.env` file that happens
    # to sit at-or-above the process's *current working directory* - it has
    # no idea where this project actually lives. That works fine when you
    # launch the app from the project root in a terminal, but silently finds
    # nothing (no error - env vars just stay unset) launched any other way:
    # a desktop shortcut, a different cwd, `python -m` from elsewhere, a
    # packaged build. Supabase sync then quietly no-ops with no indication
    # why. Anchor the search at this file's own location and walk upward,
    # so it finds the project's `.env` regardless of the working directory
    # the app was launched from.
    _env_path = find_dotenv(filename=".env", usecwd=False)
    if not _env_path:
        # find_dotenv()'s own upward search starts from the caller's frame,
        # which can behave unexpectedly in a packaged/frozen build - fall
        # back to walking up from this file's own directory explicitly.
        _here = os.path.dirname(os.path.abspath(__file__))
        _candidate = _here
        for _ in range(6):  # project root is a few levels up at most
            _maybe = os.path.join(_candidate, ".env")
            if os.path.isfile(_maybe):
                _env_path = _maybe
                break
            _parent = os.path.dirname(_candidate)
            if _parent == _candidate:
                break
            _candidate = _parent
    if _env_path:
        load_dotenv(_env_path)
    else:
        load_dotenv()  # last resort: cwd-based default behavior

except ImportError:
    # This used to be a silent no-op - if `python-dotenv` simply isn't
    # installed in whatever Python environment is running the app (a very
    # easy state to end up in: a venv the dependency was never installed
    # into, a system Python separate from the one `pip install`s went to,
    # etc.), a `.env` file could sit right there, fully correct, and never
    # be read - with zero indication why Supabase sync (or anything else
    # reading env vars) just quietly never turns on. DOTENV_AVAILABLE lets
    # callers (e.g. the Settings screen's Cloud Sync panel) surface a
    # concrete "python-dotenv isn't installed" message instead of the
    # generic "not configured" one, which sends someone chasing the wrong
    # problem (their .env contents) instead of the real one (pip install).
    _DOTENV_AVAILABLE = False

DOTENV_AVAILABLE: bool = _DOTENV_AVAILABLE

# --- Reproducibility / sampling -------------------------------------------
RANDOM_SEED: int = 2026
SAMPLE_SIZE_PER_DATASET: int = 200

# Version of the frozen benchmark samples (seed 2026, 200 questions/dataset,
# deterministic SciQ option shuffle). Bump when the frozen samples change.
DATASET_VERSION: str = "1.0"

# --- Dataset registry (HuggingFace source; used only by the dev refresh tool,
# never at product runtime - the app ships frozen samples) ------------------
DATASETS: dict[str, dict[str, Optional[str]]] = {
    "arc_challenge": {
        "huggingface_dataset": "allenai/ai2_arc",
        "config": "ARC-Challenge",
        "split": "test",
    },
    "sciq": {
        "huggingface_dataset": "allenai/sciq",
        "config": None,
        "split": "test",
    },
}

# --- Prompt conditions (order matters - used for deterministic sorting) ----
PROMPT_CONDITIONS: tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4")

# --- Inference parameters --------------------------------------------------
TEMPERATURE: float = 0.0
NUM_PREDICT: int = 450
REQUEST_TIMEOUT: int = 180

# --- Ollama endpoint (base URL is configurable; paths derive from it) ------
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_GENERATE_PATH: str = "/api/generate"
OLLAMA_TAGS_PATH: str = "/api/tags"
OLLAMA_SHOW_PATH: str = "/api/show"
OLLAMA_PULL_PATH: str = "/api/pull"

# --- Validated models (highlighted in the UI; the app accepts any installed
# Ollama model, these four are the research-validated set) ------------------
# ``min_ram_gb`` is the RAM Ollama/the model runtime recommends to load and
# run that model comfortably (roughly 2x the parameter count in GB, rounded
# up to a common tier). It's advisory only - used to warn the user before a
# pull that's unlikely to run well on their machine, never to hard-block it.
MODELS: dict[str, dict[str, str | int]] = {
    "llama3.2:3b": {"label": "Llama-3.2 (3B)", "min_ram_gb": 8},
    "gemma3:4b": {"label": "Gemma-3 (4B)", "min_ram_gb": 8},
    "phi4-mini:latest": {"label": "Phi-4-mini (3.8B)", "min_ram_gb": 8},
    "mistral:7b": {"label": "Mistral-7B", "min_ram_gb": 16},
}

# --- Provenance identifiers ------------------------------------------------
EXPERIMENT_ID: str = "PRISM-EXP1-v1"
TEMPLATE_VERSION: str = "1.0"
PROTOCOL_VERSION: str = "1.0"

# --- Cloud sync (Supabase) --------------------------------------------------
# Runtime sync (app/services/supabase_sync.py) always uses the *anon* key - # safe to ship in a client build as long as supabase/schema.sql's RLS
# policies restrict the anon role to insert/upsert on runs + run_results.
# Ported from Karuna's env-var convention: PRISM_SUPABASE_URL /
# PRISM_SUPABASE_PUBLISHABLE_KEY (read from a local .env via python-dotenv, or
# from the real environment in a packaged build) overrides the values below,
# so a packaged executable doesn't need the project's keys baked into source.
# The literals here are the fallback used when no environment/​.env value is
# set (e.g. a source checkout with no .env configured yet).
# No real project credentials are baked in here - set PRISM_SUPABASE_URL and
# PRISM_SUPABASE_PUBLISHABLE_KEY as real environment variables (or in a local
# .env) before packaging/running. Empty defaults mean sync is simply disabled
# until configured, rather than silently pointing at someone else's project.
# (PRISM_SUPABASE_ANON_KEY is still read as a fallback for projects still on
# Supabase's legacy JWT-based keys.)
SUPABASE_URL: str = os.environ.get("PRISM_SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = os.environ.get(
    "PRISM_SUPABASE_PUBLISHABLE_KEY",
    os.environ.get("PRISM_SUPABASE_ANON_KEY", ""),  # legacy fallback
)

# Admin-side push (scripts/push_bundled_results_to_supabase.py only - never
# imported by the GUI). Mirrors Karuna's push_to_supabase.py: reads the
# secret key from the environment so it's never bundled into the packaged
# executable at all. (PRISM_SUPABASE_SERVICE_ROLE_KEY is still read as a
# fallback for projects still on Supabase's legacy JWT-based keys.)
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get(
    "PRISM_SUPABASE_SECRET_KEY",
    os.environ.get("PRISM_SUPABASE_SERVICE_ROLE_KEY", ""),  # legacy fallback
)



def ollama_url(base_url: str | None, path: str) -> str:
    """Compose an Ollama endpoint URL from a base URL and a path.

    ``base_url=None`` falls back to :data:`OLLAMA_BASE_URL`. Trailing slashes on
    the base are trimmed so ``"http://host:11434/"`` and ``"http://host:11434"``
    behave identically.
    """
    base = (base_url or OLLAMA_BASE_URL).rstrip("/")
    return f"{base}{path}"