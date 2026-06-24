"""
PDF report generator — produces a professional due diligence PDF using ReportLab.

Uses ReportLab Platypus (pure Python, no system dependencies).
Also writes a standalone .html file for browser viewing.
"""

import os
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from src.schemas.models import ReportDocument
from src.synthesis.assembler import assemble_report, build_render_dicts
from src.synthesis.render_common import (
    OVERALL_NOT_COMPUTABLE,
    disclaimer_sentences,
    edgar_line,
    format_generated_et,
    strip_dashes,
    tier_coverage_parts,
)

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ── Colour palette ────────────────────────────────────────────────────────────

C_NAVY    = colors.HexColor("#1e3a8a")
C_BLUE    = colors.HexColor("#2563eb")
C_SLATE   = colors.HexColor("#64748b")
C_DARK    = colors.HexColor("#1a1a2e")
C_WHITE   = colors.white

C_GREEN_BG  = colors.HexColor("#dcfce7"); C_GREEN_FG  = colors.HexColor("#166534")
C_YELLOW_BG = colors.HexColor("#fef9c3"); C_YELLOW_FG = colors.HexColor("#854d0e")
C_RED_BG    = colors.HexColor("#fee2e2"); C_RED_FG    = colors.HexColor("#991b1b")
C_GRAY_BG   = colors.HexColor("#f1f5f9"); C_GRAY_FG   = colors.HexColor("#475569")

C_ORANGE    = colors.HexColor("#ea580c")
C_DARK_RED  = colors.HexColor("#dc2626")

C_TBL_HEADER  = C_NAVY
C_TBL_ODD     = colors.white
C_TBL_EVEN    = colors.HexColor("#f8fafc")
C_TBL_BORDER  = colors.HexColor("#e2e8f0")

C_REC_PROCEED_BG  = colors.HexColor("#f0fdf4"); C_REC_PROCEED_BAR  = colors.HexColor("#16a34a")
C_REC_CAUTION_BG  = colors.HexColor("#fff7ed"); C_REC_CAUTION_BAR  = colors.HexColor("#ea580c")
C_REC_COND_BG     = colors.HexColor("#fefce8"); C_REC_COND_BAR     = colors.HexColor("#ca8a04")
C_REC_HALT_BG     = colors.HexColor("#fef2f2"); C_REC_HALT_BAR     = colors.HexColor("#dc2626")
C_REC_UNKNOWN_BG  = colors.HexColor("#f8fafc"); C_REC_UNKNOWN_BAR  = colors.HexColor("#94a3b8")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


# ── Styles ────────────────────────────────────────────────────────────────────

_base = getSampleStyleSheet()

def _style(name, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, **kw)

S_TITLE     = _style("title",     fontSize=20, fontName="Helvetica-Bold",
                     textColor=C_NAVY, spaceAfter=2)
S_SUBTITLE  = _style("subtitle",  fontSize=9,  fontName="Helvetica",
                     textColor=C_SLATE, spaceAfter=4)
S_META      = _style("meta",      fontSize=8,  fontName="Helvetica",
                     textColor=C_SLATE)
S_H2        = _style("h2",        fontSize=13, fontName="Helvetica-Bold",
                     textColor=C_NAVY, spaceBefore=14, spaceAfter=5)
S_H3        = _style("h3",        fontSize=10, fontName="Helvetica-Bold",
                     textColor=colors.HexColor("#374151"), spaceBefore=10, spaceAfter=4)
S_BODY      = _style("body",      fontSize=9,  fontName="Helvetica",
                     textColor=C_DARK, leading=13)
S_BODY_SM   = _style("body_sm",   fontSize=8,  fontName="Helvetica",
                     textColor=C_SLATE, leading=11)
S_BOLD      = _style("bold",      fontSize=9,  fontName="Helvetica-Bold",
                     textColor=C_DARK)
S_TBL_HEAD  = _style("tbl_head",  fontSize=8,  fontName="Helvetica-Bold",
                     textColor=C_WHITE, alignment=TA_LEFT)
S_TBL_CELL  = _style("tbl_cell",  fontSize=8,  fontName="Helvetica",
                     textColor=C_DARK, leading=11)
S_TBL_CELL_SM = _style("tbl_cell_sm", fontSize=7.5, fontName="Helvetica",
                        textColor=C_SLATE, leading=10)
S_BADGE     = _style("badge",     fontSize=7.5, fontName="Helvetica-Bold",
                     alignment=TA_CENTER)
S_REC_LABEL = _style("rec_label", fontSize=8,  fontName="Helvetica-Bold",
                     textColor=C_SLATE)
S_REC_VALUE = _style("rec_value", fontSize=13, fontName="Helvetica-Bold",
                     textColor=C_DARK, spaceAfter=4)
S_REC_BODY  = _style("rec_body",  fontSize=9,  fontName="Helvetica",
                     textColor=colors.HexColor("#374151"), leading=13)


# ── Helpers ───────────────────────────────────────────────────────────────────

_CONF_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
_SEV_ORDER  = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONF_SCORES = {"high": 1.0, "medium": 0.66, "low": 0.33, "unknown": 0.0}


def _trunc(s, n=110):
    s = str(s)
    return s[:n - 2] + "…" if len(s) > n else s


def _xml_esc(s):
    """Escape text for a ReportLab Paragraph (which parses XML-like markup).

    Also normalises em/en/etc. dashes to a single hyphen (house style). URL-safe:
    these glyphs never appear in URLs, so escaping hrefs through here is harmless.
    """
    s = strip_dashes(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clean_source_label(url):
    """Clean, human-readable label for a source URL (domain or 'Source')."""
    try:
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
        return netloc or "Source"
    except Exception:
        return "Source"


def _cell(text):
    """A wrapping, XML-escaped table cell Paragraph (no truncation — full text)."""
    return Paragraph(_xml_esc(text), S_TBL_CELL)


def _src_links_para(sources):
    """Render every source as a clickable PDF link with a clean domain label."""
    parts = []
    for u in (sources or []):
        if not u:
            continue
        parts.append(f'<a href="{_xml_esc(u)}" color="#2563eb">{_xml_esc(_clean_source_label(u))}</a>')
    return Paragraph(", ".join(parts) if parts else "-", S_TBL_CELL)


def _sort_sev(items):
    return sorted(items, key=lambda d: (
        _SEV_ORDER.get(d.get("severity", ""), 99),
        _CONF_ORDER.get(d.get("confidence", "unknown"), 99),
    ))


def _section_confidence(data):
    total, count = 0.0, 0
    for k, v in data.items():
        if k == "company_name":
            continue
        if isinstance(v, dict) and "confidence" in v:
            total += _CONF_SCORES.get(v["confidence"], 0.0); count += 1
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and "confidence" in item:
                    total += _CONF_SCORES.get(item["confidence"], 0.0); count += 1
    return total / count if count else 0.0


def _report_confidence(fin, risk, social):
    parts = [(fin, 0.40), (risk, 0.40), (social, 0.20)]
    w_sum, w_tot = 0.0, 0.0
    for data, w in parts:
        if data:
            w_sum += _section_confidence(data) * w; w_tot += w
    return w_sum / w_tot if w_tot else None


def _conf_colors(conf):
    return {
        "high":    (C_GREEN_BG,  C_GREEN_FG,  "HIGH"),
        "medium":  (C_YELLOW_BG, C_YELLOW_FG, "MED"),
        "low":     (C_RED_BG,    C_RED_FG,    "LOW"),
        "unknown": (C_GRAY_BG,   C_GRAY_FG,   "N/A"),
    }.get(conf, (C_GRAY_BG, C_GRAY_FG, conf.upper()))


def _sev_colors(sev):
    return {
        "critical": (C_DARK_RED, C_WHITE,   "CRITICAL"),
        "high":     (C_ORANGE,   C_WHITE,   "HIGH"),
        "medium":   (C_YELLOW_BG, C_YELLOW_FG, "MED"),
        "low":      (C_GREEN_BG,  C_GREEN_FG,  "LOW"),
    }.get(sev, (C_GRAY_BG, C_GRAY_FG, sev.upper() if sev else "-"))


def _conf_cell(conf):
    bg, fg, label = _conf_colors(conf)
    p = Paragraph(label, ParagraphStyle("_cb", fontSize=7, fontName="Helvetica-Bold",
                                         textColor=fg, alignment=TA_CENTER))
    return Table([[p]], colWidths=[14*mm],
                 style=[("BACKGROUND", (0,0), (-1,-1), bg),
                        ("ROUNDEDCORNERS", [3]),
                        ("TOPPADDING", (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                        ("LEFTPADDING", (0,0), (-1,-1), 4),
                        ("RIGHTPADDING", (0,0), (-1,-1), 4)])


def _sev_cell(sev):
    bg, fg, label = _sev_colors(sev)
    p = Paragraph(label, ParagraphStyle("_sb", fontSize=7, fontName="Helvetica-Bold",
                                         textColor=fg, alignment=TA_CENTER))
    return Table([[p]], colWidths=[16*mm],
                 style=[("BACKGROUND", (0,0), (-1,-1), bg),
                        ("ROUNDEDCORNERS", [3]),
                        ("TOPPADDING", (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                        ("LEFTPADDING", (0,0), (-1,-1), 4),
                        ("RIGHTPADDING", (0,0), (-1,-1), 4)])


def _p(text, style=None):
    style = style or S_BODY
    return Paragraph(str(text), style)


def _hr():
    return HRFlowable(width="100%", thickness=1, color=C_TBL_BORDER, spaceAfter=4, spaceBefore=8)


# ── Table builders ────────────────────────────────────────────────────────────

def _std_table_style(n_rows):
    cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  C_TBL_HEADER),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_TBL_ODD, C_TBL_EVEN]),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_TBL_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    return TableStyle(cmds)


def _kv_table(rows_data: list[tuple], col_widths=None) -> Table:
    """Two or three-column key-value table."""
    usable = PAGE_W - 2 * MARGIN
    col_widths = col_widths or [32*mm, usable - 32*mm - _CONF_W, _CONF_W]
    rows = []
    for label, value, conf in rows_data:
        rows.append([
            _p(_xml_esc(label), S_BOLD),
            _p(_xml_esc(value), S_TBL_CELL),
            _conf_cell(conf),
        ])
    t = Table(rows, colWidths=col_widths, repeatRows=0)
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [C_TBL_ODD, C_TBL_EVEN]),
        ("GRID",   (0, 0), (-1, -1), 0.3, C_TBL_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _list_table(items: list, headers: list, builders: list, col_widths=None) -> list:
    """Return [h3_para, table] flowables for a list of DataPoints.

    builders = list of callables(dp) → cell value (str or Flowable)
    """
    if not items:
        return []
    usable = PAGE_W - 2 * MARGIN

    header_row = [_p(h, S_TBL_HEAD) for h in headers]
    data_rows = []
    for dp in items:
        row = []
        for fn in builders:
            cell = fn(dp)
            if isinstance(cell, str):
                cell = _p(cell, S_TBL_CELL)
            row.append(cell)
        data_rows.append(row)

    n = len(data_rows) + 1
    t = Table([header_row] + data_rows, colWidths=col_widths or _auto_widths(len(headers), usable))
    t.setStyle(_std_table_style(n))
    return [t, Spacer(1, 4)]


# Column widths sized so headers never wrap ("Confidence" needs ~20mm at 8pt bold).
_CONF_W = 20 * mm
_SEV_W  = 18 * mm


def _auto_widths(n_cols, usable):
    src_w = 38 * mm
    if n_cols == 2:
        # item | conf
        return [usable - _CONF_W, _CONF_W]
    if n_cols == 3:
        # item | conf | sources
        return [usable - _CONF_W - src_w, _CONF_W, src_w]
    if n_cols == 4:
        # item | sev | conf | sources
        return [usable - _SEV_W - _CONF_W - src_w, _SEV_W, _CONF_W, src_w]
    return [usable / n_cols] * n_cols


# ── Section builders ─────────────────────────────────────────────────────────

def _cover(company, now, cost, duration, report_score, financial_data, risk_data, social_data,
           sec_confs=None):
    """sec_confs: optional dict mapping "financial"/"risk"/"social_media" → 0.0-1.0 fraction."""
    usable = PAGE_W - 2 * MARGIN
    elems = []
    elems.append(Spacer(1, 2*mm))
    elems.append(_p(f"Due Diligence Report", S_SUBTITLE))
    elems.append(_p(company, S_TITLE))
    # Rule sits BELOW the title with clearance (Spacer before), never across it,
    # and a clear gap (Spacer after) separates it from the Generated/Cost line.
    elems.append(Spacer(1, 2.5*mm))
    elems.append(HRFlowable(width="100%", thickness=2, color=C_BLUE))
    elems.append(Spacer(1, 3*mm))
    elems.append(_p(f"Generated {now}  ·  Cost ${cost:.4f}  ·  Duration {duration/1000:.1f}s", S_META))
    elems.append(Spacer(1, 4*mm))

    if report_score is not None:
        pct = report_score * 100
        if pct >= 75:   badge_bg, badge_fg = C_GREEN_BG,  C_GREEN_FG
        elif pct >= 50: badge_bg, badge_fg = C_YELLOW_BG, C_YELLOW_FG
        elif pct >= 25: badge_bg, badge_fg = C_RED_BG,    C_RED_FG
        else:           badge_bg, badge_fg = C_GRAY_BG,   C_GRAY_FG

        # Overall figure plus all five section confidences in report-section order
        # (display only, from section_confidences). No per-item weight labels —
        # Research and Synthesis are not weighted into Overall.
        rows = [[_p(f"Overall Confidence: {pct:.0f}%",
                    ParagraphStyle("sc", fontSize=9, fontName="Helvetica-Bold",
                                   textColor=badge_fg))]]
        # Legacy fallback only covers the sections _cover receives data for; for
        # normal docs section_confidences carries all five, including research/synthesis.
        _fallback = {"financial": financial_data, "risk": risk_data,
                     "social_media": social_data}
        sec_parts = []
        for lbl, key in [("Research", "research"), ("Financial", "financial"),
                         ("Risk", "risk"), ("Social Media", "social_media"),
                         ("Synthesis", "synthesis")]:
            if sec_confs is not None:
                # Missing section shown honestly as n/a (never a fabricated value);
                # a missing weighted section already lowered Overall.
                sec_parts.append(f"{lbl} {sec_confs[key]*100:.0f}%" if key in sec_confs else f"{lbl} n/a")
            elif _fallback.get(key):
                sec_parts.append(f"{lbl} {_section_confidence(_fallback[key])*100:.0f}%")
        if sec_parts:
            rows.append([_p("  ·  ".join(sec_parts),
                            ParagraphStyle("scs", fontSize=8, fontName="Helvetica",
                                           textColor=badge_fg))])

        # Explicit colWidths=[usable] constrains the bar to the content frame so
        # its background cannot overflow the page/container.
        conf_tbl = Table(rows, colWidths=[usable])
        conf_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), badge_bg),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        elems.append(conf_tbl)
        # Weighting explained once, below the bar (not as per-item labels).
        elems.append(_p("Overall is a weighted blend of Financial and Risk (40% each) and "
                        "Social Media (20%); Research and Synthesis are reported but not weighted.",
                        S_BODY_SM))
        elems.append(Spacer(1, 4*mm))
    else:
        # Overall is UNDEFINED (no weighted section scorable). Show it honestly in
        # a neutral bar — never 0%, never omitted.
        nc_tbl = Table([[_p(f"Overall Confidence: {OVERALL_NOT_COMPUTABLE}",
                            ParagraphStyle("ncsc", fontSize=9, fontName="Helvetica-Bold",
                                           textColor=C_GRAY_FG))]], colWidths=[usable])
        nc_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_GRAY_BG),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        elems.append(nc_tbl)
        elems.append(Spacer(1, 4*mm))

    return elems


def _disclaimer_flowables(company):
    """Prominent disclaimer block (static render text) for the PDF, near the top."""
    usable = PAGE_W - 2 * MARGIN
    title_style = ParagraphStyle("disc_title", fontSize=8, fontName="Helvetica-Bold",
                                 textColor=colors.HexColor("#92400e"), spaceAfter=3)
    body_style = ParagraphStyle("disc_body", fontSize=8, fontName="Helvetica",
                                textColor=colors.HexColor("#713f12"), leading=11, spaceAfter=2)
    inner = [[_p("DISCLAIMER", title_style)]]
    for sentence in disclaimer_sentences(company):
        inner.append([_p(_xml_esc(sentence), body_style)])
    tbl = Table(inner, colWidths=[usable])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#fffbeb")),
        ("LINEBEFORE",    (0, 0), (0, -1), 4, colors.HexColor("#d97706")),
        ("BOX",           (0, 0), (-1, -1), 0.5, colors.HexColor("#fcd34d")),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [tbl, Spacer(1, 4*mm)]


def _assessment_section(synthesis_data):
    """Group 1 — Executive Summary prose, Key Strengths, Key Concerns, Red Flags."""
    elems = [_p("Executive Summary", S_H2)]

    summary = synthesis_data.get("executive_summary", {}).get("value", "")
    if summary and summary not in ("unknown", ""):
        elems.append(_p(_xml_esc(summary), S_BODY))
        elems.append(Spacer(1, 4*mm))

    usable = PAGE_W - 2 * MARGIN
    # Synthesized-claim tables: Item, Confidence (no Sources/Agents).
    claim_builders = [
        lambda dp: _cell(dp.get("value", "—")),
        lambda dp: _conf_cell(dp.get("confidence", "unknown")),
    ]
    claim_widths = [usable - _CONF_W, _CONF_W]

    for title, key in [("Key Strengths", "key_strengths"), ("Key Concerns", "key_concerns")]:
        items = synthesis_data.get(key, [])
        if items:
            elems.append(KeepTogether([
                _p(title, S_H3),
                *_list_table(items, ["Item", "Confidence"], claim_builders, claim_widths),
            ]))

    red_flags = synthesis_data.get("red_flags", [])
    if red_flags:
        # Severity column (and its legend) appear ONLY here.
        flag_builders = [
            lambda dp: _cell(dp.get("value", "—")),
            lambda dp: _sev_cell(dp.get("severity", "")),
            lambda dp: _conf_cell(dp.get("confidence", "unknown")),
        ]
        flag_widths = [usable - _SEV_W - _CONF_W, _SEV_W, _CONF_W]
        elems.append(KeepTogether([
            _p("Red Flags", S_H3),
            _p("Severity = risk impact  ·  Confidence = data reliability  ·  Sorted by severity",
               S_BODY_SM),
            Spacer(1, 1*mm),
            *_list_table(_sort_sev(red_flags), ["Red Flag", "Severity", "Confidence"],
                         flag_builders, flag_widths),
        ]))

    return elems


def _open_items_section(synthesis_data, gaps):
    """Group 2 — Data Quality, Data Conflicts, Information Gaps, Follow-Up Questions."""
    elems = [_hr(), _p("Data Quality & Open Items", S_H2)]
    usable = PAGE_W - 2 * MARGIN

    if synthesis_data:
        dq = synthesis_data.get("data_quality", {})
        if dq.get("value") and dq["value"] not in ("unknown", ""):
            elems.append(_p(f"Data Quality: <b>{_xml_esc(dq['value'].upper())}</b>", S_BODY))
            elems.append(Spacer(1, 2*mm))

        conflicts = synthesis_data.get("data_conflicts", [])
        if conflicts:
            c_builders = [
                lambda dp: _cell(dp.get("value", "—")),
                lambda dp: _conf_cell(dp.get("confidence", "unknown")),
            ]
            elems.append(KeepTogether([
                _p("Data Conflicts", S_H3),
                *_list_table(conflicts, ["Conflict", "Confidence"], c_builders,
                             [usable - _CONF_W, _CONF_W]),
            ]))

    elems.append(_p("Information Gaps", S_H3))
    if gaps:
        for g in gaps:
            elems.append(_p(f"• <b>{_xml_esc(g.field)}</b> ({_xml_esc(g.agent)}): {_xml_esc(g.reason)}", S_BODY))
    else:
        elems.append(_p("• No critical gaps identified", S_BODY))

    if synthesis_data:
        fq = synthesis_data.get("follow_up_questions", [])
        if fq:
            fq_widths = [usable * 0.6, usable * 0.4]
            fq_builders = [
                lambda dp: _cell(dp.get("value", "—")),
                lambda dp: _cell(dp.get("reasoning", "") or "—"),
            ]
            elems.append(Spacer(1, 2*mm))
            elems.append(KeepTogether([
                _p("Follow-Up Questions", S_H3),
                *_list_table(fq, ["Question", "Why It Matters"], fq_builders, fq_widths),
            ]))

    return elems


# Attribute list tables: Item, Confidence (no Sources).
def _attr_builders():
    return [lambda dp: _cell(dp.get("value", "—")),
            lambda dp: _conf_cell(dp.get("confidence", "unknown"))]


# Evidence list tables: Item, Confidence, Sources (clickable).
def _evidence_builders():
    return [lambda dp: _cell(dp.get("value", "—")),
            lambda dp: _conf_cell(dp.get("confidence", "unknown")),
            lambda dp: _src_links_para(dp.get("sources", []))]


def _research_section(data):
    elems = [_hr(), _p("Company Profile", S_H2)]
    kv = []
    for label, key in [("Description", "description"), ("Founded", "founded_year"),
                        ("Headquarters", "headquarters"), ("Employees", "employee_count"),
                        ("Industry", "industry"), ("Website", "website")]:
        dp = data.get(key, {})
        kv.append((label, dp.get("value", "—"), dp.get("confidence", "unknown")))
    elems.append(_kv_table(kv))
    elems.append(Spacer(1, 4*mm))

    for title, key, builders, headers in [
        ("Key Products",       "key_products",       _attr_builders(),     ["Product", "Confidence"]),
        ("Leadership",         "key_leadership",     _attr_builders(),     ["Name / Title", "Confidence"]),
        ("Technology Stack",   "technology_stack",   _attr_builders(),     ["Technology", "Confidence"]),
        ("Recent Developments","recent_developments",_evidence_builders(), ["Development", "Confidence", "Sources"]),
        ("Notable Patents",    "notable_patents",    _evidence_builders(), ["Patent", "Confidence", "Sources"]),
    ]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(items, headers, builders)]))
    return elems


def _financial_section(data):
    elems = [_hr(), _p("Financial Profile", S_H2)]
    kv = []
    for label, key in [("Revenue", "revenue"), ("Revenue Growth", "revenue_growth"),
                        ("Profitability", "profitability"), ("Total Funding", "total_funding"),
                        ("Last Funding Round", "last_funding_round"), ("Valuation", "valuation"),
                        ("Revenue Model", "revenue_model")]:
        dp = data.get(key, {})
        kv.append((label, dp.get("value", "—"), dp.get("confidence", "unknown")))
    elems.append(_kv_table(kv))
    elems.append(Spacer(1, 4*mm))

    # Financial Risks is rendered in the Risk section so all risk content sits together.
    for title, key, builders, headers in [
        ("Key Investors",          "key_investors",          _attr_builders(),     ["Investor", "Confidence"]),
        ("Key Customers",          "key_customers",          _attr_builders(),     ["Customer", "Confidence"]),
        ("Recent Financial Events","recent_financial_events",_evidence_builders(), ["Event", "Confidence", "Sources"]),
    ]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(items, headers, builders)]))
    return elems


def _risk_section(data, financial_data=None):
    elems = [_hr(), _p("Risk Assessment", S_H2)]

    overall = data.get("overall_risk_rating", {})
    rating  = overall.get("value", "unknown").upper()
    conf    = overall.get("confidence", "unknown")
    bg, fg, lbl = _conf_colors(conf)
    elems.append(_p(f"Overall Risk Rating: <b>{_xml_esc(rating)}</b>  [{lbl} confidence]", S_BODY))

    summary = data.get("risk_summary", {})
    if summary.get("value") and summary["value"] != "unknown":
        # Plain narrative prose — no recommendation/callout wrapper.
        elems.append(Spacer(1, 2*mm))
        elems.append(_p(_xml_esc(summary["value"]), S_BODY))

    elems.append(Spacer(1, 3*mm))

    # Risk sub-tables are evidence tables: Item, Confidence, Sources.
    # Severity stays in the data (used for sorting) but is not a column here.
    for title, key in [
        ("Regulatory Risks", "regulatory_risks"), ("Legal Risks", "legal_risks"),
        ("Cybersecurity Risks", "cybersecurity_risks"), ("Operational Risks", "operational_risks"),
        ("Reputational Risks", "reputational_risks"), ("ESG Risks", "esg_risks"),
        ("Pending Litigation", "pending_litigation"),
    ]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(_sort_sev(items),
                                                    ["Risk", "Confidence", "Sources"],
                                                    _evidence_builders())]))

    # Financial Risks grouped here with the rest of the risk content.
    if financial_data:
        fr = financial_data.get("financial_risks", [])
        if fr:
            elems.append(KeepTogether([_p("Financial Risks", S_H3),
                                       *_list_table(fr, ["Risk", "Confidence", "Sources"],
                                                    _evidence_builders())]))

    nfc = data.get("notable_federal_contracts", [])
    if nfc:
        elems.append(KeepTogether([_p("Notable Federal Contracts", S_H3),
                                   *_list_table(nfc, ["Contract", "Confidence", "Sources"],
                                                _evidence_builders())]))
    return elems


def _social_section(data):
    elems = [_hr(), _p("Social Media & Sentiment", S_H2)]

    sentiment = data.get("overall_sentiment", {})
    elems.append(_p(f"Overall Sentiment: <b>{_xml_esc(sentiment.get('value', '—').upper())}</b>", S_BODY))

    summary = data.get("sentiment_summary", {})
    if summary.get("value") and summary["value"] != "unknown":
        info_tbl = Table([[_p(_xml_esc(summary["value"]), S_REC_BODY)]],
                         colWidths=[PAGE_W - 2*MARGIN - 4*mm])
        info_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#eff6ff")),
            ("LINEBEFORE",  (0,0), (0,-1), 3, C_BLUE),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING",(0,0), (-1,-1), 8),
            ("TOPPADDING",  (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ]))
        elems.append(Spacer(1, 3*mm))
        elems.append(info_tbl)

    elems.append(Spacer(1, 3*mm))
    kv = []
    for label, key in [("Twitter/X", "twitter_presence"), ("LinkedIn", "linkedin_presence"),
                        ("Reddit", "reddit_sentiment"), ("Glassdoor", "glassdoor_rating")]:
        dp = data.get(key, {})
        kv.append((label, dp.get("value", "—"), dp.get("confidence", "unknown")))
    elems.append(_kv_table(kv))
    elems.append(Spacer(1, 3*mm))

    for title, key in [("Notable Mentions", "notable_mentions"), ("Trending Topics", "trending_topics"),
                        ("Customer Complaints", "customer_complaints"), ("Positive Signals", "positive_signals")]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(items, ["Item", "Confidence", "Sources"],
                                                    _evidence_builders())]))
    return elems


def _methodology_section(trace_summary, meta=None):
    cost       = trace_summary.get("total_cost_usd", 0)
    tokens     = trace_summary.get("total_input_tokens", 0) + trace_summary.get("total_output_tokens", 0)
    llm_calls  = trace_summary.get("total_llm_calls", 0)
    tool_calls = trace_summary.get("total_tool_calls", 0)
    duration   = trace_summary.get("total_duration_ms", 0)

    elems = [
        _hr(),
        _p("Methodology", S_H2),
        _p(f"Generated by multi-agent pipeline: {llm_calls} LLM calls, "
           f"{tool_calls} tool invocations, {tokens:,} tokens, "
           f"{duration/1000:.1f}s, ${cost:.4f}.", S_BODY_SM),
        Spacer(1, 3*mm),
    ]

    by_agent = trace_summary.get("by_agent", {})
    if by_agent:
        header = [_p(h, S_TBL_HEAD) for h in ["Agent", "LLM Calls", "Tool Calls", "Tokens", "Cost"]]
        rows = [header]
        for agent, d in by_agent.items():
            t = d["input_tokens"] + d["output_tokens"]
            rows.append([_p(agent, S_TBL_CELL), _p(str(d["llm_calls"]), S_TBL_CELL),
                         _p(str(d["tool_calls"]), S_TBL_CELL), _p(f"{t:,}", S_TBL_CELL),
                         _p(f"${d['cost_usd']:.4f}", S_TBL_CELL)])
        usable = PAGE_W - 2 * MARGIN
        tbl = Table(rows, colWidths=[40*mm, 25*mm, 25*mm, usable-130*mm, 25*mm])
        tbl.setStyle(_std_table_style(len(rows)))
        elems.append(tbl)

    # Source Tier Coverage + EDGAR (shared formatters — match Markdown/HTML).
    if meta is not None:
        parts = tier_coverage_parts(getattr(meta, "tier_attempts", None))
        if parts:
            elems.append(Spacer(1, 2*mm))
            elems.append(_p(f"<b>Source Tier Coverage:</b> {_xml_esc(' · '.join(parts))}", S_BODY_SM))
        eline = edgar_line(getattr(meta, "edgar_lookup_status", None), getattr(meta, "edgar_cik", None))
        if eline:
            label, _, rest = eline.partition(": ")
            elems.append(_p(f"<b>{_xml_esc(label)}:</b> {_xml_esc(rest)}", S_BODY_SM))

    errors = trace_summary.get("errors", [])
    if errors:
        elems.append(_p("Errors During Research", S_H3))
        for e in errors:
            elems.append(_p(f"• [{e['agent']}] {e['error']}", S_BODY_SM))

    return elems


# ── Standalone HTML output ────────────────────────────────────────────────────

_HTML_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:11pt;
     line-height:1.55;color:#1a1a2e;max-width:900px;margin:32px auto;padding:0 24px}
h1{font-size:22pt;color:#1e3a8a;margin:0 0 10px}
/* Title rule sits BELOW the company name with clearance — never across it. */
.title-rule{border:none;border-top:2px solid #2563eb;width:100%;margin:0 0 14px}
.header-meta{margin:0 0 12px}
h2{font-size:14pt;color:#1e3a8a;border-bottom:2px solid #e2e8f0;padding-bottom:4px;margin-top:28px}
h3{font-size:11pt;color:#374151;margin-top:16px;margin-bottom:6px}
table{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:9.5pt}
thead tr{background:#1e3a8a;color:#fff}
/* nowrap keeps the "Confidence" header on one line (no "Confid/ence" fold). */
thead th{padding:6px 10px;text-align:left;font-size:9pt;white-space:nowrap}
tbody tr:nth-child(even){background:#f8fafc}
tbody td{padding:5px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;
         overflow-wrap:anywhere}
/* Overall + section confidence bar — constrained so it never overflows. */
.confbar{width:100%;border-radius:6px;padding:8px 14px;margin:0 0 6px;font-size:9.5pt}
.confbar .overall{font-weight:700}
.confbar .sections{color:#475569;font-weight:400}
.confnote{color:#64748b;font-size:8pt;margin:0 0 14px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:8pt;font-weight:600}
.h{background:#dcfce7;color:#166534}.m{background:#fef9c3;color:#854d0e}
.l{background:#fee2e2;color:#991b1b}.u{background:#f1f5f9;color:#475569}
.sc{background:#dc2626;color:#fff}.sh{background:#ea580c;color:#fff}
.sm{background:#fef9c3;color:#854d0e}.sl{background:#dcfce7;color:#166534}
.disclaimer{box-sizing:border-box;width:100%;background:#fffbeb;border:1px solid #fcd34d;
            border-left:4px solid #d97706;border-radius:6px;padding:10px 14px;margin:0 0 16px;
            font-size:9pt;color:#713f12}
.disclaimer .dtitle{font-weight:700;text-transform:uppercase;letter-spacing:.03em;
                    color:#92400e;margin-bottom:4px}
.disclaimer p{margin:3px 0}
.narrative{margin:8px 0 14px}
hr{border:none;border-top:1px solid #e2e8f0;margin:24px 0}
.meta{color:#64748b;font-size:9pt}
"""

def _esc(s):
    # Normalise dashes (house style) then HTML-escape. URL-safe: em/en dashes
    # never occur in URLs, so escaping href values through here is harmless.
    s = strip_dashes(s)
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _hconf(c):
    cls = {"high":"h","medium":"m","low":"l","unknown":"u"}.get(c,"u")
    lbl = {"high":"HIGH","medium":"MED","low":"LOW","unknown":"N/A"}.get(c,c.upper())
    return f'<span class="badge {cls}">{lbl}</span>'

def _hsev(s):
    cls = {"critical":"sc","high":"sh","medium":"sm","low":"sl"}.get(s,"u")
    lbl = {"critical":"CRITICAL","high":"HIGH","medium":"MED","low":"LOW"}.get(s,s.upper() if s else "—")
    return f'<span class="badge {cls}">{lbl}</span>'

def _hsrc(sources):
    """Render every source as a clickable HTML link with a clean domain label."""
    parts = [f'<a href="{_esc(u)}" target="_blank" rel="noopener">{_esc(_clean_source_label(u))}</a>'
             for u in (sources or []) if u]
    return ", ".join(parts) if parts else "-"

def _html_table(headers, items, builders):
    if not items:
        return ""
    ths = "".join(f"<th>{h}</th>" for h in headers)
    rows = ""
    for dp in items:
        cells = "".join(f"<td>{fn(dp)}</td>" for fn in builders)
        rows += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table>"


def _render_html(company, now, cost, duration, report_score,
                 research_data, financial_data, risk_data, social_media_data, synthesis_data,
                 trace_summary, gaps=None, sec_confs=None, meta=None):
    def kv_html(fields, data):
        rows = ""
        for label, key in fields:
            dp = data.get(key, {})
            rows += (f"<tr><td><strong>{_esc(label)}</strong></td>"
                     f"<td>{_esc(dp.get('value','—'))}</td>"
                     f"<td>{_hconf(dp.get('confidence','unknown'))}</td></tr>")
        return f"<table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table>"

    # Attribute list tables: Item, Confidence (no Sources). Full text, no truncation.
    attr = [lambda dp: _esc(dp.get("value","—")),
            lambda dp: _hconf(dp.get("confidence","unknown"))]
    # Evidence tables: Item, Confidence, Sources (clickable).
    evidence = [lambda dp: _esc(dp.get("value","—")),
                lambda dp: _hconf(dp.get("confidence","unknown")),
                lambda dp: _hsrc(dp.get("sources", []))]

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Due Diligence: {_esc(company)}</title><style>{_HTML_CSS}</style></head><body>
<p class="meta">Automated multi-agent research report</p>
<h1>Due Diligence: {_esc(company)}</h1>
<hr class="title-rule">
<p class="meta header-meta">Generated {_esc(now)} &nbsp;·&nbsp; Cost ${cost:.4f} &nbsp;·&nbsp; Duration {duration/1000:.1f}s</p>"""

    if report_score is not None:
        pct = report_score * 100
        if pct >= 75:   bar_bg, bar_fg = "#dcfce7", "#166534"
        elif pct >= 50: bar_bg, bar_fg = "#fef9c3", "#854d0e"
        elif pct >= 25: bar_bg, bar_fg = "#fee2e2", "#991b1b"
        else:           bar_bg, bar_fg = "#f1f5f9", "#475569"
        # All five section confidences in report-section order (display only, from
        # section_confidences). No per-item weight labels — Research and Synthesis
        # are not weighted into Overall, so showing weights would be misleading.
        sec_parts = []
        for lbl, key in [("Research", "research"), ("Financial", "financial"),
                         ("Risk", "risk"), ("Social Media", "social_media"),
                         ("Synthesis", "synthesis")]:
            if sec_confs is not None:
                # Missing section shown honestly as n/a, never a fabricated value.
                sec_parts.append(f"{lbl} {sec_confs[key]*100:.0f}%" if key in sec_confs else f"{lbl} n/a")
        sections_html = ""
        note_html = ""
        if sec_parts:
            sections_html = f'<span class="sections"> &nbsp;·&nbsp; {_esc("  ·  ".join(sec_parts))}</span>'
            note_html = ('<div class="confnote">Overall is a weighted blend of Financial '
                         'and Risk (40% each) and Social Media (20%); Research and Synthesis '
                         'are reported but not weighted.</div>')
        html += (f'<div class="confbar" style="background:{bar_bg};color:{bar_fg}">'
                 f'<span class="overall">Overall Confidence: {pct:.0f}%</span>'
                 f'{sections_html}</div>{note_html}')
    else:
        # Overall is UNDEFINED (no weighted section scorable). Show it honestly in a
        # neutral bar — never 0%, never a blank/omitted line.
        html += (f'<div class="confbar" style="background:#f1f5f9;color:#475569">'
                 f'<span class="overall">Overall Confidence: {_esc(OVERALL_NOT_COMPUTABLE)}</span></div>')

    # ── Prominent disclaimer (static render text) ────────────────────────────
    disc_html = "".join(f"<p>{_esc(s)}</p>" for s in disclaimer_sentences(company))
    html += f'<div class="disclaimer"><div class="dtitle">Disclaimer</div>{disc_html}</div>'

    # ── Group 1: Assessment ──────────────────────────────────────────────────
    if synthesis_data:
        summary = synthesis_data.get("executive_summary", {}).get("value", "")
        html += "<h2>Executive Summary</h2>"
        if summary and summary not in ("unknown",""):
            html += f"<p>{_esc(summary)}</p>"

        for title, key in [("Key Strengths","key_strengths"),("Key Concerns","key_concerns")]:
            items = synthesis_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(["Item","Confidence"], items, attr)

        red_flags = synthesis_data.get("red_flags", [])
        if red_flags:
            # Severity column (and legend) appear ONLY here.
            html += ("<h3>Red Flags</h3>"
                     "<p class='meta'>Severity = risk impact; Confidence = data reliability. "
                     "Sorted by severity (most severe first).</p>")
            html += _html_table(
                ["Red Flag","Severity","Confidence"], _sort_sev(red_flags),
                [lambda dp: _esc(dp.get("value","—")),
                 lambda dp: _hsev(dp.get("severity","")),
                 lambda dp: _hconf(dp.get("confidence","unknown"))])

    # ── Group 2: Data Quality & Open Items ───────────────────────────────────
    if synthesis_data or gaps:
        html += "<hr><h2>Data Quality &amp; Open Items</h2>"
        if synthesis_data:
            dq = synthesis_data.get("data_quality", {})
            if dq.get("value") and dq["value"] not in ("unknown",""):
                html += f"<p><strong>Data Quality:</strong> {_esc(dq['value'].upper())}</p>"
            conflicts = synthesis_data.get("data_conflicts", [])
            if conflicts:
                html += "<h3>Data Conflicts</h3>" + _html_table(["Conflict","Confidence"], conflicts, attr)
        html += "<h3>Information Gaps</h3><ul>"
        if gaps:
            for g in gaps:
                html += f"<li><strong>{_esc(g.field)}</strong> ({_esc(g.agent)}): {_esc(g.reason)}</li>"
        else:
            html += "<li>No critical gaps identified</li>"
        html += "</ul>"
        if synthesis_data:
            fq = synthesis_data.get("follow_up_questions", [])
            if fq:
                html += "<h3>Follow-Up Questions</h3>" + _html_table(
                    ["Question","Why It Matters"], fq,
                    [lambda dp: _esc(dp.get("value","—")),
                     lambda dp: _esc(dp.get("reasoning","") or "—")])

    # ── Group 3: Company Profile ─────────────────────────────────────────────
    if research_data:
        html += "<hr><h2>Company Profile</h2>"
        html += kv_html([("Description","description"),("Founded","founded_year"),
                          ("Headquarters","headquarters"),("Employees","employee_count"),
                          ("Industry","industry"),("Website","website")], research_data)
        for title, key, builders, headers in [
            ("Key Products","key_products",attr,["Item","Confidence"]),
            ("Leadership","key_leadership",attr,["Item","Confidence"]),
            ("Technology Stack","technology_stack",attr,["Item","Confidence"]),
            ("Recent Developments","recent_developments",evidence,["Item","Confidence","Sources"]),
            ("Notable Patents","notable_patents",evidence,["Item","Confidence","Sources"]),
        ]:
            items = research_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(headers, items, builders)

    # ── Group 4: Financial detail ────────────────────────────────────────────
    if financial_data:
        html += "<hr><h2>Financial Profile</h2>"
        html += kv_html([("Revenue","revenue"),("Revenue Growth","revenue_growth"),
                          ("Profitability","profitability"),("Total Funding","total_funding"),
                          ("Last Funding Round","last_funding_round"),("Valuation","valuation"),
                          ("Revenue Model","revenue_model")], financial_data)
        for title, key, builders, headers in [
            ("Key Investors","key_investors",attr,["Item","Confidence"]),
            ("Key Customers","key_customers",attr,["Item","Confidence"]),
            ("Recent Financial Events","recent_financial_events",evidence,["Item","Confidence","Sources"]),
        ]:
            items = financial_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(headers, items, builders)

    # ── Group 5: Risk detail (Financial Risks grouped here too) ──────────────
    if risk_data:
        overall = risk_data.get("overall_risk_rating", {})
        html += (f"<hr><h2>Risk Assessment</h2>"
                 f"<p><strong>Overall Risk Rating:</strong> {_esc(overall.get('value','—').upper())} "
                 f"{_hconf(overall.get('confidence','unknown'))}</p>")
        summary = risk_data.get("risk_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            html += f'<p class="narrative">{_esc(summary["value"])}</p>'
        # Risk sub-tables are evidence tables: Item, Confidence, Sources (no Severity).
        for title, key in [("Regulatory Risks","regulatory_risks"),("Legal Risks","legal_risks"),
                             ("Cybersecurity Risks","cybersecurity_risks"),("Operational Risks","operational_risks"),
                             ("Reputational Risks","reputational_risks"),("ESG Risks","esg_risks"),
                             ("Pending Litigation","pending_litigation")]:
            items = risk_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(
                    ["Risk","Confidence","Sources"], _sort_sev(items), evidence)
        if financial_data:
            fr = financial_data.get("financial_risks", [])
            if fr:
                html += "<h3>Financial Risks</h3>" + _html_table(
                    ["Risk","Confidence","Sources"], fr, evidence)
        nfc = risk_data.get("notable_federal_contracts", [])
        if nfc:
            html += "<h3>Notable Federal Contracts</h3>" + _html_table(
                ["Contract","Confidence","Sources"], nfc, evidence)

    # ── Group 6: Social & sentiment ──────────────────────────────────────────
    if social_media_data:
        sentiment = social_media_data.get("overall_sentiment", {})
        html += (f"<hr><h2>Social Media &amp; Sentiment</h2>"
                 f"<p><strong>Sentiment:</strong> {_esc(sentiment.get('value','—').upper())} "
                 f"{_hconf(sentiment.get('confidence','unknown'))}</p>")
        html += kv_html([("Twitter/X","twitter_presence"),("LinkedIn","linkedin_presence"),
                          ("Reddit","reddit_sentiment"),("Glassdoor","glassdoor_rating")], social_media_data)
        for title, key in [("Notable Mentions","notable_mentions"),("Trending Topics","trending_topics"),
                             ("Customer Complaints","customer_complaints"),("Positive Signals","positive_signals")]:
            items = social_media_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(["Item","Confidence","Sources"], items, evidence)

    # ── Group 7: Methodology (full — matches Markdown/PDF) ────────────────────
    html += "<hr><h2>Methodology</h2>"
    by_agent = trace_summary.get("by_agent", {})
    agents_str = ", ".join(by_agent.keys()) if by_agent else "agents"
    m_tokens = trace_summary.get("total_input_tokens", 0) + trace_summary.get("total_output_tokens", 0)
    m_dur = trace_summary.get("total_duration_ms", 0) / 1000
    m_cost = trace_summary.get("total_cost_usd", 0)
    # Totals line
    html += (f"<p class='meta'>This report was generated by the {_esc(agents_str)} agents, making "
             f"{trace_summary.get('total_llm_calls',0)} LLM calls and "
             f"{trace_summary.get('total_tool_calls',0)} tool invocations in "
             f"{m_dur:.1f} seconds at a cost of ${m_cost:.4f} "
             f"({m_tokens:,} tokens).</p>")
    # Per-agent table
    if by_agent:
        rows = ""
        for agent, d in by_agent.items():
            t = d["input_tokens"] + d["output_tokens"]
            rows += (f"<tr><td>{_esc(agent)}</td><td>{d['llm_calls']}</td>"
                     f"<td>{d['tool_calls']}</td><td>{t:,}</td>"
                     f"<td>${d['cost_usd']:.4f}</td></tr>")
        html += ("<table><thead><tr><th>Agent</th><th>LLM Calls</th><th>Tool Calls</th>"
                 f"<th>Tokens</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table>")
    # Source Tier Coverage + EDGAR (shared formatters)
    if meta is not None:
        parts = tier_coverage_parts(getattr(meta, "tier_attempts", None))
        if parts:
            html += f"<p class='meta'><strong>Source Tier Coverage:</strong> {_esc(' · '.join(parts))}</p>"
        eline = edgar_line(getattr(meta, "edgar_lookup_status", None), getattr(meta, "edgar_cik", None))
        if eline:
            label, _, rest = eline.partition(": ")
            html += f"<p class='meta'><strong>{_esc(label)}:</strong> {_esc(rest)}</p>"

    html += "</body></html>"
    # House style: normalise em/en/etc. dashes to a single hyphen across the whole
    # document (covers CSS comments and any authored literal). URL-safe — these
    # glyphs never appear in URLs.
    return strip_dashes(html)


# ── Main entry points ─────────────────────────────────────────────────────────

def render_pdf_report_from_doc(
    doc: ReportDocument,
    output_dir: str = "outputs",
) -> tuple[str, str]:
    """Render a canonical ReportDocument as a styled PDF and companion HTML.

    This is the primary entry point. The old dict-based render_pdf_report() is a
    deprecated shim that assembles a ReportDocument and calls this function.
    Returns (html_path, pdf_path).
    """
    research_data, financial_data, risk_data, social_media_data, synthesis_data, trace_summary = (
        build_render_dicts(doc)
    )
    m = doc.run_metadata
    now = format_generated_et(doc.generated_at)
    company = doc.company_name
    cost = m.cost_usd
    duration = m.duration_ms
    # Read from run_metadata (computed at assembly time); fall back for legacy docs.
    sc = doc.run_metadata.section_confidences
    oc = doc.run_metadata.overall_confidence
    if not sc:
        import warnings as _w
        _w.warn("run_metadata.section_confidences is empty — falling back to on-the-fly computation")
        report_score = _report_confidence(financial_data, risk_data, social_media_data)
        _sec_confs = None  # signal _cover to compute per-section itself
    else:
        report_score = oc / 100.0 if oc is not None else None
        _sec_confs = {k: v / 100.0 for k, v in sc.items()}

    os.makedirs(output_dir, exist_ok=True)
    slug = company.lower().replace(" ", "_").replace(".", "")
    pdf_path = os.path.join(output_dir, f"report_{slug}.pdf")
    html_path = os.path.join(output_dir, f"report_{slug}.html")

    def _header_footer(canvas, rl_doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(C_SLATE)
        canvas.drawString(MARGIN, 12*mm, f"Due Diligence Report: {company}")
        canvas.drawRightString(PAGE_W - MARGIN, 12*mm, f"Page {rl_doc.page}  ·  {now}")
        canvas.setStrokeColor(C_TBL_BORDER)
        canvas.line(MARGIN, 14*mm, PAGE_W - MARGIN, 14*mm)
        canvas.restoreState()

    rl_doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=20*mm,
    )

    elems = []
    elems += _cover(company, now, cost, duration, report_score, financial_data, risk_data, social_media_data, _sec_confs)
    # Prominent disclaimer directly under the header/confidence bar.
    elems += _disclaimer_flowables(company)
    # 1. Assessment
    if synthesis_data:
        elems += _assessment_section(synthesis_data)
    # 2. Data quality & open items (Information Gaps moved up here)
    if synthesis_data or doc.gaps:
        elems += _open_items_section(synthesis_data, doc.gaps)
    # 3. Company profile
    if research_data:
        elems += _research_section(research_data)
    # 4. Financial detail
    if financial_data:
        elems += _financial_section(financial_data)
    # 5. Risk detail (Financial Risks rendered here too)
    if risk_data:
        elems += _risk_section(risk_data, financial_data)
    # 6. Social & sentiment
    if social_media_data:
        elems += _social_section(social_media_data)
    # 7. Methodology (per-agent table, totals, tier coverage, EDGAR)
    elems += _methodology_section(trace_summary, m)

    rl_doc.build(elems, onFirstPage=_header_footer, onLaterPages=_header_footer)

    html_content = _render_html(
        company, now, cost, duration, report_score,
        research_data, financial_data, risk_data, social_media_data,
        synthesis_data, trace_summary, doc.gaps, _sec_confs, m,
    )
    with open(html_path, "w") as f:
        f.write(html_content)

    return html_path, pdf_path


def render_pdf_report(
    research_data: Optional[dict],
    trace_summary: dict,
    output_dir: str = "outputs",
    financial_data: Optional[dict] = None,
    risk_data: Optional[dict] = None,
    social_media_data: Optional[dict] = None,
    synthesis_data: Optional[dict] = None,
) -> tuple[str, str]:
    """Deprecated shim — assemble a ReportDocument and call render_pdf_report_from_doc.

    TODO(P2): remove this shim once all callers pass a ReportDocument directly.
    Returns (html_path, pdf_path).
    """
    doc = assemble_report(
        research_data, financial_data, risk_data, social_media_data, synthesis_data, trace_summary
    )
    return render_pdf_report_from_doc(doc, output_dir)
