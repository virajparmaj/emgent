"""Create and safely finalize self-contained job-research run folders."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import jobs


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
TIMEZONE = ZoneInfo("America/Chicago")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve_run(value: Path) -> Path:
    path = value if value.is_absolute() else ROOT / value
    path = path.resolve()
    if path.parent != RUNS.resolve():
        raise ValueError(f"Run must be a direct child of {RUNS}")
    return path


def index_text(metadata_override: dict | None = None) -> str:
    rows = []
    for metadata_path in sorted(RUNS.glob("*/run.json"), reverse=True):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_override and metadata.get("run_id") == metadata_override.get("run_id"):
            metadata = metadata_override
        run_id = metadata["run_id"]
        rows.append(
            f"| [{run_id}]({run_id}/README.md) | {metadata['date']} | "
            f"{metadata['status']} | {metadata.get('job_count', 0)} | "
            f"{metadata.get('scope', 'Not set')} |"
        )
    body = [
        "# Research runs", "",
        "Each directory is a permanent snapshot of one authorized search. Open its README for",
        "the shortlist, verification notes, and links to saved job data and source evidence.", "",
        "| Run | Date | Status | Jobs | Scope |", "|---|---|---:|---:|---|", *rows, "",
    ]
    return "\n".join(body)


def rebuild_index() -> None:
    RUNS.mkdir(exist_ok=True)
    (RUNS / "README.md").write_text(index_text(), encoding="utf-8")


def create_run(run_date: date, scope: str, target_count: int) -> Path:
    if not 1 <= target_count <= 10:
        raise ValueError("The current policy permits a target count from 1 through 10")
    RUNS.mkdir(exist_ok=True)
    prefix = run_date.isoformat() + "-run-"
    numbers = [
        int(path.name.removeprefix(prefix))
        for path in RUNS.glob(prefix + "[0-9][0-9][0-9]")
        if path.name.removeprefix(prefix).isdigit()
    ]
    run_id = f"{prefix}{max(numbers, default=0) + 1:03d}"
    run_dir = RUNS / run_id
    for child in ["evidence", "evidence-digests", "discovery", "reviewed-not-selected"]:
        (run_dir / child).mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id,
        "date": run_date.isoformat(),
        "status": "in_progress",
        "scope": scope,
        "target_count": target_count,
        "job_count": 0,
        "company_count": 0,
        "created_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "completed_at": None,
        "artifacts": {
            "summary": "README.md", "plan": "plan.json", "jobs_json": "jobs.json",
            "jobs_csv": "jobs.csv", "candidates": "candidates.json", "usage": "usage.jsonl",
            "coverage": "coverage.json", "manifest": "manifest.json", "evidence": "evidence/",
            "evidence_digests": "evidence-digests/", "discovery": "discovery/",
            "reviewed_not_selected": "reviewed-not-selected/",
        },
    }
    write_json(run_dir / "run.json", metadata)
    write_json(run_dir / "jobs.json", [])
    (run_dir / "usage.jsonl").write_text("", encoding="utf-8")
    (run_dir / "README.md").write_text(
        f"# {run_id}\n\n- **Date:** {run_date.isoformat()}\n- **Status:** In progress\n"
        f"- **Scope:** {scope}\n- **Target:** {target_count} jobs\n\n"
        "## Search plan\n\nSee `plan.json` for the structured daily plan and budgets.\n\n"
        "## Run checklist\n\n"
        "- [ ] Prepare and review the discovery request plan.\n"
        "- [ ] Execute only the explicitly authorized Firecrawl requests.\n"
        "- [ ] Triage and deduplicate candidates before scraping.\n"
        "- [ ] Verify canonical postings and preserve evidence.\n"
        "- [ ] Extract facts with explicit nulls and review score judgments.\n"
        "- [ ] Finalize the run and review its manifest and coverage report.\n\n"
        "## Results\n\nAdd the concise reviewed-job table when the run is complete.\n\n"
        "## Verification notes\n\nRecord important source and availability notes here.\n\n"
        "## Reviewed but not selected\n\nRecord the main exclusions and reasons here.\n",
        encoding="utf-8",
    )
    rebuild_index()
    return run_dir


def _path_has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while current != stop:
        if current.is_symlink():
            return True
        current = current.parent
    return stop.is_symlink()


def validate_contained_file(run_dir: Path, relative_value: str, folder: str) -> Path:
    source = Path(relative_value)
    if source.is_absolute() or ".." in source.parts:
        raise ValueError(f"Unsafe {folder} path: {relative_value}")
    expected_prefix = run_dir.relative_to(ROOT) / folder
    if source.parts[:len(expected_prefix.parts)] != expected_prefix.parts:
        raise ValueError(f"Path must be beneath {expected_prefix.as_posix()}/")
    absolute = ROOT / source
    container = run_dir / folder
    resolved = absolute.resolve(strict=False)
    try:
        resolved.relative_to(container.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes {container}") from exc
    if _path_has_symlink(absolute, container):
        raise ValueError(f"Symlinks are not accepted for permanent run evidence: {relative_value}")
    if not absolute.is_file():
        raise ValueError(f"Missing regular file: {relative_value}")
    return absolute


def _preflight(run_dir: Path, expected_count: int | None) -> tuple[dict, list[dict], list[dict], list[Path]]:
    metadata_path = run_dir / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("status") == "complete":
        raise ValueError("This run is already complete and cannot be finalized again")
    target = metadata.get("target_count")
    if type(target) is not int or target < 1:
        raise ValueError("run.json has no valid target_count")
    if expected_count is not None and expected_count != target:
        raise ValueError("--expected-count may confirm but cannot override the stored run target")
    records = json.loads((run_dir / "jobs.json").read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("jobs.json must contain a JSON array")
    if len(records) != target:
        raise ValueError(f"Expected {target} reviewed jobs in this run; found {len(records)}")

    as_of = date.fromisoformat(metadata["date"])
    prepared: list[dict] = []
    evidence: list[Path] = []
    seen: dict[tuple[str, tuple], int] = {}
    for index, record in enumerate(records):
        row = jobs.prepare(record, as_of)
        if row.get("source_type") == "AGGREGATOR":
            raise ValueError("Aggregator-only records cannot be finalized as canonical selected evidence")
        for identity_name, identity in jobs.identity_keys(row).items():
            key = (identity_name, identity)
            if key in seen:
                raise ValueError(
                    f"Duplicate {identity_name} identity in jobs.json records {seen[key] + 1} and {index + 1}"
                )
            seen[key] = index
        evidence.append(validate_contained_file(run_dir, str(row.get("source_file") or ""), "evidence"))
        digest = row.get("evidence_digest_file")
        if digest:
            evidence.append(validate_contained_file(run_dir, str(digest), "evidence-digests"))
        prepared.append(row)
    return metadata, records, prepared, evidence


def _backup_database(source: Path, destination: Path, schema_path: Path) -> None:
    if source.exists():
        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_db = sqlite3.connect(destination)
        try:
            source_db.backup(destination_db)
        finally:
            destination_db.close()
            source_db.close()
    else:
        with closing(sqlite3.connect(destination)) as db:
            db.row_factory = sqlite3.Row
            jobs.migrate(db, schema_path)
            db.commit()


def _distribution(rows: list[sqlite3.Row], field: str, missing: str = "UNKNOWN") -> dict[str, int]:
    return dict(sorted(Counter(str(row[field] or missing) for row in rows).items()))


def _coverage_section(
    rows: list[sqlite3.Row],
    records: list[dict] | None = None,
    company_cap: int | None = 2,
    company_goal: int | None = 8,
) -> dict:
    total = len(rows)
    company_labels: dict[str, str] = {}
    normalized_company_counts = Counter()
    for row in rows:
        key = str(row["company"]).casefold()
        company_labels.setdefault(key, str(row["company"]))
        normalized_company_counts[key] += 1
    company_counts = Counter({company_labels[key]: count for key, count in normalized_company_counts.items()})
    ordered = company_counts.most_common()
    largest = ordered[0][1] if ordered else 0
    top_three = sum(count for _, count in ordered[:3])
    warnings: list[str] = []
    if company_goal is not None and len(company_counts) < min(company_goal, total):
        warnings.append(f"Fewer than {min(company_goal, total)} companies are represented")
    if company_cap is not None and any(count > company_cap for count in company_counts.values()):
        warnings.append(f"At least one company exceeds the {company_cap}-job soft cap")
    if company_cap is not None and total and largest / total > company_cap / total:
        warnings.append(f"Largest-company share exceeds {company_cap / total:.0%}")
    if records is not None and total and top_three / total > 0.50:
        warnings.append("Top-three-company share exceeds 50%")
    if records and company_cap is not None:
        over_cap = {company.casefold() for company, count in company_counts.items() if count > company_cap}
        if any(str(record.get("company", "")).casefold() in over_cap and not record.get("diversity_exception") for record in records):
            warnings.append("An over-cap selection lacks a documented diversity exception")
    return {
        "selected_job_count": total,
        "unique_company_count": len(company_counts),
        "largest_company_share": round(largest / total, 4) if total else 0,
        "top_three_company_share": round(top_three / total, 4) if total else 0,
        "company_distribution": dict(sorted(company_counts.items())),
        "role_family_distribution": _distribution(rows, "role_family"),
        "geography_distribution": _distribution(rows, "location"),
        "freshness_distribution": _distribution(rows, "freshness"),
        "company_archetype_distribution": _distribution(rows, "company_archetype", "UNCLASSIFIED"),
        "track_distribution": {
            "primary": sum(row["role_family"] != "EMBEDDED_ML" for row in rows),
            "secondary": sum(row["role_family"] == "EMBEDDED_ML" for row in rows),
        },
        "warnings": warnings,
    }


def build_coverage(
    selected: list[sqlite3.Row], master: list[sqlite3.Row], records: list[dict], plan: dict | None = None,
) -> dict:
    plan = plan or {}
    return {
        "generated_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "run": _coverage_section(
            selected, records, plan.get("company_cap", 2), plan.get("company_goal", min(8, len(selected)))
        ),
        "rolling_master": _coverage_section(master, None, None, None),
    }


def _hash_entry(path: Path, display_path: str) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": display_path, "sha256": digest.hexdigest(), "size": path.stat().st_size}


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0] if value else None


def _manifest(
    run_dir: Path,
    metadata: dict,
    finalized_at: str,
    job_count: int,
    company_count: int,
    staged_csv: Path,
    staged_readme: Path,
    evidence: list[Path],
) -> dict:
    artifacts = [
        _hash_entry(run_dir / "jobs.json", f"runs/{run_dir.name}/jobs.json"),
        _hash_entry(staged_csv, f"runs/{run_dir.name}/jobs.csv"),
        _hash_entry(staged_readme, f"runs/{run_dir.name}/README.md"),
    ]
    artifacts.extend(_hash_entry(path, path.relative_to(ROOT).as_posix()) for path in sorted(set(evidence)))
    return {
        "run_id": metadata["run_id"],
        "finalized_at": finalized_at,
        "job_count": job_count,
        "company_count": company_count,
        "artifacts": artifacts,
        "tools": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "firecrawl_cli": _version(["firecrawl", "--version"]),
            "repository_commit": _version(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        },
    }


def _promote(files: list[tuple[Path, Path]]) -> None:
    """Replace a group of files and restore all prior outputs if promotion raises."""
    backups: list[tuple[Path, Path | None]] = []
    promoted: list[Path] = []
    backup_dir = files[0][0].parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    try:
        for index, (staged, target) in enumerate(files):
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = None
            if target.exists():
                backup = backup_dir / f"{index:02d}-{target.name}"
                shutil.copy2(target, backup)
            backups.append((target, backup))
            os.replace(staged, target)
            promoted.append(target)
    except Exception:
        for target, backup in reversed(backups):
            if backup is None:
                if target in promoted and target.exists():
                    target.unlink()
            else:
                os.replace(backup, target)
        raise


def finalize_run(run_dir: Path, expected_count: int | None = None) -> dict:
    run_dir = resolve_run(run_dir)
    metadata, records, prepared, evidence = _preflight(run_dir, expected_count)
    active_sidecars = [
        path for path in (ROOT / "jobs.db-wal", ROOT / "jobs.db-shm", ROOT / "jobs.db-journal")
        if path.exists()
    ]
    if active_sidecars:
        raise ValueError("Refusing finalization while SQLite sidecar files are present; close other database users")
    schema_path = jobs.SCHEMA_PATH

    with tempfile.TemporaryDirectory(prefix="finalize-", dir=ROOT) as temporary:
        staging = Path(temporary)
        staged_db = staging / "jobs.db"
        staged_master_csv = staging / "master-jobs.csv"
        staged_run_csv = staging / "run-jobs.csv"
        staged_metadata = staging / "run.json"
        staged_coverage = staging / "coverage.json"
        staged_index = staging / "runs-README.md"
        staged_readme = staging / "run-README.md"
        staged_manifest = staging / "manifest.json"
        _backup_database(ROOT / "jobs.db", staged_db, schema_path)

        with closing(sqlite3.connect(staged_db)) as db:
            db.row_factory = sqlite3.Row
            jobs.migrate(db, schema_path)
            columns = jobs._table_columns(db, "jobs") - {"id"}
            selected_ids: list[int] = []
            outcomes: list[str] = []
            for row in prepared:
                unknown = set(row) - columns
                if unknown:
                    raise ValueError(f"Unknown input fields: {unknown}")
                matches = jobs.find_matches(db, row)
                if len({match["id"] for match in matches}) > 1:
                    raise ValueError("Conflicting duplicate identities in the master database")
                outcome, job_id = jobs.upsert(db, row)
                outcomes.append(outcome)
                selected_ids.append(job_id)
            if len(set(selected_ids)) != metadata["target_count"]:
                raise ValueError(
                    f"Expected {metadata['target_count']} unique selected jobs; found {len(set(selected_ids))}"
                )
            placeholders = ", ".join("?" for _ in selected_ids)
            selected_rows = db.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY fit_score DESC, company, title",
                tuple(selected_ids),
            ).fetchall()
            if jobs.export_rows(db, selected_rows, staged_run_csv) != metadata["target_count"]:
                raise ValueError("The exported unique run count does not match the target count")
            master_rows = db.execute("SELECT * FROM jobs ORDER BY fit_score DESC, company, title").fetchall()
            master_count = jobs.export_rows(db, master_rows, staged_master_csv)
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"Staged SQLite integrity check failed: {integrity}")
            plan_path = run_dir / "plan.json"
            run_plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
            coverage = build_coverage(selected_rows, master_rows, records, run_plan)
            db.commit()

        finalized_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        completed_metadata = dict(metadata)
        completed_metadata.update({
            "status": "complete",
            "job_count": len(selected_rows),
            "company_count": len({str(row["company"]).casefold() for row in selected_rows}),
            "completed_at": finalized_at,
        })
        write_json(staged_metadata, completed_metadata)
        write_json(staged_coverage, coverage)
        staged_index.write_text(index_text(completed_metadata), encoding="utf-8")
        staged_readme.write_text(
            (run_dir / "README.md").read_text(encoding="utf-8").replace(
                "- **Status:** In progress", "- **Status:** Complete", 1
            ),
            encoding="utf-8",
        )
        manifest = _manifest(
            run_dir, metadata, finalized_at, len(selected_rows), completed_metadata["company_count"],
            staged_run_csv, staged_readme, evidence,
        )
        write_json(staged_manifest, manifest)

        _promote([
            (staged_db, ROOT / "jobs.db"),
            (staged_master_csv, ROOT / "jobs.csv"),
            (staged_run_csv, run_dir / "jobs.csv"),
            (staged_coverage, run_dir / "coverage.json"),
            (staged_index, RUNS / "README.md"),
            (staged_readme, run_dir / "README.md"),
            (staged_metadata, run_dir / "run.json"),
            (staged_manifest, run_dir / "manifest.json"),
        ])

    return {
        "run_id": metadata["run_id"], "jobs": len(selected_rows), "master_jobs": master_count,
        "outcomes": outcomes, "warnings": coverage["run"]["warnings"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    new = subparsers.add_parser("new", help="Create the next structured run folder")
    new.add_argument("--date", type=date.fromisoformat, default=datetime.now(TIMEZONE).date())
    new.add_argument("--scope", required=True)
    new.add_argument("--target-count", type=int, required=True)
    final = subparsers.add_parser("finalize", help="Validate, stage, import, and snapshot a run")
    final.add_argument("run", type=Path)
    final.add_argument("--expected-count", type=int)
    subparsers.add_parser("index", help="Rebuild runs/README.md")
    args = parser.parse_args()
    if args.command == "new":
        print(create_run(args.date, args.scope, args.target_count).relative_to(ROOT))
    elif args.command == "finalize":
        print(json.dumps(finalize_run(args.run, args.expected_count)))
    else:
        rebuild_index()
        print(RUNS / "README.md")


if __name__ == "__main__":
    main()
