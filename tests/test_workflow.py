import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

from employment_agent import jobs, run_manager, workflow
from tests.helpers import isolated_runs, record


class FakeRunner(workflow.CommandRunner):
    def __init__(self, empty_search=False):
        self.calls = []
        self.empty_search = empty_search

    def run(self, command, output_path):
        self.calls.append(command)
        operation = command[1]
        if operation == "search":
            web = [] if self.empty_search else [{
                "url": "https://jobs.example.com/role?utm_source=test",
                "title": "Risk Analyst", "description": "Credit risk data Python SQL",
            }]
            payload = {"success": True, "data": {"web": web}, "creditsUsed": 1}
        elif operation == "scrape":
            payload = {
                "markdown": ("Risk analyst responsibilities and qualifications. " * 20),
                "links": ["https://jobs.example.com/role/apply"],
                "metadata": {"title": "Risk Analyst", "scrapeId": "scrape-1", "creditsUsed": 1},
            }
        else:
            payload = {"success": True, "data": []}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = isolated_runs(self.root)
        self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def make_plan(self, budgets=None, zero_yield=4):
        return workflow.create_plan(
            date(2026, 10, 4), "fixture", budgets=budgets, zero_yield_stop=zero_yield
        )

    def test_plan_defaults_and_target_limit(self):
        run_dir = self.make_plan()
        plan = json.loads((run_dir / "plan.json").read_text())
        self.assertEqual(plan["target_count"], 10)
        self.assertEqual(plan["operation_budgets"], workflow.DEFAULT_BUDGETS)
        self.assertEqual(plan["network_execution"], "explicit_execute_flag_only")
        with self.assertRaisesRegex(ValueError, "at most ten"):
            workflow.create_plan(date(2026, 10, 4), "too large", target_count=11)

    def test_discovery_dry_run_never_calls_runner(self):
        run_dir = self.make_plan()
        with mock.patch.object(workflow.CommandRunner, "run", side_effect=AssertionError("network")):
            request_plan = workflow.generate_discovery_plan(run_dir)
        self.assertEqual(request_plan["request_count"], 20)
        self.assertTrue((run_dir / "discovery" / "request-plan.json").is_file())
        self.assertFalse(any((run_dir / spec["output"]).exists() for spec in request_plan["requests"]))

    def test_budget_enforcement_and_fingerprint_cache(self):
        run_dir = self.make_plan({"search": 1, "scrape": 0, "map": 0, "crawl": 0, "interact": 0})
        plan = workflow.load_plan(run_dir)
        spec = {
            "operation": "search", "parameters": {"query": "risk", "limit": 1, "sources": "web", "country": "US"},
            "output": "discovery/one.json",
        }
        runner = FakeRunner()
        first = workflow.execute_request(run_dir, spec, plan, runner)
        second = workflow.execute_request(run_dir, spec, plan, runner)
        self.assertEqual((first["status"], second["status"]), ("successful", "cached"))
        self.assertEqual(len(runner.calls), 1)
        other = dict(spec)
        other["parameters"] = dict(spec["parameters"], query="fraud")
        other["output"] = "discovery/two.json"
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            workflow.execute_request(run_dir, other, plan, runner)

    def test_zero_yield_stopping_rule(self):
        budgets = {"search": 5, "scrape": 0, "map": 0, "crawl": 0, "interact": 0}
        run_dir = self.make_plan(budgets, zero_yield=2)
        runner = FakeRunner(empty_search=True)
        result = workflow.execute_discovery(run_dir, runner)
        self.assertTrue(result["stopped_for_zero_yield"])
        self.assertEqual(len(runner.calls), 2)

    def test_triage_normalizes_and_merges_provenance(self):
        run_dir = self.make_plan({"search": 2, "scrape": 0, "map": 0, "crawl": 0, "interact": 0})
        request_plan = workflow.generate_discovery_plan(run_dir)
        urls = [
            "https://jobs.example.com/role?utm_source=one",
            "https://jobs.example.com/role?utm_medium=two#apply",
        ]
        for spec, url in zip(request_plan["requests"], urls):
            output = run_dir / spec["output"]
            output.write_text(json.dumps({"data": {"web": [{"url": url, "title": "Risk Analyst"}]}}))
        ledger = workflow.triage(run_dir, self.root / "missing.db")
        self.assertEqual(ledger["counts"]["unique"], 1)
        self.assertEqual(len(ledger["candidates"][0]["provenance"]), 2)
        self.assertFalse(ledger["candidates"][0]["verified"])
        self.assertIsNone(ledger["candidates"][0]["open_status"])

    def test_verification_dry_run_and_digest(self):
        run_dir = self.make_plan({"search": 0, "scrape": 2, "map": 1, "crawl": 1, "interact": 1})
        candidate = {
            "candidate_id": "abc123", "canonical_url": "https://jobs.example.com/role",
            "title_hint": "Risk Analyst", "company_hint": "Example", "decision": "retained",
            "verified": False,
        }
        workflow.write_json(run_dir / "candidates.json", {"candidates": [candidate]})
        with mock.patch.object(workflow.CommandRunner, "run", side_effect=AssertionError("network")):
            prepared = workflow.generate_verification_plan(run_dir)
        self.assertEqual(len(prepared["pipelines"]), 1)
        runner = FakeRunner()
        result = workflow.execute_verification(run_dir, runner)
        self.assertEqual(result["digests"], 1)
        self.assertEqual(len(runner.calls), 1)
        digest_path = run_dir / "evidence-digests" / "abc123.json"
        digest = json.loads(digest_path.read_text())
        self.assertEqual(digest["fields"]["salary"]["basis"], "unknown")
        self.assertEqual(digest["fields"]["canonical_source_sha256"]["basis"], "calculated")

    def test_review_status_reasons_and_feedback(self):
        db_path = self.root / "jobs.db"
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            jobs.migrate(db)
            row = jobs.prepare(record(source_file="runs/test/evidence/job.json"), date(2026, 10, 4))
            _, job_id = jobs.upsert(db, row)
            db.commit()
        workflow.review_set(db_path, job_id, "skip", "TOO_SENIOR", "Requires five years")
        shown = workflow.review_show(db_path, job_id)
        self.assertEqual(shown["status"], "skip")
        self.assertEqual(shown["review_events"][0]["reason_code"], "TOO_SENIOR")
        summary = workflow.feedback_summary(db_path)
        self.assertEqual(summary["groups"]["reason"]["TOO_SENIOR"], 1)
        self.assertTrue(summary["proposed_adaptations"])

    def test_coverage_reports_concentration_without_changing_rows(self):
        db_path = self.root / "coverage.db"
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            jobs.migrate(db)
            for index in range(1, 11):
                company = "Concentrated" if index <= 3 else f"Company {index}"
                row = jobs.prepare(
                    record(index=index, company=company, source_file="runs/test/evidence/job.json"),
                    date(2026, 10, 4),
                )
                jobs.upsert(db, row)
            rows = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            section = run_manager._coverage_section(rows, [dict(row) for row in rows])
            db.commit()
        self.assertEqual(section["selected_job_count"], 10)
        self.assertIn("At least one company exceeds the 2-job soft cap", section["warnings"])
        self.assertIn("An over-cap selection lacks a documented diversity exception", section["warnings"])


if __name__ == "__main__":
    unittest.main()
