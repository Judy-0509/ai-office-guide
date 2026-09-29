"""python-pptx exporter: native, editable PowerPoint text, tables, and charts from the deck
spec (`spec.py`). Same geometry constants as the web preview -- see spec.py's design tokens
and the work order's px grid.

Geometry note: the deck canvas is 1920x1080 px and the slide is 16:9 at 13.333x7.5 in
(the work order's own "1 px = 6350 EMU" figure). 6350 EMU = 0.5 pt, so px->pt uses the same
0.5 factor -- keeping font/line sizes on the same scale as position/size avoids text and
shapes drifting apart on the canvas.
"""

from __future__ import annotations

import io
import math
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_MARKER_STYLE, XL_TICK_MARK
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from . import spec

PX_TO_EMU = 6350
PX_TO_PT = 0.5
CHART_FONT_PT = 12  # = 24px at the 0.5 pt/px factor -- python-pptx's chart default is ~18pt

SLIDE_W_PX = 1920
SLIDE_H_PX = 1080
PAD_TOP, PAD_SIDE, PAD_BOTTOM = 104, 64, 160
HEADER_MIN_H = 172
NUMBERED = "①②③④⑤"


def _emu(px: float) -> Emu:
    return Emu(round(px * PX_TO_EMU))


def _pt(px: float) -> Pt:
    return Pt(px * PX_TO_PT)


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F or 0xAC00 <= cp <= 0xD7A3
        or 0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
    )


def _estimate_lines(text: str, width_px: float, size_px: float) -> int:
    """Rough wrapped-line count (CJK glyphs ~1em wide, everything else ~0.62em, plus a 15%
    safety margin -- bold runs and digit/Latin-heavy text (DRAM, QoQ, +25%, USD, ...) render
    wider than a plain-text estimate). Used only to size a text box generously so a later block
    never overlaps it -- python-pptx has no way to ask PowerPoint how text will actually wrap
    at export time, so this must err toward extra whitespace, never toward overlap."""
    if not text:
        return 1
    total = sum(size_px if _is_cjk(ch) else size_px * 0.62 for ch in text) * 1.15
    return max(1, math.ceil(total / max(1, width_px)))


def _round_step(span: float) -> float:
    """A "nice" axis step (1/2/5 x a power of ten) for padding a numeric range to round
    numbers, roughly 4-8 gridlines across the span."""
    if span <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(span))
    for base in (1, 2, 5, 10):
        step = base * magnitude
        if span / step <= 8:
            return step
    return 10 * magnitude


def _label_color_on(fill_hex: str) -> str:
    """White text on the two dark fills (accent, the darkest two gray bars), ink on the light
    ones -- matches the web preview's in-bar label rule."""
    return "#ffffff" if fill_hex in (spec.ACCENT, spec.GRAY_BARS[0], spec.GRAY_BARS[1]) else spec.INK


def _run(paragraph, text: str, *, size_px: float, color: str, bold: bool = False) -> None:
    run = paragraph.add_run()
    run.text = text
    run.font.size = _pt(size_px)
    run.font.bold = bold
    run.font.name = spec.FONT
    run.font.color.rgb = _rgb(color)


def _textbox(slide, x, y, w, h, text: str, *, size_px: float, color: str,
             bold: bool = False, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(_emu(x), _emu(y), _emu(w), _emu(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = align
    _run(p, text or "", size_px=size_px, color=color, bold=bold)
    return box


def _textbox_with_highlight(slide, x, y, w, h, text: str, highlight: str | None, *,
                             size_px: float, color: str, highlight_color: str, bold: bool = False):
    box = slide.shapes.add_textbox(_emu(x), _emu(y), _emu(w), _emu(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    text = text or ""
    idx = text.find(highlight) if highlight else -1
    if idx == -1:
        _run(p, text, size_px=size_px, color=color, bold=bold)
    else:
        before, mid, after = text[:idx], text[idx:idx + len(highlight)], text[idx + len(highlight):]
        if before:
            _run(p, before, size_px=size_px, color=color, bold=bold)
        _run(p, mid, size_px=size_px, color=highlight_color, bold=bold)
        if after:
            _run(p, after, size_px=size_px, color=color, bold=bold)
    return box


def _numbered_text_block(slide, x, y, w, h, items: list[tuple[str, str, str]], *, size_px: float = 24):
    """One text box, one paragraph per (number, keyword, text) item, with a hanging indent so
    a wrapped second line aligns under the keyword rather than under the number. Replaces a
    fixed-height box per row, which overlapped the next row whenever text wrapped to 2+ lines."""
    box = slide.shapes.add_textbox(_emu(x), _emu(y), _emu(w), _emu(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    number_w = _emu(34)
    for idx, (num, keyword, text) in enumerate(items):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.line_spacing = 1.3
        if idx > 0:
            p.space_before = _pt(10)
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(number_w))
        pPr.set("indent", str(-number_w))
        _run(p, num + " ", size_px=size_px, color=spec.ACCENT, bold=True)
        if keyword:
            _run(p, keyword, size_px=size_px, color=spec.BODY, bold=True)
            _run(p, " " + text, size_px=size_px, color=spec.BODY, bold=False)
        else:
            _run(p, text, size_px=size_px, color=spec.BODY, bold=False)
    return box


def _rect(slide, x, y, w, h, fill_color: str | None, *, line_color: str | None = None,
          line_pt: float = 0.75):
    box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _emu(x), _emu(y), _emu(w), _emu(h))
    if fill_color:
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(fill_color)
    else:
        box.fill.background()
    if line_color:
        box.line.color.rgb = _rgb(line_color)
        box.line.width = Pt(line_pt)
    else:
        box.line.fill.background()
    box.shadow.inherit = False
    return box


# --- header / footer -------------------------------------------------------------------------
def _render_header(slide, slide_spec: dict) -> float:
    x = PAD_SIDE
    w = SLIDE_W_PX - 2 * PAD_SIDE
    y = PAD_TOP
    _textbox(slide, x, y, w, 32, slide_spec.get("kicker", ""), size_px=24, color=spec.ACCENT, bold=True)
    y += 40
    _textbox(slide, x, y, w, 72, slide_spec.get("title", ""), size_px=56, color=spec.INK, bold=True)
    y += 80
    if slide_spec.get("subtitle"):
        _textbox(slide, x, y, w, 44, slide_spec["subtitle"], size_px=32, color=spec.BODY)
    rule_y = PAD_TOP + HEADER_MIN_H
    _rect(slide, x, rule_y, w, 2, spec.INK)
    return rule_y + 24


def _render_footer(slide, footer: str, source: str | None) -> None:
    text = f"{footer} · {source}" if source else footer
    _textbox(slide, PAD_SIDE, SLIDE_H_PX - 64, 1792, 32, text, size_px=24, color=spec.MUTED)


def _set_crosses_minimum(axis) -> None:
    """Force <c:crosses val="min"/> directly -- see the note where this is called."""
    crosses_el = axis._element.find(qn("c:crosses"))
    if crosses_el is not None:
        crosses_el.set("val", "min")


def _pin_plot_area(chart, *, x: float, y: float, w: float, h: float) -> None:
    """Pin the chart's INNER plot area to fixed fractions (0-1) of the chart frame via
    c:plotArea/c:layout/c:manualLayout. Without this, PowerPoint auto-sizes the plot area
    based on label content, so a row's pixel position can't be predicted at export time --
    which is what a separate, external label column needs to line up with the dots."""
    plot_area = chart._chartSpace.find(qn("c:chart")).find(qn("c:plotArea"))
    layout = plot_area.makeelement(qn("c:layout"), {})
    manual = plot_area.makeelement(qn("c:manualLayout"), {})
    layout.append(manual)
    for tag, val in (
        ("c:layoutTarget", "inner"), ("c:xMode", "edge"), ("c:yMode", "edge"),
        ("c:x", str(x)), ("c:y", str(y)), ("c:w", str(w)), ("c:h", str(h)),
    ):
        manual.append(plot_area.makeelement(qn(tag), {"val": val}))
    plot_area.insert(0, layout)


# --- native charts / table --------------------------------------------------------------------
def _force_horizontal_axis_labels(axis, *, categories: list[str], chart_w_px: float) -> None:
    """With several narrow categories, PowerPoint's own auto-fit otherwise (a) rotates tick
    labels and can truncate them with an ellipsis, or (b) once rotation is forced off, skips
    every other label -- and PowerPoint does not actually wrap axis tick labels onto a second
    line the way normal text boxes do, so with many categories forcing horizontal + all-shown
    just crams them together instead. There is no font size that avoids both rotation AND
    overlap for, say, 8 categories at 12pt, so this sizes down (never rotates, cuts, or skips)
    to fit the longest category name into its own column, based on the real chart width."""
    n = max(1, len(categories))
    longest_chars = max((len(c) for c in categories), default=1)
    col_w_px = chart_w_px / n
    # A CJK glyph is roughly 1em (= 2 * size_pt px at this module's 0.5 pt/px factor) wide;
    # fit the longest label into ~85% of its column so adjacent labels never touch.
    size_pt = max(7, min(CHART_FONT_PT, (col_w_px * 0.85) / max(1, longest_chars) / 2))
    axis.tick_labels.font.size = Pt(size_pt)  # also creates the txPr/bodyPr element
    axis.tick_labels.font.name = spec.FONT
    axis.tick_labels.font.color.rgb = _rgb(spec.BODY)
    body_pr = axis._element.find(qn("c:txPr")).find(qn("a:bodyPr"))
    body_pr.set("rot", "0")
    body_pr.set("vert", "horz")
    lbl_offset = axis._element.find(qn("c:lblOffset"))
    tick_lbl_skip = axis._element.makeelement(qn("c:tickLblSkip"), {"val": "1"})
    lbl_offset.addnext(tick_lbl_skip)


def _style_chart_text(chart) -> None:
    """No chart title (the panel already has its own title textbox above the chart); all chart
    text (data labels, axis tick labels, legend) explicitly 12pt/Malgun Gothic -- python-pptx's
    default chart font is much larger and dominated the panel."""
    chart.has_title = False
    chart.font.size = Pt(CHART_FONT_PT)
    chart.font.name = spec.FONT
    chart.font.color.rgb = _rgb(spec.BODY)


def _add_bar_chart(slide, panel: dict, x, y, w, h):
    categories = list(panel.get("categories", []))
    values = list(panel.get("values", []))
    forecast = panel.get("forecast") or []
    display_categories = [
        f"{c} (E)" if i < len(forecast) and forecast[i] else c
        for i, c in enumerate(categories)
    ]
    chart_data = CategoryChartData()
    chart_data.categories = display_categories
    chart_data.add_series(panel.get("title", "값"), values)
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, _emu(x), _emu(y), _emu(w), _emu(h), chart_data)
    chart = frame.chart
    _style_chart_text(chart)
    chart.has_legend = False
    chart.value_axis.visible = False
    chart.value_axis.has_major_gridlines = False
    chart.category_axis.has_major_gridlines = False
    _force_horizontal_axis_labels(chart.category_axis, categories=display_categories, chart_w_px=w)
    plot = chart.plots[0]
    plot.gap_width = 80
    plot.has_data_labels = True
    plot.data_labels.number_format = "0"
    plot.data_labels.number_format_is_linked = False
    plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
    plot.data_labels.font.size = Pt(CHART_FONT_PT)
    plot.data_labels.font.name = spec.FONT
    plot.data_labels.font.color.rgb = _rgb(spec.INK)
    series = plot.series[0]
    highlight = panel.get("highlight")
    for i, point in enumerate(series.points):
        is_forecast = i < len(forecast) and forecast[i]
        is_hl = highlight is not None and i == highlight
        # python-pptx has no pattern-fill API for chart points -- forecast bars get a lighter
        # gray plus the "(E)" category suffix above instead of the web preview's diagonal hatch.
        color = spec.ACCENT if is_hl else (spec.GRAY_BARS[2] if is_forecast else spec.GRAY_BARS[0])
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = _rgb(color)
    return chart


def _add_stacked_chart(slide, panel: dict, x, y, w, h):
    rows = panel.get("rows", [])
    categories = [r.get("label", "") for r in rows]
    part_names: list[str] = []
    for r in rows:
        for part in r.get("parts", []):
            if part.get("name") not in part_names:
                part_names.append(part.get("name"))
    # value_matrix[series_idx][row_idx] -> the raw count, kept alongside the chart so data
    # labels can show it directly: python-pptx's "0%" number format multiplies the raw value
    # by 100 (it doesn't know it's already a % of the 100%-stacked total), which read "600%"
    # for a value of 6 -- so labels are custom text ("부정 6"), not a number format at all.
    value_matrix = [
        [next((p.get("value", 0) for p in r.get("parts", []) if p.get("name") == name), 0)
         for r in rows]
        for name in part_names
    ]
    row_totals = [sum(value_matrix[s][r] for s in range(len(part_names))) or 1
                  for r in range(len(rows))]
    chart_data = CategoryChartData()
    chart_data.categories = categories
    for name, values in zip(part_names, value_matrix):
        chart_data.add_series(name, values)
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.BAR_STACKED_100, _emu(x), _emu(y), _emu(w), _emu(h), chart_data)
    chart = frame.chart
    _style_chart_text(chart)
    chart.has_legend = False  # labels are drawn in-bar instead
    chart.value_axis.visible = False
    chart.value_axis.has_major_gridlines = False
    chart.category_axis.has_major_gridlines = False
    # Keep the row name (tick label) but hide the axis line and tick marks themselves -- an
    # axis line running down the left edge added nothing once the bars fill the full width.
    chart.category_axis.major_tick_mark = XL_TICK_MARK.NONE
    chart.category_axis.minor_tick_mark = XL_TICK_MARK.NONE
    chart.category_axis.format.line.fill.background()
    plot = chart.plots[0]
    plot.has_data_labels = True
    highlight_name = panel.get("highlight")
    for s_idx, series in enumerate(plot.series):
        is_hl = highlight_name is not None and series.name == highlight_name
        color = (spec.ACCENT if is_hl
                 else spec.GRAY_BARS[part_names.index(series.name) % len(spec.GRAY_BARS)])
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = _rgb(color)
        label_color = _label_color_on(color)
        for p_idx, point in enumerate(series.points):
            value = value_matrix[s_idx][p_idx]
            share = value / row_totals[p_idx]
            # A segment wins less than ~15% of the bar has no room for its name too --
            # "긍정 2" doesn't fit a sliver, so it shows just "2".
            text = str(value) if share < 0.15 else f"{series.name} {value}"
            point.data_label.has_text_frame = True
            point.data_label.text_frame.text = text
            for para in point.data_label.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(CHART_FONT_PT)
                    run.font.name = spec.FONT
                    run.font.color.rgb = _rgb(label_color)
    return chart


# Dots/scatter layout constants (px, this module's units unless noted).
DOTS_LABEL_COL_W = 150
DOTS_COL_GAP = 8
DOTS_PLOT_TOP_MARGIN = 10
DOTS_PLOT_LEFT_MARGIN = 10
DOTS_PLOT_RIGHT_MARGIN = 55   # room for a RIGHT-positioned value label near the plot's right edge
DOTS_AXIS_BOTTOM_MARGIN = 40  # fixed room for the X axis line + its 12pt tick labels


def _add_scatter_chart(slide, panel: dict, x, y, w, h):
    """Item labels are a separate LEFT COLUMN of text boxes, not chart data labels -- the
    previous approach (labels drawn BY the chart, "label value" together) had no way to
    guarantee the bottom row cleared the X axis or that two close values' labels didn't
    collide, especially in a short (2-3 panel) layout where each row is only ~13px tall. A
    manual plot-area layout (see _pin_plot_area) makes each row's pixel position exact and
    predictable, so the external label column can be positioned to match it exactly."""
    items = panel.get("items", [])
    n = len(items)
    if n == 0:
        return None

    inner_h_px = max(1.0, h - DOTS_PLOT_TOP_MARGIN - DOTS_AXIS_BOTTOM_MARGIN)
    row_h_px = inner_h_px / n
    # Row pitch rule: below ~18pt (=36px here) of row height, EVERY label in this chart (item
    # names and values alike) uses 10pt -- uniform, never a per-pair patch.
    label_size_pt = CHART_FONT_PT if (row_h_px * PX_TO_PT) >= 18 else 10

    chart_x = x + DOTS_LABEL_COL_W + DOTS_COL_GAP
    chart_w = max(1.0, w - DOTS_LABEL_COL_W - DOTS_COL_GAP)
    highlight_label = panel.get("highlight")

    # Left column: one right-aligned text box per item, vertically centered on its own row --
    # row i's center is (i+0.5)/n of the way down the (pinned) inner plot area, top to bottom.
    for i, item in enumerate(items):
        row_top_px = DOTS_PLOT_TOP_MARGIN + inner_h_px * i / n
        is_hl = highlight_label is not None and item.get("label") == highlight_label
        box = slide.shapes.add_textbox(
            _emu(x), _emu(y + row_top_px), _emu(DOTS_LABEL_COL_W), _emu(row_h_px))
        tf = box.text_frame
        # A textbox defaults to auto_size=SHAPE_TO_FIT_TEXT, which silently overrides the
        # explicit height/position set above (PowerPoint resizes the shape to fit a single
        # line, breaking the row-by-row alignment with the chart this box exists to match).
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.word_wrap = False
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        _run(p, item.get("label", ""), size_px=label_size_pt * 2,
             color=spec.ACCENT if is_hl else spec.INK, bold=is_hl)

    chart_data = XyChartData()
    for i, item in enumerate(items):
        series = chart_data.add_series(item.get("label", ""))
        # y = n-1-i: item 0 plots at the TOP, matching reading order top-to-bottom, evenly
        # spaced one row per item (not by value).
        series.add_data_point(item.get("value", 0), n - 1 - i)
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.XY_SCATTER, _emu(chart_x), _emu(y), _emu(chart_w), _emu(h), chart_data)
    chart = frame.chart
    _style_chart_text(chart)
    chart.has_legend = False
    chart.value_axis.visible = False  # the Y axis is just an item slot, not real data
    chart.value_axis.has_major_gridlines = False  # horizontal gridlines off
    # The category (X) axis crosses the value (Y) axis at Y=0 by default ("autoZero") -- since
    # the bottom row's Y IS 0, the X axis and its tick labels would render right on top of the
    # bottom row regardless of padding. Crossing at the axis MINIMUM moves the X axis below
    # every row instead. python-pptx's `value_axis.crosses` setter is a no-op on this axis
    # element (the getter reads back MINIMUM but the underlying <c:crosses> XML never changes
    # from "autoZero" -- verified directly), so this sets the XML attribute itself.
    chart.value_axis.minimum_scale = -0.5
    chart.value_axis.maximum_scale = n - 0.5
    _set_crosses_minimum(chart.value_axis)

    values = [item.get("value", 0) for item in items]
    lo, hi = min(values), max(values)
    step = _round_step((hi - lo) or max(1.0, abs(hi) * 0.1))
    chart.category_axis.minimum_scale = math.floor(lo / step) * step - step * 0.5
    chart.category_axis.maximum_scale = math.ceil(hi / step) * step + step * 0.5
    chart.category_axis.has_major_gridlines = True  # light vertical gridlines
    chart.category_axis.major_gridlines.format.line.color.rgb = _rgb(spec.BORDER)
    chart.category_axis.major_gridlines.format.line.width = Pt(0.75)
    chart.category_axis.tick_labels.font.size = Pt(CHART_FONT_PT)  # X axis labels: fixed 12pt
    chart.category_axis.tick_labels.font.name = spec.FONT
    chart.category_axis.tick_labels.font.color.rgb = _rgb(spec.BODY)

    # Pin the inner plot area to fixed pixel margins (converted to fractions of the chart
    # frame) so the dots' row positions are deterministic and match the left column exactly,
    # and the bottom margin is a fixed size (not a fraction of a possibly-tiny row height).
    x_frac = DOTS_PLOT_LEFT_MARGIN / chart_w
    w_frac = max(0.05, (chart_w - DOTS_PLOT_LEFT_MARGIN - DOTS_PLOT_RIGHT_MARGIN) / chart_w)
    y_frac = DOTS_PLOT_TOP_MARGIN / h
    h_frac = max(0.05, inner_h_px / h)
    _pin_plot_area(chart, x=x_frac, y=y_frac, w=w_frac, h=h_frac)

    plot = chart.plots[0]
    for i, series in enumerate(plot.series):
        is_hl = highlight_label is not None and items[i].get("label") == highlight_label
        color = spec.ACCENT if is_hl else spec.GRAY_BARS[1]
        marker = series.marker
        marker.style = XL_MARKER_STYLE.CIRCLE
        marker.size = 10
        marker.format.fill.solid()
        marker.format.fill.fore_color.rgb = _rgb(color)
        marker.format.line.fill.background()
        series.format.line.fill.background()  # markers only, no connecting line
        point = series.points[0]
        point.data_label.has_text_frame = True
        point.data_label.position = XL_LABEL_POSITION.RIGHT
        point.data_label.text_frame.text = str(items[i].get("value", ""))  # value only
        for para in point.data_label.text_frame.paragraphs:
            for run in para.runs:
                run.font.size = Pt(label_size_pt)
                run.font.name = spec.FONT
                run.font.color.rgb = _rgb(spec.INK)
    return chart


def _column_widths_px(columns: list, rows: list, total_w_px: float, *, min_frac: float = 0.08) -> list[int]:
    """Column widths proportional to content length, each floored at `min_frac` of the total
    width -- equal widths wasted space on short columns ("시각") and cramped long ones."""
    n = len(columns)
    if n == 0:
        return []
    lengths = []
    for c in range(n):
        header_len = len(str(columns[c]))
        cell_len = max((len(str(row[c])) for row in rows if c < len(row)), default=0)
        lengths.append(max(header_len, cell_len, 1))
    total_len = sum(lengths)
    fracs = [max(min_frac, length / total_len) for length in lengths]
    fracs = [f / sum(fracs) for f in fracs]  # renormalize to 1.0 after applying the floor
    widths = [round(total_w_px * f) for f in fracs]
    widths[-1] += round(total_w_px) - sum(widths)  # absorb rounding drift into the last column
    return widths


def _add_native_table(slide, panel: dict, x, y, w, h):
    columns = panel.get("columns", [])
    rows_data = panel.get("rows", [])
    n_rows, n_cols = len(rows_data) + 1, max(len(columns), 1)
    frame = slide.shapes.add_table(n_rows, n_cols, _emu(x), _emu(y), _emu(w), _emu(h))
    table = frame.table
    for c, width_px in enumerate(_column_widths_px(columns, rows_data, w)):
        table.columns[c].width = _emu(width_px)
    highlight_rows = set(panel.get("highlight_rows") or [])
    for c, col_name in enumerate(columns):
        cell = table.cell(0, c)
        cell.text = str(col_name)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(spec.PANEL_BG)
        for run in cell.text_frame.paragraphs[0].runs:
            run.font.bold, run.font.size, run.font.name = True, _pt(24), spec.FONT
            run.font.color.rgb = _rgb(spec.INK)
    for r_idx, row in enumerate(rows_data, start=1):
        is_hl = (r_idx - 1) in highlight_rows
        for c, cell_value in enumerate(row):
            cell = table.cell(r_idx, c)
            cell.text = str(cell_value)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(spec.ACCENT_TINT if is_hl else "#ffffff")
            for run in cell.text_frame.paragraphs[0].runs:
                run.font.size, run.font.name = _pt(24), spec.FONT
                run.font.color.rgb = _rgb(spec.BODY)
    return table


def _render_panel_content(slide, panel: dict, x, y, w, h):
    box = _rect(slide, x, y, w, h, "#ffffff", line_color=spec.BORDER)
    pad_x, pad_y = 20, 16
    inner_x, inner_w = x + pad_x, w - 2 * pad_x
    _textbox(slide, inner_x, y + pad_y, inner_w, 30, panel.get("title", ""),
             size_px=24, color=spec.INK, bold=True)
    _textbox(slide, inner_x, y + pad_y + 30, inner_w, 26, panel.get("caption", ""),
             size_px=24, color=spec.MUTED)
    content_y = y + pad_y + 60
    content_h = (y + h - pad_y) - content_y
    ptype = panel.get("type")
    if ptype == "bar":
        _add_bar_chart(slide, panel, inner_x, content_y, inner_w, content_h)
    elif ptype == "stacked":
        _add_stacked_chart(slide, panel, inner_x, content_y, inner_w, content_h)
    elif ptype == "dots":
        _add_scatter_chart(slide, panel, inner_x, content_y, inner_w, content_h)
    elif ptype == "table":
        _add_native_table(slide, panel, inner_x, content_y, inner_w, content_h)
    return box


def _render_panels(slide, panels: list[dict], x, y, w, h):
    n = len(panels)
    if n == 0:
        return
    gap = 16
    if n == 1:
        _render_panel_content(slide, panels[0], x, y, w, h)
    elif n == 2:
        each_h = (h - gap) / 2
        _render_panel_content(slide, panels[0], x, y, w, each_h)
        _render_panel_content(slide, panels[1], x, y + each_h + gap, w, each_h)
    else:
        top_h = (h - gap) * 0.55
        bottom_h = h - gap - top_h
        each_w = (w - gap) / 2
        _render_panel_content(slide, panels[0], x, y, each_w, top_h)
        _render_panel_content(slide, panels[1], x + each_w + gap, y, each_w, top_h)
        _render_panel_content(slide, panels[2], x, y + top_h + gap, w, bottom_h)


# --- layouts --------------------------------------------------------------------------------
def _render_summary(slide, slide_spec: dict, body_top: float) -> None:
    x = PAD_SIDE
    col_w = 600
    y = body_top

    judgment = slide_spec.get("judgment", {})
    callout_h = 140
    _rect(slide, x, y, col_w, callout_h, spec.ACCENT_TINT)
    _rect(slide, x, y, 4, callout_h, spec.ACCENT)
    pad_x, pad_y = 20, 14
    _textbox(slide, x + pad_x, y + pad_y, col_w - 2 * pad_x, 28, "판단",
             size_px=24, color=spec.ACCENT, bold=True)
    _textbox_with_highlight(
        slide, x + pad_x, y + pad_y + 30, col_w - 2 * pad_x, 40,
        judgment.get("headline", ""), judgment.get("highlight"),
        size_px=28, color=spec.INK, highlight_color=spec.ACCENT, bold=True)
    _textbox(slide, x + pad_x, y + pad_y + 72, col_w - 2 * pad_x, 44, judgment.get("detail", ""),
             size_px=24, color=spec.BODY)
    y += callout_h + 12

    _textbox(slide, x, y, col_w, 28, "근거", size_px=24, color=spec.MUTED, bold=True)
    y += 32
    evidence = slide_spec.get("evidence", [])
    evidence_items = [
        (NUMBERED[i] if i < len(NUMBERED) else str(i + 1), e.get("keyword", ""), e.get("text", ""))
        for i, e in enumerate(evidence)
    ]
    text_w = col_w - 38
    evidence_lines = sum(_estimate_lines(f"{kw} {tx}", text_w, 24) for _, kw, tx in evidence_items)
    evidence_h = max(32.0, evidence_lines * 24 * 1.3 + max(0, len(evidence_items) - 1) * 10)
    _numbered_text_block(slide, x, y, col_w, evidence_h, evidence_items, size_px=24)
    y += evidence_h + 12

    _textbox(slide, x, y, col_w, 28, "시사점", size_px=24, color=spec.MUTED, bold=True)
    y += 32
    implication = slide_spec.get("implication", {})
    impl_text = implication.get("text", "")
    impl_h = max(32.0, _estimate_lines(impl_text, col_w, 24) * 24 * 1.3)
    _textbox(slide, x, y, col_w, impl_h, impl_text, size_px=24, color=spec.BODY)
    y += impl_h + 4
    _textbox(slide, x, y, col_w, 32, "→ " + implication.get("action", ""),
             size_px=24, color=spec.ACCENT, bold=True)

    right_x = x + col_w + 24
    right_w = (SLIDE_W_PX - PAD_SIDE) - right_x
    bottom = SLIDE_H_PX - PAD_BOTTOM
    _render_panels(slide, slide_spec.get("panels", []), right_x, body_top, right_w, bottom - body_top)


def _render_table_layout(slide, slide_spec: dict, body_top: float) -> None:
    x = PAD_SIDE
    w = SLIDE_W_PX - 2 * PAD_SIDE
    bottom = SLIDE_H_PX - PAD_BOTTOM
    note = slide_spec.get("note")
    table_h = (bottom - body_top) - (44 if note else 0)
    _add_native_table(slide, slide_spec.get("table", {}), x, body_top, w, table_h)
    if note:
        _textbox(slide, x, body_top + table_h + 10, w, 32, note, size_px=24, color=spec.MUTED)


def _render_chart_layout(slide, slide_spec: dict, body_top: float) -> None:
    x = PAD_SIDE
    bottom = SLIDE_H_PX - PAD_BOTTOM
    h = bottom - body_top
    chart_w = 1200
    _render_panel_content(slide, slide_spec.get("chart", {}), x, body_top, chart_w, h)
    notes_x = x + chart_w + 24
    notes_w = (SLIDE_W_PX - PAD_SIDE) - notes_x
    notes = slide_spec.get("notes", [])
    items = [
        (NUMBERED[i] if i < len(NUMBERED) else str(i + 1), n.get("keyword", ""), n.get("text", ""))
        for i, n in enumerate(notes)
    ]
    if items:
        _numbered_text_block(slide, notes_x, body_top, notes_w, h, items, size_px=24)


def _render_slide(slide, slide_spec: dict, footer: str) -> None:
    body_top = _render_header(slide, slide_spec)
    layout = slide_spec.get("layout")
    if layout == "summary":
        _render_summary(slide, slide_spec, body_top)
    elif layout == "table":
        _render_table_layout(slide, slide_spec, body_top)
    elif layout == "chart":
        _render_chart_layout(slide, slide_spec, body_top)
    _render_footer(slide, footer, slide_spec.get("source"))
    slide.notes_slide.notes_text_frame.text = ""


def build_presentation(deck: dict) -> Presentation:
    prs = Presentation()
    prs.slide_width = _emu(SLIDE_W_PX)
    prs.slide_height = _emu(SLIDE_H_PX)
    blank = prs.slide_layouts[6]
    footer = deck.get("footer", "")
    for slide_spec in deck.get("slides", []):
        slide = prs.slides.add_slide(blank)
        _render_slide(slide, slide_spec, footer)
    return prs


def export_bytes(deck: dict) -> bytes:
    prs = build_presentation(deck)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def export_to_file(deck: dict, path: Any) -> None:
    build_presentation(deck).save(str(path))
