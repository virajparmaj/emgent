"""Create and finalize self-contained job-search run folders. No network calls."""

import argparse
import csv
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jobs


ROOT = Path(__file__).resolve().parent
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


def rebuild_index() -> None:
    rows = []
    for metadata_path in sorted(RUNS.glob("*/run.json"), reverse=True):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
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
    RUNS.mkdir(exist_ok=True)
    (RUNS / "README.md").write_text("\n".join(body), encoding="utf-8")


def create_run(run_date: date, scope: str, target_count: int) -> Path:
    RUNS.mkdir(exist_ok=True)
    prefix = run_date.isoformat() + "-run-"
    numbers = [int(path.name.removeprefix(prefix)) for path in RUNS.glob(prefix + "[0-9][0-9][0-9]")
               if path.name.removeprefix(prefix).isdigit()]
    run_id = f"{prefix}{max(numbers, default=0) + 1:03d}"
    run_dir = RUNS / run_id
    for child in ["evidence", "discovery", "reviewed-not-selected"]:
        (run_dir / child).mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id, "date": run_date.isoformat(), "status": "in_progress",
        "scope": scope, "target_count": target_count, "job_count": 0, "company_count": 0,
        "created_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"), "completed_at": None,
        "artifacts": {"summary": "README.md", "jobs_json": "jobs.json", "jobs_csv": "jobs.csv",
                      "evidence": "evidence/", "discovery": "discovery/",
                      "reviewed_not_selected": "reviewed-not-selected/"},
    }
    write_json(run_dir / "run.json", metadata)
    write_json(run_dir / "jobs.json", [])
    (run_dir / "README.md").write_text(
        f"# {run_id}\n\n- **Date:** {run_date.isoformat()}\n- **Status:** In progress\n"
        f"- **Scope:** {scope}\n- **Target:** {target_count} jobs\n\n"
        "## Search plan\n\n"
        "- Geography:\n- Primary role families:\n- Secondary role families:\n"
        "- Track allocation:\n- Company preferences:\n- Explicit exclusions:\n\n"
        "## Run checklist\n\n"
        "- [ ] Run a small Firecrawl health-check search.\n"
        "- [ ] Discover a focused candidate set.\n"
        "- [ ] Scrape every shortlisted canonical posting.\n"
        "- [ ] Verify current availability when possible.\n"
        "- [ ] Extract fields with explicit NULLs for unknown facts.\n"
        "- [ ] Score, deduplicate, and save selected evidence.\n"
        "- [ ] Finalize the run and review its CSV snapshot.\n\n"
        "## Results\n\nAdd the concise reviewed-job table when the run is complete.\n\n"
        "## Verification notes\n\nRecord important source and availability notes here.\n\n"
        "## Reviewed but not selected\n\nRecord the main exclusions and reasons here.\n",
        encoding="utf-8")
    rebuild_index()
    return run_dir


def export_run_csv(db: sqlite3.Connection, records: list[dict], output: Path) -> int:
    urls = [jobs.canonicalize(record["canonical_url"]) for record in records]
    if urls:
        placeholders = ", ".join("?" for _ in urls)
        rows = db.execute(f"SELECT * FROM jobs WHERE canonical_url IN ({placeholders}) "
                          "ORDER BY fit_score DESC, company, title", urls).fetchall()
    else:
        rows = []
    columns = [column[1] for column in db.execute("PRAGMA table_info(jobs)")]
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(["NULL" if row[key] is None else row[key] for key in columns] for row in rows)
    return len(rows)


def finalize_run(run_dir: Path, expected_count: int | None) -> dict:
    metadata_path = run_dir / "run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    records = json.loads((run_dir / "jobs.json").read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("jobs.json must contain a JSON array")
    target = expected_count if expected_count is not None else metadata.get("target_count")
    if target is not None and len(records) != target:
        raise ValueError(f"Expected {target} reviewed jobs in this run; found {len(records)}")
    run_relative = run_dir.relative_to(ROOT).as_posix() + "/"
    for record in records:
        source = record.get("source_file")
        if not source or not source.startswith(run_relative):
            raise ValueError(f"Every record must reference evidence inside {run_relative}")
        if not (ROOT / source).is_file():
            raise ValueError(f"Missing source evidence: {source}")

    as_of = date.fromisoformat(metadata["date"])
    outcomes = []
    with sqlite3.connect(ROOT / "jobs.db") as db:
        db.row_factory = sqlite3.Row
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        columns = {column[1] for column in db.execute("PRAGMA table_info(jobs)")} - {"id"}
        for record in records:
            row = jobs.prepare(record, as_of)
            if set(row) - columns:
                raise ValueError(f"Unknown input fields: {set(row) - columns}")
            outcomes.append(jobs.upsert(db, row))
        run_count = export_run_csv(db, records, run_dir / "jobs.csv")
        master_count = jobs.export(db, ROOT / "jobs.csv")

    metadata.update({"status": "complete", "job_count": run_count,
                     "company_count": len({record["company"] for record in records}),
                     "completed_at": datetime.now(TIMEZONE).isoformat(timespec="seconds")})
    write_json(metadata_path, metadata)
    rebuild_index()
    return {"run_id": metadata["run_id"], "jobs": run_count, "master_jobs": master_count,
            "outcomes": outcomes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    new = subparsers.add_parser("new", help="Create the next structured run folder")
    new.add_argument("--date", type=date.fromisoformat, default=datetime.now(TIMEZONE).date())
    new.add_argument("--scope", required=True)
    new.add_argument("--target-count", type=int, required=True)
    final = subparsers.add_parser("finalize", help="Validate, import, and snapshot a completed run")
    final.add_argument("run", type=Path)
    final.add_argument("--expected-count", type=int)
    subparsers.add_parser("index", help="Rebuild runs/README.md")
    args = parser.parse_args()
    if args.command == "new":
        print(create_run(args.date, args.scope, args.target_count).relative_to(ROOT))
    elif args.command == "finalize":
        print(json.dumps(finalize_run(resolve_run(args.run), args.expected_count)))
    else:
        rebuild_index()
        print(RUNS / "README.md")


if __name__ == "__main__":
    main()
