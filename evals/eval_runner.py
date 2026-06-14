"""
Ground truth evaluation for agent outputs.

Compares structured agent output (CompanyResearch, CompanyFinancials, etc.)
against curated ground truth fixtures to measure factual accuracy and
confidence calibration.

Usage:
    from evals.eval_runner import GroundTruthEvaluator

    evaluator = GroundTruthEvaluator()
    results = evaluator.evaluate("stripe", research_data, financial_data)
    evaluator.print_scorecard(results)
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


GROUND_TRUTH_DIR = Path(__file__).parent / "ground_truth"


@dataclass
class FieldResult:
    """Result of evaluating a single field against ground truth."""
    field_name: str
    agent: str
    expected: str
    actual: str
    confidence: str
    match: bool
    match_type: str  # "exact", "fuzzy", "contains", "list_overlap", "missing"


@dataclass
class EvalResults:
    """Aggregated evaluation results for one company."""
    company_name: str
    field_results: List[FieldResult] = field(default_factory=list)
    trace_id: Optional[str] = None

    @property
    def total_fields(self) -> int:
        return len(self.field_results)

    @property
    def matched_fields(self) -> int:
        return sum(1 for f in self.field_results if f.match)

    @property
    def accuracy(self) -> float:
        if self.total_fields == 0:
            return 0.0
        return self.matched_fields / self.total_fields

    @property
    def missing_fields(self) -> int:
        return sum(1 for f in self.field_results if f.match_type == "missing")

    def by_agent(self) -> Dict[str, List[FieldResult]]:
        agents: Dict[str, List[FieldResult]] = {}
        for fr in self.field_results:
            agents.setdefault(fr.agent, []).append(fr)
        return agents

    def by_confidence(self) -> Dict[str, Dict[str, int]]:
        """Accuracy breakdown by confidence level."""
        buckets: Dict[str, Dict[str, int]] = {}
        for fr in self.field_results:
            conf = fr.confidence
            if conf not in buckets:
                buckets[conf] = {"total": 0, "correct": 0}
            buckets[conf]["total"] += 1
            if fr.match:
                buckets[conf]["correct"] += 1
        return buckets


def _normalize(value: str) -> str:
    """Normalize a string for comparison."""
    return value.strip().lower().replace(",", "").replace(".", "").replace("-", " ")


def _fuzzy_match(expected: str, actual: str) -> bool:
    """Check if expected matches actual via substring or word overlap.

    Matches if:
    - One string contains the other (substring), OR
    - All words in the expected value appear in the actual value (word overlap)
    """
    e = _normalize(expected)
    a = _normalize(actual)
    if not e or not a:
        return False
    # Substring containment
    if e in a or a in e:
        return True
    # Word overlap: all expected words present in actual
    expected_words = set(e.split())
    actual_words = set(a.split())
    if expected_words and expected_words.issubset(actual_words):
        return True
    return False


def _list_overlap(expected_list: List[str], actual_value: str) -> tuple[bool, str]:
    """Check how many expected items appear in the actual value.

    Returns (has_any_match, match_detail).
    """
    actual_norm = _normalize(actual_value)
    found = [item for item in expected_list if _normalize(item) in actual_norm]
    if found:
        return True, f"{len(found)}/{len(expected_list)} found"
    return False, f"0/{len(expected_list)} found"


class GroundTruthEvaluator:
    """Evaluates agent outputs against ground truth fixtures."""

    def __init__(self, ground_truth_dir: Optional[Path] = None):
        self.ground_truth_dir = ground_truth_dir or GROUND_TRUTH_DIR

    def load_ground_truth(self, company_key: str) -> dict:
        """Load ground truth for a company by key (e.g., 'stripe')."""
        path = self.ground_truth_dir / f"{company_key}.json"
        if not path.exists():
            raise FileNotFoundError(f"No ground truth fixture at {path}")
        with open(path) as f:
            return json.load(f)

    def available_companies(self) -> List[str]:
        """List company keys that have ground truth fixtures."""
        return sorted(
            p.stem for p in self.ground_truth_dir.glob("*.json")
        )

    def evaluate(
        self,
        company_key: str,
        research_data: Optional[dict] = None,
        financial_data: Optional[dict] = None,
        trace_id: Optional[str] = None,
    ) -> EvalResults:
        """Evaluate agent outputs against ground truth.

        Args:
            company_key: Key to load ground truth (e.g., "stripe")
            research_data: CompanyResearch dict from research agent
            financial_data: CompanyFinancials dict from financial agent
            trace_id: Optional trace_id to link eval to a pipeline run
        """
        gt = self.load_ground_truth(company_key)
        results = EvalResults(company_name=gt["company_name"], trace_id=trace_id)

        if research_data and "research" in gt:
            self._eval_research(gt["research"], research_data, results)

        if financial_data and "financial" in gt:
            self._eval_financial(gt["financial"], financial_data, results)

        return results

    def _eval_research(self, gt: dict, data: dict, results: EvalResults):
        """Evaluate research agent output fields."""
        # Scalar DataPoint fields
        scalar_fields = ["founded_year", "headquarters", "industry", "website"]
        for field_name in scalar_fields:
            if field_name not in gt:
                continue
            self._eval_scalar_field(
                field_name=field_name,
                agent="research",
                expected=gt[field_name],
                data_point=data.get(field_name),
                results=results,
            )

        # List DataPoint fields (key_leadership, key_products)
        list_fields = ["key_leadership", "key_products"]
        for field_name in list_fields:
            if field_name not in gt:
                continue
            self._eval_list_field(
                field_name=field_name,
                agent="research",
                expected_items=gt[field_name],
                data_points=data.get(field_name, []),
                results=results,
            )

    def _eval_financial(self, gt: dict, data: dict, results: EvalResults):
        """Evaluate financial agent output fields."""
        scalar_fields = ["revenue", "revenue_model"]
        for field_name in scalar_fields:
            if field_name not in gt:
                continue
            self._eval_scalar_field(
                field_name=field_name,
                agent="financial",
                expected=gt[field_name],
                data_point=data.get(field_name),
                results=results,
            )

        list_fields = ["key_investors"]
        for field_name in list_fields:
            if field_name not in gt:
                continue
            self._eval_list_field(
                field_name=field_name,
                agent="financial",
                expected_items=gt[field_name],
                data_points=data.get(field_name, []),
                results=results,
            )

    def _eval_scalar_field(
        self,
        field_name: str,
        agent: str,
        expected: str,
        data_point: Optional[dict],
        results: EvalResults,
    ):
        """Evaluate a single scalar DataPoint against expected value."""
        if data_point is None or data_point.get("value", "unknown") == "unknown":
            results.field_results.append(FieldResult(
                field_name=field_name,
                agent=agent,
                expected=expected,
                actual="(missing)",
                confidence="unknown",
                match=False,
                match_type="missing",
            ))
            return

        actual = data_point["value"]
        confidence = data_point.get("confidence", "unknown")

        # Try exact match first, then fuzzy
        if _normalize(expected) == _normalize(actual):
            match, match_type = True, "exact"
        elif _fuzzy_match(expected, actual):
            match, match_type = True, "fuzzy"
        else:
            match, match_type = False, "mismatch"

        results.field_results.append(FieldResult(
            field_name=field_name,
            agent=agent,
            expected=expected,
            actual=actual,
            confidence=confidence,
            match=match,
            match_type=match_type,
        ))

    def _eval_list_field(
        self,
        field_name: str,
        agent: str,
        expected_items: List[str],
        data_points: List[dict],
        results: EvalResults,
    ):
        """Evaluate a list of DataPoints against expected items.

        Concatenates all DataPoint values and checks how many expected
        items appear in the combined text.
        """
        if not data_points:
            results.field_results.append(FieldResult(
                field_name=field_name,
                agent=agent,
                expected=", ".join(expected_items),
                actual="(missing)",
                confidence="unknown",
                match=False,
                match_type="missing",
            ))
            return

        # Combine all values for matching
        combined = " | ".join(dp.get("value", "") for dp in data_points)
        # Use highest confidence from the list
        confidences = [dp.get("confidence", "unknown") for dp in data_points]
        confidence = _best_confidence(confidences)

        has_match, detail = _list_overlap(expected_items, combined)

        results.field_results.append(FieldResult(
            field_name=field_name,
            agent=agent,
            expected=", ".join(expected_items),
            actual=combined[:200],
            confidence=confidence,
            match=has_match,
            match_type=f"list_overlap ({detail})",
        ))


def _best_confidence(confidences: List[str]) -> str:
    """Return the highest confidence level from a list."""
    order = ["high", "medium", "low", "unknown"]
    for level in order:
        if level in confidences:
            return level
    return "unknown"


# ── SQLite persistence ──────────────────────────────────────────

def persist_eval_results(results: EvalResults, db_path: str):
    """Write eval results to the same observability DB.

    Creates an `eval_results` table if it doesn't exist.
    Each row is one field evaluation, linked by trace_id.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            trace_id TEXT,
            company_name TEXT NOT NULL,
            agent TEXT NOT NULL,
            field_name TEXT NOT NULL,
            expected TEXT NOT NULL,
            actual TEXT NOT NULL,
            confidence TEXT,
            match INTEGER NOT NULL,
            match_type TEXT NOT NULL,
            PRIMARY KEY (trace_id, agent, field_name)
        )
    """)

    for fr in results.field_results:
        conn.execute(
            """INSERT OR REPLACE INTO eval_results
                (trace_id, company_name, agent, field_name, expected, actual,
                 confidence, match, match_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                results.trace_id or "", results.company_name, fr.agent,
                fr.field_name, fr.expected, fr.actual, fr.confidence,
                1 if fr.match else 0, fr.match_type,
            ),
        )

    conn.commit()
    conn.close()


# ── Pretty printing ─────────────────────────────────────────────

def print_scorecard(results: EvalResults):
    """Print a formatted eval scorecard to the terminal."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print()
    console.rule(f"[bold blue]Eval: {results.company_name}[/bold blue]")

    if results.trace_id:
        console.print(f"  Trace: {results.trace_id}")

    # Overall accuracy
    console.print(
        f"  Accuracy: {results.matched_fields}/{results.total_fields} "
        f"({results.accuracy:.0%})"
    )
    console.print(f"  Missing:  {results.missing_fields}")

    # Field-level table
    table = Table(title="Field Results")
    table.add_column("Agent", style="cyan")
    table.add_column("Field")
    table.add_column("Expected")
    table.add_column("Actual")
    table.add_column("Confidence")
    table.add_column("Match")
    table.add_column("Type")

    for fr in results.field_results:
        match_style = "green" if fr.match else "red"
        match_symbol = "Y" if fr.match else "N"
        table.add_row(
            fr.agent,
            fr.field_name,
            fr.expected[:40],
            fr.actual[:40],
            fr.confidence,
            f"[{match_style}]{match_symbol}[/{match_style}]",
            fr.match_type,
        )

    console.print(table)

    # Confidence calibration
    cal = results.by_confidence()
    if cal:
        cal_table = Table(title="Confidence Calibration")
        cal_table.add_column("Confidence")
        cal_table.add_column("Correct", justify="right")
        cal_table.add_column("Total", justify="right")
        cal_table.add_column("Accuracy", justify="right")

        for level in ["high", "medium", "low", "unknown"]:
            if level in cal:
                b = cal[level]
                acc = b["correct"] / b["total"] if b["total"] else 0
                cal_table.add_row(
                    level, str(b["correct"]), str(b["total"]), f"{acc:.0%}"
                )

        console.print(cal_table)

    # Per-agent breakdown
    by_agent = results.by_agent()
    if len(by_agent) > 1:
        agent_table = Table(title="By Agent")
        agent_table.add_column("Agent", style="cyan")
        agent_table.add_column("Correct", justify="right")
        agent_table.add_column("Total", justify="right")
        agent_table.add_column("Accuracy", justify="right")

        for agent_name, fields in by_agent.items():
            correct = sum(1 for f in fields if f.match)
            total = len(fields)
            acc = correct / total if total else 0
            agent_table.add_row(agent_name, str(correct), str(total), f"{acc:.0%}")

        console.print(agent_table)

    console.rule()
