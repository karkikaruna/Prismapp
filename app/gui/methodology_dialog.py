"""Methodology panel — the app's own "methods section".

A research desktop app should be able to explain itself the way a paper
does: what's being measured, on what data, under what protocol. This dialog
surfaces the fixed methodology constants straight from prism_core.config
(so it can never drift out of sync with what a run actually did) alongside
the metric definitions research users actually need reminding of.

Opened from Help -> Methodology in the menu bar (see main_window.py).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QWidget, QFrame,
)

from prism_core import config

_METRIC_DEFINITIONS = [
    ("Answer Recovery Rate", "Fraction of responses that could be parsed into a usable answer."),
    ("Accuracy", "Recovered answer matches the expected answer."),
    ("Conditional Accuracy", "Accuracy computed only among successfully recovered answers."),
    ("Instruction Compliance Rate", "How often the model followed the required output format."),
    ("Unknown Rate", "Responses that couldn't be safely resolved — refusal, ambiguity, or unparseable."),
    ("Agreement", "How consistently the model answers the same across P0\u2013P4 for one question."),
    ("Prompt Sensitivity", "How much the answer changes purely because of prompt phrasing."),
    ("Question Majority Accuracy", "Whether the majority answer across P0\u2013P4 matches ground truth."),
    ("Answer Unanimous Rate", "Fraction of questions where all 5 prompt conditions agree."),
    ("Prompt-Invariant Incorrect Rate", "Questions where the model is consistently wrong across all prompt conditions."),
]

_PROMPT_CONDITIONS = [
    ("P0", "Minimal", "Bare instruction, no scaffolding."),
    ("P1", "Direct", "Plain, ordinary phrasing of the question."),
    ("P2", "Structured", "Question presented with labelled sections."),
    ("P3", "Role-based", "Framed through an expert persona."),
    ("P4", "Careful analysis", "Explicitly encourages step-by-step reasoning."),
]


def _rule() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    return line


class MethodologyDialog(QDialog):
    """Read-only reference panel — no state, no wiring beyond Close."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Methodology")
        self.setMinimumSize(560, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)

        title = QLabel("PRISM \u2014 Methodology")
        title.setObjectName("H1")
        lay.addWidget(title)

        subtitle = QLabel(
            "Prompt Reliability through Intelligent Semantic Multiplexing. "
            "A reliability and stability benchmark, not a leaderboard \u2014 it "
            "measures whether a model keeps giving the same, correct answer "
            "when the same question is asked in different but semantically "
            "equivalent ways."
        )
        subtitle.setObjectName("Body")
        subtitle.setWordWrap(True)
        lay.addWidget(subtitle)
        lay.addWidget(_rule())

        # ---- protocol block, pulled live from config so it can't drift ----
        lay.addWidget(self._section_label("Protocol"))
        protocol_rows = [
            ("Domain", "Education / science question-answering"),
            ("Datasets", ", ".join(config.DATASETS.keys())),
            ("Sample size", f"{config.SAMPLE_SIZE_PER_DATASET} questions per dataset"),
            ("Dataset version", config.DATASET_VERSION),
            ("Prompt conditions", ", ".join(config.PROMPT_CONDITIONS)),
            ("Temperature", str(config.TEMPERATURE)),
            ("Random seed", str(config.RANDOM_SEED)),
            ("Protocol version", config.PROTOCOL_VERSION),
            ("Template version", config.TEMPLATE_VERSION),
        ]
        for label, value in protocol_rows:
            lay.addWidget(self._kv_row(label, value))
        lay.addWidget(_rule())

        # ---- prompt conditions ----
        lay.addWidget(self._section_label("Prompt conditions (P0\u2013P4)"))
        lay.addWidget(self._note(
            "Every question is asked five different but semantically "
            "equivalent ways. This is the backbone of the consistency "
            "analysis \u2014 it's what lets PRISM measure whether phrasing "
            "changes the answer, independent of whether the model actually "
            "knows the material."
        ))
        for code, name, desc in _PROMPT_CONDITIONS:
            lay.addWidget(self._kv_row(f"{code} \u2014 {name}", desc))
        lay.addWidget(_rule())

        # ---- metric definitions ----
        lay.addWidget(self._section_label("Metric definitions"))
        lay.addWidget(self._note(
            "These are kept deliberately separate rather than collapsed "
            "into one arbitrary \u201creliability score.\u201d A model can look "
            "highly stable while being reliably wrong \u2014 consistency \u2260 "
            "correctness."
        ))
        for name, desc in _METRIC_DEFINITIONS:
            lay.addWidget(self._kv_row(name, desc))

        lay.addStretch(1)

        # ---- footer ----
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 12, 20, 16)
        footer.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        outer.addLayout(footer)

    # ---------------- helpers ----------------

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("H2")
        return lbl

    def _note(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("Faint")
        lbl.setWordWrap(True)
        return lbl

    def _kv_row(self, label: str, value: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(10)

        k = QLabel(label)
        k.setObjectName("Faint")
        k.setFixedWidth(190)
        k.setWordWrap(True)
        h.addWidget(k, 0, Qt.AlignmentFlag.AlignTop)

        v = QLabel(value)
        v.setObjectName("Body")
        v.setWordWrap(True)
        h.addWidget(v, 1)

        return row
