import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

from employment_agent import run_manager
from tests.helpers import isolated_runs, populate_run, record


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = isolated_runs(self.root)
        self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def make_run(self, count=2):
        run_dir = run_manager.create_run(date(2026, 10, 4), "fixture", count)
        records = [record(index=index) for index in range(1, count + 1)]
        populate_run(run_dir, records)
        return run_dir, records

    def test_success_manifest_hashes_and_repeat_refusal(self):
        run_dir, _ = self.make_run(2)
        result = run_manager.finalize_run(run_dir, 2)
        self.assertEqual(result["jobs"], 2)
        metadata = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(metadata["status"], "complete")
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["job_count"], 2)
        csv_entry = next(item for item in manifest["artifacts"] if item["path"].endswith("/jobs.csv"))
        self.assertEqual(csv_entry["sha256"], hashlib.sha256((run_dir / "jobs.csv").read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.root / "jobs.db")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        with self.assertRaisesRegex(ValueError, "already complete"):
            run_manager.finalize_run(run_dir)

    def test_target_mismatch_leaves_outputs_unchanged(self):
        run_dir = run_manager.create_run(date(2026, 10, 4), "fixture", 2)
        records = [record(index=1)]
        populate_run(run_dir, records)
        before_metadata = (run_dir / "run.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Expected 2"):
            run_manager.finalize_run(run_dir)
        self.assertFalse((self.root / "jobs.db").exists())
        self.assertFalse((run_dir / "jobs.csv").exists())
        self.assertEqual((run_dir / "run.json").read_bytes(), before_metadata)

    def test_duplicate_within_run_rejected_before_database_change(self):
        run_dir, records = self.make_run(2)
        records[1]["canonical_url"] = records[0]["canonical_url"]
        records[1]["job_url"] = records[0]["job_url"]
        populate_run(run_dir, records)
        with self.assertRaisesRegex(ValueError, "Duplicate canonical_url"):
            run_manager.finalize_run(run_dir)
        self.assertFalse((self.root / "jobs.db").exists())
        self.assertFalse((run_dir / "manifest.json").exists())

    def test_job_id_and_title_location_duplicates_within_run(self):
        for identity in ("job_id", "title_location"):
            with self.subTest(identity=identity):
                run_dir = run_manager.create_run(date(2026, 10, 6), identity, 2)
                records = [record(index=20), record(index=21)]
                if identity == "job_id":
                    records[0]["job_id"] = records[1]["job_id"] = "same"
                    records[1]["company"] = records[0]["company"]
                else:
                    records[1]["company"] = records[0]["company"].swapcase()
                    records[1]["title"] = records[0]["title"].upper()
                    records[1]["location"] = records[0]["location"].upper()
                populate_run(run_dir, records)
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    run_manager.finalize_run(run_dir)

    def test_expected_count_cannot_override_target(self):
        run_dir, _ = self.make_run(1)
        with self.assertRaisesRegex(ValueError, "cannot override"):
            run_manager.finalize_run(run_dir, 2)

    def test_aggregator_only_selection_cannot_finalize(self):
        run_dir = run_manager.create_run(date(2026, 10, 7), "aggregator", 1)
        records = [record(index=30, source_type="AGGREGATOR")]
        populate_run(run_dir, records)
        with self.assertRaisesRegex(ValueError, "Aggregator-only"):
            run_manager.finalize_run(run_dir)

    def test_missing_traversal_and_symlink_evidence_rejected(self):
        for mode in ("missing", "traversal", "symlink"):
            with self.subTest(mode=mode):
                run_dir = run_manager.create_run(date(2026, 10, 5), mode, 1)
                item = record(index=10 + len(list(run_manager.RUNS.iterdir())))
                if mode == "missing":
                    item["source_file"] = f"runs/{run_dir.name}/evidence/missing.json"
                elif mode == "traversal":
                    item["source_file"] = f"runs/{run_dir.name}/evidence/../README.md"
                else:
                    outside = run_dir / "outside.json"
                    outside.write_text("{}")
                    link = run_dir / "evidence" / "link.json"
                    link.symlink_to(outside)
                    item["source_file"] = link.relative_to(run_manager.ROOT).as_posix()
                (run_dir / "jobs.json").write_text(json.dumps([item]))
                with self.assertRaises(ValueError):
                    run_manager.finalize_run(run_dir)

    def test_promotion_failure_restores_all_targets(self):
        staging = self.root / "stage"
        targets = self.root / "targets"
        staging.mkdir()
        targets.mkdir()
        pairs = []
        for index in range(3):
            source = staging / f"stage{index}"
            target = targets / f"target{index}"
            source.write_text(f"new-{index}")
            target.write_text(f"old-{index}")
            pairs.append((source, target))
        real_replace = os.replace
        failed = False

        def flaky(source, target):
            nonlocal failed
            if Path(source).name == "stage1" and not failed:
                failed = True
                raise OSError("simulated promotion failure")
            return real_replace(source, target)

        with mock.patch("employment_agent.run_manager.os.replace", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "simulated"):
                run_manager._promote(pairs)
        self.assertEqual([path.read_text() for _, path in pairs], ["old-0", "old-1", "old-2"])


if __name__ == "__main__":
    unittest.main()
