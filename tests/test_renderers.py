"""Render-layer tests: presentation pass over a canonical ReportDocument.

Covers the render contract:
  - rendering is read-only (does not mutate the canonical document)
  - no Agents column anywhere
  - Severity only on the Red Flags table (with matching legend)
  - single Overall Confidence in the header (no weighted breakdown)
  - no Investment Recommendation label/value; executive-summary prose present
  - every source is a clickable link with a clean label (no truncated raw URLs)
  - grouped section order with Information Gaps beside Data Quality
  - the three renderers (Markdown, HTML, PDF) agree
"""

import json
import os
import tempfile

import pytest

from src.synthesis.assembler import annotate_claim_ids, assemble_report
from src.synthesis.report_generator import (
    render_report_from_doc,
    _clean_source_label,
    _source_links_md,
    _md_cell,
)
from src.synthesis.pdf_report import render_pdf_report_from_doc


def _dp(value, conf="high", sources=None, sev=None, reasoning=None):
    d = {"value": value, "confidence": conf, "sources": sources or []}
    if sev:
        d["severity"] = sev
    if reasoning:
        d["reasoning"] = reasoning
    return d


def _full_doc():
    """A valid ReportDocument exercising every section, sources, and severity."""
    research = annotate_claim_ids({
        "company_name": "Acme",
        "description": _dp("A | tech co\nwith pipes", sources=["https://acme.com"]),
        "founded_year": _dp("2001"),
        "headquarters": _dp("NYC"),
        "employee_count": _dp("100", "medium"),
        "industry": _dp("Tech"),
        "website": _dp("https://acme.com"),
        "key_products": [_dp("Widget")],
        "key_leadership": [_dp("Jane Doe, CEO")],
        "technology_stack": [_dp("Python")],
        "recent_developments": [_dp(
            "Launched X",
            sources=["https://www.reuters.com/article/x",
                     "https://blog.example.io/p?a=1&b=2"],
        )],
        "notable_patents": [],
    })
    rid = research["description"]["_claim_id"]

    financial = {
        "company_name": "Acme",
        "revenue": _dp("$1B"), "revenue_growth": _dp("10%"),
        "profitability": _dp("Profitable"),
        "total_funding": _dp("unknown", "unknown"),
        "last_funding_round": _dp("unknown", "unknown"),
        "valuation": _dp("$5B"), "revenue_model": _dp("SaaS"),
        "key_investors": [_dp("Sequoia")], "key_customers": [_dp("BigCo")],
        "financial_risks": [_dp("FX risk", sources=["https://sec.gov/x"])],
        "recent_financial_events": [_dp("Q4 earnings", sources=["https://sec.gov/y"])],
    }
    risk = {
        "company_name": "Acme",
        "overall_risk_rating": _dp("medium"),
        "risk_summary": _dp("Moderate risk."),
        "regulatory_risks": [_dp("Reg A", sources=["https://ec.europa.eu/z"], sev="high")],
        "legal_risks": [_dp("Suit B", sources=["https://www.courtlistener.com/d/1"], sev="critical")],
        "cybersecurity_risks": [_dp("Breach C", sev="low")],
        "operational_risks": [],
        "reputational_risks": [],
        "esg_risks": [_dp("Carbon", sev="medium")],
        "pending_litigation": [_dp("Case D", sources=["https://www.courtlistener.com/d/2"], sev="high")],
        "government_contract_exposure": _dp("unknown", "unknown"),
        "notable_federal_contracts": [],
    }
    social = {
        "company_name": "Acme",
        "overall_sentiment": _dp("positive"),
        "sentiment_summary": _dp("Liked."),
        "twitter_presence": _dp("Active"), "linkedin_presence": _dp("Active"),
        "reddit_sentiment": _dp("Mixed", "medium"), "glassdoor_rating": _dp("4.0"),
        "notable_mentions": [_dp("TechCrunch piece", sources=["https://techcrunch.com/a"])],
        "trending_topics": [_dp("AI")],
        "customer_complaints": [_dp("Slow support", sources=["https://www.reddit.com/r/x"])],
        "positive_signals": [_dp("Award")],
    }
    # Synthesis SPECIFIC claims must carry synthesized_from referencing an upstream id.
    def _sclaim(value, sev=None):
        d = _dp(value, reasoning="rationale text", sev=sev)
        d["synthesized_from"] = [rid]
        return d

    synth = {
        "company_name": "Acme",
        "executive_summary": _dp("Acme is a solid company with strong fundamentals.",
                                 reasoning="overall"),
        "investment_recommendation": _dp("proceed_with_conditions", reasoning="net assessment"),
        "recommendation_rationale": _dp("Because reasons.", reasoning="detail"),
        "key_strengths": [_sclaim("Strong revenue")],
        "key_concerns": [_sclaim("FX exposure")],
        "red_flags": [_sclaim("Pending lawsuit", sev="high")],
        "data_conflicts": [_sclaim("Employee count differs")],
        "follow_up_questions": [{"value": "What about X?", "confidence": "low",
                                 "sources": [], "reasoning": "Matters because Y"}],
        "data_quality": _dp("medium", "medium", reasoning="mixed sourcing"),
    }
    ts = {"trace_id": "t", "total_cost_usd": 0.5, "total_duration_ms": 1000,
          "total_llm_calls": 5, "total_tool_calls": 3,
          "total_input_tokens": 100, "total_output_tokens": 50,
          "by_agent": {"research": {"llm_calls": 3, "tool_calls": 2,
                                    "input_tokens": 60, "output_tokens": 30,
                                    "cost_usd": 0.3, "duration_ms": 600, "errors": 0}}}
    edgar = {"edgar_lookup_status": "succeeded", "cik": "0000320193",
             "is_sec_reporting": True, "sec_risk_factors": []}
    return assemble_report(research, financial, risk, social, synth, ts, edgar_data=edgar)


# ── Read-only guarantee ───────────────────────────────────────────────────────

def test_rendering_does_not_mutate_canonical_document():
    """Rendering must be read-only: the canonical JSON is byte-identical after."""
    doc = _full_doc()
    before = doc.model_dump_json()
    with tempfile.TemporaryDirectory() as d:
        render_report_from_doc(doc, d)
        render_pdf_report_from_doc(doc, d)
    after = doc.model_dump_json()
    assert before == after, "renderers must not mutate the canonical ReportDocument"


def test_mutation_guard_would_catch_a_mutating_renderer():
    """Sanity: the guard compares serialized JSON, so a mutation WOULD be caught."""
    doc = _full_doc()
    before = doc.model_dump_json()
    # Simulate an accidental mutation a buggy renderer might do.
    doc.synthesis.data_quality.value = "low"
    assert doc.model_dump_json() != before


# ── Markdown checklist ────────────────────────────────────────────────────────

class TestMarkdownRender:
    def setup_method(self):
        with tempfile.TemporaryDirectory() as d:
            self.md = render_report_from_doc(_full_doc(), d)

    def test_no_agents_column(self):
        assert "| Agents" not in self.md and "Agents |" not in self.md

    def test_no_investment_recommendation_label(self):
        assert "Recommendation:" not in self.md
        assert "INVESTMENT RECOMMENDATION" not in self.md

    def test_executive_summary_prose_present(self):
        assert "Acme is a solid company with strong fundamentals." in self.md

    def test_overall_and_section_confidences_present(self):
        # Overall figure plus all five section confidences (no old | Weight | table,
        # no per-item weight labels — Research/Synthesis must not show a weight).
        assert "**Overall Confidence:**" in self.md
        assert "| Weight |" not in self.md
        for name in ("Research", "Financial", "Risk", "Social Media", "Synthesis"):
            assert f"{name} " in self.md
        assert "(40%):" not in self.md and "(20%):" not in self.md
        assert "(0%)" not in self.md
        # Weighting explained once via a note.
        assert "reported but not weighted" in self.md

    def test_section_confidence_order(self):
        order = ["Research", "Financial", "Risk", "Social Media", "Synthesis"]
        positions = [self.md.index(f"{n} ") for n in order]
        assert positions == sorted(positions)

    def test_severity_only_on_red_flags(self):
        # Exactly one Severity column header, and it sits in the Assessment group.
        assert self.md.count("| Severity |") == 1
        assert self.md.index("Red Flags") < self.md.index("| Severity |")
        # Risk sub-tables must NOT carry severity — they are evidence tables.
        risk_section = self.md[self.md.index("## Risk Assessment"):]
        assert "| Severity |" not in risk_section

    def test_sources_are_clickable_links(self):
        assert "[reuters.com](https://www.reuters.com/article/x)" in self.md
        assert "[sec.gov](https://sec.gov/x)" in self.md

    def test_no_midword_truncation_marker(self):
        assert "..." not in self.md
        assert "…" not in self.md

    def test_information_gaps_beside_data_quality(self):
        assert "Data Quality & Open Items" in self.md
        assert self.md.index("Data Quality & Open Items") < self.md.index("Information Gaps")
        # Gaps moved up — they precede the company profile, not at the very end.
        assert self.md.index("Information Gaps") < self.md.index("## Company Profile")

    def test_financial_risks_grouped_in_risk_section(self):
        assert self.md.index("## Risk Assessment") < self.md.index("Financial Risks")

    def test_pipe_in_cell_does_not_break_table(self):
        # The literal value contained a pipe and newline; content survives, escaped.
        assert "tech co" in self.md and "with pipes" in self.md


# ── HTML checklist ────────────────────────────────────────────────────────────

class TestHtmlRender:
    def setup_method(self):
        with tempfile.TemporaryDirectory() as d:
            html_path, _ = render_pdf_report_from_doc(_full_doc(), d)
            self.html = open(html_path).read()

    def test_no_agents_column(self):
        assert "<th>Agents</th>" not in self.html

    def test_no_investment_recommendation(self):
        assert "Investment Recommendation" not in self.html
        # The old recommendation callout element must not be rendered (CSS class
        # definition may remain in the stylesheet, but no element should use it).
        assert 'class="rec-value"' not in self.html
        assert "Proceed with Conditions" not in self.html

    def test_executive_summary_prose_present(self):
        assert "Acme is a solid company with strong fundamentals." in self.html

    def test_overall_and_section_confidences_present(self):
        assert "Overall Confidence:" in self.html
        for name in ("Research", "Financial", "Risk", "Social Media", "Synthesis"):
            assert f"{name} " in self.html
        # No per-item weight labels and never a fabricated/0% weight.
        assert "(40%)" not in self.html.split("<h2>")[0]  # header region only
        assert "(0%)" not in self.html
        assert "reported but not weighted" in self.html

    def test_section_confidence_order(self):
        order = ["Research", "Financial", "Risk", "Social Media", "Synthesis"]
        header = self.html.split("<h2>")[0]
        positions = [header.index(f"{n} ") for n in order]
        assert positions == sorted(positions)

    def test_title_rule_below_title_not_through_it(self):
        # The title rule is its own element AFTER the <h1>, not a border on the h1
        # and not before it — so no line crosses the company name.
        assert '<hr class="title-rule">' in self.html
        h1_end = self.html.index("</h1>")
        assert self.html.index('<hr class="title-rule">') > h1_end
        # h1 itself carries no border (which would strike through the text).
        assert "h1{font-size:22pt;color:#1e3a8a;margin:0 0 10px}" in self.html

    def test_confidence_header_does_not_wrap(self):
        assert "white-space:nowrap" in self.html  # applied to thead th

    def test_confbar_constrained_no_overflow(self):
        # box-sizing border-box globally + width:100% keeps the bar in its container.
        assert "box-sizing:border-box" in self.html
        assert ".confbar{width:100%" in self.html

    def test_severity_only_on_red_flags(self):
        assert self.html.count("<th>Severity</th>") == 1

    def test_sources_are_clickable_anchors(self):
        assert '<a href="https://www.reuters.com/article/x"' in self.html
        assert ">reuters.com</a>" in self.html

    def test_information_gaps_present_and_grouped(self):
        assert "Information Gaps" in self.html
        assert "Data Quality &amp; Open Items" in self.html
        assert self.html.index("Information Gaps") < self.html.index("<h2>Company Profile</h2>")

    def test_company_profile_header(self):
        assert "<h2>Company Profile</h2>" in self.html

    def test_financial_risks_grouped_in_risk_section(self):
        assert self.html.index("Risk Assessment") < self.html.index("Financial Risks")


# ── Disclaimer (HTML + Markdown) ──────────────────────────────────────────────

class TestDisclaimer:
    def setup_method(self):
        doc = _full_doc()
        with tempfile.TemporaryDirectory() as d:
            self.md = render_report_from_doc(doc, d)
            html_path, _ = render_pdf_report_from_doc(doc, d)
            self.html = open(html_path).read()

    def _block(self, text):
        # The disclaimer must appear before the Executive Summary in both formats.
        lo = text.lower()
        assert "disclaimer" in lo
        return text

    def test_disclaimer_present_before_executive_summary_html(self):
        assert self.html.lower().index("disclaimer") < self.html.index("Executive Summary")

    def test_disclaimer_present_before_executive_summary_md(self):
        assert self.md.lower().index("disclaimer") < self.md.index("Executive Summary")

    def test_disclaimer_states_required_points(self):
        for blob in (self.html, self.md):
            self._block(blob)
            assert "automated multi-agent AI pipeline" in blob
            assert "public sources" in blob
            assert "demonstration" in blob
            assert "not investment advice" in blob
            assert "not affiliated with, authorized by, or endorsed by" in blob

    def test_disclaimer_names_subject_company_generically(self):
        # Uses the company name from data (Acme), not a hard-coded brand.
        assert "endorsed by Acme" in self.html
        assert "endorsed by Acme" in self.md

    def test_verification_caveat_present_and_generic(self):
        caveat = "independently verified before any reliance"
        assert caveat in self.html and caveat in self.md
        for blob in (self.html, self.md):
            assert "source-tier reliability, not independent verification" in blob
            assert "allegations involving" in blob and "third parties" in blob


def test_disclaimer_caveat_source_has_no_hardcoded_company():
    """The disclaimer/caveat path must not hard-code any company/brand name."""
    import src.synthesis.render_common as rc
    src_text = open(rc.__file__).read()
    for banned in ("Apple", "Lens", "Luxshare", "Tesla", "Google", "Microsoft"):
        assert banned not in src_text, f"hard-coded '{banned}' in render_common"
    # Generic fallback exists when no name is supplied.
    assert "the subject company" in " ".join(rc.disclaimer_sentences(None))
    assert "endorsed by Acme Co" in " ".join(rc.disclaimer_sentences("Acme Co"))


# ── HTML methodology restored (matches Markdown/PDF) ──────────────────────────

class TestHtmlMethodology:
    def setup_method(self):
        doc = _full_doc()
        with tempfile.TemporaryDirectory() as d:
            html_path, _ = render_pdf_report_from_doc(doc, d)
            self.html = open(html_path).read()
            self.md = render_report_from_doc(doc, d)

    def test_per_agent_table_present(self):
        meth = self.html[self.html.index("<h2>Methodology</h2>"):]
        assert "<th>Agent</th>" in meth
        assert "<th>LLM Calls</th>" in meth and "<th>Tool Calls</th>" in meth
        assert "<th>Tokens</th>" in meth and "<th>Cost</th>" in meth
        assert "research" in meth  # the agent row

    def test_totals_line_present(self):
        meth = self.html[self.html.index("<h2>Methodology</h2>"):]
        assert "LLM calls" in meth and "tool invocations" in meth

    def test_source_tier_coverage_line_present(self):
        assert "Source Tier Coverage:" in self.html
        # identical content present in Markdown too
        assert "Source Tier Coverage:" in self.md

    def test_edgar_line_present(self):
        assert "EDGAR:" in self.html and "succeeded" in self.html
        assert "EDGAR:" in self.md


# ── Recommendation styling removed ────────────────────────────────────────────

class TestRecommendationStylingRemoved:
    def setup_method(self):
        doc = _full_doc()
        with tempfile.TemporaryDirectory() as d:
            html_path, _ = render_pdf_report_from_doc(doc, d)
            self.html = open(html_path).read()

    def test_rec_classes_gone_from_stylesheet(self):
        for cls in (".rec{", ".rec-proceed", ".rec-caution", ".rec-cond",
                    ".rec-halt", ".rec-unk", ".rec-label", ".rec-value"):
            assert cls not in self.html, f"dead CSS class {cls} still present"

    def test_no_rec_wrapper_used(self):
        assert 'class="rec' not in self.html

    def test_risk_narrative_text_intact(self):
        # The risk summary prose must still be fully present (just unwrapped).
        assert "Moderate risk." in self.html


# ── PDF smoke ─────────────────────────────────────────────────────────────────

def test_pdf_renders_nonempty():
    with tempfile.TemporaryDirectory() as d:
        _, pdf_path = render_pdf_report_from_doc(_full_doc(), d)
        assert os.path.getsize(pdf_path) > 1000


# ── Overall confidence = None ("not computable") rendering ─────────────────────

def _doc_overall_none():
    """A doc with NO weighted section (only research present) → overall is None."""
    research = annotate_claim_ids({
        "company_name": "Acme",
        "description": _dp("A company"),
        "founded_year": _dp("2001"), "headquarters": _dp("NYC"),
        "employee_count": _dp("100"), "industry": _dp("Tech"),
        "website": _dp("https://acme.com"),
        "key_products": [], "key_leadership": [], "technology_stack": [],
        "recent_developments": [], "notable_patents": [],
    })
    ts = {"trace_id": "t", "total_cost_usd": 0.1, "total_duration_ms": 1000,
          "total_llm_calls": 1, "total_tool_calls": 0,
          "total_input_tokens": 10, "total_output_tokens": 5, "by_agent": {}}
    doc = assemble_report(research, None, None, None, None, ts)
    assert doc.run_metadata.overall_confidence is None  # precondition for these tests
    return doc


_NOT_COMPUTABLE = "not computable (no weighted section available)"


def test_overall_none_markdown_is_explicit_not_blank_or_zero():
    with tempfile.TemporaryDirectory() as d:
        md = render_report_from_doc(_doc_overall_none(), d)
    # The overall line is PRESENT and explicit, never omitted.
    assert "**Overall Confidence:**" in md
    assert _NOT_COMPUTABLE in md
    # Never rendered as 0% (the failure this guards against).
    assert "Overall Confidence:** 0%" not in md and "Overall Confidence: 0%" not in md


def test_overall_none_html_is_explicit_not_blank_or_zero():
    with tempfile.TemporaryDirectory() as d:
        html_path, _ = render_pdf_report_from_doc(_doc_overall_none(), d)
        html = open(html_path).read()
    assert "Overall Confidence" in html            # not omitted
    assert _NOT_COMPUTABLE in html
    assert ">Overall Confidence: 0%" not in html   # not zero


def test_overall_none_pdf_renders_without_error():
    with tempfile.TemporaryDirectory() as d:
        _, pdf_path = render_pdf_report_from_doc(_doc_overall_none(), d)
        assert os.path.getsize(pdf_path) > 1000


def test_overall_present_regression_still_numeric():
    """At-least-one-present: a numeric overall still renders with a % badge and the
    weighting note — the not-computable branch must not affect healthy reports."""
    with tempfile.TemporaryDirectory() as d:
        md = render_report_from_doc(_full_doc(), d)
        html_path, _ = render_pdf_report_from_doc(_full_doc(), d)
        html = open(html_path).read()
    assert _NOT_COMPUTABLE not in md and _NOT_COMPUTABLE not in html
    assert "**Overall Confidence:**" in md and "%" in md.split("**Overall Confidence:**")[1][:40]
    assert "Overall Confidence:" in html and "reported but not weighted" in html


# ── Source-link helpers ───────────────────────────────────────────────────────

def test_clean_source_label_domain_or_source():
    assert _clean_source_label("https://www.reuters.com/x") == "reuters.com"
    assert _clean_source_label("https://sec.gov/y") == "sec.gov"
    assert _clean_source_label("not a url") == "Source"


def test_source_links_md_renders_all_sources():
    md = _source_links_md(["https://a.com/1", "https://b.org/2"])
    assert "[a.com](https://a.com/1)" in md
    assert "[b.org](https://b.org/2)" in md
    assert _source_links_md([]) == "—"


def test_md_cell_escapes_pipes_and_newlines():
    assert _md_cell("a | b\nc") == "a \\| b c"
    assert _md_cell("") == "—"


def test_generated_timestamp_renders_in_us_eastern():
    """The 'Generated' time is displayed in US Eastern (DST-aware), not UTC."""
    from datetime import datetime, timezone
    from src.synthesis.render_common import format_generated_et

    # Summer → EDT (UTC-4): 05:34 UTC = 01:34 EDT
    assert format_generated_et(datetime(2026, 6, 21, 5, 34, tzinfo=timezone.utc)) == "2026-06-21 01:34 EDT"
    # Winter → EST (UTC-5): 05:34 UTC = 00:34 EST
    assert format_generated_et(datetime(2026, 1, 15, 5, 34, tzinfo=timezone.utc)) == "2026-01-15 00:34 EST"
    # Naive datetime is treated as UTC.
    assert format_generated_et(datetime(2026, 6, 21, 5, 34)).endswith("EDT")


def test_rendered_generated_line_uses_eastern_zone():
    """End-to-end: the rendered MD and HTML 'Generated' line carries an ET label."""
    doc = _full_doc()
    with tempfile.TemporaryDirectory() as d:
        md = render_report_from_doc(doc, d)
        html_path, _ = render_pdf_report_from_doc(doc, d)
        html = open(html_path).read()
    gen_line = next(l for l in md.splitlines() if l.startswith("**Generated:**"))
    assert gen_line.endswith("EDT") or gen_line.endswith("EST")
    assert "Generated " in html and ("EDT" in html or "EST" in html)


# ── House style: no "double dash" glyphs in any rendered report ────────────────

_DASH_GLYPHS = "‒–—―−"  # figure, en, em, horizontal bar, minus


def _doc_with_dashy_data():
    """A document whose claim text contains em/en dashes (as agent output would)."""
    from src.synthesis.assembler import annotate_claim_ids, assemble_report
    research = annotate_claim_ids({
        "company_name": "Acme",
        "description": {"value": "Acme — a company — with em dashes; range 2020–2024.",
                        "confidence": "high", "sources": ["https://acme.com"]},
        "founded_year": {"value": "2001", "confidence": "high", "sources": []},
        "headquarters": {"value": "NYC", "confidence": "high", "sources": []},
        "employee_count": {"value": "100", "confidence": "medium", "sources": []},
        "industry": {"value": "Tech", "confidence": "high", "sources": []},
        "website": {"value": "https://acme.com", "confidence": "high", "sources": []},
        "key_products": [{"value": "Widget — flagship", "confidence": "high", "sources": []}],
        "key_leadership": [], "technology_stack": [],
        "recent_developments": [{"value": "Launched X — the next gen", "confidence": "high",
                                 "sources": ["https://example.com/a--b"]}],
        "notable_patents": [],
    })
    ts = {"trace_id": "t", "total_cost_usd": 0.1, "total_duration_ms": 100,
          "total_llm_calls": 1, "total_tool_calls": 0,
          "total_input_tokens": 10, "total_output_tokens": 5, "by_agent": {}}
    return assemble_report(research, None, None, None, None, ts)


def test_no_unicode_dashes_in_rendered_markdown():
    doc = _doc_with_dashy_data()
    with tempfile.TemporaryDirectory() as d:
        md = render_report_from_doc(doc, d)
    assert not any(g in md for g in _DASH_GLYPHS), "em/en dash leaked into Markdown"
    # The em-dash separated content survives as hyphenated text (no content lost).
    assert "Acme - a company - with em dashes" in md


def test_no_unicode_dashes_in_rendered_html():
    doc = _doc_with_dashy_data()
    with tempfile.TemporaryDirectory() as d:
        html_path, _ = render_pdf_report_from_doc(doc, d)
        html = open(html_path).read()
    assert not any(g in html for g in _DASH_GLYPHS), "em/en dash leaked into HTML"
    # A source URL containing '--' must NOT be corrupted by dash normalisation.
    assert "https://example.com/a--b" in html


def test_full_doc_renders_without_unicode_dashes():
    doc = _full_doc()
    with tempfile.TemporaryDirectory() as d:
        md = render_report_from_doc(doc, d)
        html_path, _ = render_pdf_report_from_doc(doc, d)
        html = open(html_path).read()
    assert not any(g in md for g in _DASH_GLYPHS)
    assert not any(g in html for g in _DASH_GLYPHS)
