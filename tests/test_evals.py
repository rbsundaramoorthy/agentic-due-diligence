"""
Tests for the ground truth evaluation system.

Run with: pytest tests/test_evals.py -v
"""

import json
import sqlite3
from pathlib import Path

import pytest

from evals.eval_runner import (
    GroundTruthEvaluator,
    EvalResults,
    FieldResult,
    persist_eval_results,
    _normalize,
    _fuzzy_match,
    _list_overlap,
    _best_confidence,
)


# ── Helpers ──────────────────────────────────────────────────────


def make_dp(value, confidence="high"):
    """Create a DataPoint-like dict."""
    return {"value": value, "confidence": confidence, "sources": []}


# ── Unit tests: matching functions ───────────────────────────────


class TestNormalize:
    def test_strips_whitespace(self):
        assert _normalize("  hello  ") == "hello"

    def test_lowercases(self):
        assert _normalize("San Francisco") == "san francisco"

    def test_removes_commas_and_periods(self):
        assert _normalize("1,000.5") == "10005"


class TestFuzzyMatch:
    def test_exact_match(self):
        assert _fuzzy_match("2010", "2010") is True

    def test_case_insensitive(self):
        assert _fuzzy_match("San Francisco", "san francisco") is True

    def test_contained_in(self):
        assert _fuzzy_match("San Francisco", "San Francisco, CA, USA") is True

    def test_no_match(self):
        assert _fuzzy_match("New York", "San Francisco") is False

    def test_empty_strings(self):
        assert _fuzzy_match("", "something") is False
        assert _fuzzy_match("something", "") is False


class TestListOverlap:
    def test_full_overlap(self):
        match, detail = _list_overlap(
            ["Patrick Collison", "John Collison"],
            "Patrick Collison | John Collison",
        )
        assert match is True
        assert "2/2" in detail

    def test_partial_overlap(self):
        match, detail = _list_overlap(
            ["Patrick Collison", "John Collison"],
            "Patrick Collison | Someone Else",
        )
        assert match is True
        assert "1/2" in detail

    def test_no_overlap(self):
        match, detail = _list_overlap(
            ["Patrick Collison"],
            "Elon Musk",
        )
        assert match is False
        assert "0/1" in detail


class TestBestConfidence:
    def test_high_wins(self):
        assert _best_confidence(["low", "high", "medium"]) == "high"

    def test_medium_if_no_high(self):
        assert _best_confidence(["low", "medium"]) == "medium"

    def test_unknown_fallback(self):
        assert _best_confidence(["unknown"]) == "unknown"

    def test_empty_list(self):
        assert _best_confidence([]) == "unknown"


# ── Unit tests: EvalResults ──────────────────────────────────────


class TestEvalResults:
    def test_accuracy_calculation(self):
        results = EvalResults(
            company_name="TestCo",
            field_results=[
                FieldResult("f1", "research", "a", "a", "high", True, "exact"),
                FieldResult("f2", "research", "b", "c", "high", False, "mismatch"),
            ],
        )
        assert results.accuracy == pytest.approx(0.5)
        assert results.matched_fields == 1
        assert results.total_fields == 2

    def test_accuracy_empty(self):
        results = EvalResults(company_name="TestCo")
        assert results.accuracy == pytest.approx(0.0)

    def test_missing_fields_count(self):
        results = EvalResults(
            company_name="TestCo",
            field_results=[
                FieldResult("f1", "research", "a", "(missing)", "unknown", False, "missing"),
                FieldResult("f2", "research", "b", "b", "high", True, "exact"),
            ],
        )
        assert results.missing_fields == 1

    def test_by_agent(self):
        results = EvalResults(
            company_name="TestCo",
            field_results=[
                FieldResult("f1", "research", "a", "a", "high", True, "exact"),
                FieldResult("f2", "financial", "b", "b", "high", True, "exact"),
            ],
        )
        by_agent = results.by_agent()
        assert "research" in by_agent
        assert "financial" in by_agent
        assert len(by_agent["research"]) == 1

    def test_by_confidence(self):
        results = EvalResults(
            company_name="TestCo",
            field_results=[
                FieldResult("f1", "research", "a", "a", "high", True, "exact"),
                FieldResult("f2", "research", "b", "c", "high", False, "mismatch"),
                FieldResult("f3", "research", "d", "d", "low", True, "exact"),
            ],
        )
        cal = results.by_confidence()
        assert cal["high"]["total"] == 2
        assert cal["high"]["correct"] == 1
        assert cal["low"]["total"] == 1
        assert cal["low"]["correct"] == 1

    def test_trace_id(self):
        results = EvalResults(company_name="TestCo", trace_id="abc123")
        assert results.trace_id == "abc123"


# ── Integration: GroundTruthEvaluator ────────────────────────────


class TestGroundTruthEvaluator:
    def test_available_companies(self):
        evaluator = GroundTruthEvaluator()
        companies = evaluator.available_companies()
        assert "stripe" in companies
        assert "anthropic" in companies
        assert "shopify" in companies

    def test_load_ground_truth(self):
        evaluator = GroundTruthEvaluator()
        gt = evaluator.load_ground_truth("stripe")
        assert gt["company_name"] == "Stripe"
        assert gt["research"]["founded_year"] == "2010"

    def test_load_missing_company(self):
        evaluator = GroundTruthEvaluator()
        with pytest.raises(FileNotFoundError):
            evaluator.load_ground_truth("nonexistent_corp")

    def test_evaluate_research_exact_match(self):
        evaluator = GroundTruthEvaluator()
        research_data = {
            "company_name": "Stripe",
            "founded_year": make_dp("2010"),
            "headquarters": make_dp("San Francisco"),
            "industry": make_dp("Fintech"),
            "website": make_dp("https://stripe.com"),
            "key_leadership": [make_dp("Patrick Collison"), make_dp("John Collison")],
            "key_products": [make_dp("Stripe Payments"), make_dp("Stripe Connect")],
        }
        results = evaluator.evaluate("stripe", research_data=research_data)
        assert results.company_name == "Stripe"
        assert results.total_fields == 6  # 4 scalar + 2 list
        assert results.accuracy > 0.8  # most should match

    def test_evaluate_research_fuzzy_match(self):
        evaluator = GroundTruthEvaluator()
        research_data = {
            "company_name": "Stripe",
            "founded_year": make_dp("2010"),
            "headquarters": make_dp("San Francisco, California, USA"),
            "industry": make_dp("Financial technology (fintech)"),
            "website": make_dp("https://stripe.com"),
            "key_leadership": [],
            "key_products": [],
        }
        results = evaluator.evaluate("stripe", research_data=research_data)
        # headquarters and industry should fuzzy match
        hq = next(f for f in results.field_results if f.field_name == "headquarters")
        assert hq.match is True
        assert hq.match_type == "fuzzy"

    def test_evaluate_missing_fields(self):
        evaluator = GroundTruthEvaluator()
        research_data = {
            "company_name": "Stripe",
            "founded_year": make_dp("unknown", "unknown"),
            "headquarters": None,
        }
        results = evaluator.evaluate("stripe", research_data=research_data)
        missing = [f for f in results.field_results if f.match_type == "missing"]
        assert len(missing) >= 2  # founded_year (unknown) and headquarters (None)

    def test_evaluate_financial(self):
        evaluator = GroundTruthEvaluator()
        financial_data = {
            "company_name": "Stripe",
            "revenue": make_dp("$5.1 billion in 2024"),
            "revenue_model": make_dp("Transaction-based fees"),
            "key_investors": [
                make_dp("Sequoia Capital"),
                make_dp("Andreessen Horowitz"),
                make_dp("Tiger Global"),
            ],
        }
        results = evaluator.evaluate("stripe", financial_data=financial_data)
        assert results.total_fields == 3
        rev = next(f for f in results.field_results if f.field_name == "revenue")
        assert rev.match is True  # "billion" fuzzy matches "$5.1 billion in 2024"
        rm = next(f for f in results.field_results if f.field_name == "revenue_model")
        assert rm.match is True  # "transaction fees" fuzzy matches "Transaction-based fees"

    def test_evaluate_both_agents(self):
        evaluator = GroundTruthEvaluator()
        research_data = {
            "company_name": "Stripe",
            "founded_year": make_dp("2010"),
            "headquarters": make_dp("San Francisco"),
            "industry": make_dp("Fintech"),
            "website": make_dp("https://stripe.com"),
            "key_leadership": [make_dp("Patrick Collison")],
            "key_products": [make_dp("Stripe Payments")],
        }
        financial_data = {
            "company_name": "Stripe",
            "revenue_model": make_dp("Transaction fees"),
            "key_investors": [make_dp("Sequoia Capital")],
        }
        results = evaluator.evaluate(
            "stripe", research_data=research_data, financial_data=financial_data,
            trace_id="test123",
        )
        by_agent = results.by_agent()
        assert "research" in by_agent
        assert "financial" in by_agent
        assert results.trace_id == "test123"

    def test_evaluate_with_no_data(self):
        evaluator = GroundTruthEvaluator()
        results = evaluator.evaluate("stripe")
        assert results.total_fields == 0
        assert results.accuracy == pytest.approx(0.0)

    def test_evaluate_anthropic(self):
        evaluator = GroundTruthEvaluator()
        research_data = {
            "company_name": "Anthropic",
            "founded_year": make_dp("2021"),
            "headquarters": make_dp("San Francisco"),
            "industry": make_dp("AI safety and research"),
            "website": make_dp("https://anthropic.com"),
            "key_leadership": [make_dp("Dario Amodei"), make_dp("Daniela Amodei")],
            "key_products": [make_dp("Claude AI assistant")],
        }
        results = evaluator.evaluate("anthropic", research_data=research_data)
        assert results.accuracy > 0.8


# ── Persistence ──────────────────────────────────────────────────


class TestPersistEvalResults:
    def test_persist_and_read(self, tmp_path):
        db_path = str(tmp_path / "test_eval.db")
        results = EvalResults(
            company_name="TestCo",
            trace_id="t1",
            field_results=[
                FieldResult("founded_year", "research", "2010", "2010", "high", True, "exact"),
                FieldResult("headquarters", "research", "SF", "(missing)", "unknown", False, "missing"),
            ],
        )
        persist_eval_results(results, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM eval_results ORDER BY field_name"
        ).fetchall()]
        conn.close()

        assert len(rows) == 2
        assert rows[0]["field_name"] == "founded_year"
        assert rows[0]["match"] == 1
        assert rows[0]["trace_id"] == "t1"
        assert rows[1]["field_name"] == "headquarters"
        assert rows[1]["match"] == 0
        assert rows[1]["match_type"] == "missing"

    def test_persist_upsert(self, tmp_path):
        """Verify INSERT OR REPLACE works for re-evaluations."""
        db_path = str(tmp_path / "test_eval.db")
        results1 = EvalResults(
            company_name="TestCo",
            trace_id="t1",
            field_results=[
                FieldResult("founded_year", "research", "2010", "2011", "high", False, "mismatch"),
            ],
        )
        persist_eval_results(results1, db_path)

        results2 = EvalResults(
            company_name="TestCo",
            trace_id="t1",
            field_results=[
                FieldResult("founded_year", "research", "2010", "2010", "high", True, "exact"),
            ],
        )
        persist_eval_results(results2, db_path)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()
        match = conn.execute("SELECT match FROM eval_results").fetchone()
        conn.close()

        assert rows[0] == 1  # upserted, not duplicated
        assert match[0] == 1  # updated to correct

    def test_persist_without_trace_id(self, tmp_path):
        db_path = str(tmp_path / "test_eval.db")
        results = EvalResults(
            company_name="TestCo",
            field_results=[
                FieldResult("f1", "research", "a", "a", "high", True, "exact"),
            ],
        )
        persist_eval_results(results, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM eval_results").fetchone())
        conn.close()
        assert row["trace_id"] == ""


# ── Cross-compatibility with existing tracer ─────────────────────


class TestEvalTracerIntegration:
    def test_eval_results_share_db_with_tracer(self, tmp_path):
        """Verify eval_results table coexists with runs/spans tables."""
        from src.observability.tracer import AgentTracer

        db_path = str(tmp_path / "shared.db")

        # Write tracer data
        tracer = AgentTracer("TestCo")
        span = tracer.start_span("test_llm", "research", "llm_call")
        tracer.end_span(span, input_tokens=100, output_tokens=50, model="claude-sonnet-4-20250514")
        tracer.persist(db_path)

        # Write eval data linked to same trace
        results = EvalResults(
            company_name="TestCo",
            trace_id=tracer.run_id,
            field_results=[
                FieldResult("founded_year", "research", "2020", "2020", "high", True, "exact"),
            ],
        )
        persist_eval_results(results, db_path)

        # Verify both tables exist and can be joined
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        runs = conn.execute("SELECT * FROM runs").fetchall()
        assert len(runs) == 1

        evals = conn.execute("SELECT * FROM eval_results").fetchall()
        assert len(evals) == 1

        # Join works
        joined = conn.execute(
            "SELECT r.company_name, e.field_name, e.match "
            "FROM runs r JOIN eval_results e ON r.trace_id = e.trace_id"
        ).fetchall()
        assert len(joined) == 1
        assert dict(joined[0])["field_name"] == "founded_year"

        conn.close()

    def test_eval_results_share_db_with_agent_db(self, tmp_path):
        """Verify eval_results coexists with agent_runs/llm_calls/etc."""
        from src.observability.agent_db import AgentDB

        db_path = str(tmp_path / "shared2.db")

        # Write agent_db data
        db = AgentDB(db_path)
        db.start_run("research", "test task", company_name="TestCo", trace_id="t1")
        db.end_run("t1", "research", "complete", 1, 0, 100, 50, 0.01)

        # Write eval data
        results = EvalResults(
            company_name="TestCo",
            trace_id="t1",
            field_results=[
                FieldResult("founded_year", "research", "2020", "2020", "high", True, "exact"),
            ],
        )
        persist_eval_results(results, db_path)

        # Join across layers
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        joined = conn.execute(
            "SELECT ar.agent, ar.status, e.field_name, e.match "
            "FROM agent_runs ar JOIN eval_results e "
            "ON ar.trace_id = e.trace_id AND ar.agent = e.agent"
        ).fetchall()
        assert len(joined) == 1
        row = dict(joined[0])
        assert row["status"] == "complete"
        assert row["match"] == 1

        conn.close()
