"""InferenceService - run every prompt condition for one dataset × model.

Ported from the research ``src/inference.py``, preserving the raw-record schema
(``build_success_record`` / ``build_error_record``) and the rolling-latency ETA
exactly. Four deliberate product changes:

1. **Configurable endpoint.** Generation goes through
   :func:`prism_core.ollama.generate` (built on the configurable base URL), not a
   hard-coded ``config.OLLAMA_URL``. The primitive is injectable (``generate_fn``)
   so the pipeline is testable headless with no network.
2. **Any installed model.** ``model_label`` resolves via ``config.MODELS`` with a
   fallback to the raw tag, so a model outside the four validated ones does not
   ``KeyError`` (the research code assumed a known model).
3. **Clean, atomic writes - the overwrite/duplication fix.** The research engine
   *appended* every record, so ``--overwrite`` (and interrupted reruns) could
   accrete duplicate lines and later break the scorer's join. Here the run holds
   exactly one record per ``(question_id, prompt_id)`` key in memory and writes
   the whole file with an atomic temp-file replace. Duplicates are impossible by
   construction. **Error records are still written** (``status="error"``) because
   the parser turns them into observed ``UNKNOWN`` responses - dropping them would
   silently change the methodology.
4. **Qt-free progress + cancel.** Progress is reported through a plain
   :class:`InferenceProgress` callback and cancellation through a
   :class:`threading.Event`, so ``prism_core`` stays free of any GUI dependency.

Resume semantics: without ``overwrite`` the existing file's ``success`` records
are reused verbatim and only non-success keys are re-attempted; with
``overwrite`` the file is regenerated from scratch.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from prism_core import config, ollama


# --- fatal-condition detection ---------------------------------------------
# Not every failed request is equal. A single bad response from the model
# (refusal, malformed output, one dropped HTTP call) is expected and handled
# as an ordinary "error" record - the parser treats it as an observed
# UNKNOWN and the run keeps going. But some failures mean the *inference
# backend itself* is in trouble (out of memory, Ollama process crashed/
# unreachable) and grinding through the remaining requests would just
# produce a wall of identical errors. Those are treated as fatal: the run
# halts (never crashes the app) and hands control back to the UI so the
# person can retry, continue, or stop.
_FATAL_CONSECUTIVE_ERROR_THRESHOLD = 5


def classify_error(exc: Exception) -> Optional[str]:
    """Return a short human-readable fatal reason, or None if recoverable.

    Recognizes out-of-memory conditions and total loss of contact with the
    Ollama server. Anything else (timeouts, malformed responses, a single
    refusal) is left as a normal per-request error.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, MemoryError) or "out of memory" in text or "oom" in text or "cuda out of memory" in text:
        return "The system ran out of memory while running inference."
    if isinstance(exc, ConnectionError) or "connection refused" in text or "connection reset" in text \
            or "failed to establish a new connection" in text or "remote end closed connection" in text:
        return "Lost connection to the Ollama server - it may have crashed or stopped."
    if "model requires more system memory" in text or "requires more system memory" in text:
        return "This model requires more memory than is available on this system."
    return None


# --- ported helpers --------------------------------------------------------
def utc_timestamp() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def prompt_hash(prompt_text: str) -> str:
    """Stable SHA-256 identifier for the exact prompt sent."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def resolve_model_label(model_name: str) -> str:
    """Human label for a model tag, falling back to the tag for any model.

    The research code used ``config.MODELS[model_name]["label"]`` directly, which
    would raise for a model outside the four validated ones. The product accepts
    any installed model, so an unknown tag simply labels itself.
    """
    entry = config.MODELS.get(model_name)
    if entry and entry.get("label"):
        return entry["label"]
    return model_name


# --- progress / result value objects --------------------------------------
@dataclass(frozen=True)
class InferenceProgress:
    """One progress event for a single dataset × model run (Qt-free)."""

    dataset: str
    model: str
    request_number: int          # resolved keys so far (reused + executed)
    total: int                   # total keys in this run
    question_id: str
    prompt_id: str
    status: str                  # "reused" | "success" | "error"
    percent: float
    elapsed_seconds: float
    latency_seconds: Optional[float] = None
    avg_latency_seconds: Optional[float] = None
    eta_seconds: Optional[float] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class InferenceResult:
    """Outcome of one dataset × model run."""

    dataset: str
    model: str
    raw_path: Path
    total: int
    reused: int
    executed: int
    succeeded: int
    errored: int
    cancelled: bool
    halted: bool = False
    halt_reason: Optional[str] = None

    @property
    def resolved(self) -> int:
        """Keys with a record written (reused + executed)."""
        return self.reused + self.executed


# --- raw record builders (verbatim schema, product-adapted plumbing) -------
def build_success_record(
    *,
    experiment_id: str,
    dataset_name: str,
    model_name: str,
    question: dict[str, Any],
    prompt: dict[str, Any],
    raw_response: str,
    latency_seconds: float,
    temperature: float,
    num_predict: int,
) -> dict[str, Any]:
    """Immutable raw-response record for a successful request."""
    prompt_text = str(prompt["prompt_text"])
    return {
        "experiment_id": experiment_id,
        "protocol_version": config.PROTOCOL_VERSION,
        "dataset": dataset_name,
        "question_id": str(question["question_id"]),
        "model": model_name,
        "model_label": resolve_model_label(model_name),
        "prompt_id": str(prompt["prompt_id"]),
        "template_version": prompt.get("template_version"),
        "template_sha256": prompt.get("template_sha256"),
        "prompt_sha256": prompt_hash(prompt_text),
        "prompt_text": prompt_text,
        "expected_answer": question["correct_answer"],
        "raw_response": raw_response,
        "latency_seconds": round(latency_seconds, 4),
        "temperature": temperature,
        "num_predict": num_predict,
        "timestamp_utc": utc_timestamp(),
        "status": "success",
    }


def build_error_record(
    *,
    experiment_id: str,
    dataset_name: str,
    model_name: str,
    question: dict[str, Any],
    prompt: dict[str, Any],
    error: Exception,
    temperature: float,
    num_predict: int,
) -> dict[str, Any]:
    """Auditable record for an inference failure (kept in the raw file)."""
    prompt_text = str(prompt["prompt_text"])
    return {
        "experiment_id": experiment_id,
        "protocol_version": config.PROTOCOL_VERSION,
        "dataset": dataset_name,
        "question_id": str(question["question_id"]),
        "model": model_name,
        "model_label": resolve_model_label(model_name),
        "prompt_id": str(prompt["prompt_id"]),
        "template_version": prompt.get("template_version"),
        "template_sha256": prompt.get("template_sha256"),
        "prompt_sha256": prompt_hash(prompt_text),
        "prompt_text": prompt_text,
        "expected_answer": question["correct_answer"],
        "raw_response": None,
        "latency_seconds": None,
        "temperature": temperature,
        "num_predict": num_predict,
        "timestamp_utc": utc_timestamp(),
        "status": "error",
        "error_type": type(error).__name__,
        "error_message": str(error),
    }


# --- durable, atomic JSONL write -------------------------------------------
def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write all records to ``path`` via a temp file + atomic replace.

    This is what guarantees at most one record per key on disk - the file is
    rewritten wholesale from the in-memory result set, never appended to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _load_existing_success(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Load reusable ``success`` records keyed by ``(question_id, prompt_id)``."""
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return existing
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") != "success":
                continue
            question_id = record.get("question_id")
            prompt_id = record.get("prompt_id")
            if question_id is not None and prompt_id is not None:
                existing[(str(question_id), str(prompt_id))] = record
    return existing


def _ordered_records(
    expected: list[tuple[str, str, dict, dict]],
    results: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Results in protocol (expected-key) order, skipping unresolved keys."""
    ordered: list[dict[str, Any]] = []
    for question_id, prompt_id, _question, _prompt in expected:
        record = results.get((question_id, prompt_id))
        if record is not None:
            ordered.append(record)
    return ordered


def run_dataset_model(
    *,
    dataset_name: str,
    model_name: str,
    prompts_doc: dict[str, Any],
    raw_path: Path,
    base_url: Optional[str] = None,
    temperature: float = config.TEMPERATURE,
    num_predict: int = config.NUM_PREDICT,
    timeout: float = config.REQUEST_TIMEOUT,
    experiment_id: str = config.EXPERIMENT_ID,
    max_questions: Optional[int] = None,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[InferenceProgress], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    generate_fn: Optional[Callable[..., tuple[str, float]]] = None,
    checkpoint_every: int = 50,
    fatal_callback: Optional[Callable[[str], None]] = None,
) -> InferenceResult:
    """Run all prompt conditions for one dataset × model into ``raw_path``.

    ``prompts_doc`` is a loaded ``*_prompts.json`` artifact (see
    :func:`prism_core.prompts.load_prompts`). ``generate_fn`` defaults to
    :func:`prism_core.ollama.generate` and is injectable for headless tests.
    """
    generate = generate_fn if generate_fn is not None else ollama.generate

    questions = prompts_doc.get("questions", [])
    if max_questions is not None:
        questions = questions[:max_questions]

    # Flatten to the ordered list of expected (question_id, prompt_id) keys.
    expected: list[tuple[str, str, dict, dict]] = []
    for question in questions:
        question_id = str(question["question_id"])
        for prompt in question["prompts"]:
            expected.append((question_id, str(prompt["prompt_id"]), question, prompt))
    total = len(expected)

    existing = {} if overwrite else _load_existing_success(raw_path)

    # Prefill with reusable successes for keys that are part of this selection.
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for question_id, prompt_id, _question, _prompt in expected:
        reuse = existing.get((question_id, prompt_id))
        if reuse is not None:
            results[(question_id, prompt_id)] = reuse

    reused = len(results)
    done = reused
    executed = succeeded = errored = 0
    cancelled = False
    halted = False
    halt_reason: Optional[str] = None
    consecutive_errors = 0
    latency_window: list[float] = []
    run_start = time.monotonic()

    def _percent() -> float:
        return (100.0 * done / total) if total else 100.0

    # Announce reused work once (rather than one event per fast-forwarded key).
    if progress_callback is not None and reused:
        progress_callback(
            InferenceProgress(
                dataset=dataset_name,
                model=model_name,
                request_number=done,
                total=total,
                question_id="",
                prompt_id="",
                status="reused",
                percent=_percent(),
                elapsed_seconds=0.0,
            )
        )

    for question_id, prompt_id, question, prompt in expected:
        key = (question_id, prompt_id)
        if key in results:  # reused success - nothing to do
            continue
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break

        prompt_text = str(prompt["prompt_text"])
        error_message: Optional[str] = None
        last_latency: Optional[float] = None

        try:
            raw_response, latency = generate(
                model_name=model_name,
                prompt_text=prompt_text,
                base_url=base_url,
                temperature=temperature,
                num_predict=num_predict,
                timeout=timeout,
            )
            results[key] = build_success_record(
                experiment_id=experiment_id,
                dataset_name=dataset_name,
                model_name=model_name,
                question=question,
                prompt=prompt,
                raw_response=raw_response,
                latency_seconds=latency,
                temperature=temperature,
                num_predict=num_predict,
            )
            succeeded += 1
            last_latency = latency
            latency_window.append(latency)
            latency_window = latency_window[-20:]  # rolling window over last 20
            status = "success"
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 - any failure becomes an error record, never a crash
            error_message = str(exc)
            results[key] = build_error_record(
                experiment_id=experiment_id,
                dataset_name=dataset_name,
                model_name=model_name,
                question=question,
                prompt=prompt,
                error=exc,
                temperature=temperature,
                num_predict=num_predict,
            )
            errored += 1
            status = "error"
            consecutive_errors += 1

            fatal_reason = classify_error(exc)
            if fatal_reason is None and consecutive_errors >= _FATAL_CONSECUTIVE_ERROR_THRESHOLD:
                fatal_reason = (
                    f"{consecutive_errors} inference requests in a row have failed - "
                    "the model or Ollama server may be unresponsive."
                )
            if fatal_reason is not None:
                halted = True
                halt_reason = fatal_reason

        executed += 1
        done += 1

        avg_latency = (sum(latency_window) / len(latency_window)) if latency_window else None
        eta = (avg_latency * (total - done)) if avg_latency is not None else None

        if progress_callback is not None:
            progress_callback(
                InferenceProgress(
                    dataset=dataset_name,
                    model=model_name,
                    request_number=done,
                    total=total,
                    question_id=question_id,
                    prompt_id=prompt_id,
                    status=status,
                    percent=_percent(),
                    elapsed_seconds=time.monotonic() - run_start,
                    latency_seconds=last_latency,
                    avg_latency_seconds=avg_latency,
                    eta_seconds=eta,
                    error_message=error_message,
                )
            )

        # Periodic checkpoint so a crash mid-run leaves a resumable file.
        if checkpoint_every and executed % checkpoint_every == 0:
            _atomic_write_jsonl(raw_path, _ordered_records(expected, results))

        if halted:
            # Save everything gathered so far immediately, then stop the loop.
            # The app is never killed by this - it's a controlled halt so the
            # UI can show what happened and let the person retry, continue
            # from here, or stop for good.
            _atomic_write_jsonl(raw_path, _ordered_records(expected, results))
            if fatal_callback is not None:
                try:
                    fatal_callback(halt_reason or "Inference halted after a fatal error.")
                except Exception:  # noqa: BLE001 - never let a UI callback crash the engine
                    pass
            break

    # Final durable write (also covers the cancelled / all-reused / halted cases).
    _atomic_write_jsonl(raw_path, _ordered_records(expected, results))

    return InferenceResult(
        dataset=dataset_name,
        model=model_name,
        raw_path=raw_path,
        total=total,
        reused=reused,
        executed=executed,
        succeeded=succeeded,
        errored=errored,
        cancelled=cancelled,
        halted=halted,
        halt_reason=halt_reason,
    )