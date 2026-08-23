"""PRISM response parser (ParserService).

Ported verbatim from the research ``src/response_parser.py``. Converts a
free-form model response into a standardized answer plus a parse status and an
instruction-compliance flag, doing *safe* extraction - it deliberately does not
grab the first A/B/C/D it sees in reasoning text, and it honors explicit
final-answer declarations, revisions, refusals, and ambiguity.

The regexes and the ``parse_response`` decision cascade are unchanged from the
validated pipeline; characterization tests lock this against the on-disk
research ``parsed/`` output. The only differences from the research module are
mechanical: no ``sys.path``/``import config`` shim and no ``argparse`` CLI (the
product orchestrates parsing via services). ``parse_jsonl_file`` already takes
explicit paths and is preserved as-is.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LETTERS = frozenset({"A", "B", "C", "D"})

# one letter
_CLEAN_LETTER_RE = re.compile(
    r"^\s*"
    r"[\(\[]?\s*"
    r"([ABCD])"
    r"\s*[\)\]]?"
    r"[.:,;]?"
    r"\s*$",
    re.IGNORECASE,
)

# ula
_LEADING_LETTER_RE = re.compile(
    r"^\s*"
    r"[\(\[]?\s*"
    r"([ABCD])"
    r"\s*[\)\]]?"
    r"(?:[.:])?"
    r"\s+"
    r"\S",
    re.IGNORECASE,
)

# black bird
_FINAL_PATTERNS = (
    re.compile(
        r"\b(?:therefore|thus|hence|so)\s*,?\s*"
        r"(?:(?:the)\s+)?"
        r"(?:(?:final)\s+)?"
        r"(?:answer|option|choice)?\s*"
        r"(?:is|would\s+be|should\s+be)?\s*"
        r"[:\-]?\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfinal\s+answer\s*"
        r"(?:is|would\s+be|should\s+be)?\s*"
        r"[:\-]?\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
)

# Explicit
_EXPLICIT_PATTERNS = (
    re.compile(
        r"\b(?:the\s+)?"
        r"(?:correct|selected|chosen)\s+"
        r"(?:answer|option|choice)\s+"
        r"(?:is|would\s+be|should\s+be)\s*"
        r"[:\-]?\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my\s+)?"
        r"(?:answer|choice|selection)\s+"
        r"(?:is|would\s+be|should\s+be)\s*"
        r"[:\-]?\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+"
        r"(?:would\s+)?"
        r"(?:choose|select|pick|go\s+with)\s+"
        r"(?:option\s+)?"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\banswer\s*[:=\-]\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\banswer\s+"
        r"(?:is|would\s+be|should\s+be)\s*"
        r"[:\-]?\s*"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:the\s+)?"
        r"(?:correct\s+)?answer\s+"
        r"[\(\[]?\s*([ABCD])\b",
        re.IGNORECASE,
    ),
)

# FSS
_REVISION_SIGNALS = (
    re.compile(
        r"\bhowever\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bon\s+reflection\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bafter\s+reconsider(?:ing|ation)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breconsider(?:ing|ed)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bactually\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi\s+(?:would|will)\s+revise\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+(?:answer|choice)\s+has\s+changed\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi\s+was\s+wrong\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\binstead\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcorrection\b",
        re.IGNORECASE,
    ),
)

# FUB
_NEGATED_PATTERNS = (
    re.compile(
        r"\b(?:option|answer)\s+([ABCD])\s+"
        r"(?:is|was)\s+"
        r"(?:incorrect|wrong|false|not\s+correct|not\s+right)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\banswer\b[^.\n]{0,40}?\bnot\s+([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:would\s+not|wouldn'?t|will\s+not|won'?t)\s+"
        r"(?:choose|select|pick)\s+([ABCD])\b",
        re.IGNORECASE,
    ),
)

# goomi's trend
_REFUSAL_PATTERNS = (
    re.compile(r"\bi\s+don't\s+know\b", re.IGNORECASE),
    re.compile(
        r"\bi\s+(?:cannot|can't)\s+(?:determine|answer)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:insufficient|not\s+enough)\s+information\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bunable\s+to\s+(?:determine|answer)\b",
        re.IGNORECASE,
    ),
)
# ...
_AMBIGUITY_PATTERNS = (
    re.compile(
        r"\b([ABCD])\s+or\s+([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b([ABCD])\s+and\s+([ABCD])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:answers?|options?)\s+(?:are|could\s+be)\s*"
        r"[\:\-]?\s*"
        r"(?:[ABCD](?:\s*[,/&]\s*[ABCD])+)",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class Candidate:
    letter: str
    position: int
    kind: str


def _result(
    *,
    parsed_answer: str,
    parse_status: str,
    instruction_compliant: bool,
) -> dict[str, Any]:
    return {
        "parsed_answer": parsed_answer,
        "parse_status": parse_status,
        "instruction_compliant": instruction_compliant,
    }


def normalize_response(text: str | None) -> str:
    """Normalize line endings and outer whitespace only."""
    if text is None:
        return ""

    return (
        str(text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def _unique_letters(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        letter = value.upper()
        if letter in LETTERS and letter not in seen:
            seen.add(letter)
            result.append(letter)

    return result


def _negated_letters(text: str) -> set[str]:
    negated: set[str] = set()

    for pattern in _NEGATED_PATTERNS:
        for match in pattern.finditer(text):
            negated.add(match.group(1).upper())

    return negated


def _collect_candidates(
    patterns: Iterable[re.Pattern[str]],
    text: str,
    *,
    kind: str,
    negated: set[str],
) -> list[Candidate]:
    candidates: list[Candidate] = []

    for pattern in patterns:
        for match in pattern.finditer(text):
            letter = match.group(1).upper()
            if letter not in negated:
                candidates.append(
                    Candidate(
                        letter=letter,
                        position=match.start(),
                        kind=kind,
                    )
                )

    candidates.sort(key=lambda candidate: candidate.position)
    return candidates


def _revision_after(
    text: str,
    position: int,
) -> bool:
    """
    Whether a clear revision/reconsideration signal appears after a candidate.

    This is intentionally conservative: ordinary reasoning after a final
    answer does not invalidate the answer unless the text explicitly signals
    reconsideration.
    """
    tail = text[position:]

    return any(
        pattern.search(tail)
        for pattern in _REVISION_SIGNALS
    )


def _candidate_after_revision(
    text: str,
    position: int,
    negated: set[str],
) -> Candidate | None:
    """
    Recover an explicit answer stated AFTER the first clear revision signal.

    Crucially, the original declaration itself must not be re-matched as the
    "later" answer.
    """
    tail = text[position:]

    revision_positions: list[int] = []
    for pattern in _REVISION_SIGNALS:
        for match in pattern.finditer(tail):
            revision_positions.append(match.start())

    if not revision_positions:
        return None

    revision_start = min(revision_positions)
    revised_tail = tail[revision_start:]

    final_candidates = _collect_candidates(
        _FINAL_PATTERNS,
        revised_tail,
        kind="explicit_final_answer",
        negated=negated,
    )
    explicit_candidates = _collect_candidates(
        _EXPLICIT_PATTERNS,
        revised_tail,
        kind="explicit_answer",
        negated=negated,
    )

    combined = final_candidates + explicit_candidates
    if not combined:
        return None

    combined.sort(key=lambda candidate: candidate.position)
    latest = combined[-1]

    return Candidate(
        letter=latest.letter,
        position=position + revision_start + latest.position,
        kind=latest.kind,
    )


def _has_clear_answer_revision(
    text: str,
    candidate: Candidate,
    *,
    negated: set[str],
) -> tuple[bool, Candidate | None]:
    """
    Determine whether a candidate is subsequently revised.

    Returns:
        (revision_detected, later_candidate)
    """
    revision_tail = text[candidate.position + 1:]

    if not any(
        pattern.search(revision_tail)
        for pattern in _REVISION_SIGNALS
    ):
        return False, None

    later = _candidate_after_revision(
        text,
        candidate.position + 1,
        negated,
    )

    return True, later


def _final_candidate(
    candidates: list[Candidate],
    text: str,
    negated: set[str],
) -> Candidate | None:
    """
    Resolve final-answer candidates.

    Multiple same-kind declarations are handled by latest explicit commitment.
    A later explicit reconsideration can replace the earlier candidate.
    """
    if not candidates:
        return None

    latest = candidates[-1]

    revised, later_candidate = _has_clear_answer_revision(
        text,
        latest,
        negated=negated,
    )

    if revised:
        if later_candidate is not None:
            return later_candidate

        # The model explicitly reopened the decision but did not provide a
        # new option. Do not guess.
        return None

    return latest


def _leading_candidate(
    text: str,
    negated: set[str],
) -> Candidate | None:
    match = _LEADING_LETTER_RE.match(text)

    if not match:
        return None

    letter = match.group(1).upper()

    if letter in negated:
        return None

    return Candidate(
        letter=letter,
        position=match.start(),
        kind="leading_letter",
    )


def _standalone_final_candidate(
    text: str,
    negated: set[str],
) -> Candidate | None:
    """
    Recover a final standalone letter from a longer response.

    We reject a pure option-list such as:
        A
        B
        C
        D
    """
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    if not lines:
        return None

    last_line = lines[-1]
    match = _CLEAN_LETTER_RE.fullmatch(last_line)

    if not match:
        return None

    letter = match.group(1).upper()
    if letter in negated:
        return None

    meaningful_preceding = any(
        not _CLEAN_LETTER_RE.fullmatch(line)
        for line in lines[:-1]
    )

    if not meaningful_preceding:
        return None

    # Compute the line's approximate position for audit-independent ordering.
    position = text.rfind(last_line)

    return Candidate(
        letter=letter,
        position=max(position, 0),
        kind="standalone_final_letter",
    )


def _ambiguity_detected(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in _AMBIGUITY_PATTERNS
    )


def parse_response(
    raw_response: str | None,
) -> dict[str, Any]:
    """
    Parse one raw response.

    Priority:
        1. clean answer-only
        2. explicit final/conclusion declaration
        3. generic explicit answer/selection declaration
        4. leading-letter answer, if not superseded by later explicit answer
        5. final standalone letter
        6. refusal
        7. ambiguity
        8. UNKNOWN
    """
    text = normalize_response(raw_response)

    if not text:
        return _result(
            parsed_answer="UNKNOWN",
            parse_status="empty",
            instruction_compliant=False,
        )

    clean_match = _CLEAN_LETTER_RE.fullmatch(text)

    if clean_match:
        return _result(
            parsed_answer=clean_match.group(1).upper(),
            parse_status="clean_letter",
            instruction_compliant=True,
        )

    negated = _negated_letters(text)

    final_candidates = _collect_candidates(
        _FINAL_PATTERNS,
        text,
        kind="explicit_final_answer",
        negated=negated,
    )

    explicit_candidates = _collect_candidates(
        _EXPLICIT_PATTERNS,
        text,
        kind="explicit_answer",
        negated=negated,
    )
    leading = _leading_candidate(text, negated)
    if final_candidates:
        latest_final = _final_candidate(
            final_candidates,
            text,
            negated,
        )

        if latest_final is not None:
            return _result(
                parsed_answer=latest_final.letter,
                parse_status=latest_final.kind,
                instruction_compliant=False,
            )

        return _result(
            parsed_answer="UNKNOWN",
            parse_status="revised_without_final_answer",
            instruction_compliant=False,
        )
    if explicit_candidates:
        latest_explicit = _final_candidate(
            explicit_candidates,
            text,
            negated,
        )

        if latest_explicit is not None:
            return _result(
                parsed_answer=latest_explicit.letter,
                parse_status="explicit_answer",
                instruction_compliant=False,
            )

        return _result(
            parsed_answer="UNKNOWN",
            parse_status="revised_without_final_answer",
            instruction_compliant=False,
        )
    if _ambiguity_detected(text):
        return _result(
            parsed_answer="UNKNOWN",
            parse_status="ambiguous_response",
            instruction_compliant=False,
        )
    nonempty_lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    if (
        len(nonempty_lines) > 1
        and all(
            _CLEAN_LETTER_RE.fullmatch(line)
            for line in nonempty_lines
        )
    ):
        return _result(
            parsed_answer="UNKNOWN",
            parse_status="unparseable",
            instruction_compliant=False,
        )
    if leading is not None:
        return _result(
            parsed_answer=leading.letter,
            parse_status="leading_letter",
            instruction_compliant=False,
        )
    standalone = _standalone_final_candidate(
        text,
        negated,
    )

    if standalone is not None:
        return _result(
            parsed_answer=standalone.letter,
            parse_status="standalone_final_letter",
            instruction_compliant=False,
        )

    if any(
        pattern.search(text)
        for pattern in _REFUSAL_PATTERNS
    ):
        return _result(
            parsed_answer="UNKNOWN",
            parse_status="refusal_or_uncertainty",
            instruction_compliant=False,
        )

    if _ambiguity_detected(text):
        return _result(
            parsed_answer="UNKNOWN",
            parse_status="ambiguous_response",
            instruction_compliant=False,
        )

    return _result(
        parsed_answer="UNKNOWN",
        parse_status="unparseable",
        instruction_compliant=False,
    )


PARSED_PASSTHROUGH_FIELDS = (
    "experiment_id",
    "protocol_version",
    "dataset",
    "question_id",
    "model",
    "prompt_id",
)


def parse_record(record: dict[str, Any]) -> dict[str, Any]:
    """
    Create a compact parsed record.

    The original raw response remains in the run's raw_responses/ directory.
    """
    parsed = parse_response(
        record.get("raw_response")
    )

    result = {
        field: record[field]
        for field in PARSED_PASSTHROUGH_FIELDS
        if field in record
    }

    result.update(parsed)
    return result


def parse_jsonl_file(
    input_path: Path,
    output_path: Path,
) -> tuple[int, int]:
    """
    Parse a JSONL raw-response file into a separate parsed artifact.
    """
    input_path = input_path.resolve()
    output_path = output_path.resolve()

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path}"
        )

    if input_path == output_path:
        raise ValueError(
            "Parsed output cannot overwrite the raw-response file."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    processed = 0
    malformed = 0

    with (
        input_path.open(
            "r",
            encoding="utf-8",
        ) as source,
        output_path.open(
            "w",
            encoding="utf-8",
        ) as target,
    ):
        for line_number, line in enumerate(
            source,
            start=1,
        ):
            line = line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                print(
                    f"Warning: skipping malformed JSONL line "
                    f"{line_number} in {input_path}"
                )
                continue

            target.write(
                json.dumps(
                    parse_record(record),
                    ensure_ascii=False,
                )
                + "\n"
            )

            processed += 1

    return processed, malformed