"""Home / landing screen - shown when the PRISM brand mark in the toolbar
is clicked. A calm, centered hero (mark, title, "Get Started" CTA) rather
than a working screen; its only job is to send the user on to the
Benchmark tab.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources"
LOGO_ICON_PATH = RESOURCES_DIR / "prism_logo.png"


class HomeScreen(QWidget):
    """Centered hero landing page: mark, title, Get Started."""

    get_started_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("HomeRoot")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(0)
        outer.addStretch(1)

        self.logo_lbl = QLabel()
        self.logo_lbl.setObjectName("HomeLogo")
        self.logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if LOGO_ICON_PATH.exists():
            pix = QPixmap(str(LOGO_ICON_PATH))
            if not pix.isNull():
                self.logo_lbl.setPixmap(
                    pix.scaledToHeight(72, Qt.TransformationMode.SmoothTransformation)
                )
        outer.addWidget(self.logo_lbl)
        outer.addSpacing(28)

        title = QLabel("Prompt Reliability Benchmark for Small Language Models")
        title.setObjectName("HomeTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        outer.addWidget(title)
        outer.addSpacing(28)

        self.get_started_btn = QPushButton("Get Started  \u2192")
        self.get_started_btn.setObjectName("HomeGetStartedBtn")
        self.get_started_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # NOTE: connect to a real slot, not directly to get_started_clicked.emit.
        # QPushButton.clicked emits a bool ("checked"); binding that straight
        # to a no-argument Signal's .emit blows up on the argument mismatch,
        # which was silently swallowing the click - the button looked fine
        # but never actually fired.
        self.get_started_btn.clicked.connect(self._on_get_started_clicked)

        outer.addWidget(self.get_started_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addStretch(1)

    def _on_get_started_clicked(self, checked: bool = False) -> None:
        self.get_started_clicked.emit()