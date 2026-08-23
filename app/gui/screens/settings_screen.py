from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
    QScrollArea,
)

from app.gui.widgets import card, h2, body, SectionHeader, centered_column

# TODO: replace with the real PRISM website once it's live.
PRISM_WEBSITE_URL = "https://prism-theta-mocha.vercel.app/"
PRISM_DOCS_URL = "https://prism-theta-mocha.vercel.app/docs"
PRISM_GITHUB_URL = "https://github.com/Nabin-16/Reliability-test-result-model-versions"


class SettingsScreen(QWidget):
    theme_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        root = centered_column(content, max_width=760, margins=(28, 36, 28, 36), spacing=22)

        root.addWidget(SectionHeader("Settings"))

        appearance = card("Card")
        al = QVBoxLayout(appearance)
        al.setContentsMargins(24, 22, 24, 22)
        al.setSpacing(12)
        al.addWidget(h2("Appearance"))

        row = QHBoxLayout()
        row.addWidget(body("Choose the theme PRISM runs in."))
        row.addStretch(1)

        self.dark_btn = QPushButton("Dark Mode")
        self.light_btn = QPushButton("Light Mode")
        for b in (self.dark_btn, self.light_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setCheckable(True)
        self.dark_btn.clicked.connect(lambda: self._pick("dusk"))
        self.light_btn.clicked.connect(lambda: self._pick("paper"))
        row.addWidget(self.dark_btn)
        row.addWidget(self.light_btn)
        al.addLayout(row)
        root.addWidget(appearance)

        about = card("Card")
        bl = QVBoxLayout(about)
        bl.setContentsMargins(24, 22, 24, 22)
        bl.setSpacing(10)
        bl.addWidget(h2("About PRISM"))

        bl.addWidget(body(
            "<b>PRISM (Prompt Reliability Through Intelligent Semantic "
            "Multiplexing)</b> is a benchmark and desktop application for "
            "evaluating the behavioral consistency of small, locally "
            "runnable language models. It measures how a model's output "
            "changes when a single underlying question is presented "
            "through multiple, semantically equivalent prompt "
            "formulations."
        ))
        bl.addWidget(body(
            "The objective is to make prompt-dependent variation in model "
            "behavior an observable, quantifiable property rather than an "
            "implicit assumption of model evaluation."
        ))
        bl.addWidget(body(
            "For each question, PRISM issues the same underlying query "
            "through several distinct prompt formulations, evaluates each "
            "resulting response independently, and reports the degree to "
            "which the model's behavior remained stable across those "
            "formulations."
        ))
        bl.addWidget(body(
            "Benchmark execution takes place entirely on the local device. "
            "The accompanying website provides documentation and access "
            "to previously published benchmark results."
        ))

        links_row = QHBoxLayout()
        self.website_btn = QPushButton("Visit Website")
        self.docs_btn = QPushButton("Documentation")
        for b in (self.website_btn, self.docs_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.website_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(PRISM_WEBSITE_URL))
        )
        self.docs_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(PRISM_DOCS_URL))
        )
        links_row.addWidget(self.website_btn)
        links_row.addWidget(self.docs_btn)
        links_row.addStretch(1)
        bl.addLayout(links_row)

        what_card = card("CardRaised")
        wl = QVBoxLayout(what_card)
        wl.setContentsMargins(16, 14, 16, 14)
        wl.setSpacing(6)
        wl.addWidget(h2("Methodology"))
        wl.addWidget(body(
            "1. Each question is presented through <b>five distinct "
            "prompt formulations</b>."
        ))
        wl.addWidget(body(
            "2. Each resulting response is evaluated independently for "
            "<b>correctness, instruction compliance, and answer "
            "recoverability</b>."
        ))
        wl.addWidget(body(
            "3. Results indicate where a model's behavior <b>remained "
            "stable</b> and where it <b>diverged</b> across those "
            "formulations."
        ))
        bl.addWidget(what_card)
        root.addWidget(about)

        # ------------------------------------------------------------
        # Open Source / GitHub section
        # ------------------------------------------------------------
        source = card("Card")
        sl = QVBoxLayout(source)
        sl.setContentsMargins(24, 22, 24, 22)
        sl.setSpacing(10)
        sl.addWidget(h2("Open Source"))
        sl.addWidget(body(
            "Check out the available models on github."
        ))

        source_row = QHBoxLayout()
        self.github_btn = QPushButton("View on GitHub")
        self.github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(PRISM_GITHUB_URL))
        )
        source_row.addWidget(self.github_btn)
        source_row.addStretch(1)
        sl.addLayout(source_row)
    

        root.addWidget(source)

        root.addStretch(1)

    def set_active(self, theme: str) -> None:
        self.dark_btn.setChecked(theme == "dusk")
        self.light_btn.setChecked(theme == "paper")

    def _pick(self, theme: str) -> None:
        self.set_active(theme)
        self.theme_changed.emit(theme)
