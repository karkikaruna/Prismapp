"""PRISM consistency scorer (ScoringService).

Ported from the research ``src/consistency_scorer.py`` - the first stage that
uses benchmark ground truth. Scores each parsed response against the expected
answer and aggregates the five prompt conditions of a question into one
question/model metrics record (agreement, ``prompt_sensitivity = 1 - agreement``,
majority answer with deterministic tie-break, unanimity, majority-correct, and
the recovery/compliance/accuracy rates).

The scoring logic, the join key ``(dataset, question_id, model, prompt_id)``,
and the fail-loud duplicate/missing/inconsistent-ground-truth guards are
unchanged. The only adaptation: :func:`score_parsed_file` takes explicit
run-scoped directories instead of the global ``config.RESULTS_*`` constants, so
each benchmark run scores into its own ``runs/<id>/`` tree. The ``argparse`` CLI
is dropped.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from prism_core import config

VALID_ANSWERS = frozenset({"A", "B", "C", "D"})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load all non-empty JSON objects from a JSONL file."""
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    records: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Malformed JSON on line {line_number} in {path}"
                ) from exc

            if not isinstance(record, dict):
                raise ValueError(
                    f"Expected JSON object on line {line_number} in {path}"
                )

            records.append(record)

    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write records as UTF-8 JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False) + "\n"
            )


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    expected = str(record.get("expected_answer", "")).upper()
    parsed = str(record.get("parsed_answer", "")).upper()

    if expected not in VALID_ANSWERS:
        raise ValueError(
            f"Invalid expected answer {expected!r} for "
            f"{record.get('question_id')!r}"
        )

    answer_recovered = parsed in VALID_ANSWERS
    instruction_compliant = bool(
        record.get("instruction_compliant", False)
    )

    result = dict(record)
    result["answer_recovered"] = answer_recovered

    result["usable"] = answer_recovered
    result["correct"] = bool(
        answer_recovered and parsed == expected
    )

    result["instruction_compliant"] = instruction_compliant

    return result


def join_raw_and_parsed(
    raw_records: list[dict[str, Any]],
    parsed_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_index: dict[
        tuple[str, str, str, str],
        dict[str, Any],
    ] = {}

    for raw in raw_records:
        key = (
            str(raw.get("dataset")),
            str(raw.get("question_id")),
            str(raw.get("model")),
            str(raw.get("prompt_id")),
        )

        if key in raw_index:
            raise ValueError(f"Duplicate raw-response key: {key}")

        raw_index[key] = raw

    scored: list[dict[str, Any]] = []

    for parsed in parsed_records:
        key = (
            str(parsed.get("dataset")),
            str(parsed.get("question_id")),
            str(parsed.get("model")),
            str(parsed.get("prompt_id")),
        )

        raw = raw_index.get(key)
        if raw is None:
            raise ValueError(
                f"No raw response found for parsed record: {key}"
            )

        if "expected_answer" not in raw:
            raise ValueError(
                f"Raw record has no expected_answer for {key}"
            )

        combined = {
            **parsed,
            "expected_answer": str(
                raw["expected_answer"]
            ).upper(),
        }

        scored.append(score_record(combined))

    return scored


def _prompt_order(prompt_id: str) -> tuple[int, str]:
    """Deterministic ordering for prompt conditions."""
    try:
        index = list(config.PROMPT_CONDITIONS).index(prompt_id)
    except ValueError:
        index = 999

    return index, prompt_id


def calculate_question_metrics(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Aggregate prompt-level records into one question/model record.

    Definitions:
        expected_prompt_count:
            Number of prompt conditions in the protocol.

        observed_prompt_count:
            Number of actual records present.

        missing_prompt_count:
            expected_prompt_count - observed_prompt_count.

        answer_recovery_rate:
            recovered answers / observed responses.

        instruction_compliance_rate:
            compliant responses / observed responses.

        prompt_response_accuracy:
            correct recovered answers / observed responses.

        conditional_accuracy:
            correct recovered answers / recovered answers.

        agreement:
            majority recovered answer count / recovered answer count.

        prompt_sensitivity:
            1 - agreement.

    Missing prompt records are NOT silently counted as UNKNOWN.
    """
    groups: dict[
        tuple[str, str, str],
        list[dict[str, Any]],
    ] = {}

    for record in records:
        key = (
            str(record.get("dataset")),
            str(record.get("question_id")),
            str(record.get("model")),
        )
        groups.setdefault(key, []).append(record)

    metrics: list[dict[str, Any]] = []

    expected_prompt_ids = list(config.PROMPT_CONDITIONS)
    expected_prompt_count = len(expected_prompt_ids)

    for (dataset, question_id, model), group in sorted(groups.items()):
        group = sorted(
            group,
            key=lambda item: _prompt_order(
                str(item.get("prompt_id"))
            ),
        )

        prompt_ids = [
            str(record.get("prompt_id"))
            for record in group
        ]

        if len(prompt_ids) != len(set(prompt_ids)):
            raise ValueError(
                f"Duplicate prompt IDs for "
                f"{dataset}/{question_id}/{model}: {prompt_ids}"
            )

        expected_answers = {
            str(record["expected_answer"]).upper()
            for record in group
        }

        if len(expected_answers) != 1:
            raise ValueError(
                f"Inconsistent ground truth for "
                f"{dataset}/{question_id}/{model}: "
                f"{expected_answers}"
            )

        expected_answer = next(iter(expected_answers))

        responses: dict[str, str] = {}
        prompt_correctness: dict[str, bool] = {}
        prompt_compliance: dict[str, bool] = {}
        prompt_recovery: dict[str, bool] = {}

        valid_answers: list[str] = []

        for record in group:
            prompt_id = str(record["prompt_id"])
            parsed = str(record["parsed_answer"]).upper()
            recovered = parsed in VALID_ANSWERS
            compliant = bool(
                record.get("instruction_compliant", False)
            )

            responses[prompt_id] = parsed
            prompt_correctness[prompt_id] = bool(
                record["correct"]
            )
            prompt_compliance[prompt_id] = compliant
            prompt_recovery[prompt_id] = recovered

            if recovered:
                valid_answers.append(parsed)

        observed_prompt_count = len(group)
        missing_prompt_count = max(
            expected_prompt_count - observed_prompt_count,
            0,
        )

        valid_count = len(valid_answers)
        unknown_count = observed_prompt_count - valid_count

        compliant_count = sum(
            prompt_compliance.values()
        )
        correct_count = sum(
            bool(record["correct"])
            for record in group
        )

        if valid_count == 0:
            majority_answer = "UNKNOWN"
            majority_count = 0
            agreement = 0.0
            prompt_sensitivity = 1.0
            answer_unanimous = False
            majority_correct = False
        else:
            counts = Counter(valid_answers)
            majority_count = max(counts.values())

            tied = sorted(
                answer
                for answer, count in counts.items()
                if count == majority_count
            )

            # Deterministic tie-break.
            majority_answer = tied[0]

            agreement = majority_count / valid_count
            prompt_sensitivity = 1.0 - agreement

            answer_unanimous = (
                observed_prompt_count == expected_prompt_count
                and valid_count == expected_prompt_count
                and len(counts) == 1
            )

            majority_correct = (
                majority_answer == expected_answer
            )

        metrics.append(
            {
                "experiment_id": group[0].get("experiment_id"),
                "protocol_version": group[0].get("protocol_version"),
                "dataset": dataset,
                "question_id": question_id,
                "model": model,
                "expected_answer": expected_answer,

                # Prompt-level detail.
                "responses": responses,
                "prompt_correctness": prompt_correctness,
                "prompt_compliance": prompt_compliance,
                "prompt_recovery": prompt_recovery,

                # Coverage.
                "expected_prompt_count": expected_prompt_count,
                "observed_prompt_count": observed_prompt_count,
                "missing_prompt_count": missing_prompt_count,

                # Recovery / compliance.
                "valid_response_count": valid_count,
                "unknown_count": unknown_count,
                "answer_recovery_rate": (
                    valid_count / observed_prompt_count
                    if observed_prompt_count
                    else 0.0
                ),
                "unknown_rate": (
                    unknown_count / observed_prompt_count
                    if observed_prompt_count
                    else 0.0
                ),
                "instruction_compliance_rate": (
                    compliant_count / observed_prompt_count
                    if observed_prompt_count
                    else 0.0
                ),

                # Accuracy.
                "correct_response_count": correct_count,
                "prompt_response_accuracy": (
                    correct_count / observed_prompt_count
                    if observed_prompt_count
                    else 0.0
                ),
                "conditional_accuracy": (
                    correct_count / valid_count
                    if valid_count
                    else 0.0
                ),

                # Cross-prompt answer behavior.
                "majority_answer": majority_answer,
                "majority_count": majority_count,
                "agreement": round(agreement, 6),
                "prompt_sensitivity": round(
                    prompt_sensitivity,
                    6,
                ),
                "answer_unanimous": answer_unanimous,
                "majority_correct": majority_correct,
            }
        )

    return metrics


def score_parsed_file(
    parsed_path: Path,
    *,
    raw_dir: Path,
    scored_dir: Path,
    summary_dir: Path,
) -> tuple[Path, Path]:
    """Score one parsed file and create question-level metrics.

    The parsed file is paired with its raw-response counterpart of the same
    basename in ``raw_dir`` (the pipeline names both ``{dataset}__{safe_model}
    .jsonl``). Scored records are written to ``scored_dir`` under the same
    basename, and question-level metrics to ``summary_dir`` as
    ``{stem}_question_metrics.jsonl``.

    Unlike the research version, the three directories are explicit run-scoped
    arguments rather than global ``config.RESULTS_*`` constants.
    """
    parsed_path = parsed_path.resolve()

    parsed_records = load_jsonl(parsed_path)

    raw_path = (raw_dir / parsed_path.name).resolve()

    raw_records = load_jsonl(raw_path)

    scored_records = join_raw_and_parsed(
        raw_records,
        parsed_records,
    )

    scored_path = (scored_dir / parsed_path.name).resolve()

    write_jsonl(scored_path, scored_records)

    question_metrics = calculate_question_metrics(
        scored_records
    )

    summary_path = (
        summary_dir
        / f"{parsed_path.stem}_question_metrics.jsonl"
    ).resolve()

    write_jsonl(
        summary_path,
        question_metrics,
    )

    return scored_path, summary_path