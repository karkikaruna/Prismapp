"""
PRISM theme system.

Two palettes (dusk / paper), one QSS template. Soft, rounded finish: every
card, panel, button, input, and table uses a generous corner radius (see
R_SM / R_MD below) instead of sharp native-chrome corners, real 1px hairline
borders instead of shadows, flat surfaces, and the OS's own UI font. The
goal is a calmer, friendlier "app" feel rather than pointed/rectangular
native chrome.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    # base surfaces, darkest -> lightest elevation
    bg: str
    surface: str
    surface_raised: str
    surface_hover: str
    # text
    text: str
    text_dim: str
    text_faint: str
    # accents
    accent: str
    accent_soft: str
    accent_text: str
    # status
    good: str
    warn: str
    bad: str
    # structure
    hairline: str
    shadow: str


DUSK = Palette(
    name="dusk",
    bg="#0b0b0e",
    surface="#111114",
    surface_raised="#18181c",
    surface_hover="#212126",
    text="#f2f2f3",
    text_dim="#a8a8b0",
    text_faint="#75757e",
    accent="#8f7cf2",
    accent_soft="#211f2b",
    accent_text="#c9befa",
    good="#5fd18e",
    warn="#e8b45c",
    bad="#ea6b6b",
    hairline="#2a2a30",
    shadow="#000000",
)

PAPER = Palette(
    name="paper",
    bg="#f3f1eb",
    surface="#fbfaf6",
    surface_raised="#ffffff",
    surface_hover="#eeece3",
    text="#2a2733",
    text_dim="#6b6879",
    text_faint="#9c99a8",
    accent="#6a52d6",
    accent_soft="#eae5fb",
    accent_text="#5a41c4",
    good="#28925f",
    warn="#b4791a",
    bad="#c94f4f",
    hairline="#d8d2c4",
    shadow="#c9c4b6",
)

# Rounded corner radii, kept as named constants so every surface (cards,
# panels, buttons, inputs, tables, pills) shares the same soft, friendly
# roundedness instead of pointed rectangles.
R_NONE = "0px"
R_SM = "10px"
R_MD = "14px"
R_LG = "18px"


UI_FONT = '"Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Noto Sans", sans-serif'
# A serif face for headings/brand only - the rest of the chrome (nav, body
# copy, controls) stays the native UI font so the app still reads as real OS
# chrome, not a themed webpage. Reserved for titles and section headers,
# giving the app the register of a research report rather than a product
# dashboard. Data itself stays in the existing monospace (see MONO_FONT
# below / the JetBrains-style QSS rules further down) - a report typeface
# for prose, a tabular typeface for numbers, a UI typeface for controls.
SERIF_FONT = '"Source Serif Pro", "Georgia", "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif'


def build_qss(p: Palette) -> str:
    return f"""
    * {{
        font-family: {UI_FONT};
        outline: none;
    }}

    QWidget {{
        background: transparent;
        color: {p.text};
        font-size: 13px;
    }}

    QMainWindow, #AppRoot {{
        background: {p.bg};
    }}

    QDialog {{
        background: {p.surface};
    }}

    /* ---------- Sidebar ---------- */
    #Sidebar {{
        background: {p.surface};
        border-right: 1px solid {p.hairline};
    }}
    #SidebarBrand {{
        color: {p.text};
        font-family: {SERIF_FONT};
        font-size: 17px;
        font-weight: 700;
        letter-spacing: 1.5px;
    }}
    #SidebarTagline {{
        color: {p.text_faint};
        font-size: 10.5px;
        letter-spacing: 0.5px;
    }}
    QPushButton#NavButton {{
        text-align: left;
        padding: 8px 12px;
        border-radius: {R_SM};
        border: 1px solid transparent;
        color: {p.text_dim};
        background: transparent;
        font-size: 13px;
        font-weight: 500;
    }}
    QPushButton#NavButton:hover {{
        background: {p.surface_hover};
        color: {p.text};
    }}
    QPushButton#NavButton:checked {{
        background: {p.accent_soft};
        color: {p.accent_text};
        border: 1px solid {p.hairline};
        font-weight: 600;
    }}

    /* ---------- Cards / surfaces ---------- */
    QFrame#Card {{
        background: {p.surface};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#CardFlat {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#CardRaised {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#ErrorBanner {{
        background: {p.surface_raised};
        border: 1px solid {p.bad};
        border-left: 4px solid {p.bad};
        border-radius: {R_SM};
    }}
    /* ---------- Dashboard (prompt-reliability layout) ---------- */
    QWidget#DashboardContainer {{
        background: {p.bg};
    }}
    QFrame#FilterBar {{
        background: {p.surface};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#KpiCard {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QLabel#KpiTitle {{
        color: {p.text_dim};
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    QLabel#KpiValue {{
        color: {p.text};
        font-family: {SERIF_FONT};
        font-size: 24px;
        font-weight: 600;
    }}
    QFrame#Divider {{
        background: {p.hairline};
        max-height: 1px;
        min-height: 1px;
        border: none;
    }}

    /* ---------- Typography helpers ---------- */
    QLabel#H1 {{
        font-family: {SERIF_FONT};
        font-size: 22px;
        font-weight: 600;
        color: {p.text};
        letter-spacing: 0.1px;
    }}
    QLabel#H2 {{
        font-family: {SERIF_FONT};
        font-size: 15.5px;
        font-weight: 600;
        color: {p.text};
        letter-spacing: 0.1px;
    }}
    QLabel#Body {{
        font-size: 13px;
        color: {p.text_dim};
        line-height: 150%;
    }}
    QLabel#Faint {{
        font-size: 11.5px;
        color: {p.text_faint};
        letter-spacing: 0.15px;
    }}
    QLabel#Kicker {{
        font-size: 11px;
        font-weight: 700;
        color: {p.text_faint};
        letter-spacing: 2px;
    }}
    QLabel#Stat {{ font-family: "Cascadia Mono", "SFMono-Regular", "Consolas", "Menlo", monospace; font-size: 22px; font-weight: 600; color: {p.text}; }}
    QLabel#StatLabel {{ font-size: 10.5px; color: {p.text_faint}; letter-spacing: 0.8px; text-transform: uppercase; }}
    QLabel#Pill {{
        padding: 2px 9px;
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        font-size: 10.5px;
        font-weight: 700;
        letter-spacing: 0.4px;
    }}

    /* ---------- Buttons ---------- */
    QPushButton {{
        background: {p.surface_raised};
        color: {p.text};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 7px 16px;
        font-size: 12.5px;
        font-weight: 500;
        letter-spacing: 0.1px;
    }}
    QPushButton:hover {{ background: {p.surface_hover}; border-color: {p.text_faint}; }}
    QPushButton:pressed {{ background: {p.accent_soft}; }}
    QPushButton:disabled {{ color: {p.text_faint}; border-color: {p.hairline}; }}

    QPushButton#Primary {{
        background: {p.accent};
        color: #ffffff;
        border: 1px solid {p.accent};
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {p.accent_text}; border-color: {p.accent_text}; }}
    QPushButton#Primary:pressed {{ background: {p.accent}; }}
    QPushButton#Primary:disabled {{
        background: {p.surface_hover}; color: {p.text_faint}; border-color: {p.hairline};
    }}

    QPushButton#Danger {{
        background: transparent;
        color: {p.bad};
        border: 1px solid {p.bad};
    }}
    QPushButton#Danger:hover {{ background: rgba(234,107,107,0.12); }}

    QPushButton#Ghost {{
        background: transparent;
        border: 1px solid transparent;
        color: {p.text_dim};
        padding: 5px 8px;
    }}
    QPushButton#Ghost:hover {{ color: {p.text}; border-color: {p.hairline}; }}

    /* ---------- Inputs ---------- */
    QLineEdit, QComboBox, QSpinBox {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 7px 10px;
        color: {p.text};
        selection-background-color: {p.accent_soft};
    }}
    QLineEdit:focus, QComboBox:focus {{ border: 1px solid {p.accent}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        selection-background-color: {p.accent_soft};
        selection-color: {p.accent_text};
        outline: none;
        padding: 2px;
    }}

    QCheckBox {{ color: {p.text_dim}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px; border-radius: {R_SM};
        border: 1px solid {p.hairline}; background: {p.surface_raised};
    }}
    QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
    QCheckBox::indicator:hover {{ border-color: {p.text_faint}; }}

    /* ---------- Progress ---------- */
    QProgressBar {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        height: 14px;
        text-align: center;
        color: {p.text_dim};
        font-size: 10.5px;
        font-weight: 600;
    }}
    QProgressBar::chunk {{
        background: {p.accent};
    }}
    QProgressBar#OverallBar {{
        height: 14px;
        border-radius: 7px;
    }}
    QProgressBar#OverallBar::chunk {{
        background: {p.accent};
        border-radius: 7px;
    }}
    QProgressBar#DatasetBar {{
        height: 10px;
        border-radius: 5px;
        background: {p.surface};
    }}
    QProgressBar#DatasetBar::chunk {{
        background: {p.good};
        border-radius: 5px;
    }}
    /* Ollama install + model-pull bars only - the percent text sits
       right on top of the accent-colored fill, so it needs to be light
       to stay readable instead of the usual dim/dark progress-bar text. */
    QProgressBar#OllamaTaskBar {{
        color: #ffffff;
        border-radius: 7px;
    }}
    QProgressBar#OllamaTaskBar::chunk {{
        background: {p.accent};
        border-radius: 7px;
    }}

    /* ---------- Tables ---------- */
    QTableWidget, QTableView {{
        background: {p.surface};
        alternate-background-color: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        gridline-color: {p.hairline};
        selection-background-color: {p.accent_soft};
        selection-color: {p.text};
    }}
    QHeaderView::section {{
        background: {p.surface_raised};
        color: {p.text_faint};
        padding: 8px 10px;
        border: none;
        border-bottom: 1px solid {p.hairline};
        border-right: 1px solid {p.hairline};
        font-size: 10.5px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }}
    QTableWidget::item {{ padding: 6px 10px; border: none; }}
    QTableCornerButton::section {{ background: {p.surface_raised}; border: none; border-bottom: 1px solid {p.hairline}; }}

    /* ---------- Tabs ---------- */
    QTabWidget::pane {{ border-top: 1px solid {p.hairline}; margin-top: 6px; }}
    QTabBar::tab {{
        background: transparent;
        color: {p.text_faint};
        padding: 7px 4px;
        margin-right: 20px;
        font-size: 12.5px;
        font-weight: 600;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
    QTabBar::tab:hover {{ color: {p.text}; }}

    /* ---------- Scrollbars ---------- */
    QScrollBar:vertical {{
        background: transparent; width: 12px; margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {p.hairline}; border-radius: {R_SM}; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0px; }}
    QScrollBar::handle:horizontal {{ background: {p.hairline}; border-radius: {R_SM}; min-width: 30px; }}

    QListWidget {{
        background: {p.surface};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 2px;
    }}
    QListWidget::item {{
        padding: 7px 8px;
        border-radius: {R_SM};
        color: {p.text_dim};
    }}
    QListWidget::item:selected {{
        background: {p.accent_soft};
        color: {p.accent_text};
    }}
    QListWidget::item:hover {{ background: {p.surface_hover}; }}

    QTextEdit, QPlainTextEdit {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        color: {p.text_dim};
        font-family: "Cascadia Mono", "SFMono-Regular", "Consolas", "Menlo", monospace;
        font-size: 11.5px;
        padding: 8px;
    }}

    QToolTip {{
        background: {p.surface_raised};
        color: {p.text};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 4px 7px;
    }}

    QSplitter::handle {{ background: {p.hairline}; }}

    /* ---------- Startup / splash screen (single elongated, unboxed column) ---------- */
    #StartupRoot {{
        background: {p.surface};
    }}
    QLabel#StartupEyebrow {{
        color: {p.text_faint};
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 2.6px;
    }}
    QLabel#BrandIcon {{
        margin: 0 0 0 6px;
    }}
    QLabel#BrandMark {{
        color: {p.text};
        font-size: 15px;
        font-weight: 700;
        letter-spacing: 1.5px;
        margin: 0 10px 0 6px;
    }}
    QLabel#StartupTitle {{
        font-family: {SERIF_FONT};
        font-size: 26px;
        font-weight: 600;
        color: {p.text};
        letter-spacing: 0.2px;
    }}
    QLabel#StartupSubtitle {{
        font-size: 13px;
        color: {p.text_dim};
        line-height: 150%;
    }}
    QLabel#StartupFootnote {{
        font-size: 11px;
        color: {p.text_faint};
    }}
    QLabel#StartupStatusText {{
        font-size: 12.5px;
        color: {p.text_dim};
        font-weight: 600;
        letter-spacing: 0.2px;
    }}
    QLabel#FieldLabel {{
        font-size: 11px;
        font-weight: 700;
        color: {p.text_faint};
        letter-spacing: 1px;
    }}
    QComboBox#StartupCombo, QLineEdit#StartupCombo {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 8px 12px;
        font-size: 13.5px;
        color: {p.text};
    }}
    QComboBox#StartupCombo:focus, QLineEdit#StartupCombo:focus {{ border: 1px solid {p.accent}; }}
    QLabel#SectionLabel {{
        font-size: 12.5px;
        font-weight: 700;
        color: {p.text_dim};
        letter-spacing: 0.2px;
    }}
    QLabel#ModelRowStar {{
        color: {p.warn};
        font-size: 15px;
    }}
    QPushButton#StartupPrimaryBtn, QPushButton#StartupPrimaryBtnReady {{
        background: {p.accent};
        color: #ffffff;
        border: 1px solid {p.accent};
        border-radius: {R_MD};
        font-size: 14px;
        font-weight: 700;
        letter-spacing: 0.3px;
    }}
    QPushButton#StartupPrimaryBtn:hover, QPushButton#StartupPrimaryBtnReady:hover {{
        background: {p.accent_text};
        border-color: {p.accent_text};
    }}
    /* Compact warning banner inside the "Select a model" panel - only
    shown when Ollama isn't reachable, in place of the old full-screen
    connecting/error hero. */
    QFrame#StartupWarningBanner {{
        background: rgba(234,107,107,0.08);
        border: 1px solid rgba(234,107,107,0.35);
        border-radius: {R_SM};
    }}
    QPushButton#StartupSkipLink {{
        background: transparent;
        border: none;
        color: {p.accent_text};
        font-size: 11.5px;
        font-weight: 600;
        text-align: center;
        padding: 2px 0px;
    }}
    QPushButton#StartupSkipLink:hover {{
        color: {p.accent};
        text-decoration: underline;
    }}
    /* "Manage models" as a quiet inline toggle - no button box, just text
    that gains a hairline underline on hover, matching the unboxed page. */
    QPushButton#ManageModelsBtn {{
        background: transparent;
        border: none;
        color: {p.accent_text};
        font-size: 12.5px;
        font-weight: 600;
        text-align: center;
        padding: 4px 0px;
    }}
    QPushButton#ManageModelsBtn:hover {{
        color: {p.accent};
        text-decoration: underline;
    }}
    QPushButton#ManageModelsBtn:pressed {{ color: {p.accent}; }}

    /* Model list row on the startup screen - sits unboxed on the page
    background (no parent card), so it carries its own subtle surface and
    hairline; the whole row is clickable to select it, and a selected row
    gets an accent border + soft accent tint instead of a checkmark. */
    QFrame#ModelListRow {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#ModelListRow:hover {{
        background: {p.surface_hover};
    }}
    QFrame#ModelListRow[selected="true"] {{
        background: {p.accent_soft};
        border: 1.5px solid {p.accent};
    }}

    /* ---------- Home / landing screen (shown on launch, before startup) ---------- */
    #HomeRoot {{
        background: {p.bg};
    }}
    QLabel#HomeTitle {{
        font-family: {SERIF_FONT};
        font-size: 26px;
        font-weight: 700;
        color: {p.text};
        letter-spacing: 0.2px;
    }}
    QPushButton#HomeGetStartedBtn {{
        background: {p.accent};
        color: #ffffff;
        border: 1px solid {p.accent};
        border-radius: {R_MD};
        font-size: 14px;
        font-weight: 700;
        letter-spacing: 0.3px;
        padding: 11px 26px;
    }}
    QPushButton#HomeGetStartedBtn:hover {{
        background: {p.accent_text};
        border-color: {p.accent_text};
    }}
    QPushButton#HomeGetStartedBtn:pressed {{ background: {p.accent}; }}

    /* ---------- Native desktop chrome: toolbar / status bar (menu bar removed) ---------- */
    QToolBar#AppToolBar {{
        background: {p.surface};
        border-bottom: 1px solid {p.hairline};
        padding: 9px 14px;
        spacing: 4px;
    }}
    QToolBar#AppToolBar QToolButton {{
        background: transparent;
        color: {p.text_dim};
        border: 1px solid transparent;
        border-radius: {R_SM};
        padding: 7px 14px;
        font-size: 12.5px;
        font-weight: 500;
        letter-spacing: 0.1px;
    }}
    QToolBar#AppToolBar QToolButton:hover {{ background: {p.surface_hover}; color: {p.text}; }}
    QToolBar#AppToolBar QToolButton:checked {{
        background: {p.accent_soft}; color: {p.accent_text}; border-color: {p.hairline};
    }}
    QToolBar::separator {{ background: {p.hairline}; width: 1px; margin: 4px 8px; }}

    QStatusBar#AppStatusBar {{
        background: {p.surface};
        border-top: 1px solid {p.hairline};
        color: {p.text_faint};
        font-size: 11px;
    }}
    QStatusBar#AppStatusBar QLabel {{ color: {p.text_faint}; font-size: 11px; padding: 0 6px; }}

    /* ---------- Model badge / compare chip ---------- */
    QFrame#ModelBadge {{
        background: {p.accent_soft};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QLabel#ModelBadgeText {{
        color: {p.accent_text};
        font-size: 11.5px;
        font-weight: 700;
    }}
    QLabel#ModelBadgeCaption {{
        color: {p.text_faint};
        font-size: 9.5px;
        letter-spacing: 0.5px;
    }}
    QPushButton#ChangeModelBtn {{
        background: {p.surface_raised};
        color: {p.accent_text};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 6px 13px;
        font-size: 12px;
        font-weight: 600;
    }}
    QPushButton#ChangeModelBtn:hover {{
        background: {p.accent};
        color: #ffffff;
        border-color: {p.accent};
    }}
    QPushButton#CompareBtn {{
        background: {p.surface_raised};
        color: {p.text};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
        padding: 6px 13px;
        font-size: 12px;
        font-weight: 500;
    }}
    QPushButton#CompareBtn:hover {{ background: {p.accent_soft}; color: {p.accent_text}; }}
    QPushButton#CompareBtn:checked {{ background: {p.accent}; color: #ffffff; border-color: {p.accent}; }}

    /* ---------- Compare drawer / empty-state CTA ---------- */
    QFrame#CompareDrawer {{
        background: {p.surface_raised};
        border: 1px solid {p.hairline};
        border-radius: {R_SM};
    }}
    QFrame#EmptyStateCard {{
        background: {p.surface_raised};
        border: 1px dashed {p.hairline};
        border-radius: {R_SM};
    }}
    QLabel#MetricValue {{
        font-size: 24px; font-weight: 700; color: {p.text};
    }}
    QLabel#MetricLabel {{
        font-size: 10px; font-weight: 700; color: {p.text_faint}; letter-spacing: 0.6px;
    }}
    QLabel#MetricCompare {{
        font-size: 11px; font-weight: 600; color: {p.good}; margin-top: 1px;
    }}
    QLabel#Kicker {{
        font-size: 10.5px; font-weight: 700; color: {p.accent_text};
        letter-spacing: 1.2px;
    }}
    """