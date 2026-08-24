from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from prism_core import (
    __version__ as APP_VERSION,
    config,
    fingerprint,
    inference,
    ollama,
    parser,
    prompts,
    report,
    scoring,
    store,
)
from prism_core.inference import InferenceProgress, InferenceResult
from prism_core.paths import RunPaths


@dataclass(frozen=True)
class BenchmarkStage:
    """A coarse stage transition in a benchmark run (Qt-free)."""

    benchmark_run_id: str
    stage: str            # start|inference|parse|score|dataset_done|report|completed|cancelled
    message: str
    dataset: Optional[str]
    dataset_index: int
    dataset_count: int


@dataclass(frozen=True)
class DatasetOutcome:
    """Everything produced for one dataset in a run."""

    dataset: str
    config_fingerprint: str
    question_count: int
    inference: InferenceResult
    parsed_processed: int
    parsed_malformed: int
    scored_path: Path
    question_metrics_path: Path
    summary: dict[str, Any]      # the model_dataset_summary row for this dataset


@dataclass(frozen=True)
class BenchmarkResult:
    """Outcome of a full benchmark run."""

    benchmark_run_id: str
    model: str
    model_digest: Optional[str]
    datasets: list[str]
    run_dir: Path
    summary_json_path: Path
    status: str                  # "completed" | "cancelled" | "halted"
    cancelled: bool
    halted: bool = False
    halt_reason: Optional[str] = None
    outcomes: list[DatasetOutcome] = None  # type: ignore[assignment]


def _emit_stage(
    callback: Optional[Callable[[BenchmarkStage], None]],
    *,
    benchmark_run_id: str,
    stage: str,
    message: str,
    dataset: Optional[str],
    index: int,
    count: int,
) -> None:
    if callback is not None:
        callback(
            BenchmarkStage(
                benchmark_run_id=benchmark_run_id,
                stage=stage,
                message=message,
                dataset=dataset,
                dataset_index=index,
                dataset_count=count,
            )
        )


def _validate_prompts_doc(doc: dict[str, Any], dataset: str) -> None:
    """Guard that a bundled prompt artifact matches the active protocol."""
    expected_conditions = list(config.PROMPT_CONDITIONS)
    if doc.get("prompt_conditions") != expected_conditions:
        raise ValueError(
            f"{dataset}: prompt conditions do not match config. "
            f"Expected {expected_conditions}, found {doc.get('prompt_conditions')}."
        )
    if doc.get("template_version") != config.TEMPLATE_VERSION:
        raise ValueError(
            f"{dataset}: template version mismatch. "
            f"Expected {config.TEMPLATE_VERSION!r}, found {doc.get('template_version')!r}."
        )


def run_benchmark(
    *,
    model_name: str,
    datasets: Sequence[str],
    runs_root: Path | str,
    conn: Optional[sqlite3.Connection] = None,
    base_url: Optional[str] = None,
    model_digest: Optional[str] = None,
    resolve_digest: bool = True,
    benchmark_run_id: Optional[str] = None,
    max_questions: Optional[int] = None,
    overwrite: bool = False,
    temperature: float = config.TEMPERATURE,
    num_predict: int = config.NUM_PREDICT,
    timeout: float = config.REQUEST_TIMEOUT,
    experiment_id: str = config.EXPERIMENT_ID,
    load_prompts_fn: Optional[Callable[[str], dict[str, Any]]] = None,
    generate_fn: Optional[Callable[..., tuple[str, float]]] = None,
    progress_callback: Optional[Callable[[InferenceProgress], None]] = None,
    stage_callback: Optional[Callable[[BenchmarkStage], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    fatal_callback: Optional[Callable[[str], None]] = None,
) -> BenchmarkResult:
    """Run the full PRISM benchmark for one model over ``datasets``.

    ``datasets`` are validated against :data:`prism_core.config.DATASETS`.
    ``model_digest`` may be supplied directly (offline/tests); otherwise it is
    resolved via Ollama unless ``resolve_digest`` is False.
    """
    dataset_list = list(datasets)
    for dataset in dataset_list:
        if dataset not in config.DATASETS:
            raise ValueError(
                f"Unknown dataset {dataset!r}. Available: {list(config.DATASETS)}"
            )

    load_prompts = load_prompts_fn if load_prompts_fn is not None else prompts.load_prompts
    run_id = benchmark_run_id or fingerprint.new_benchmark_run_id()
    paths = RunPaths.for_run(runs_root, run_id).ensure()
    dataset_count = len(dataset_list)

    _emit_stage(
        stage_callback, benchmark_run_id=run_id, stage="start",
        message=f"Starting benchmark for {model_name}", dataset=None,
        index=0, count=dataset_count,
    )

    # Preload prompt artifacts once; derive effective per-dataset question counts.
    docs: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for dataset in dataset_list:
        doc = load_prompts(dataset)
        _validate_prompts_doc(doc, dataset)
        questions = doc.get("questions", [])
        if max_questions is not None:
            questions = questions[:max_questions]
        docs[dataset] = doc
        counts[dataset] = len(questions)

    distinct_counts = set(counts.values())
    run_question_count = next(iter(distinct_counts)) if len(distinct_counts) == 1 else None

    # Resolve the model digest (skippable / injectable for offline runs).
    digest = model_digest
    if digest is None and resolve_digest:
        try:
            digest = ollama.model_digest(model_name, base_url=base_url)
        except ollama.OllamaError:
            digest = None

    created_utc = inference.utc_timestamp()
    if conn is not None:
        existing_run = conn.execute(
            "SELECT 1 FROM runs WHERE benchmark_run_id = ?", (run_id,)
        ).fetchone()
        if existing_run is not None:
            # Resuming a run_id that already has a row (e.g. continuing an
            # interrupted run across an app restart) - update it in place
            # instead of a plain INSERT, which would otherwise raise on the
            # benchmark_run_id primary key conflict.
            store.set_run_status(conn, run_id, "running")
        else:
            store.insert_run(
                conn,
                benchmark_run_id=run_id,
                created_utc=created_utc,
                status="running",
                model=model_name,
                datasets=dataset_list,
                run_dir=str(paths.root),
                model_digest=digest,
                question_count=run_question_count,
                app_version=APP_VERSION,
                protocol_version=config.PROTOCOL_VERSION,
            )

    outcomes: list[DatasetOutcome] = []
    all_metrics: list[dict[str, Any]] = []
    cancelled = False
    halted = False
    halt_reason: Optional[str] = None

    for index, dataset in enumerate(dataset_list):
        doc = docs[dataset]

        # --- inference -----------------------------------------------------
        _emit_stage(
            stage_callback, benchmark_run_id=run_id, stage="inference",
            message=f"Running inference on {dataset}", dataset=dataset,
            index=index, count=dataset_count,
        )
        raw_path = paths.raw_file(dataset, model_name)
        inf = inference.run_dataset_model(
            dataset_name=dataset,
            model_name=model_name,
            prompts_doc=doc,
            raw_path=raw_path,
            base_url=base_url,
            temperature=temperature,
            num_predict=num_predict,
            timeout=timeout,
            experiment_id=experiment_id,
            max_questions=max_questions,
            overwrite=overwrite,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            generate_fn=generate_fn,
            fatal_callback=fatal_callback,
        )
        if inf.cancelled:
            cancelled = True
            break
        if inf.halted:
            # A fatal condition (OOM, lost connection, etc.) stopped inference
            # partway through this dataset. Whatever was gathered is already
            # saved to disk (see inference.run_dataset_model), but there isn't
            # a complete response set to parse/score for this dataset yet, so
            # skip straight to writing a partial summary and stopping - the
            # app itself keeps running and the person can retry or continue.
            halted = True
            halt_reason = inf.halt_reason
            outcomes.append(
                DatasetOutcome(
                    dataset=dataset,
                    config_fingerprint="",
                    question_count=counts[dataset],
                    inference=inf,
                    parsed_processed=0,
                    parsed_malformed=0,
                    scored_path=paths.scored_dir / f"{dataset}__{model_name.replace(':', '_')}.jsonl",
                    question_metrics_path=paths.summary_dir / f"{dataset}__{model_name.replace(':', '_')}.jsonl",
                    summary={},
                )
            )
            break

        # --- parse ---------------------------------------------------------
        _emit_stage(
            stage_callback, benchmark_run_id=run_id, stage="parse",
            message=f"Parsing responses for {dataset}", dataset=dataset,
            index=index, count=dataset_count,
        )
        parsed_path = paths.parsed_file(dataset, model_name)
        processed, malformed = parser.parse_jsonl_file(raw_path, parsed_path)

        # --- score ---------------------------------------------------------
        _emit_stage(
            stage_callback, benchmark_run_id=run_id, stage="score",
            message=f"Scoring {dataset}", dataset=dataset,
            index=index, count=dataset_count,
        )
        scored_path, question_metrics_path = scoring.score_parsed_file(
            parsed_path,
            raw_dir=paths.raw_dir,
            scored_dir=paths.scored_dir,
            summary_dir=paths.summary_dir,
        )
        question_metrics = scoring.load_jsonl(question_metrics_path)
        all_metrics.extend(question_metrics)

        # --- provenance + per-dataset summary ------------------------------
        fp = fingerprint.config_fingerprint(
            model_tag=model_name,
            model_digest=digest,
            dataset=dataset,
            question_count=counts[dataset],
            template_sha256=fingerprint.template_hashes_from_prompts(doc),
            temperature=temperature,
            num_predict=num_predict,
        )
        dataset_rows = report.build_model_dataset_summary(question_metrics)
        dataset_summary = dataset_rows[0] if dataset_rows else {}

        if conn is not None:
            store.insert_run_result(
                conn,
                benchmark_run_id=run_id,
                dataset=dataset,
                config_fingerprint=fp,
                metrics=dataset_summary,
                model=model_name,
            )

        outcomes.append(
            DatasetOutcome(
                dataset=dataset,
                config_fingerprint=fp,
                question_count=counts[dataset],
                inference=inf,
                parsed_processed=processed,
                parsed_malformed=malformed,
                scored_path=scored_path,
                question_metrics_path=question_metrics_path,
                summary=dataset_summary,
            )
        )
        _emit_stage(
            stage_callback, benchmark_run_id=run_id, stage="dataset_done",
            message=f"Finished {dataset}", dataset=dataset,
            index=index, count=dataset_count,
        )

    # --- combined report over all completed datasets ----------------------
    status = "halted" if halted else ("cancelled" if cancelled else "completed")
    _emit_stage(
        stage_callback, benchmark_run_id=run_id, stage="report",
        message="Building summary report", dataset=None,
        index=dataset_count, count=dataset_count,
    )
    report.write_csv(
        paths.summary_dir / "model_dataset_summary.csv",
        report.build_model_dataset_summary(all_metrics),
    )
    report.write_csv(
        paths.summary_dir / "model_prompt_summary.csv",
        report.build_model_prompt_summary(all_metrics),
    )

    finished_utc = inference.utc_timestamp()
    summary_doc = {
        "benchmark_run_id": run_id,
        "created_utc": created_utc,
        "finished_utc": finished_utc,
        "status": status,
        "halt_reason": halt_reason,
        "app_version": APP_VERSION,
        "protocol_version": config.PROTOCOL_VERSION,
        "experiment_id": experiment_id,
        "model": {
            "tag": model_name,
            "label": inference.resolve_model_label(model_name),
            "digest": digest,
        },
        "inference": {
            "temperature": temperature,
            "num_predict": num_predict,
            "base_url": base_url or config.OLLAMA_BASE_URL,
            "request_timeout": timeout,
            "max_questions": max_questions,
        },
        "dataset_version": config.DATASET_VERSION,
        "template_version": config.TEMPLATE_VERSION,
        "prompt_conditions": list(config.PROMPT_CONDITIONS),
        "random_seed": config.RANDOM_SEED,
        "datasets": [
            {
                "dataset": outcome.dataset,
                "config_fingerprint": outcome.config_fingerprint,
                "question_count": outcome.question_count,
                "inference": {
                    "total": outcome.inference.total,
                    "reused": outcome.inference.reused,
                    "executed": outcome.inference.executed,
                    "succeeded": outcome.inference.succeeded,
                    "errored": outcome.inference.errored,
                },
                "parsed": {
                    "processed": outcome.parsed_processed,
                    "malformed": outcome.parsed_malformed,
                },
                "summary": outcome.summary,
            }
            for outcome in outcomes
        ],
    }
    paths.summary_json.write_text(
        json.dumps(summary_doc, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if conn is not None:
        store.set_run_status(conn, run_id, status, finished_utc=finished_utc)

    _emit_stage(
        stage_callback, benchmark_run_id=run_id, stage=status,
        message=f"Benchmark {status}", dataset=None,
        index=dataset_count, count=dataset_count,
    )

    return BenchmarkResult(
        benchmark_run_id=run_id,
        model=model_name,
        model_digest=digest,
        datasets=dataset_list,
        run_dir=paths.root,
        summary_json_path=paths.summary_json,
        status=status,
        cancelled=cancelled,
        halted=halted,
        halt_reason=halt_reason,
        outcomes=outcomes,
    )