"""
PDF report generator — produces a professional due diligence PDF using ReportLab.

Uses ReportLab Platypus (pure Python, no system dependencies).
Also writes a standalone .html file for browser viewing.
"""

import os
from datetime import datetime
from typing import Optional

from src.schemas.models import ReportDocument
from src.synthesis.assembler import assemble_report, build_render_dicts

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
    }.get(sev, (C_GRAY_BG, C_GRAY_FG, sev.upper() if sev else "—"))


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
    col_widths = col_widths or [32*mm, usable - 32*mm - 14*mm, 14*mm]
    rows = []
    for label, value, conf in rows_data:
        rows.append([
            _p(label, S_BOLD),
            _p(_trunc(str(value)), S_TBL_CELL),
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


def _auto_widths(n_cols, usable):
    badge_w = 16 * mm
    src_w   = 30 * mm
    if n_cols == 2:
        return [usable - 14*mm, 14*mm]
    if n_cols == 3:
        return [usable - 14*mm - src_w, 14*mm, src_w]
    if n_cols == 4:
        # item | sev | conf | src
        return [usable - 16*mm - 14*mm - src_w, 16*mm, 14*mm, src_w]
    return [usable / n_cols] * n_cols


# ── Section builders ─────────────────────────────────────────────────────────

def _cover(company, now, cost, duration, report_score, financial_data, risk_data, social_data,
           sec_confs=None):
    """sec_confs: optional dict mapping "financial"/"risk"/"social_media" → 0.0-1.0 fraction."""
    elems = []
    elems.append(Spacer(1, 2*mm))
    elems.append(_p(f"Due Diligence Report", S_SUBTITLE))
    elems.append(_p(company, S_TITLE))
    elems.append(HRFlowable(width="100%", thickness=2, color=C_BLUE, spaceAfter=6))
    elems.append(_p(f"Generated {now}  ·  Cost ${cost:.4f}  ·  Duration {duration/1000:.1f}s", S_META))
    elems.append(Spacer(1, 4*mm))

    if report_score is not None:
        pct = report_score * 100
        if pct >= 75:   badge_bg, badge_fg = C_GREEN_BG,  C_GREEN_FG
        elif pct >= 50: badge_bg, badge_fg = C_YELLOW_BG, C_YELLOW_FG
        elif pct >= 25: badge_bg, badge_fg = C_RED_BG,    C_RED_FG
        else:           badge_bg, badge_fg = C_GRAY_BG,   C_GRAY_FG

        score_label = _p(f"Overall Report Confidence: {pct:.0f}%",
                         ParagraphStyle("sc", fontSize=9, fontName="Helvetica-Bold",
                                        textColor=badge_fg))
        score_row = [[score_label]]
        for lbl, key, data, w in [("Financial",   "financial",    financial_data, "40%"),
                                    ("Risk",        "risk",         risk_data,      "40%"),
                                    ("Social Media","social_media", social_data,    "20%")]:
            if sec_confs and key in sec_confs:
                sec = sec_confs[key] * 100
                score_row[0].append(_p(f"{lbl} ({w}): {sec:.0f}%", S_BODY_SM))
            elif data:
                sec = _section_confidence(data) * 100
                score_row[0].append(_p(f"{lbl} ({w}): {sec:.0f}%", S_BODY_SM))
            else:
                score_row[0].append(_p(f"{lbl} ({w}): —", S_BODY_SM))

        conf_tbl = Table(score_row, colWidths=None)
        conf_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), badge_bg),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        elems.append(conf_tbl)
        elems.append(Spacer(1, 4*mm))

    return elems


_REC_MAP = {
    "strong_proceed": (C_REC_PROCEED_BG, C_REC_PROCEED_BAR, "Strong Proceed"),
    "proceed":        (C_REC_PROCEED_BG, C_REC_PROCEED_BAR, "Proceed"),
    "proceed_with_conditions": (C_REC_COND_BG, C_REC_COND_BAR, "Proceed with Conditions"),
    "caution":        (C_REC_CAUTION_BG, C_REC_CAUTION_BAR, "Caution"),
    "do_not_proceed": (C_REC_HALT_BG,   C_REC_HALT_BAR,   "Do Not Proceed"),
}


def _exec_summary(synthesis_data):
    elems = [_p("Executive Summary", S_H2)]

    rec      = synthesis_data.get("investment_recommendation", {})
    val      = rec.get("value", "unknown").lower()
    rec_conf = rec.get("confidence", "unknown")
    bg, bar, label = _REC_MAP.get(val, (C_REC_UNKNOWN_BG, C_REC_UNKNOWN_BAR,
                                        val.replace("_", " ").title()))

    _, fg, conf_lbl = _conf_colors(rec_conf)
    rec_inner = [
        [_p("INVESTMENT RECOMMENDATION", S_REC_LABEL)],
        [_p(label, S_REC_VALUE)],
    ]

    rationale = synthesis_data.get("recommendation_rationale", {}).get("value", "")
    summary   = synthesis_data.get("executive_summary", {}).get("value", "")
    if rationale and rationale not in ("unknown", ""):
        rec_inner.append([_p(rationale, S_REC_BODY)])
    if summary and summary not in ("unknown", ""):
        rec_inner.append([_p(summary, ParagraphStyle("sum", fontSize=9, fontName="Helvetica-Oblique",
                                                      textColor=colors.HexColor("#4b5563"), leading=13))])

    rec_tbl = Table(rec_inner, colWidths=[PAGE_W - 2*MARGIN - 4*mm])
    rec_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), bg),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE",    (0, 0), (0, -1), 4, bar),
    ]))
    elems.append(rec_tbl)
    elems.append(Spacer(1, 4*mm))

    usable = PAGE_W - 2 * MARGIN
    synth_builders = [
        lambda dp: _trunc(dp.get("value", "—"), 150),
        lambda dp: _conf_cell(dp.get("confidence", "unknown")),
        lambda dp: _trunc(", ".join(dp.get("sources", [])[:4]) or "—", 40),
    ]
    synth_widths = [usable - 14*mm - 30*mm, 14*mm, 30*mm]

    for title, key in [("Key Strengths", "key_strengths"), ("Key Concerns", "key_concerns")]:
        items = synthesis_data.get(key, [])
        if items:
            elems.append(KeepTogether([
                _p(title, S_H3),
                *_list_table(items, ["Item", "Confidence", "Agents"], synth_builders, synth_widths),
            ]))

    red_flags = synthesis_data.get("red_flags", [])
    if red_flags:
        flag_builders = [
            lambda dp: _trunc(dp.get("value", "—"), 130),
            lambda dp: _sev_cell(dp.get("severity", "")),
            lambda dp: _conf_cell(dp.get("confidence", "unknown")),
            lambda dp: _trunc(", ".join(dp.get("sources", [])[:4]) or "—", 30),
        ]
        flag_widths = [usable - 16*mm - 14*mm - 28*mm, 16*mm, 14*mm, 28*mm]
        elems.append(KeepTogether([
            _p("Red Flags", S_H3),
            *_list_table(_sort_sev(red_flags), ["Red Flag", "Severity", "Confidence", "Agents"],
                         flag_builders, flag_widths),
        ]))

    conflicts = synthesis_data.get("data_conflicts", [])
    if conflicts:
        c_builders = [
            lambda dp: _trunc(dp.get("value", "—"), 150),
            lambda dp: _trunc(" vs ".join(dp.get("sources", [])[:2]) or "—", 40),
            lambda dp: _conf_cell(dp.get("confidence", "unknown")),
        ]
        elems.append(KeepTogether([
            _p("Data Conflicts", S_H3),
            *_list_table(conflicts, ["Conflict", "Agents", "Confidence"], c_builders),
        ]))

    fq = synthesis_data.get("follow_up_questions", [])
    if fq:
        fq_widths = [(usable)*0.6, (usable)*0.4]
        fq_builders = [
            lambda dp: _trunc(dp.get("value", "—"), 160),
            lambda dp: _trunc(dp.get("reasoning", "") or "—", 100),
        ]
        elems.append(KeepTogether([
            _p("Follow-Up Questions", S_H3),
            *_list_table(fq, ["Question", "Why It Matters"], fq_builders, fq_widths),
        ]))

    dq = synthesis_data.get("data_quality", {})
    if dq.get("value") and dq["value"] not in ("unknown", ""):
        elems.append(_p(f"Data Quality: {dq['value'].upper()}", S_BODY_SM))

    return elems


def _research_section(data):
    elems = [_hr(), _p("Company Overview", S_H2)]
    kv = []
    for label, key in [("Description", "description"), ("Founded", "founded_year"),
                        ("Headquarters", "headquarters"), ("Employees", "employee_count"),
                        ("Industry", "industry"), ("Website", "website")]:
        dp = data.get(key, {})
        kv.append((label, dp.get("value", "—"), dp.get("confidence", "unknown")))
    elems.append(_kv_table(kv))
    elems.append(Spacer(1, 4*mm))

    std = [lambda dp: _trunc(dp.get("value", "—")),
           lambda dp: _conf_cell(dp.get("confidence", "unknown"))]
    std_src = std + [lambda dp: _trunc(", ".join(dp.get("sources", [])[:2]) or "—", 50)]
    usable = PAGE_W - 2 * MARGIN

    for title, key, builders, headers in [
        ("Key Products",       "key_products",       std,     ["Product", "Confidence"]),
        ("Leadership",         "key_leadership",     std,     ["Name / Title", "Confidence"]),
        ("Technology Stack",   "technology_stack",   std,     ["Technology", "Confidence"]),
        ("Recent Developments","recent_developments",std_src, ["Development", "Confidence", "Sources"]),
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

    std = [lambda dp: _trunc(dp.get("value", "—")),
           lambda dp: _conf_cell(dp.get("confidence", "unknown"))]
    std_src = std + [lambda dp: _trunc(", ".join(dp.get("sources", [])[:2]) or "—", 50)]

    for title, key, builders, headers in [
        ("Key Investors",          "key_investors",          std,     ["Investor", "Confidence"]),
        ("Key Customers",          "key_customers",          std,     ["Customer", "Confidence"]),
        ("Financial Risks",        "financial_risks",        std_src, ["Risk", "Confidence", "Sources"]),
        ("Recent Financial Events","recent_financial_events",std_src, ["Event", "Confidence", "Sources"]),
    ]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(items, headers, builders)]))
    return elems


def _risk_section(data):
    elems = [_hr(), _p("Risk Assessment", S_H2)]

    overall = data.get("overall_risk_rating", {})
    rating  = overall.get("value", "unknown").upper()
    conf    = overall.get("confidence", "unknown")
    bg, fg, lbl = _conf_colors(conf)
    elems.append(_p(f"Overall Risk Rating: <b>{rating}</b>  [{lbl} confidence]", S_BODY))

    summary = data.get("risk_summary", {})
    if summary.get("value") and summary["value"] != "unknown":
        warn_tbl = Table([[_p(summary["value"], S_REC_BODY)]],
                         colWidths=[PAGE_W - 2*MARGIN - 4*mm])
        warn_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#fff7ed")),
            ("LINEBEFORE",  (0,0), (0,-1), 3, C_ORANGE),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING",(0,0), (-1,-1), 8),
            ("TOPPADDING",  (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ]))
        elems.append(Spacer(1, 3*mm))
        elems.append(warn_tbl)

    elems.append(Spacer(1, 3*mm))
    elems.append(_p("Severity = risk impact  ·  Confidence = data reliability  ·  Sorted by severity",
                     S_BODY_SM))
    elems.append(Spacer(1, 2*mm))

    usable = PAGE_W - 2 * MARGIN
    risk_builders = [
        lambda dp: _trunc(dp.get("value", "—"), 110),
        lambda dp: _sev_cell(dp.get("severity", "")),
        lambda dp: _conf_cell(dp.get("confidence", "unknown")),
        lambda dp: _trunc(", ".join(dp.get("sources", [])[:2]) or "—", 45),
    ]
    risk_widths = [usable - 16*mm - 14*mm - 35*mm, 16*mm, 14*mm, 35*mm]
    risk_headers = ["Risk", "Severity", "Confidence", "Sources"]

    for title, key in [
        ("Regulatory Risks", "regulatory_risks"), ("Legal Risks", "legal_risks"),
        ("Cybersecurity Risks", "cybersecurity_risks"), ("Operational Risks", "operational_risks"),
        ("Reputational Risks", "reputational_risks"), ("ESG Risks", "esg_risks"),
        ("Pending Litigation", "pending_litigation"),
    ]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(_sort_sev(items), risk_headers,
                                                    risk_builders, risk_widths)]))
    return elems


def _social_section(data):
    elems = [_hr(), _p("Social Media & Sentiment", S_H2)]

    sentiment = data.get("overall_sentiment", {})
    elems.append(_p(f"Overall Sentiment: <b>{sentiment.get('value', '—').upper()}</b>", S_BODY))

    summary = data.get("sentiment_summary", {})
    if summary.get("value") and summary["value"] != "unknown":
        info_tbl = Table([[_p(summary["value"], S_REC_BODY)]],
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

    std_src = [lambda dp: _trunc(dp.get("value", "—")),
               lambda dp: _conf_cell(dp.get("confidence", "unknown")),
               lambda dp: _trunc(", ".join(dp.get("sources", [])[:2]) or "—", 45)]

    for title, key in [("Notable Mentions", "notable_mentions"), ("Trending Topics", "trending_topics"),
                        ("Customer Complaints", "customer_complaints"), ("Positive Signals", "positive_signals")]:
        items = data.get(key, [])
        if items:
            elems.append(KeepTogether([_p(title, S_H3),
                                       *_list_table(items, ["Item", "Confidence", "Sources"], std_src)]))
    return elems


def _gaps_section(research, financial, risk, social):
    elems = [_hr(), _p("Information Gaps", S_H2)]
    bullets = []
    for label, data in [("Research", research), ("Financial", financial),
                         ("Risk", risk), ("Social Media", social)]:
        if not data:
            continue
        for key, dp in data.items():
            if isinstance(dp, dict) and dp.get("confidence") == "unknown":
                bullets.append(f"<b>{key}</b> ({label}): No reliable data found")
    if not bullets:
        bullets = ["No critical gaps identified"]
    for b in bullets:
        elems.append(_p(f"• {b}", S_BODY))
    return elems


def _methodology_section(trace_summary):
    cost       = trace_summary.get("total_cost_usd", 0)
    tokens     = trace_summary.get("total_input_tokens", 0) + trace_summary.get("total_output_tokens", 0)
    llm_calls  = trace_summary.get("total_llm_calls", 0)
    tool_calls = trace_summary.get("total_tool_calls", 0)
    duration   = trace_summary.get("total_duration_ms", 0)

    elems = [
        _hr(),
        _p("Methodology", S_H2),
        _p(f"Generated by multi-agent pipeline — {llm_calls} LLM calls, "
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

    errors = trace_summary.get("errors", [])
    if errors:
        elems.append(_p("Errors During Research", S_H3))
        for e in errors:
            elems.append(_p(f"• [{e['agent']}] {e['error']}", S_BODY_SM))

    return elems


# ── Standalone HTML output ────────────────────────────────────────────────────

_HTML_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:11pt;
     line-height:1.55;color:#1a1a2e;max-width:900px;margin:32px auto;padding:0 24px}
h1{font-size:22pt;color:#1e3a8a;margin-bottom:4px}
h2{font-size:14pt;color:#1e3a8a;border-bottom:2px solid #e2e8f0;padding-bottom:4px;margin-top:28px}
h3{font-size:11pt;color:#374151;margin-top:16px;margin-bottom:6px}
table{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:9.5pt}
thead tr{background:#1e3a8a;color:#fff}
thead th{padding:6px 10px;text-align:left;font-size:9pt}
tbody tr:nth-child(even){background:#f8fafc}
tbody td{padding:5px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:8pt;font-weight:600}
.h{background:#dcfce7;color:#166534}.m{background:#fef9c3;color:#854d0e}
.l{background:#fee2e2;color:#991b1b}.u{background:#f1f5f9;color:#475569}
.sc{background:#dc2626;color:#fff}.sh{background:#ea580c;color:#fff}
.sm{background:#fef9c3;color:#854d0e}.sl{background:#dcfce7;color:#166534}
.rec{border-radius:6px;padding:14px 18px;margin:12px 0}
.rec-proceed{background:#f0fdf4;border-left:4px solid #16a34a}
.rec-caution{background:#fff7ed;border-left:4px solid #ea580c}
.rec-cond{background:#fefce8;border-left:4px solid #ca8a04}
.rec-halt{background:#fef2f2;border-left:4px solid #dc2626}
.rec-unk{background:#f8fafc;border-left:4px solid #94a3b8}
.rec-label{font-size:8pt;font-weight:700;text-transform:uppercase;color:#6b7280}
.rec-value{font-size:13pt;font-weight:700;margin:4px 0}
hr{border:none;border-top:1px solid #e2e8f0;margin:24px 0}
.meta{color:#64748b;font-size:9pt}
"""

_REC_HTML_CLASS = {
    "strong_proceed": ("rec-proceed", "Strong Proceed"),
    "proceed":        ("rec-proceed", "Proceed"),
    "proceed_with_conditions": ("rec-cond", "Proceed with Conditions"),
    "caution":        ("rec-caution", "Caution"),
    "do_not_proceed": ("rec-halt",  "Do Not Proceed"),
}

def _esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _hconf(c):
    cls = {"high":"h","medium":"m","low":"l","unknown":"u"}.get(c,"u")
    lbl = {"high":"HIGH","medium":"MED","low":"LOW","unknown":"N/A"}.get(c,c.upper())
    return f'<span class="badge {cls}">{lbl}</span>'

def _hsev(s):
    cls = {"critical":"sc","high":"sh","medium":"sm","low":"sl"}.get(s,"u")
    lbl = {"critical":"CRITICAL","high":"HIGH","medium":"MED","low":"LOW"}.get(s,s.upper() if s else "—")
    return f'<span class="badge {cls}">{lbl}</span>'

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
                 trace_summary):
    def kv_html(fields, data):
        rows = ""
        for label, key in fields:
            dp = data.get(key, {})
            rows += (f"<tr><td><strong>{label}</strong></td>"
                     f"<td>{_esc(_trunc(dp.get('value','—')))}</td>"
                     f"<td>{_hconf(dp.get('confidence','unknown'))}</td></tr>")
        return f"<table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table>"

    std = [lambda dp: _esc(_trunc(dp.get("value","—"))),
           lambda dp: _hconf(dp.get("confidence","unknown"))]
    std_src = std + [lambda dp: _esc(_trunc(", ".join(dp.get("sources",[])[:2]) or "—", 60))]

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Due Diligence: {_esc(company)}</title><style>{_HTML_CSS}</style></head><body>
<p class="meta">Automated multi-agent research report</p>
<h1>Due Diligence: {_esc(company)}</h1>
<p class="meta">Generated {_esc(now)} &nbsp;·&nbsp; Cost ${cost:.4f} &nbsp;·&nbsp; Duration {duration/1000:.1f}s"""

    if report_score is not None:
        pct = report_score * 100
        html += f" &nbsp;·&nbsp; Overall Confidence: <strong>{pct:.0f}%</strong>"
    html += "</p>"

    if synthesis_data:
        rec = synthesis_data.get("investment_recommendation", {})
        val = rec.get("value", "unknown").lower()
        rec_cls, rec_lbl = _REC_HTML_CLASS.get(val, ("rec-unk", val.replace("_"," ").title()))
        rationale = synthesis_data.get("recommendation_rationale", {}).get("value", "")
        summary   = synthesis_data.get("executive_summary", {}).get("value", "")
        html += f"<h2>Executive Summary</h2>"
        html += f'<div class="rec {rec_cls}"><div class="rec-label">Investment Recommendation</div>'
        html += f'<div class="rec-value">{_esc(rec_lbl)}</div>'
        if rationale and rationale not in ("unknown",""):
            html += f"<p>{_esc(rationale)}</p>"
        if summary and summary not in ("unknown",""):
            html += f"<p><em>{_esc(summary)}</em></p>"
        html += "</div>"

        for title, key in [("Key Strengths","key_strengths"),("Key Concerns","key_concerns")]:
            items = synthesis_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(
                    ["Item","Confidence","Agents"], items,
                    [lambda dp: _esc(_trunc(dp.get("value","—"),150)),
                     lambda dp: _hconf(dp.get("confidence","unknown")),
                     lambda dp: _esc(", ".join(dp.get("sources",[])[:4]) or "—")])

        red_flags = synthesis_data.get("red_flags", [])
        if red_flags:
            html += "<h3>Red Flags</h3>" + _html_table(
                ["Red Flag","Severity","Confidence","Agents"], _sort_sev(red_flags),
                [lambda dp: _esc(_trunc(dp.get("value","—"),150)),
                 lambda dp: _hsev(dp.get("severity","")),
                 lambda dp: _hconf(dp.get("confidence","unknown")),
                 lambda dp: _esc(", ".join(dp.get("sources",[])[:4]) or "—")])

    if research_data:
        html += "<hr><h2>Company Overview</h2>"
        html += kv_html([("Description","description"),("Founded","founded_year"),
                          ("Headquarters","headquarters"),("Employees","employee_count"),
                          ("Industry","industry"),("Website","website")], research_data)
        for title, key in [("Key Products","key_products"),("Leadership","key_leadership"),
                             ("Technology Stack","technology_stack"),("Recent Developments","recent_developments")]:
            items = research_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(["Item","Confidence"], items, std)

    if financial_data:
        html += "<hr><h2>Financial Profile</h2>"
        html += kv_html([("Revenue","revenue"),("Revenue Growth","revenue_growth"),
                          ("Profitability","profitability"),("Total Funding","total_funding"),
                          ("Last Funding Round","last_funding_round"),("Valuation","valuation"),
                          ("Revenue Model","revenue_model")], financial_data)
        for title, key in [("Key Investors","key_investors"),("Key Customers","key_customers"),
                             ("Financial Risks","financial_risks"),("Recent Financial Events","recent_financial_events")]:
            items = financial_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(["Item","Confidence"], items, std)

    if risk_data:
        overall = risk_data.get("overall_risk_rating", {})
        html += (f"<hr><h2>Risk Assessment</h2>"
                 f"<p><strong>Overall Risk Rating:</strong> {_esc(overall.get('value','—').upper())} "
                 f"{_hconf(overall.get('confidence','unknown'))}</p>")
        summary = risk_data.get("risk_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            html += f'<div class="rec rec-caution"><p>{_esc(summary["value"])}</p></div>'
        risk_builders = [lambda dp: _esc(_trunc(dp.get("value","—"),110)),
                         lambda dp: _hsev(dp.get("severity","")),
                         lambda dp: _hconf(dp.get("confidence","unknown")),
                         lambda dp: _esc(", ".join(dp.get("sources",[])[:2]) or "—")]
        for title, key in [("Regulatory Risks","regulatory_risks"),("Legal Risks","legal_risks"),
                             ("Cybersecurity Risks","cybersecurity_risks"),("Operational Risks","operational_risks"),
                             ("Reputational Risks","reputational_risks"),("ESG Risks","esg_risks"),
                             ("Pending Litigation","pending_litigation")]:
            items = risk_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(
                    ["Risk","Severity","Confidence","Sources"], _sort_sev(items), risk_builders)

    if social_media_data:
        sentiment = social_media_data.get("overall_sentiment", {})
        html += (f"<hr><h2>Social Media &amp; Sentiment</h2>"
                 f"<p><strong>Sentiment:</strong> {_esc(sentiment.get('value','—').upper())} "
                 f"{_hconf(sentiment.get('confidence','unknown'))}</p>")
        for title, key in [("Twitter/X","twitter_presence"),("LinkedIn","linkedin_presence"),
                             ("Reddit","reddit_sentiment"),("Glassdoor","glassdoor_rating")]:
            dp = social_media_data.get(key, {})
            html += f"<p><strong>{title}:</strong> {_esc(_trunc(dp.get('value','—')))} {_hconf(dp.get('confidence','unknown'))}</p>"
        for title, key in [("Notable Mentions","notable_mentions"),("Trending Topics","trending_topics"),
                             ("Customer Complaints","customer_complaints"),("Positive Signals","positive_signals")]:
            items = social_media_data.get(key, [])
            if items:
                html += f"<h3>{title}</h3>" + _html_table(["Item","Confidence"], items, std)

    html += "<hr><h2>Methodology</h2>"
    by_agent = trace_summary.get("by_agent", {})
    agents_str = ", ".join(by_agent.keys()) if by_agent else "agents"
    html += (f"<p class='meta'>Generated by {_esc(agents_str)} — "
             f"{trace_summary.get('total_llm_calls',0)} LLM calls, "
             f"{trace_summary.get('total_tool_calls',0)} tool invocations.</p>")

    html += "</body></html>"
    return html


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
    now = doc.generated_at.strftime("%Y-%m-%d %H:%M")
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
    if synthesis_data:
        elems += _exec_summary(synthesis_data)
    if research_data:
        elems += _research_section(research_data)
    if financial_data:
        elems += _financial_section(financial_data)
    if risk_data:
        elems += _risk_section(risk_data)
    if social_media_data:
        elems += _social_section(social_media_data)
    elems += _gaps_section(research_data, financial_data, risk_data, social_media_data)
    elems += _methodology_section(trace_summary)

    rl_doc.build(elems, onFirstPage=_header_footer, onLaterPages=_header_footer)

    html_content = _render_html(
        company, now, cost, duration, report_score,
        research_data, financial_data, risk_data, social_media_data,
        synthesis_data, trace_summary,
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
