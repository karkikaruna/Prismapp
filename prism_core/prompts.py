"""PRISM prompt rendering (PromptService).

Ported from the research ``src/prompt_variations.py``. Loads the five frozen
prompt templates (P0–P4) and renders them against frozen question samples. The
template set, the required ``{question}``/``{option_a..d}`` placeholders, the
SHA-256 identity of each template, and the exact rendering (``.format`` then
``strip() + "\\n"``) are preserved so generated prompt text - and its hash - is
identical to the validated pipeline.

Differences from the research module (plumbing only): resource directories come
from :mod:`prism_core.resources` (bundled, read-only) instead of the research
``config.DATA_*`` globals, and the ``argparse`` CLI is dropped. A ``load_prompts``
reader is added for the runtime path, which consumes the bundled, precomputed
``*_prompts.json`` rather than re-rendering.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prism_core import config, resources

PROCESSED_DIR = resources.PROCESSED_DIR
TEMPLATE_DIR = resources.TEMPLATES_DIR
PROMPT_DIR = resources.PROMPTS_DIR

TEMPLATE_VERSION = config.TEMPLATE_VERSION

TEMPLATE_FILES: dict[str, str] = {
    "P0": "p0_minimal.txt",
    "P1": "p1_direct.txt",
    "P2": "p2_structured.txt",
    "P3": "p3_role_based.txt",
    "P4": "p4_careful_analysis.txt",
}

REQUIRED_FIELDS = (
    "question",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
)


@dataclass(frozen=True)
class PromptTemplate:
    """A frozen prompt template and its identifying metadata."""

    prompt_id: str
    filename: str
    text: str
    sha256: str


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON artifact and fail with a useful message."""
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a UTF-8 JSON artifact with stable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _sha256(text: str) -> str:
    """Return the SHA-256 hash of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_templates() -> dict[str, PromptTemplate]:
    """Load and validate all five frozen prompt templates."""
    templates: dict[str, PromptTemplate] = {}

    for prompt_id, filename in TEMPLATE_FILES.items():
        path = TEMPLATE_DIR / filename

        if not path.exists():
            raise FileNotFoundError(
                f"Prompt template {prompt_id} is missing: {path}"
            )

        text = path.read_text(encoding="utf-8")

        if not text.strip():
            raise ValueError(f"Prompt template {prompt_id} is empty: {path}")

        missing_fields = [
            field
            for field in REQUIRED_FIELDS
            if "{" + field + "}" not in text
        ]

        if missing_fields:
            raise ValueError(
                f"{path.name} is missing required placeholders: "
                f"{', '.join(missing_fields)}"
            )

        templates[prompt_id] = PromptTemplate(
            prompt_id=prompt_id,
            filename=filename,
            text=text,
            sha256=_sha256(text),
        )

    return templates


def _question_context(question: dict[str, Any]) -> dict[str, str]:
    """Convert a frozen question record into template placeholders."""
    required_keys = {"question", "options", "correct_answer"}

    missing = required_keys - question.keys()
    if missing:
        raise ValueError(
            f"Question {question.get('question_id', '<unknown>')} is missing: "
            f"{', '.join(sorted(missing))}"
        )

    options = question["options"]

    missing_options = [
        letter for letter in ("A", "B", "C", "D")
        if letter not in options
    ]

    if missing_options:
        raise ValueError(
            f"Question {question.get('question_id', '<unknown>')} is missing "
            f"options: {', '.join(missing_options)}"
        )

    return {
        "question": str(question["question"]).strip(),
        "option_a": str(options["A"]).strip(),
        "option_b": str(options["B"]).strip(),
        "option_c": str(options["C"]).strip(),
        "option_d": str(options["D"]).strip(),
    }


def render_prompt(
    question: dict[str, Any],
    template: PromptTemplate,
) -> str:
    """Render one prompt template for one frozen question."""
    context = _question_context(question)

    try:
        rendered = template.text.format(**context)
    except KeyError as exc:
        raise ValueError(
            f"Template {template.filename} contains an unknown placeholder: "
            f"{exc.args[0]!r}"
        ) from exc

    return rendered.strip() + "\n"


def generate_question_prompts(
    question: dict[str, Any],
    templates: dict[str, PromptTemplate],
) -> list[dict[str, Any]]:
    """Generate P0-P4 for one frozen question."""
    question_id = str(question["question_id"])

    generated: list[dict[str, Any]] = []

    for prompt_id, template in templates.items():
        generated.append(
            {
                "question_id": question_id,
                "prompt_id": prompt_id,
                "template_version": TEMPLATE_VERSION,
                "template_file": template.filename,
                "template_sha256": template.sha256,
                "prompt_text": render_prompt(question, template),
            }
        )

    return generated


def generate_dataset_prompts(
    sample_path: Path,
    templates: dict[str, PromptTemplate],
) -> dict[str, Any]:
    """Generate the complete prompt artifact for one frozen dataset sample."""
    sample = _load_json(sample_path)

    dataset_name = str(sample["dataset"])
    questions = sample["questions"]

    all_prompts: list[dict[str, Any]] = []

    for question in questions:
        question_prompts = generate_question_prompts(
            question,
            templates,
        )

        all_prompts.append(
            {
                "question_id": str(question["question_id"]),
                "correct_answer": question["correct_answer"],
                "prompts": question_prompts,
            }
        )

    return {
        "dataset": dataset_name,
        "template_version": TEMPLATE_VERSION,
        "sampling_seed": sample.get("sampling_seed"),
        "sample_size": len(questions),
        "prompt_conditions": list(TEMPLATE_FILES.keys()),
        "templates": [
            {
                "prompt_id": template.prompt_id,
                "filename": template.filename,
                "sha256": template.sha256,
            }
            for template in templates.values()
        ],
        "questions": all_prompts,
    }


def save_dataset_prompts(
    dataset_name: str,
    payload: dict[str, Any],
) -> Path:
    """Save a generated prompt artifact under the bundled prompts directory.

    This is a development-time operation (regenerating the shipped
    ``*_prompts.json``); at runtime the app reads the precomputed artifact via
    :func:`load_prompts`.
    """
    output_path = PROMPT_DIR / f"{dataset_name}_prompts.json"
    _write_json(output_path, payload)
    return output_path


def load_prompts(dataset_name: str) -> dict[str, Any]:
    """Load the bundled, precomputed prompt artifact for a dataset.

    This is the runtime path: the shipped ``*_prompts.json`` already contains
    the exact rendered prompt text and template hashes, so no re-rendering is
    needed to run a benchmark.
    """
    return _load_json(resources.prompts_path(dataset_name))