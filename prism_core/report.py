"""PRISM summary reporting (ReportService).

Ported from the research ``src/summary_report.py``. Reads the question-level
metrics produced by :mod:`prism_core.scoring` and rolls them up into the two
report CSVs:

* ``model_dataset_summary.csv`` - one row per model × dataset,
* ``model_prompt_summary.csv`` - one row per model × dataset × prompt condition.

The aggregation math, the metric distinctions (recovery vs. compliance vs.
accuracy), the rounding, and the column order are unchanged, so the CSVs match
the validated pipeline byte-for-byte. The only adaptation:
:func:`find_question_metrics_files` takes an explicit ``summary_dir`` instead of
the global ``config.RESULTS_SUMMARY_DIR``. The ``argparse`` CLI is dropped.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load non-empty JSON objects from a JSONL file."""
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


def find_question_metrics_files(summary_dir: Path) -> list[Path]:
    return sorted(
        summary_dir.glob(
            "*_question_metrics.jsonl"
        )
    )


def _safe_mean(
    values: list[float],
) -> float:
    return (
        sum(values) / len(values)
        if values
        else 0.0
    )


def build_model_dataset_summary(
    all_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)

    for record in all_records:
        groups[
            (
                str(record["model"]),
                str(record["dataset"]),
            )
        ].append(record)

    rows: list[dict[str, Any]] = []

    for (model, dataset), records in sorted(groups.items()):
        n_questions = len(records)

        expected_prompt_count = sum(
            int(record.get("expected_prompt_count", 0))
            for record in records
        )
        observed_prompt_count = sum(
            int(record.get("observed_prompt_count", 0))
            for record in records
        )
        missing_prompt_count = sum(
            int(record.get("missing_prompt_count", 0))
            for record in records
        )
        recovered_count = sum(
            int(record.get("valid_response_count", 0))
            for record in records
        )
        unknown_count = sum(
            int(record.get("unknown_count", 0))
            for record in records
        )
        compliant_count = sum(
            sum(
                1
                for value in record.get(
                    "prompt_compliance", {}
                ).values()
                if bool(value)
            )
            for record in records
        )
        correct_count = sum(
            int(record.get("correct_response_count", 0))
            for record in records
        )

        answer_recovery_rate = (
            recovered_count / observed_prompt_count
            if observed_prompt_count
            else 0.0
        )
        unknown_rate = (
            unknown_count / observed_prompt_count
            if observed_prompt_count
            else 0.0
        )
        instruction_compliance_rate = (
            compliant_count / observed_prompt_count
            if observed_prompt_count
            else 0.0
        )
        prompt_response_accuracy = (
            correct_count / observed_prompt_count
            if observed_prompt_count
            else 0.0
        )
        conditional_accuracy = (
            correct_count / recovered_count
            if recovered_count
            else 0.0
        )

        mean_agreement = _safe_mean(
            [
                float(record.get("agreement", 0.0))
                for record in records
            ]
        )
        mean_sensitivity = _safe_mean(
            [
                float(
                    record.get(
                        "prompt_sensitivity",
                        0.0,
                    )
                )
                for record in records
            ]
        )

        complete_questions = [
            record
            for record in records
            if int(
                record.get(
                    "missing_prompt_count",
                    0,
                )
            )
            == 0
        ]

        unanimous_rate = (
            sum(
                bool(record.get("answer_unanimous", False))
                for record in complete_questions
            )
            / len(complete_questions)
            if complete_questions
            else 0.0
        )

        prompt_invariant_incorrect_rate = (
            sum(
                1
                for record in complete_questions
                if bool(
                    record.get(
                        "answer_unanimous",
                        False,
                    )
                )
                and not bool(
                    record.get(
                        "majority_correct",
                        False,
                    )
                )
            )
            / len(complete_questions)
            if complete_questions
            else 0.0
        )
        majority_accuracy = (
            sum(
                bool(
                    record.get(
                        "majority_correct",
                        False,
                    )
                )
                for record in records
            )
            / n_questions
            if n_questions
            else 0.0
        )

        rows.append(
            {
                "model": model,
                "dataset": dataset,
                "n_questions": n_questions,

                # Coverage.
                "expected_prompt_responses": expected_prompt_count,
                "observed_prompt_responses": observed_prompt_count,
                "missing_prompt_responses": missing_prompt_count,

                # Core accuracy/recovery/compliance metrics.
                "prompt_response_accuracy": round(
                    prompt_response_accuracy,
                    4,
                ),
                "conditional_accuracy": round(
                    conditional_accuracy,
                    4,
                ),
                "answer_recovery_rate": round(
                    answer_recovery_rate,
                    4,
                ),
                "unknown_rate": round(
                    unknown_rate,
                    4,
                ),
                "instruction_compliance_rate": round(
                    instruction_compliance_rate,
                    4,
                ),

                # Question-level accuracy.
                "question_majority_accuracy": round(
                    majority_accuracy,
                    4,
                ),

                # Consistency / sensitivity.
                "mean_agreement": round(
                    mean_agreement,
                    4,
                ),
                "mean_prompt_sensitivity": round(
                    mean_sensitivity,
                    4,
                ),

                # Complete-question consistency metrics.
                "complete_questions": len(complete_questions),
                "answer_unanimous_rate": round(
                    unanimous_rate,
                    4,
                ),
                "prompt_invariant_incorrect_rate": round(
                    prompt_invariant_incorrect_rate,
                    4,
                ),
            }
        )

    return rows


def build_model_prompt_summary(
    all_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Create one row per model/dataset/prompt condition.

    Accuracy is calculated over observed prompt responses, not only compliant
    responses. Instruction compliance is reported independently.
    """
    groups: dict[
        tuple[str, str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)

    for record in all_records:
        model = str(record["model"])
        dataset = str(record["dataset"])
        for prompt_id, correct in record.get(
            "prompt_correctness",
            {},
        ).items():
            key = (
                model,
                dataset,
                str(prompt_id),
            )
            groups[key].append(
                {
                    "correct": bool(correct),
                    "compliant": bool(
                        record.get(
                            "prompt_compliance",
                            {},
                        ).get(
                            prompt_id,
                            False,
                        )
                    ),
                    "recovered": bool(
                        record.get(
                            "prompt_recovery",
                            {},
                        ).get(
                            prompt_id,
                            False,
                        )
                    ),
                }
            )

    rows: list[dict[str, Any]] = []

    for key in sorted(groups):
        model, dataset, prompt_id = key
        observations = groups[key]

        n = len(observations)
        recovered_count = sum(
            obs["recovered"]
            for obs in observations
        )
        correct_count = sum(
            obs["correct"]
            for obs in observations
        )
        compliant_count = sum(
            obs["compliant"]
            for obs in observations
        )

        rows.append(
            {
                "model": model,
                "dataset": dataset,
                "prompt_condition": prompt_id,
                "n_observed": n,
                "n_recovered": recovered_count,
                "n_correct": correct_count,
                "n_instruction_compliant": compliant_count,
                "answer_recovery_rate": round(
                    recovered_count / n if n else 0.0,
                    4,
                ),
                "accuracy": round(
                    correct_count / n if n else 0.0,
                    4,
                ),
                "conditional_accuracy": round(
                    correct_count / recovered_count
                    if recovered_count
                    else 0.0,
                    4,
                ),
                "instruction_compliant_rate": round(
                    compliant_count / n if n else 0.0,
                    4,
                ),
                "unknown_rate": round(
                    (n - recovered_count) / n
                    if n
                    else 0.0,
                    4,
                ),
            }
        )

    return rows


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        print(
            f"  [skip] no rows for {path.name}"
        )
        return

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()),
        )
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"  wrote {len(rows)} rows -> {path}"
    )