"""Local SQLite import/export for manually reviewed job research. No network calls."""

import argparse
import csv
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SCORE_LIMITS = {
    "statistics_ml": 30,
    "finance_risk": 25,
    "seniority": 15,
    "technical": 10,
    "company_priority": 10,
    "geography": 5,
    "recency": 5,
}
RECENCY_POINTS = {"VERY_NEW": 5, "NEW": 4, "RECENT": 3, "ACTIVE": 2, "OLD": 0, "UNKNOWN": 0}
REVIEW_STATUSES = {"new", "review", "apply", "applied", "interview", "rejected", "closed", "skip"}


def normalize(value: str) -> str:
    """Ignore case and punctuation without dropping meaningful seniority words."""
    return " ".join(re.findall(r"\w+", value.casefold()))


def normalize_location(value: str) -> str:
    value = normalize(value)
    for alias, replacement in {
        "new york city": "new york",
        "nyc": "new york",
        "remote usa": "remote us",
        "united states remote": "remote us",
        "remote united states": "remote us",
    }.items():
        value = re.sub(rf"\b{alias}\b", replacement, value)
    return value


def canonicalize(url: str) -> str:
    """Remove tracking; preserve query parameters needed to identify an ATS job."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("A canonical posting must have a public HTTPS URL")
    tracking = {"gh_src", "gh_jid", "source", "ref", "referrer", "lever-source"}
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_") and k not in tracking]
    host = parts.netloc.lower()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def freshness(posted: str | None, as_of: date) -> str:
    if posted is None:
        return "UNKNOWN"
    age = (as_of - date.fromisoformat(posted)).days
    if age < 0:
        raise ValueError("Future posting date: review source before importing")
    for maximum, label in [(3, "VERY_NEW"), (7, "NEW"), (14, "RECENT"), (30, "ACTIVE")]:
        if age <= maximum:
            return label
    return "OLD"


def prepare(record: dict, as_of: date) -> dict:
    row = dict(record)
    for field in ["company", "title", "location", "job_url", "canonical_url", "score_explanation"]:
        if not row.get(field):
            raise ValueError(f"Missing required field: {field}")
    row["canonical_url"] = canonicalize(row["canonical_url"])
    row["normalized_title"] = normalize(row["title"])
    row["normalized_location"] = normalize_location(row["location"])
    row["date_discovered"] = row.get("date_discovered") or as_of.isoformat()
    row["freshness"] = freshness(row.get("date_posted"), as_of)
    score = dict(row["score_breakdown"])
    score["recency"] = RECENCY_POINTS[row["freshness"]]
    if set(score) != set(SCORE_LIMITS):
        raise ValueError("Score breakdown must contain all seven scoring dimensions")
    for key, maximum in SCORE_LIMITS.items():
        if type(score[key]) is not int or not 0 <= score[key] <= maximum:
            raise ValueError(f"Invalid {key} score")
    row["fit_score"] = sum(score.values())
    row["fit_category"] = next(
        label for threshold, label in [(90, "EXCEPTIONAL"), (80, "STRONG"), (70, "REVIEW"),
                                       (60, "BORDERLINE"), (0, "SKIP")]
        if row["fit_score"] >= threshold
    )
    row["score_breakdown"] = score
    row.setdefault("status", "new")
    if row.get("open_status") == "OPEN" and not all(
        row.get(key) for key in ["verified_at", "open_evidence", "source_file"]
    ):
        raise ValueError("OPEN requires a verification timestamp and saved source evidence")
    row["updated_at"] = datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds")
    return {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
            for key, value in row.items()}


def upsert(db: sqlite3.Connection, row: dict) -> str:
    """Match all three identity rules and preserve the user's review status."""
    matches = db.execute(
        "SELECT * FROM jobs WHERE canonical_url = ? OR "
        "(company = ? COLLATE NOCASE AND job_id IS NOT NULL AND job_id = ?) OR "
        "(company = ? COLLATE NOCASE AND normalized_title = ? AND normalized_location = ?)",
        (row["canonical_url"], row["company"], row.get("job_id"), row["company"],
         row["normalized_title"], row["normalized_location"]),
    ).fetchall()
    if len(matches) > 1:
        raise ValueError("Conflicting duplicate identities: review existing rows before merging")
    if not matches:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        db.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(row.values()))
        return "inserted"
    old = dict(matches[0])
    # A discovery-only aggregator must not replace a previously verified original posting.
    if row.get("ats_source") == "AGGREGATOR" and old.get("ats_source") != "AGGREGATOR":
        return "retained official source"
    row["date_discovered"] = min(old["date_discovered"], row["date_discovered"])
    row["status"] = old["status"]
    assignments = ", ".join(f"{key} = ?" for key in row)
    db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*row.values(), old["id"]))
    return "updated"


def export(db: sqlite3.Connection, output: Path) -> int:
    rows = db.execute("SELECT * FROM jobs ORDER BY fit_score DESC, company, title").fetchall()
    columns = [column[1] for column in db.execute("PRAGMA table_info(jobs)")]
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(["NULL" if row[key] is None else row[key] for key in columns] for row in rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "import", "export", "status"])
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--db", type=Path, default=ROOT / "jobs.db")
    parser.add_argument("--csv", type=Path, default=ROOT / "jobs.csv")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--job-id", type=int, help="Local SQLite id for the status command")
    parser.add_argument("--status", choices=sorted(REVIEW_STATUSES))
    args = parser.parse_args()
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        db.executescript((ROOT / "schema.sql").read_text())
        if args.command == "import":
            if args.input is None:
                parser.error("import requires a reviewed JSON input file")
            records = json.loads(args.input.read_text())
            if not isinstance(records, list):
                raise ValueError("Expected a JSON array of reviewed job records")
            columns = {column[1] for column in db.execute("PRAGMA table_info(jobs)")} - {"id"}
            as_of = args.as_of or datetime.now(ZoneInfo("America/Chicago")).date()
            outcomes = []
            for record in records:
                row = prepare(record, as_of)
                if set(row) - columns:
                    raise ValueError(f"Unknown input fields: {set(row) - columns}")
                outcomes.append(upsert(db, row))
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if args.expected_count is not None and count != args.expected_count:
                raise ValueError(f"Expected {args.expected_count} unique jobs; found {count}")
            print(json.dumps({"records": len(records), "unique_jobs": count, "outcomes": outcomes}))
        elif args.command == "export":
            print(f"Exported {export(db, args.csv)} jobs to {args.csv}")
        elif args.command == "status":
            if args.job_id is None or args.status is None:
                parser.error("status requires --job-id and --status")
            result = db.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                                (args.status, datetime.now().astimezone().isoformat(), args.job_id))
            if result.rowcount != 1:
                raise ValueError("Unknown local job id")
            print(f"Set job {args.job_id} to {args.status}")
        else:
            print(f"Initialized {args.db}")


if __name__ == "__main__":
    main()
