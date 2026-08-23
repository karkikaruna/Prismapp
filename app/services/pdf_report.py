"""PDF summary export for the Dashboard screen's "Download PDF" button.

Builds a single-model (optionally vs. a comparison model) benchmark summary
as a PDF, using reportlab. This is intentionally the *only* place PDF layout
lives - the Dashboard screen just gathers the rows/images and hands them to
:func:`build_model_summary_pdf`.

Layout, in order:

1. Letterhead - logo + title/subtitle/run metadata, laid out as a masthead
   rather than stacked plain paragraphs.
2. KPI strip - four headline numbers (datasets, questions, accuracy,
   compliance) so a reader gets the takeaway before any table.
3. Results by dataset - a single table (primary model, plus the comparison
   model's rows interleaved by dataset when one is selected) so the two are
   genuinely easy to scan side by side, instead of two stacked tables.
4. Prompt-condition (P0-P4) breakdown, aggregated to one row per condition
   across all datasets - never a dataset x condition table.
5. Chart images grabbed live from the Dashboard's on-screen charts, so the
   PDF matches what the user was looking at when they clicked "Download PDF".
6. A cross-model leaderboard, for context beyond the one/two models above.
7. Conclusion - a short, plain-language paragraph restating the report's
   own headline numbers (never a new metric): the primary model's
   accuracy/compliance, how it compared to the comparison model when one
   is selected, and where it sits on the full leaderboard.

A separate entry point, :func:`build_all_models_summary_pdf`, covers the
"All Models" case (no single primary model selected): letterhead, an
aggregate KPI strip, one combined dataset-level table across every locally
available model, the leaderboard, and its own conclusion - the same visual
language as the single/comparison report above, just scoped to everything
instead of one or two models. The full per-model, per-dataset data table
only ever appears in *that* report, never bundled into a single-model PDF.

Every table is set to the document's full usable width (no more tiny
left-aligned islands with dead space to the right), every page carries a
running header rule and a "Page N of M" footer, and every value is
defensively coerced with ``_f``/``_pct`` since the input dicts come straight
from CSV/SQLite reads and may have missing or blank fields (e.g. a dataset
with zero completed requests).
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

# ---- Palette ---------------------------------------------------------
# One accent color, used consistently for every table header and rule, is
# what makes the report read as one designed document rather than several
# ad-hoc tables pasted together.
_ACCENT = colors.HexColor("#5b4bd6")
_ACCENT_SOFT = colors.HexColor("#eeebfb")
_INK = colors.HexColor("#221f2b")
_BODY_TEXT = colors.HexColor("#3a3745")
_TEXT_FAINT = colors.HexColor("#6b6878")
_HAIRLINE = colors.HexColor("#dcd8ea")
_ROW_ALT = colors.HexColor("#f5f3fb")

_LOGO_PATH = Path(__file__).resolve().parent.parent / "resources" / "prism_logo.png"
_PAGE_MARGIN = 0.7 * inch


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value: Any) -> str:
    return f"{_f(value) * 100.0:.1f}%"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "PrismTitle", parent=base["Title"], fontSize=20, leading=24,
            textColor=_INK, alignment=TA_CENTER if False else 0,
        ),
        "subtitle": ParagraphStyle(
            "PrismSubtitle", parent=base["Normal"], fontSize=11, leading=15,
            textColor=_BODY_TEXT, spaceAfter=2,
        ),
        "h2": ParagraphStyle(
            "PrismH2", parent=base["Heading2"], fontSize=13.5, leading=17,
            textColor=_INK, spaceBefore=0, spaceAfter=0,
        ),
        "body": ParagraphStyle(
            "PrismBody", parent=base["Normal"], fontSize=9.5, leading=13,
            textColor=_BODY_TEXT,
        ),
        "caption": ParagraphStyle(
            "PrismCaption", parent=base["Normal"], fontSize=8.5, leading=12,
            textColor=_TEXT_FAINT,
        ),
        "kpi_value": ParagraphStyle(
            "PrismKpiValue", parent=base["Normal"], fontSize=19, leading=22,
            textColor=_INK, alignment=1, fontName="Helvetica-Bold",
        ),
        "kpi_label": ParagraphStyle(
            "PrismKpiLabel", parent=base["Normal"], fontSize=7.6, leading=10,
            textColor=_TEXT_FAINT, alignment=1, fontName="Helvetica-Bold",
        ),
    }


# Table cell styles are used from plain module functions (not just
# _styles()), so they're defined once at module scope rather than threaded
# through every table-building function's arguments.
_TABLE_HEADER_STYLE = ParagraphStyle(
    "PrismTableHeader", fontName="Helvetica-Bold", fontSize=8.7, leading=10.5,
    textColor=colors.white, alignment=TA_CENTER,
)
_TABLE_HEADER_STYLE_LEFT = ParagraphStyle(
    "PrismTableHeaderLeft", fontName="Helvetica-Bold", fontSize=8.7, leading=10.5,
    textColor=colors.white, alignment=0,
)
_TABLE_LABEL_STYLE = ParagraphStyle(
    "PrismTableLabel", fontName="Helvetica", fontSize=8.7, leading=10.5,
    textColor=_BODY_TEXT,
)


def _section(title: str, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """A section heading with a rule under it, styled consistently
    everywhere it's used - this is what makes each section read as a
    distinct block instead of a heading floating above a table."""
    return [
        Spacer(1, 16),
        Paragraph(title, styles["h2"]),
        HRFlowable(width="100%", thickness=1, color=_HAIRLINE, spaceBefore=4, spaceAfter=10),
    ]


def _header_cells(headers: list[str], style: ParagraphStyle, *, n_left: int = 0) -> list[Paragraph]:
    """Headers as wrapping Paragraphs, not bare strings - a bare string in a
    reportlab Table never wraps, so a long header (e.g. "Invariant Wrong")
    in a narrow column silently overflows past the table edge instead of
    breaking onto a second line. The first ``n_left`` columns (the label
    columns, e.g. Dataset/Model) are left-aligned to match the left-aligned
    label cells beneath them; the rest are centered like their numeric data.
    """
    cells = []
    for i, h in enumerate(headers):
        cells.append(Paragraph(h, _TABLE_HEADER_STYLE_LEFT if i < n_left else style))
    return cells


def _text_cell(value: str, style: ParagraphStyle) -> Paragraph:
    """Same wrapping fix as ``_header_cells``, for label-ish data cells
    (model/dataset/condition names) that can run long."""
    return Paragraph(value, style)


def _table_style(header_bg: colors.Color = _ACCENT, *, font_size: float = 8.7) -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
        ("GRID", (0, 0), (-1, -1), 0.5, _HAIRLINE),
        ("LINEBELOW", (0, 0), (-1, 0), 1, header_bg),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ])


def _fractions_to_widths(fractions: list[float], width: float) -> list[float]:
    return [width * f for f in fractions]


def _kpi_card_table(cards: list[tuple[str, str]], width: float, styles: dict[str, ParagraphStyle]) -> Table:
    """Shared renderer for the headline KPI strip - four (value, label)
    cards laid out identically whether they summarize one model or all of
    them, so both report types read as the same visual language."""
    cells = [[
        Paragraph(f"<b>{value}</b>", styles["kpi_value"]),
    ] for value, _label in cards]
    label_cells = [Paragraph(label.upper(), styles["kpi_label"]) for _value, label in cards]

    data = [[c[0] for c in cells], label_cells]
    table = Table(data, colWidths=[width / len(cards)] * len(cards), rowHeights=[30, 16])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ACCENT_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.75, _HAIRLINE),
        ("LINEAFTER", (0, 0), (-2, -1), 0.75, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
    ]))
    return table


def _kpi_strip(dataset_rows: list[dict[str, Any]], width: float, styles: dict[str, ParagraphStyle]) -> Optional[Table]:
    """Four headline numbers up top so the reader gets the takeaway before
    any table - the kind of at-a-glance summary a benchmark report should
    lead with, not bury under raw rows."""
    if not dataset_rows:
        return None

    n_datasets = len(dataset_rows)
    accuracy, compliance = _weighted_accuracy(dataset_rows)
    total_q = sum(_f(r.get("n_questions")) for r in dataset_rows)

    cards = [
        (str(n_datasets), "Datasets"),
        (str(int(total_q)), "Questions"),
        (_pct(accuracy), "Accuracy"),
        (_pct(compliance), "Compliance"),
    ]
    return _kpi_card_table(cards, width, styles)


def _all_models_kpi_strip(
    rows: list[dict[str, Any]], n_models: int, width: float, styles: dict[str, ParagraphStyle],
) -> Optional[Table]:
    """Same four-card KPI strip as the single-model report, but scoped to
    every model at once: model count and distinct dataset count in place
    of a single model's dataset count, then the same weighted accuracy/
    compliance figures across all of it."""
    if not rows:
        return None

    n_datasets = len({str(r.get("dataset", "")) for r in rows})
    accuracy, compliance = _weighted_accuracy(rows)

    cards = [
        (str(n_models), "Models"),
        (str(n_datasets), "Datasets"),
        (_pct(accuracy), "Accuracy"),
        (_pct(compliance), "Compliance"),
    ]
    return _kpi_card_table(cards, width, styles)


def _metrics_table(rows: list[dict[str, Any]], width: float, *, show_model_col: bool) -> Table:
    """One row per (model, dataset) with the core accuracy/consistency
    metrics side by side. When a comparison model is active, rows for both
    models are interleaved by dataset (sorted together) so they can
    genuinely be scanned side by side in one table, rather than as two
    separate stacked tables."""
    if show_model_col:
        headers = ["Model", "Dataset", "Qs", "Accuracy", "Agreement", "Compliance", "Recovery", "Unknown", "Inv. Wrong"]
        fractions = [0.12, 0.15, 0.055, 0.115, 0.115, 0.125, 0.115, 0.10, 0.105]
    else:
        headers = ["Dataset", "Qs", "Accuracy", "Agreement", "Compliance", "Recovery", "Unknown", "Inv. Wrong"]
        fractions = [0.17, 0.07, 0.135, 0.135, 0.145, 0.135, 0.10, 0.105]

    data = [_header_cells(headers, _TABLE_HEADER_STYLE, n_left=2 if show_model_col else 1)]
    for row in rows:
        line = []
        if show_model_col:
            line.append(_text_cell(str(row.get("_model_label", "")), _TABLE_LABEL_STYLE))
        line.append(_text_cell(str(row.get("dataset", "")), _TABLE_LABEL_STYLE))
        line.extend([
            str(int(_f(row.get("n_questions")))),
            _pct(row.get("prompt_response_accuracy")),
            _pct(row.get("mean_agreement")),
            _pct(row.get("instruction_compliance_rate")),
            _pct(row.get("answer_recovery_rate")),
            _pct(row.get("unknown_rate")),
            _pct(row.get("prompt_invariant_incorrect_rate")),
        ])
        data.append(line)

    table = Table(data, colWidths=_fractions_to_widths(fractions, width), hAlign="LEFT", repeatRows=1)
    table.setStyle(_table_style())
    return table


def _aggregate_prompt_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-dataset prompt-condition rows into one row per prompt
    condition (P0-P4), weighted by ``n_observed`` across all datasets.

    ``model_prompt_rows`` returns a row for every dataset/condition pair, so
    a model benchmarked on several datasets produces a table with
    dataset_count * condition_count rows - long enough to spill across many
    PDF pages. The PDF only needs the per-condition picture, so we weight-
    average the rates here instead of listing every dataset separately.
    """
    groups: dict[str, dict[str, float]] = {}
    for row in rows:
        cond = str(row.get("prompt_condition", ""))
        g = groups.setdefault(cond, {"n_observed": 0.0, "acc": 0.0, "compliance": 0.0, "recovery": 0.0})
        n = _f(row.get("n_observed"))
        g["n_observed"] += n
        g["acc"] += _f(row.get("accuracy")) * n
        g["compliance"] += _f(row.get("instruction_compliant_rate")) * n
        g["recovery"] += _f(row.get("answer_recovery_rate")) * n

    out = []
    for cond, g in sorted(groups.items()):
        n = g["n_observed"] or 1.0
        out.append({
            "prompt_condition": cond,
            "n_observed": g["n_observed"],
            "accuracy": g["acc"] / n,
            "instruction_compliant_rate": g["compliance"] / n,
            "answer_recovery_rate": g["recovery"] / n,
        })
    return out


def _prompt_table(rows: list[dict[str, Any]], width: float) -> Table:
    """Aggregate table: one row per prompt condition (P0-P4), summed/averaged
    across all datasets - see :func:`_aggregate_prompt_rows`."""
    headers = ["Condition", "Questions", "Accuracy", "Compliance", "Recovery"]
    fractions = [0.16, 0.18, 0.22, 0.22, 0.22]
    data = [_header_cells(headers, _TABLE_HEADER_STYLE, n_left=1)]
    for row in _aggregate_prompt_rows(rows):
        data.append([
            _text_cell(str(row.get("prompt_condition", "")), _TABLE_LABEL_STYLE),
            str(int(row.get("n_observed", 0))),
            _pct(row.get("accuracy")),
            _pct(row.get("instruction_compliant_rate")),
            _pct(row.get("answer_recovery_rate")),
        ])

    table = Table(data, colWidths=_fractions_to_widths(fractions, width), hAlign="LEFT", repeatRows=1)
    table.setStyle(_table_style())
    return table


def _all_models_table(rows: list[dict[str, Any]], width: float) -> Table:
    """Full per-(model, dataset) data for every locally available model -
    the detailed backing data behind the aggregate leaderboard above it.
    Reuses ``_metrics_table``'s column set/widths/styling verbatim (same
    headers, same fractions, same ``_table_style``) so this reads as the
    same table simply carrying more rows, not a different-looking one."""
    return _metrics_table(rows, width, show_model_col=True)


def _leaderboard_table(leaderboard: dict[str, dict[str, float]], width: float) -> Table:
    headers = ["Model", "Questions", "Accuracy", "Agreement", "Compliance", "Inv. Wrong"]
    fractions = [0.24, 0.14, 0.155, 0.155, 0.155, 0.155]
    data = [_header_cells(headers, _TABLE_HEADER_STYLE, n_left=1)]
    for tag, m in sorted(leaderboard.items(), key=lambda kv: kv[1].get("prompt_response_accuracy", 0.0), reverse=True):
        data.append([
            _text_cell(tag, _TABLE_LABEL_STYLE),
            str(int(_f(m.get("n_questions")))),
            _pct(m.get("prompt_response_accuracy")),
            _pct(m.get("mean_agreement")),
            _pct(m.get("instruction_compliance_rate")),
            _pct(m.get("prompt_invariant_incorrect_rate")),
        ])

    table = Table(data, colWidths=_fractions_to_widths(fractions, width), hAlign="LEFT", repeatRows=1)
    table.setStyle(_table_style())
    return table


def _pixmap_to_image_flowable(pixmap: Any, *, max_width: float) -> Optional[Image]:
    """Convert a QPixmap grabbed from the live dashboard charts into a
    reportlab Image flowable, scaled to fit the page width. Returns None if
    the pixmap can't be encoded (e.g. it's empty)."""
    if pixmap is None or pixmap.isNull():
        return None
    try:
        from PySide6.QtCore import QBuffer, QIODevice
    except ImportError:
        return None

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    png_bytes = bytes(buffer.data())
    buffer.close()
    if not png_bytes:
        return None

    stream = io.BytesIO(png_bytes)
    img = Image(stream)
    scale = min(1.0, max_width / img.imageWidth)
    img.drawWidth = img.imageWidth * scale
    img.drawHeight = img.imageHeight * scale
    return img


def _weighted_accuracy(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """Question-weighted (accuracy, compliance) across a set of dataset
    rows - the same weighting ``_kpi_strip`` uses, factored out so the
    conclusion can quote the identical numbers the KPI strip showed."""
    total_q = sum(_f(r.get("n_questions")) for r in rows)
    if total_q <= 0:
        return 0.0, 0.0
    accuracy = sum(_f(r.get("prompt_response_accuracy")) * _f(r.get("n_questions")) for r in rows) / total_q
    compliance = sum(_f(r.get("instruction_compliance_rate")) * _f(r.get("n_questions")) for r in rows) / total_q
    return accuracy, compliance


def _conclusion_paragraphs(
    model_label: str,
    dataset_rows: list[dict[str, Any]],
    compare_label: Optional[str],
    compare_rows: Optional[list[dict[str, Any]]],
    leaderboard: Optional[dict[str, dict[str, float]]],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Plain-language wrap-up of the report's own numbers - never a new
    metric, always a restatement of figures already shown above so the
    reader leaves with a takeaway instead of just a stack of tables."""
    sentences: list[str] = []

    acc, compliance = _weighted_accuracy(dataset_rows)
    if dataset_rows:
        sentences.append(
            f"<b>{model_label}</b> scored {_pct(acc)} accuracy and {_pct(compliance)} instruction "
            f"compliance across {len(dataset_rows)} dataset(s) in this run."
        )
    else:
        sentences.append(f"No dataset-level results were available for <b>{model_label}</b> in this run.")

    compare_active = bool(compare_label and compare_rows)
    if compare_active:
        c_acc, c_compliance = _weighted_accuracy(compare_rows or [])
        diff = acc - c_acc
        if abs(diff) < 0.005:
            sentences.append(
                f"Against <b>{compare_label}</b> ({_pct(c_acc)} accuracy), the two models performed "
                "about the same overall."
            )
        else:
            better, worse, gap = (model_label, compare_label, diff) if diff > 0 else (compare_label, model_label, -diff)
            sentences.append(
                f"<b>{better}</b> outperformed <b>{worse}</b> by {gap * 100:.1f} points of accuracy overall."
            )

    if leaderboard:
        ranked = sorted(
            leaderboard.items(), key=lambda kv: kv[1].get("prompt_response_accuracy", 0.0), reverse=True,
        )
        tags = [tag for tag, _m in ranked]
        primary_tags = [t for t in tags if model_label.lower() in t.lower()] or None
        if len(ranked) > 1:
            top_tag, top_metrics = ranked[0]
            sentences.append(
                f"Across all {len(ranked)} locally benchmarked models, <b>{top_tag}</b> leads the "
                f"leaderboard at {_pct(top_metrics.get('prompt_response_accuracy'))} accuracy."
            )

    body = " ".join(sentences)
    return [Paragraph(body, styles["body"])]


def _letterhead(model_label: str, model_tag: str, run_info: dict[str, Any],
                 compare_label: Optional[str], width: float,
                 styles: dict[str, ParagraphStyle]) -> Table:
    """Logo + title/subtitle/metadata laid out as a masthead (logo left,
    text right) instead of plain stacked paragraphs."""
    text_lines = [
        Paragraph("PRISM Benchmark Summary", styles["title"]),
        Paragraph(f"{model_label}  &middot;  {model_tag}", styles["subtitle"]),
    ]
    meta_bits = []
    if run_info.get("benchmark_run_id"):
        meta_bits.append(f"Run ID: {run_info['benchmark_run_id']}")
    if run_info.get("completed_at"):
        meta_bits.append(f"Completed: {run_info['completed_at']}")
    if compare_label:
        meta_bits.append(f"Compared with: {compare_label}")
    if meta_bits:
        text_lines.append(Paragraph("  \u00b7  ".join(meta_bits), styles["caption"]))

    text_cell = text_lines

    logo_cell: Any = ""
    if _LOGO_PATH.exists():
        try:
            logo = Image(str(_LOGO_PATH))
            logo.drawWidth = 0.6 * inch
            logo.drawHeight = 0.6 * inch
            logo_cell = logo
        except Exception:
            logo_cell = ""

    row = [logo_cell, text_cell] if logo_cell != "" else [text_cell]
    col_widths = [0.75 * inch, width - 0.75 * inch] if logo_cell != "" else [width]

    head = Table([row], colWidths=col_widths)
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("LEFTPADDING", (-1, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return head


class _FooterCanvas(Canvas):
    """Draws a running top rule and a "Page N of M" footer on every page.

    Total page count isn't known until the whole document is built, so
    this buffers each page's canvas state and re-plays them at ``save()``
    time once ``M`` is known - the standard reportlab two-pass recipe.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_decoration(total)
            super().showPage()
        super().save()

    def _draw_decoration(self, total_pages: int) -> None:
        page_w, page_h = letter
        self.saveState()

        # Slim brand rule across the very top of every page.
        self.setFillColor(_ACCENT)
        self.rect(0, page_h - 4, page_w, 4, stroke=0, fill=1)

        # Footer: hairline + report name (left) + page count (right).
        self.setStrokeColor(_HAIRLINE)
        self.setLineWidth(0.75)
        self.line(_PAGE_MARGIN, 0.55 * inch, page_w - _PAGE_MARGIN, 0.55 * inch)
        self.setFont("Helvetica", 8)
        self.setFillColor(_TEXT_FAINT)
        self.drawString(_PAGE_MARGIN, 0.38 * inch, "PRISM Benchmark Report")
        self.drawRightString(page_w - _PAGE_MARGIN, 0.38 * inch, f"Page {self._pageNumber} of {total_pages}")

        self.restoreState()


def _all_models_conclusion_paragraphs(
    rows: list[dict[str, Any]],
    leaderboard: Optional[dict[str, dict[str, float]]],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Wrap-up for the All Models report - same rule as the single-model
    conclusion (restate figures already shown above, never a new metric),
    scoped to the whole leaderboard instead of one model."""
    n_models = len({str(r.get("_model_label", "")) for r in rows})
    n_datasets = len({str(r.get("dataset", "")) for r in rows})
    accuracy, compliance = _weighted_accuracy(rows)

    sentences = [
        f"Across <b>{n_models}</b> locally benchmarked model(s) and <b>{n_datasets}</b> "
        f"dataset(s), overall accuracy is {_pct(accuracy)} with {_pct(compliance)} "
        "instruction compliance."
    ]

    if leaderboard:
        ranked = sorted(
            leaderboard.items(), key=lambda kv: kv[1].get("prompt_response_accuracy", 0.0), reverse=True,
        )
        if ranked:
            top_tag, top_metrics = ranked[0]
            sentences.append(
                f"<b>{top_tag}</b> leads the leaderboard at "
                f"{_pct(top_metrics.get('prompt_response_accuracy'))} accuracy"
                + (f", with {ranked[-1][0]} lowest at "
                   f"{_pct(ranked[-1][1].get('prompt_response_accuracy'))}." if len(ranked) > 1 else ".")
            )

    return [Paragraph(" ".join(sentences), styles["body"])]


def build_all_models_summary_pdf(
    out_path: str | Path,
    *,
    all_rows: list[dict[str, Any]],
    leaderboard: Optional[dict[str, dict[str, float]]] = None,
    chart_images: Optional[Iterable[tuple[str, Any]]] = None,
) -> None:
    """Render the "All Models" PDF - used only when the Dashboard's model
    filter is set to "All Models" (no single primary model selected).

    Same visual language as :func:`build_model_summary_pdf` (letterhead,
    KPI strip, results table, charts, leaderboard, conclusion), but the
    results table carries every (model, dataset) row instead of one or two
    models, since there's no single primary model to scope it to.

    ``all_rows`` must already carry a ``_model_label`` key per row (see
    :func:`_all_models_table` / ``_metrics_table``'s ``show_model_col``).
    Raises on any I/O or rendering failure, same as
    :func:`build_model_summary_pdf`.
    """
    out_path = Path(out_path)
    styles = _styles()

    n_models = len({str(r.get("_model_label", "")) for r in all_rows})

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title="PRISM summary - All Models",
    )
    width = doc.width

    story: list[Any] = []

    # ---- Letterhead -----------------------------------------------------
    story.append(_letterhead(
        "All Models", f"{n_models} model(s) locally available", {}, None, width, styles,
    ))
    story.append(Spacer(1, 14))

    # ---- KPI strip --------------------------------------------------------
    kpi = _all_models_kpi_strip(all_rows, n_models, width, styles)
    if kpi is not None:
        story.append(kpi)

    # ---- Results by model & dataset ---------------------------------------
    story.extend(_section("Results by Model & Dataset", styles))
    if all_rows:
        story.append(_all_models_table(all_rows, width))
    else:
        story.append(Paragraph("No dataset-level results are available locally.", styles["body"]))

    # ---- Charts -----------------------------------------------------------
    images = list(chart_images or [])
    if images:
        story.append(PageBreak())
        story.extend(_section("Charts", styles))
        for title, pixmap in images:
            flowable = _pixmap_to_image_flowable(pixmap, max_width=width)
            if flowable is not None:
                story.append(KeepTogether([
                    Paragraph(str(title), styles["body"]),
                    Spacer(1, 4),
                    flowable,
                    Spacer(1, 14),
                ]))

    # ---- Leaderboard --------------------------------------------------
    if leaderboard:
        board_block = _section("All Models - Leaderboard", styles)
        board_block.append(Paragraph(
            "Average dataset-level metrics across all locally available "
            "benchmark data for every model.",
            styles["caption"],
        ))
        board_block.append(Spacer(1, 8))
        board_block.append(_leaderboard_table(leaderboard, width))
        story.append(KeepTogether(board_block))

    # ---- Conclusion ---------------------------------------------------
    conclusion_block = _section("Conclusion", styles)
    conclusion_block.extend(_all_models_conclusion_paragraphs(all_rows, leaderboard, styles))
    story.append(KeepTogether(conclusion_block))

    # ---- Generation stamp -------------------------------------------------
    story.append(Spacer(1, 16))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated {generated}", styles["caption"]))

    doc.build(story, canvasmaker=_FooterCanvas)


def build_model_summary_pdf(
    out_path: str | Path,
    *,
    model_label: str,
    model_tag: str,
    run_info: dict[str, Any],
    dataset_rows: list[dict[str, Any]],
    prompt_rows: list[dict[str, Any]],
    compare_label: Optional[str] = None,
    compare_rows: Optional[list[dict[str, Any]]] = None,
    leaderboard: Optional[dict[str, dict[str, float]]] = None,
    chart_images: Optional[Iterable[tuple[str, Any]]] = None,
) -> None:
    """Render a PDF benchmark summary for one model to ``out_path``.

    Raises on any I/O or rendering failure - the caller (Dashboard screen)
    catches this and shows the error to the user rather than silently
    producing a corrupt or empty file.
    """
    out_path = Path(out_path)
    styles = _styles()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=f"PRISM summary - {model_label}",
    )
    width = doc.width

    story: list[Any] = []

    # ---- Letterhead -----------------------------------------------------
    story.append(_letterhead(model_label, model_tag, run_info, compare_label, width, styles))
    story.append(Spacer(1, 14))

    # ---- KPI strip --------------------------------------------------------
    kpi = _kpi_strip(dataset_rows, width, styles)
    if kpi is not None:
        story.append(kpi)

    # ---- Results by dataset (primary + comparison, one combined table) --
    compare_active = bool(compare_label and compare_rows)
    combined_rows: list[dict[str, Any]] = [{**r, "_model_label": model_label} for r in dataset_rows]
    if compare_active:
        combined_rows += [{**r, "_model_label": compare_label} for r in compare_rows]
        combined_rows.sort(key=lambda r: (str(r.get("dataset", "")), r.get("_model_label") != model_label))

    story.extend(_section("Results by Dataset", styles))
    if combined_rows:
        story.append(KeepTogether(_metrics_table(combined_rows, width, show_model_col=compare_active)))
    else:
        story.append(Paragraph("No dataset-level results available for this model.", styles["body"]))

    # ---- Prompt-condition breakdown --------------------------------------
    if prompt_rows:
        prompt_block = _section("Accuracy by Prompt Condition (P0-P4)", styles)
        prompt_block.append(_prompt_table(prompt_rows, width))
        story.append(KeepTogether(prompt_block))

    # ---- Charts -----------------------------------------------------------
    images = list(chart_images or [])
    if images:
        story.append(PageBreak())
        story.extend(_section("Charts", styles))
        for title, pixmap in images:
            flowable = _pixmap_to_image_flowable(pixmap, max_width=width)
            if flowable is not None:
                story.append(KeepTogether([
                    Paragraph(str(title), styles["body"]),
                    Spacer(1, 4),
                    flowable,
                    Spacer(1, 14),
                ]))

    # ---- Leaderboard --------------------------------------------------
    if leaderboard:
        board_block = _section("All Models - Leaderboard", styles)
        board_block.append(Paragraph(
            "Average dataset-level metrics across all locally available "
            "benchmark data for every model, for context beyond the "
            "model(s) above.",
            styles["caption"],
        ))
        board_block.append(Spacer(1, 8))
        board_block.append(_leaderboard_table(leaderboard, width))
        story.append(KeepTogether(board_block))

    # ---- Conclusion ---------------------------------------------------
    conclusion_block = _section("Conclusion", styles)
    conclusion_block.extend(_conclusion_paragraphs(
        model_label, dataset_rows, compare_label, compare_rows, leaderboard, styles,
    ))
    story.append(KeepTogether(conclusion_block))

    # ---- Generation stamp -------------------------------------------------
    story.append(Spacer(1, 16))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated {generated}", styles["caption"]))

    doc.build(story, canvasmaker=_FooterCanvas)