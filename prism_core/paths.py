"""Run-scoped output layout.

Each benchmark run writes under ``runs/<benchmark_run_id>/`` keeping the exact
sibling-directory shape the research pipeline used - ``raw_responses/``,
``parsed/``, ``scored/``, ``summary/`` - so the ported scorer's filename pairing
(``{dataset}__{safe_model}.jsonl``) and the report glob work unchanged. A
canonical ``summary.json`` (the future-public artifact) sits at the run root.

This replaces the research ``config.RESULTS_*`` globals: outputs are never
coupled to a single global location.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def safe_model(model_name: str) -> str:
    """Filesystem-safe form of an Ollama model tag (``mistral:7b`` → ``mistral_7b``)."""
    return model_name.replace(":", "_").replace("/", "_")


@dataclass(frozen=True)
class RunPaths:
    """Resolves every path for one benchmark run."""

    root: Path

    @classmethod
    def for_run(cls, runs_root: Path | str, benchmark_run_id: str) -> "RunPaths":
        return cls(Path(runs_root) / benchmark_run_id)

    # --- stage directories -------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.root / "raw_responses"

    @property
    def parsed_dir(self) -> Path:
        return self.root / "parsed"

    @property
    def scored_dir(self) -> Path:
        return self.root / "scored"

    @property
    def summary_dir(self) -> Path:
        return self.root / "summary"

    @property
    def summary_json(self) -> Path:
        return self.root / "summary.json"

    # --- per dataset/model files (same basename across raw/parsed/scored) --
    def _basename(self, dataset: str, model: str) -> str:
        return f"{dataset}__{safe_model(model)}.jsonl"

    def raw_file(self, dataset: str, model: str) -> Path:
        return self.raw_dir / self._basename(dataset, model)

    def parsed_file(self, dataset: str, model: str) -> Path:
        return self.parsed_dir / self._basename(dataset, model)

    def scored_file(self, dataset: str, model: str) -> Path:
        return self.scored_dir / self._basename(dataset, model)

    def question_metrics_file(self, dataset: str, model: str) -> Path:
        return self.summary_dir / f"{dataset}__{safe_model(model)}_question_metrics.jsonl"

    def ensure(self) -> "RunPaths":
        """Create all stage directories; returns self for chaining."""
        for directory in (self.raw_dir, self.parsed_dir, self.scored_dir, self.summary_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self