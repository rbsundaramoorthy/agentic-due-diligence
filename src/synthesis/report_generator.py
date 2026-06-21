"""
Report generator — renders a canonical ReportDocument as a markdown report.

Primary entry point: render_report_from_doc(doc, output_dir)

The legacy dict-based render_report(...) is a deprecated shim that assembles
a ReportDocument from raw agent dicts and delegates to render_report_from_doc.
"""

import os
from typing import Optional
from urllib.parse import urlparse

from src.schemas.models import Claim, ConfidenceLevel, ReportDocument, SeverityLevel
from src.synthesis.assembler import assemble_report, build_render_dicts
from src.synthesis.render_common import (
    disclaimer_sentences,
    edgar_line,
    strip_dashes,
    tier_coverage_parts,
)


def _clean_source_label(url: str) -> str:
    """Return a clean, human-readable label for a source URL (domain or 'Source')."""
    try:
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
        return netloc or "Source"
    except Exception:
        return "Source"


def _source_links_md(sources: list) -> str:
    """Render every source as a clickable markdown link with a clean label.

    Renders all sources (never truncates to N), each as [domain](url). Returns
    an em-dash when there are no usable sources.
    """
    links = [f"[{_clean_source_label(u)}]({u})" for u in (sources or []) if u]
    return ", ".join(links) if links else "—"


def _md_cell(text: str) -> str:
    """Make a string safe for a markdown table cell without losing content.

    Escapes pipes and collapses newlines (markdown rows are single-line) so no
    cell ever breaks the table. Does NOT truncate — full content is preserved
    and wraps when the markdown is rendered.
    """
    s = str(text).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()
    return s or "—"


def _confidence_badge(conf: str) -> str:
    """Render a confidence level as an emoji badge for the report."""
    badges = {
        "high": "🟢 HIGH",
        "medium": "🟡 MEDIUM",
        "low": "🔴 LOW",
        "unknown": "⚫ UNKNOWN",
    }
    return badges.get(conf, f"❓ {conf}")


def _severity_badge(severity: str) -> str:
    """Render a severity level as an emoji badge for the report."""
    badges = {
        "critical": "🔴 CRITICAL",
        "high": "🟠 HIGH",
        "medium": "🟡 MEDIUM",
        "low": "🟢 LOW",
    }
    return badges.get(severity, "")


# Severity sort order: critical first, low last
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
# Confidence tiebreaker: high first
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _sort_by_severity(items: list) -> list:
    """Sort DataPoint dicts by severity (most severe first), confidence as tiebreaker."""
    return sorted(
        items,
        key=lambda dp: (
            _SEVERITY_ORDER.get(dp.get("severity", ""), 99),
            _CONFIDENCE_ORDER.get(dp.get("confidence", "unknown"), 99),
        ),
    )


def _render_data_point(dp: dict, show_sources: bool = True, show_severity: bool = False) -> str:
    """Render a single DataPoint as a readable string."""
    value = dp.get("value", "unknown")
    conf = dp.get("confidence", "unknown")
    severity = dp.get("severity")
    sources = dp.get("sources", [])

    badges = []
    if show_severity and severity:
        badges.append(_severity_badge(severity))
    badges.append(_confidence_badge(conf))
    badge_str = " | ".join(b for b in badges if b)

    line = f"{value} ({badge_str})"
    if show_sources and sources:
        source_links = ", ".join(sources[:2])  # Max 2 sources shown
        line += f"\n  _Sources: {source_links}_"
    return line


def _truncate(value: str, max_len: int = 80) -> str:
    """Truncate a string for table display."""
    if len(value) > max_len:
        return value[:max_len - 3] + "..."
    return value


def _render_list_as_table(lines: list, title: str, items: list, columns: list) -> None:
    """Render a list of DataPoint dicts as a markdown table.

    columns is a list of (header, key_or_callable) tuples.
    key_or_callable is either a string key into the dict or a callable(dp) -> str.
    """
    if not items:
        return
    lines.append(f"### {title}")
    header = " | ".join(col[0] for col in columns)
    sep = " | ".join("---" for _ in columns)
    lines.append(f"| {header} |")
    lines.append(f"| {sep} |")
    for dp in items:
        cells = []
        for _, accessor in columns:
            raw = accessor(dp) if callable(accessor) else dp.get(accessor, "—")
            cells.append(_md_cell(raw))
        lines.append(f"| {' | '.join(cells)} |")
    lines.append("")


def _claim_as_dp(claim: Optional[Claim]) -> dict:
    """Convert a Claim to the dict shape expected by the render helpers."""
    if claim is None:
        return {"value": "unknown", "confidence": "unknown", "sources": [], "severity": None}
    return {
        "value": claim.value,
        "confidence": claim.confidence.value,
        "severity": claim.severity.value if claim.severity else None,
        "sources": [s.url for s in claim.sources],
        "reasoning": claim.reasoning,
    }


def _section_confidence_from_doc(section) -> float:
    """Compute section confidence score directly from a ReportDocument section."""
    scores = {"high": 1.0, "medium": 0.66, "low": 0.33, "unknown": 0.0}
    total, count = 0.0, 0
    if section is None:
        return 0.0
    for fname in type(section).model_fields:
        val = getattr(section, fname)
        if isinstance(val, Claim):
            total += scores.get(val.confidence.value, 0.0)
            count += 1
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, Claim):
                    total += scores.get(item.confidence.value, 0.0)
                    count += 1
    return total / count if count > 0 else 0.0


def _report_confidence_from_doc(doc: ReportDocument) -> Optional[float]:
    """Compute weighted overall confidence from a ReportDocument."""
    weights = [(doc.financial, 0.40), (doc.risk, 0.40), (doc.social_media, 0.20)]
    weighted_sum, total_weight = 0.0, 0.0
    for section, weight in weights:
        if section is not None:
            weighted_sum += _section_confidence_from_doc(section) * weight
            total_weight += weight
    return weighted_sum / total_weight if total_weight > 0 else None


def _compute_section_confidence(section_data: dict) -> float:
    """Compute a confidence score (0.0–1.0) for a single agent's output.

    Scores every DataPoint field: high=1.0, medium=0.66, low=0.33, unknown=0.0.
    List fields contribute one score per item. Returns 0.0 if no data points found.
    """
    scores = {
        "high": 1.0,
        "medium": 0.66,
        "low": 0.33,
        "unknown": 0.0,
    }
    total = 0.0
    count = 0
    for key, val in section_data.items():
        if key == "company_name":
            continue
        if isinstance(val, dict) and "confidence" in val:
            total += scores.get(val["confidence"], 0.0)
            count += 1
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and "confidence" in item:
                    total += scores.get(item["confidence"], 0.0)
                    count += 1
    return total / count if count > 0 else 0.0


def _compute_report_confidence(
    financial_data: Optional[dict],
    risk_data: Optional[dict],
    social_media_data: Optional[dict],
) -> Optional[float]:
    """Compute weighted overall report confidence score.

    Weights: Financial 40%, Risk 40%, Social Media 20%.
    Only sections with data contribute; weights are renormalized.
    Returns None if no sections have data.
    """
    weights = [
        (financial_data, 0.40),
        (risk_data, 0.40),
        (social_media_data, 0.20),
    ]
    weighted_sum = 0.0
    total_weight = 0.0
    for data, weight in weights:
        if data:
            weighted_sum += _compute_section_confidence(data) * weight
            total_weight += weight
    if total_weight == 0.0:
        return None
    return weighted_sum / total_weight


def _confidence_score_badge(score: float) -> str:
    """Render a confidence score as a colored badge."""
    pct = score * 100
    if pct >= 75:
        return f"🟢 {pct:.0f}%"
    elif pct >= 50:
        return f"🟡 {pct:.0f}%"
    elif pct >= 25:
        return f"🔴 {pct:.0f}%"
    else:
        return f"⚫ {pct:.0f}%"


_RECOMMENDATION_BADGES = {
    "strong_proceed": "🟢 STRONG PROCEED",
    "proceed": "🟢 PROCEED",
    "proceed_with_conditions": "🟡 PROCEED WITH CONDITIONS",
    "caution": "🟠 CAUTION",
    "do_not_proceed": "🔴 DO NOT PROCEED",
}


def _recommendation_badge(value: str) -> str:
    return _RECOMMENDATION_BADGES.get(value.lower(), f"❓ {value.upper()}")


def render_report_from_doc(doc: ReportDocument, output_dir: str = "outputs") -> str:
    """Render a canonical ReportDocument as a markdown report.

    This is the primary entry point. The old dict-based render_report() is a
    deprecated shim that assembles a ReportDocument and calls this function.
    """
    now = doc.generated_at.strftime("%Y-%m-%d %H:%M")
    m = doc.run_metadata
    cost = m.cost_usd
    total_tokens = m.total_input_tokens + m.total_output_tokens
    llm_calls = m.total_llm_calls
    tool_calls = m.total_tool_calls
    duration_ms = m.duration_ms

    # Reconstruct legacy dicts for the existing section-rendering helpers
    research_data, financial_data, risk_data, social_media_data, synthesis_data, _ = (
        build_render_dicts(doc)
    )

    # Column grammars (one predictable shape per table type).
    # Synthesized-claim tables: Item, Confidence (no Sources, no Agents).
    _claim_cols = [
        ("Item",       lambda dp: dp.get("value", "—")),
        ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
    ]
    # Evidence tables: Item, Confidence, Sources (clickable).
    _evidence_cols = [
        ("Item",       lambda dp: dp.get("value", "—")),
        ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
        ("Sources",    lambda dp: _source_links_md(dp.get("sources", []))),
    ]

    def _kv_rows(fields: list, data: dict) -> None:
        """Append an attribute table: Field, Value, Confidence."""
        lines.append("| Field | Value | Confidence |")
        lines.append("|-------|-------|------------|")
        for label, key in fields:
            dp = data.get(key, {})
            lines.append(
                f"| {_md_cell(label)} | {_md_cell(dp.get('value', '—'))} "
                f"| {_confidence_badge(dp.get('confidence', 'unknown'))} |"
            )
        lines.append("")

    def _section(title: str) -> None:
        lines.append("---")
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")

    lines = []
    lines.append(f"# Due Diligence Report: {doc.company_name}")
    lines.append("")
    lines.append(f"**Generated:** {now}")
    lines.append(
        f"**Research Cost:** ${cost:.4f} "
        f"({total_tokens:,} tokens across {llm_calls} LLM calls, "
        f"{tool_calls} tool calls)"
    )
    lines.append(f"**Duration:** {duration_ms / 1000:.1f} seconds")

    # Overall confidence plus per-section confidences (display only — values come
    # from run_metadata.section_confidences, never recomputed here when present;
    # fall back to on-the-fly computation only for legacy docs).
    sc = doc.run_metadata.section_confidences
    oc = doc.run_metadata.overall_confidence
    if not sc:
        import warnings as _w
        _w.warn("run_metadata.section_confidences is empty — falling back to on-the-fly computation")
        report_score = _report_confidence_from_doc(doc)
    else:
        report_score = oc / 100.0 if oc is not None else None
    if report_score is not None:
        lines.append(f"**Overall Confidence:** {_confidence_score_badge(report_score)}")
        # All five section confidences in report-section order, no per-item weight
        # labels (Research and Synthesis are not weighted into Overall).
        sec_parts = []
        for label, key in [("Research", "research"), ("Financial", "financial"),
                           ("Risk", "risk"), ("Social Media", "social_media"),
                           ("Synthesis", "synthesis")]:
            if sc and key in sc:
                sec_parts.append(f"{label} {sc[key]:.0f}%")
            elif not sc:
                section = getattr(doc, key, None)
                if section is not None:
                    sec_parts.append(f"{label} {_section_confidence_from_doc(section)*100:.0f}%")
        if sec_parts:
            lines.append("")
            lines.append(" · ".join(sec_parts))
            lines.append("")
            lines.append(
                "_Overall is a weighted blend of Financial and Risk (40% each) and "
                "Social Media (20%); Research and Synthesis are reported but not weighted._"
            )
    lines.append("")

    # ── Disclaimer (prominent, near the top) ─────────────────────────────────
    lines.append("> ⚠️ **Disclaimer**  ")
    for sentence in disclaimer_sentences(doc.company_name):
        lines.append(f"> {sentence}  ")
    lines.append("")

    # ── Group 1: Assessment ──────────────────────────────────────────────────
    if synthesis_data:
        _section("Executive Summary")
        summary = synthesis_data.get("executive_summary", {})
        if summary.get("value") and summary["value"] not in ("unknown", ""):
            lines.append(summary["value"])
            lines.append("")

        _render_list_as_table(lines, "Key Strengths", synthesis_data.get("key_strengths", []), _claim_cols)
        _render_list_as_table(lines, "Key Concerns",  synthesis_data.get("key_concerns", []),  _claim_cols)

        red_flags = synthesis_data.get("red_flags", [])
        if red_flags:
            # Severity column (and its legend) appear ONLY here.
            lines.append("_Severity = risk impact; Confidence = data reliability. "
                         "Sorted by severity (most severe first)._")
            lines.append("")
            _flag_cols = [
                ("Red Flag",   lambda dp: dp.get("value", "—")),
                ("Severity",   lambda dp: _severity_badge(dp.get("severity", "")) or "—"),
                ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
            ]
            _render_list_as_table(lines, "Red Flags", _sort_by_severity(red_flags), _flag_cols)

    # ── Group 2: Data Quality & Open Items ───────────────────────────────────
    if synthesis_data or doc.gaps:
        _section("Data Quality & Open Items")

        if synthesis_data:
            dq = synthesis_data.get("data_quality", {})
            if dq.get("value") and dq["value"] not in ("unknown", ""):
                dq_badge = (_confidence_badge(dq["value"])
                            if dq["value"] in ("high", "medium", "low") else dq["value"].upper())
                lines.append(f"**Data Quality:** {dq_badge}")
                lines.append("")

            conflicts = synthesis_data.get("data_conflicts", [])
            if conflicts:
                _render_list_as_table(lines, "Data Conflicts", conflicts, _claim_cols)

        lines.append("### Information Gaps")
        if doc.gaps:
            for gap in doc.gaps:
                lines.append(f"- **{gap.field}** ({gap.agent}): {gap.reason}")
        else:
            lines.append("- No critical gaps identified")
        lines.append("")

        if synthesis_data:
            _render_list_as_table(
                lines, "Follow-Up Questions",
                synthesis_data.get("follow_up_questions", []),
                [
                    ("Question",       lambda dp: dp.get("value", "—")),
                    ("Why It Matters", lambda dp: dp.get("reasoning", "") or "—"),
                ],
            )

    # ── Group 3: Company Profile ─────────────────────────────────────────────
    if research_data:
        _section("Company Profile")
        _kv_rows([
            ("Description", "description"), ("Founded", "founded_year"),
            ("Headquarters", "headquarters"), ("Employees", "employee_count"),
            ("Industry", "industry"), ("Website", "website"),
        ], research_data)

        _render_list_as_table(lines, "Key Products",        research_data.get("key_products", []),        _claim_cols)
        _render_list_as_table(lines, "Leadership",          research_data.get("key_leadership", []),      _claim_cols)
        _render_list_as_table(lines, "Technology Stack",    research_data.get("technology_stack", []),    _claim_cols)
        _render_list_as_table(lines, "Recent Developments", research_data.get("recent_developments", []), _evidence_cols)

        patent_count = research_data.get("patent_count", {})
        if patent_count and patent_count.get("value") and patent_count["value"] not in ("unknown", ""):
            lines.append(f"**US Patent Portfolio:** {patent_count['value']} ({_confidence_badge(patent_count.get('confidence', 'unknown'))})")
            lines.append("")
        _render_list_as_table(lines, "Notable Patents", research_data.get("notable_patents", []), _evidence_cols)

    # ── Group 4: Financial Detail ────────────────────────────────────────────
    if financial_data:
        _section("Financial Profile")
        _kv_rows([
            ("Revenue", "revenue"), ("Revenue Growth", "revenue_growth"),
            ("Profitability", "profitability"), ("Total Funding", "total_funding"),
            ("Last Funding Round", "last_funding_round"), ("Valuation", "valuation"),
            ("Revenue Model", "revenue_model"),
        ], financial_data)

        _render_list_as_table(lines, "Key Investors",           financial_data.get("key_investors", []),          _claim_cols)
        _render_list_as_table(lines, "Key Customers",           financial_data.get("key_customers", []),          _claim_cols)
        _render_list_as_table(lines, "Recent Financial Events", financial_data.get("recent_financial_events", []), _evidence_cols)

    # ── Group 5: Risk Detail ─────────────────────────────────────────────────
    if risk_data:
        _section("Risk Assessment")
        overall = risk_data.get("overall_risk_rating", {})
        lines.append(f"**Overall Risk Rating:** {overall.get('value', 'unknown').upper()} ({_confidence_badge(overall.get('confidence', 'unknown'))})")
        lines.append("")
        summary = risk_data.get("risk_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            lines.append(f"> {summary['value']}")
            lines.append("")
        # Risk sub-tables are evidence tables: Item, Confidence, Sources.
        # Severity stays in the data (used for sorting) but is not a column here.
        for label, key in [
            ("Regulatory Risks",    "regulatory_risks"),
            ("Legal Risks",         "legal_risks"),
            ("Cybersecurity Risks", "cybersecurity_risks"),
            ("Operational Risks",   "operational_risks"),
            ("Reputational Risks",  "reputational_risks"),
            ("ESG Risks",           "esg_risks"),
            ("Pending Litigation",  "pending_litigation"),
        ]:
            items = risk_data.get(key, [])
            if items:
                _render_list_as_table(lines, label, _sort_by_severity(items), _evidence_cols)

        # Financial Risks grouped with the rest of the risk content.
        if financial_data:
            _render_list_as_table(lines, "Financial Risks", financial_data.get("financial_risks", []), _evidence_cols)

        gce = risk_data.get("government_contract_exposure", {})
        if gce and gce.get("value") and gce["value"] not in ("unknown", ""):
            lines.append(f"**Federal Contract Exposure:** {gce['value']} ({_confidence_badge(gce.get('confidence', 'unknown'))})")
            lines.append("")
        _render_list_as_table(lines, "Notable Federal Contracts",
                              risk_data.get("notable_federal_contracts", []), _evidence_cols)

    # ── Group 6: Social & Sentiment ──────────────────────────────────────────
    if social_media_data:
        _section("Social Media & Sentiment")
        sentiment = social_media_data.get("overall_sentiment", {})
        lines.append(f"**Overall Sentiment:** {sentiment.get('value', 'unknown').upper()} ({_confidence_badge(sentiment.get('confidence', 'unknown'))})")
        lines.append("")
        summary = social_media_data.get("sentiment_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            lines.append(f"> {summary['value']}")
            lines.append("")
        _kv_rows([
            ("Twitter/X", "twitter_presence"), ("LinkedIn", "linkedin_presence"),
            ("Reddit", "reddit_sentiment"), ("Glassdoor", "glassdoor_rating"),
        ], social_media_data)
        _render_list_as_table(lines, "Notable Mentions",    social_media_data.get("notable_mentions", []),    _evidence_cols)
        _render_list_as_table(lines, "Trending Topics",     social_media_data.get("trending_topics", []),     _evidence_cols)
        _render_list_as_table(lines, "Customer Complaints", social_media_data.get("customer_complaints", []), _evidence_cols)
        _render_list_as_table(lines, "Positive Signals",    social_media_data.get("positive_signals", []),    _evidence_cols)

    # Methodology
    lines.append("---")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    agent_names = list(m.agents.keys())
    agents_str = ", ".join(agent_names) if agent_names else "agents"
    lines.append(
        f"This report was generated by the {agents_str} agents, making "
        f"{llm_calls} LLM calls and {tool_calls} tool invocations "
        f"in {duration_ms / 1000:.1f} seconds at a cost of ${cost:.4f}."
    )
    lines.append("")
    if m.agents:
        lines.append("| Agent | LLM Calls | Tool Calls | Tokens | Cost |")
        lines.append("|-------|-----------|------------|--------|------|")
        for agent_name, adata in m.agents.items():
            tokens = adata.input_tokens + adata.output_tokens
            lines.append(
                f"| {agent_name} | {adata.llm_calls} | {adata.tool_calls} "
                f"| {tokens:,} | ${adata.cost_usd:.4f} |"
            )
        lines.append("")

    # Tier coverage (shared formatter — identical across HTML/PDF/Markdown)
    tier_parts = tier_coverage_parts(m.tier_attempts)
    if tier_parts:
        lines.append(f"**Source Tier Coverage:** {' · '.join(tier_parts)}")
        lines.append("")

    # EDGAR status (shared formatter)
    _edgar = edgar_line(m.edgar_lookup_status, m.edgar_cik)
    if _edgar:
        label, _, rest = _edgar.partition(": ")
        lines.append(f"**{label}:** {rest}")
        lines.append("")

    # House style: normalise em/en/etc. dashes to a single hyphen everywhere in
    # the rendered output (markdown "---" table/divider syntax is unaffected).
    report = strip_dashes("\n".join(lines))

    os.makedirs(output_dir, exist_ok=True)
    slug = doc.company_name.lower().replace(" ", "_").replace(".", "")
    filepath = os.path.join(output_dir, f"report_{slug}.md")
    with open(filepath, "w") as f:
        f.write(report)

    return report


def render_report(
    research_data: Optional[dict],
    trace_summary: dict,
    output_dir: str = "outputs",
    financial_data: Optional[dict] = None,
    risk_data: Optional[dict] = None,
    social_media_data: Optional[dict] = None,
    synthesis_data: Optional[dict] = None,
) -> str:
    """Deprecated shim — assemble a ReportDocument and call render_report_from_doc.

    TODO(P2): remove this shim once all callers pass a ReportDocument directly.
    """
    doc = assemble_report(
        research_data, financial_data, risk_data, social_media_data, synthesis_data, trace_summary
    )
    return render_report_from_doc(doc, output_dir)
