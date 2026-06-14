"""
Report generator — renders a canonical ReportDocument as a markdown report.

Primary entry point: render_report_from_doc(doc, output_dir)

The legacy dict-based render_report(...) is a deprecated shim that assembles
a ReportDocument from raw agent dicts and delegates to render_report_from_doc.
"""

import os
from typing import Optional

from src.schemas.models import Claim, ConfidenceLevel, ReportDocument, SeverityLevel
from src.synthesis.assembler import assemble_report, build_render_dicts


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
            if callable(accessor):
                cells.append(accessor(dp))
            else:
                cells.append(str(dp.get(accessor, "—")))
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

    # Read section/overall confidence from run_metadata (computed at assembly time).
    # Fall back to on-the-fly computation for documents assembled before this feature.
    sc = doc.run_metadata.section_confidences
    oc = doc.run_metadata.overall_confidence
    if not sc:
        import warnings as _w
        _w.warn("run_metadata.section_confidences is empty — falling back to on-the-fly computation")
        oc = _report_confidence_from_doc(doc)
        report_score = oc  # 0.0-1.0 fraction from fallback
    else:
        report_score = oc / 100.0 if oc is not None else None

    if report_score is not None:
        lines.append(f"**Overall Report Confidence:** {_confidence_score_badge(report_score)}")
        lines.append("")
        lines.append("| Section | Weight | Confidence |")
        lines.append("|---------|--------|------------|")
        for label, key, weight in [
            ("Financial",    "financial",    "40%"),
            ("Risk",         "risk",         "40%"),
            ("Social Media", "social_media", "20%"),
        ]:
            if sc and key in sc:
                sec_score = sc[key] / 100.0
            elif not sc:
                # fallback path: compute from section directly
                section = getattr(doc, key if key != "social_media" else "social_media", None)
                sec_score = _section_confidence_from_doc(section) if section else None
            else:
                sec_score = None
            if sec_score is not None:
                lines.append(f"| {label} | {weight} | {_confidence_score_badge(sec_score)} |")
            else:
                lines.append(f"| {label} | {weight} | ⚫ No data |")
        lines.append("")
    else:
        lines.append("")

    # Synthesis / Executive Summary
    if synthesis_data:
        lines.append("---")
        lines.append("")
        lines.append("## Executive Summary")
        lines.append("")

        rec = synthesis_data.get("investment_recommendation", {})
        rec_value = rec.get("value", "unknown")
        rec_conf = rec.get("confidence", "unknown")
        lines.append(
            f"**Recommendation:** {_recommendation_badge(rec_value)} "
            f"({_confidence_badge(rec_conf)})"
        )
        lines.append("")

        rationale = synthesis_data.get("recommendation_rationale", {})
        if rationale.get("value") and rationale["value"] not in ("unknown", ""):
            lines.append(f"> {rationale['value']}")
            lines.append("")

        summary = synthesis_data.get("executive_summary", {})
        if summary.get("value") and summary["value"] not in ("unknown", ""):
            lines.append(summary["value"])
            lines.append("")

        _synth_cols = [
            ("Item",       lambda dp: _truncate(dp.get("value", "—"), 100)),
            ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
            ("Agents",     lambda dp: ", ".join(dp.get("sources", [])[:4]) or "—"),
        ]
        _render_list_as_table(lines, "Key Strengths",  synthesis_data.get("key_strengths", []),  _synth_cols)
        _render_list_as_table(lines, "Key Concerns",   synthesis_data.get("key_concerns", []),   _synth_cols)

        red_flags = synthesis_data.get("red_flags", [])
        if red_flags:
            _flag_cols = [
                ("Red Flag",   lambda dp: _truncate(dp.get("value", "—"), 100)),
                ("Severity",   lambda dp: _severity_badge(dp.get("severity", "")) or "—"),
                ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
                ("Agents",     lambda dp: ", ".join(dp.get("sources", [])[:4]) or "—"),
            ]
            _render_list_as_table(lines, "Red Flags", _sort_by_severity(red_flags), _flag_cols)

        conflicts = synthesis_data.get("data_conflicts", [])
        if conflicts:
            _conflict_cols = [
                ("Conflict", lambda dp: _truncate(dp.get("value", "—"), 100)),
                ("Agents",   lambda dp: " vs ".join(dp.get("sources", [])[:2]) or "—"),
                ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
            ]
            _render_list_as_table(lines, "Data Conflicts", conflicts, _conflict_cols)

        _render_list_as_table(
            lines, "Follow-Up Questions",
            synthesis_data.get("follow_up_questions", []),
            [
                ("Question",       lambda dp: _truncate(dp.get("value", "—"), 120)),
                ("Why It Matters", lambda dp: _truncate(dp.get("reasoning", "") or "—", 80)),
            ],
        )

        dq = synthesis_data.get("data_quality", {})
        if dq.get("value") and dq["value"] not in ("unknown", ""):
            dq_badge = _confidence_badge(dq["value"]) if dq["value"] in ("high", "medium", "low") else dq["value"].upper()
            lines.append(f"**Data Quality:** {dq_badge}")
            lines.append("")

    # Research Section
    if research_data:
        lines.append("---")
        lines.append("")
        lines.append("## Company Overview")
        lines.append("")
        lines.append("| Field | Value | Confidence |")
        lines.append("|-------|-------|------------|")
        for label, key in [
            ("Description", "description"), ("Founded", "founded_year"),
            ("Headquarters", "headquarters"), ("Employees", "employee_count"),
            ("Industry", "industry"), ("Website", "website"),
        ]:
            dp = research_data.get(key, {})
            lines.append(f"| {label} | {_truncate(dp.get('value', '—'))} | {_confidence_badge(dp.get('confidence', 'unknown'))} |")
        lines.append("")

        _std_cols = [
            ("Item",       lambda dp: _truncate(dp.get("value", "—"))),
            ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
        ]
        _src_cols = _std_cols + [("Sources", lambda dp: ", ".join(dp.get("sources", [])[:2]) or "—")]
        _render_list_as_table(lines, "Key Products",        research_data.get("key_products", []),        _std_cols)
        _render_list_as_table(lines, "Leadership",          research_data.get("key_leadership", []),      _std_cols)
        _render_list_as_table(lines, "Technology Stack",    research_data.get("technology_stack", []),    _std_cols)
        _render_list_as_table(lines, "Recent Developments", research_data.get("recent_developments", []), _src_cols)

        # P3b: patent data
        patent_count = research_data.get("patent_count", {})
        if patent_count and patent_count.get("value") and patent_count["value"] not in ("unknown", ""):
            lines.append(f"**US Patent Portfolio:** {patent_count['value']} ({_confidence_badge(patent_count.get('confidence', 'unknown'))})")
            lines.append("")
        _render_list_as_table(lines, "Notable Patents", research_data.get("notable_patents", []), _src_cols)

    # Financial Section
    if financial_data:
        lines.append("---")
        lines.append("")
        lines.append("## Financial Profile")
        lines.append("")
        lines.append("| Metric | Value | Confidence |")
        lines.append("|--------|-------|------------|")
        for label, key in [
            ("Revenue", "revenue"), ("Revenue Growth", "revenue_growth"),
            ("Profitability", "profitability"), ("Total Funding", "total_funding"),
            ("Last Funding Round", "last_funding_round"), ("Valuation", "valuation"),
            ("Revenue Model", "revenue_model"),
        ]:
            dp = financial_data.get(key, {})
            lines.append(f"| {label} | {_truncate(dp.get('value', '—'))} | {_confidence_badge(dp.get('confidence', 'unknown'))} |")
        lines.append("")

        _fin_cols = [
            ("Item",       lambda dp: _truncate(dp.get("value", "—"))),
            ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
        ]
        _fin_src = _fin_cols + [("Sources", lambda dp: ", ".join(dp.get("sources", [])[:2]) or "—")]
        _render_list_as_table(lines, "Key Investors",           financial_data.get("key_investors", []),          _fin_cols)
        _render_list_as_table(lines, "Key Customers",           financial_data.get("key_customers", []),          _fin_cols)
        _render_list_as_table(lines, "Financial Risks",         financial_data.get("financial_risks", []),        _fin_src)
        _render_list_as_table(lines, "Recent Financial Events", financial_data.get("recent_financial_events", []), _fin_src)

    # Risk Section
    if risk_data:
        lines.append("---")
        lines.append("")
        lines.append("## Risk Assessment")
        lines.append("")
        overall = risk_data.get("overall_risk_rating", {})
        lines.append(f"**Overall Risk Rating:** {overall.get('value', 'unknown').upper()} ({_confidence_badge(overall.get('confidence', 'unknown'))})")
        lines.append("")
        summary = risk_data.get("risk_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            lines.append(f"> {summary['value']}")
            lines.append("")
        lines.append("_Severity = risk impact; Confidence = data reliability. Sorted by severity (most severe first)._")
        lines.append("")
        _risk_cols = [
            ("Risk",       lambda dp: _truncate(dp.get("value", "—"))),
            ("Severity",   lambda dp: _severity_badge(dp.get("severity", "")) or "—"),
            ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
            ("Sources",    lambda dp: ", ".join(dp.get("sources", [])[:2]) or "—"),
        ]
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
                _render_list_as_table(lines, label, _sort_by_severity(items), _risk_cols)

        # P3b: government contract exposure
        gce = risk_data.get("government_contract_exposure", {})
        if gce and gce.get("value") and gce["value"] not in ("unknown", ""):
            lines.append(f"**Federal Contract Exposure:** {gce['value']} ({_confidence_badge(gce.get('confidence', 'unknown'))})")
            lines.append("")
        _render_list_as_table(
            lines, "Notable Federal Contracts",
            risk_data.get("notable_federal_contracts", []),
            [
                ("Contract",   lambda dp: _truncate(dp.get("value", "—"))),
                ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
                ("Sources",    lambda dp: ", ".join(dp.get("sources", [])[:2]) or "—"),
            ],
        )

    # Social Media Section
    if social_media_data:
        lines.append("---")
        lines.append("")
        lines.append("## Social Media & Sentiment")
        lines.append("")
        sentiment = social_media_data.get("overall_sentiment", {})
        lines.append(f"**Overall Sentiment:** {sentiment.get('value', 'unknown').upper()} ({_confidence_badge(sentiment.get('confidence', 'unknown'))})")
        lines.append("")
        summary = social_media_data.get("sentiment_summary", {})
        if summary.get("value") and summary["value"] != "unknown":
            lines.append(f"> {summary['value']}")
            lines.append("")
        lines.append("| Platform | Details | Confidence |")
        lines.append("|----------|---------|------------|")
        for label, key in [
            ("Twitter/X", "twitter_presence"), ("LinkedIn", "linkedin_presence"),
            ("Reddit", "reddit_sentiment"), ("Glassdoor", "glassdoor_rating"),
        ]:
            dp = social_media_data.get(key, {})
            lines.append(f"| {label} | {_truncate(dp.get('value', '—'))} | {_confidence_badge(dp.get('confidence', 'unknown'))} |")
        lines.append("")
        _soc_cols = [
            ("Item",       lambda dp: _truncate(dp.get("value", "—"))),
            ("Confidence", lambda dp: _confidence_badge(dp.get("confidence", "unknown"))),
            ("Sources",    lambda dp: ", ".join(dp.get("sources", [])[:2]) or "—"),
        ]
        _render_list_as_table(lines, "Notable Mentions",    social_media_data.get("notable_mentions", []),    _soc_cols)
        _render_list_as_table(lines, "Trending Topics",     social_media_data.get("trending_topics", []),     _soc_cols)
        _render_list_as_table(lines, "Customer Complaints", social_media_data.get("customer_complaints", []), _soc_cols)
        _render_list_as_table(lines, "Positive Signals",    social_media_data.get("positive_signals", []),    _soc_cols)

    # Gaps — from canonical doc, not re-derived
    lines.append("---")
    lines.append("")
    lines.append("## Information Gaps")
    lines.append("")
    if doc.gaps:
        for gap in doc.gaps:
            lines.append(f"- **{gap.field}** ({gap.agent}): {gap.reason}")
    else:
        lines.append("- No critical gaps identified")
    lines.append("")

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

    # Tier coverage
    if m.tier_attempts:
        total_sources = sum(m.tier_attempts.values())
        tier_parts = []
        tier_display = {
            "primary_document":    "Primary (Tier 0)",
            "reputable_secondary": "Reputable (Tier 1)",
            "aggregator":          "Aggregator (Tier 2)",
            "community":           "Community (Tier 3)",
            "unknown":             "Unknown (Tier ?)",
        }
        for tier_key in ("primary_document", "reputable_secondary", "aggregator", "community", "unknown"):
            count = m.tier_attempts.get(tier_key, 0)
            if count > 0:
                pct = round(count / total_sources * 100)
                tier_parts.append(f"{tier_display.get(tier_key, tier_key)}: {pct}%")
        if tier_parts:
            lines.append(f"**Source Tier Coverage:** {' · '.join(tier_parts)}")
            lines.append("")

    # EDGAR status
    if m.edgar_lookup_status:
        status = m.edgar_lookup_status
        if status == "succeeded":
            cik_str = f" (CIK: {m.edgar_cik})" if m.edgar_cik else ""
            lines.append(f"**EDGAR:** ✓ succeeded{cik_str}")
        elif status == "not_sec_reporting":
            lines.append("**EDGAR:** – not SEC-reporting (private or non-US; expected)")
        elif status == "lookup_failed":
            lines.append("**EDGAR:** ⚠ lookup failed — financial data from EDGAR unavailable")
        elif status == "rate_limited":
            lines.append("**EDGAR:** ⚠ rate limited — financial data from EDGAR unavailable")
        lines.append("")

    report = "\n".join(lines)

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
