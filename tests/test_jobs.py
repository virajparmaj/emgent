import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from employment_agent import jobs
from tests.helpers import record


class FreshnessTests(unittest.TestCase):
    def test_boundaries(self):
        as_of = date(2026, 10, 4)
        expected = {
            0: "VERY_NEW", 3: "VERY_NEW", 4: "NEW", 7: "NEW", 8: "RECENT",
            14: "RECENT", 15: "ACTIVE", 30: "ACTIVE", 31: "OLD",
        }
        for age, label in expected.items():
            with self.subTest(age=age):
                self.assertEqual(jobs.freshness((as_of - timedelta(days=age)).isoformat(), as_of), label)
        self.assertEqual(jobs.freshness(None, as_of), "UNKNOWN")

    def test_future_date_rejected(self):
        with self.assertRaisesRegex(ValueError, "Future posting date"):
            jobs.freshness("2026-10-05", date(2026, 10, 4))


class CanonicalUrlTests(unittest.TestCase):
    def test_tracking_fragment_order_and_trailing_slash(self):
        value = jobs.canonicalize("https://EXAMPLE.com/jobs/42/?b=2&utm_source=x&a=1#apply")
        self.assertEqual(value, "https://example.com/jobs/42?a=1&b=2")

    def test_greenhouse_identifier_is_functional(self):
        first = jobs.canonicalize("https://boards.greenhouse.io/acme?gh_jid=111&utm_medium=x")
        second = jobs.canonicalize("https://job-boards.greenhouse.io/acme?gh_jid=222")
        self.assertNotEqual(first, second)
        self.assertIn("gh_jid=111", first)

    def test_ats_and_company_urls_preserve_unknown_queries(self):
        fixtures = [
            "https://jobs.lever.co/acme/abc?department=risk&lever-source=feed",
            "https://jobs.ashbyhq.com/acme/abc?location=US",
            "https://acme.wd1.myworkdayjobs.com/jobs/job/Analyst_R123?source=careers",
            "https://careers.example.com/job?id=123&referrer=internal",
        ]
        results = [jobs.canonicalize(value) for value in fixtures]
        self.assertNotIn("lever-source", results[0])
        self.assertIn("location=US", results[1])
        self.assertIn("source=careers", results[2])
        self.assertIn("referrer=internal", results[3])


class PolicyCalculationTests(unittest.TestCase):
    def test_geography_scoring(self):
        self.assertEqual(jobs.geography_points("Chicago, IL", "US"), 5)
        self.assertEqual(jobs.geography_points("Remote US; Phoenix, AZ", "US"), 5)
        self.assertEqual(jobs.geography_points("Boston, MA", "US"), 4)
        self.assertEqual(jobs.geography_points("Seattle, WA", "US"), 3)
        self.assertEqual(jobs.geography_points("Austin, TX", "US"), 2)
        self.assertEqual(jobs.geography_points("Berlin", "Germany"), 0)

    def test_prepare_calculates_scores_and_accepts_missing_derived(self):
        item = record(source_file="runs/test/evidence/job.json")
        item.pop("source_type")
        item["ats_source"] = "Company careers / Workday"
        row = jobs.prepare(item, date(2026, 10, 4))
        score = __import__("json").loads(row["score_breakdown"])
        self.assertEqual(score["company_priority"], 5)
        self.assertEqual(score["geography"], 5)
        self.assertEqual(score["recency"], 5)
        self.assertEqual(row["fit_score"], sum(score.values()))
        self.assertEqual(row["source_type"], "WORKDAY")

    def test_inconsistent_and_out_of_bounds_scores_rejected(self):
        item = record(source_file="runs/test/evidence/job.json")
        item["score_breakdown"]["geography"] = 2
        with self.assertRaisesRegex(ValueError, "geography"):
            jobs.prepare(item, date(2026, 10, 4))
        item = record(source_file="runs/test/evidence/job.json")
        item["score_breakdown"]["statistics_ml"] = 31
        with self.assertRaisesRegex(ValueError, "statistics_ml"):
            jobs.prepare(item, date(2026, 10, 4))

    def test_nulls_remain_null(self):
        row = jobs.prepare(record(source_file="runs/test/evidence/job.json"), date(2026, 10, 4))
        self.assertIsNone(row["salary_minimum"])
        self.assertIsNone(row["required_years_experience"])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.db"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        jobs.migrate(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def prepared(self, **kwargs):
        return jobs.prepare(record(source_file="runs/test/evidence/job.json", **kwargs), date(2026, 10, 4))

    def test_employer_scoped_job_ids(self):
        jobs.upsert(self.db, self.prepared(index=1, company="A", job_id="42"))
        jobs.upsert(self.db, self.prepared(index=2, company="B", job_id="42"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_all_duplicate_identities_and_conflict(self):
        first = self.prepared(index=1, company="A", job_id="1")
        second = self.prepared(index=2, company="A", job_id="2")
        jobs.upsert(self.db, first)
        jobs.upsert(self.db, second)
        conflicting = self.prepared(
            index=3, company="A", title="Risk Analyst 2", location="Chicago, IL",
            canonical_url=first["canonical_url"], job_id="3",
        )
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate identities"):
            jobs.upsert(self.db, conflicting)

    def test_status_preserved_and_aggregator_cannot_replace_official(self):
        row = self.prepared(index=1, company="A")
        _, job_id = jobs.upsert(self.db, row)
        self.db.execute("UPDATE jobs SET source_type = NULL, ats_source = 'Greenhouse' WHERE id = ?", (job_id,))
        jobs.set_review_status(self.db, job_id, "review", "GREAT_FIT")
        aggregate = self.prepared(index=1, company="A", source_type="AGGREGATOR")
        outcome, same_id = jobs.upsert(self.db, aggregate)
        stored = self.db.execute("SELECT status, source_type FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual((outcome, same_id), ("retained official source", job_id))
        self.assertEqual((stored["status"], stored["source_type"]), ("review", None))

    def test_review_event_and_migration_are_idempotent(self):
        row = self.prepared(index=1)
        _, job_id = jobs.upsert(self.db, row)
        jobs.set_review_status(self.db, job_id, "skip", "TOO_SENIOR", "Minimum is too high")
        jobs.migrate(self.db)
        event = self.db.execute("SELECT reason_code, note FROM review_events").fetchone()
        self.assertEqual(tuple(event), ("TOO_SENIOR", "Minimum is too high"))
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], jobs.SCHEMA_VERSION)

    def test_additive_migration_does_not_backfill_legacy_source(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY, company TEXT, job_id TEXT, ats_source TEXT);"
            "INSERT INTO jobs(company, job_id, ats_source) VALUES ('Legacy', '7', 'Greenhouse');"
        )
        jobs.migrate(legacy)
        row = legacy.execute("SELECT ats_source, source_type, source_detail FROM jobs").fetchone()
        self.assertEqual(row, ("Greenhouse", None, None))
        jobs.migrate(legacy)
        self.assertEqual(legacy.execute("PRAGMA user_version").fetchone()[0], jobs.SCHEMA_VERSION)
        legacy.close()


if __name__ == "__main__":
    unittest.main()
