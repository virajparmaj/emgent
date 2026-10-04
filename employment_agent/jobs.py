"""Local SQLite validation, import, export, and review helpers. No network calls."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
TIMEZONE = ZoneInfo("America/Chicago")
SCHEMA_VERSION = 2

SCORE_LIMITS = {
    "statistics_ml": 30,
    "finance_risk": 25,
    "seniority": 15,
    "technical": 10,
    "company_priority": 10,
    "geography": 5,
    "recency": 5,
}
QUALITATIVE_SCORES = {"statistics_ml", "finance_risk", "seniority", "technical"}
RECENCY_POINTS = {"VERY_NEW": 5, "NEW": 4, "RECENT": 3, "ACTIVE": 2, "OLD": 0, "UNKNOWN": 0}
REVIEW_STATUSES = {"new", "review", "apply", "applied", "interview", "rejected", "closed", "skip"}
REVIEW_REASONS = {
    "GREAT_FIT", "TOO_SENIOR", "WRONG_LOCATION", "TOO_ENGINEERING_HEAVY",
    "WEAK_DOMAIN_FIT", "MISSING_REQUIRED_SKILLS", "STALE_OR_CLOSED",
    "COMPANY_NOT_INTERESTING", "DUPLICATE", "OTHER",
}
SOURCE_TYPES = {
    "COMPANY_CAREERS", "GREENHOUSE", "LEVER", "ASHBY", "WORKDAY",
    "SUCCESSFACTORS", "AGGREGATOR", "OTHER",
}
TRACKING_PARAMETERS = {"gclid", "fbclid", "gh_src", "lever-source"}


def now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


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
    """Normalize a posting URL conservatively while retaining job identity parameters."""
    if not isinstance(url, str):
        raise ValueError("A canonical posting URL must be a string")
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("A canonical posting must have a public HTTPS URL")
    host = (parts.hostname or "").casefold()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    if parts.port not in (None, 443):
        host = f"{host}:{parts.port}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS:
            continue
        query.append((key, value))
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def freshness(posted: str | None, as_of: date) -> str:
    if posted is None:
        return "UNKNOWN"
    try:
        posted_date = date.fromisoformat(posted)
    except (TypeError, ValueError) as exc:
        raise ValueError("date_posted must be an ISO date or null") from exc
    age = (as_of - posted_date).days
    if age < 0:
        raise ValueError("Future posting date: review source before importing")
    for maximum, label in [(3, "VERY_NEW"), (7, "NEW"), (14, "RECENT"), (30, "ACTIVE")]:
        if age <= maximum:
            return label
    return "OLD"


def geography_points(location: str, country: str | None = None) -> int:
    """Return the best policy score supported by an explicitly stored location."""
    location_text = normalize_location(location)
    country_text = normalize(country or "")
    if any(token in location_text for token in ("remote us", "chicago", "new york")):
        return 5
    if any(token in location_text for token in (
        "san diego", "los angeles", "san francisco", "bay area", "boston",
        "washington dc", "washington d c", "district of columbia", "arlington va",
        "mclean va",
    )):
        return 4
    if "seattle" in location_text:
        return 3
    us_markers = {
        "us", "usa", "united", "states", "al", "ak", "az", "ar", "ca", "co", "ct", "de",
        "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma",
        "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd",
        "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa",
        "wv", "wi", "wy",
    }
    words = set(location_text.split()) | set(country_text.split())
    if words & us_markers:
        return 2
    return 0


def fit_category(score: int) -> str:
    for threshold, label in [(90, "EXCEPTIONAL"), (80, "STRONG"), (70, "REVIEW"),
                             (60, "BORDERLINE"), (0, "SKIP")]:
        if score >= threshold:
            return label
    raise AssertionError("score cannot be negative")


def normalize_source(record: dict) -> tuple[str, str | None]:
    supplied = record.get("source_type")
    detail = record.get("source_detail")
    if supplied is not None:
        source_type = str(supplied).upper()
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"Invalid source_type: {supplied}")
        return source_type, detail
    legacy = str(record.get("ats_source") or "").strip()
    lowered = legacy.casefold()
    if "greenhouse" in lowered:
        source_type = "GREENHOUSE"
    elif "lever" in lowered:
        source_type = "LEVER"
    elif "ashby" in lowered:
        source_type = "ASHBY"
    elif "workday" in lowered:
        source_type = "WORKDAY"
    elif "successfactors" in lowered or "success factors" in lowered:
        source_type = "SUCCESSFACTORS"
    elif "aggregator" in lowered:
        source_type = "AGGREGATOR"
    elif "company" in lowered or "career" in lowered:
        source_type = "COMPANY_CAREERS"
    else:
        source_type = "OTHER"
    return source_type, detail or legacy or None


def _validate_supplied(row: dict, field: str, calculated: object) -> None:
    if field in row and row[field] is not None and row[field] != calculated:
        raise ValueError(f"Supplied {field} is inconsistent with the calculated value")


def prepare(record: dict, as_of: date) -> dict:
    """Validate one record and calculate every deterministic policy value."""
    if not isinstance(record, dict):
        raise ValueError("Each reviewed job record must be a JSON object")
    row = dict(record)
    for field in [
        "company", "title", "role_family", "location", "job_url", "canonical_url",
        "open_status", "score_explanation",
    ]:
        if not row.get(field):
            raise ValueError(f"Missing required field: {field}")
    if row.get("company_priority_tier") is not None:
        raise ValueError("company_priority_tier must remain null until a real tier policy exists")
    nullable_facts = {
        "team_function", "country", "work_arrangement", "ats_source", "source_type",
        "source_detail", "company_archetype", "diversity_exception", "job_id", "date_posted",
        "salary_currency", "salary_notes", "required_years_experience",
        "preferred_years_experience", "required_skills", "preferred_skills",
        "education_requirements", "visa_work_authorization_language",
    }
    empty_facts = sorted(field for field in nullable_facts if row.get(field) == "")
    if empty_facts:
        raise ValueError(f"Unknown optional facts must be null, not empty strings: {empty_facts}")
    row["canonical_url"] = canonicalize(row["canonical_url"])
    row["normalized_title"] = normalize(row["title"])
    row["normalized_location"] = normalize_location(row["location"])
    row["date_discovered"] = row.get("date_discovered") or as_of.isoformat()

    calculated_freshness = freshness(row.get("date_posted"), as_of)
    _validate_supplied(row, "freshness", calculated_freshness)
    row["freshness"] = calculated_freshness

    supplied_score = row.get("score_breakdown")
    if not isinstance(supplied_score, dict):
        raise ValueError("score_breakdown must be a JSON object")
    unknown_scores = set(supplied_score) - set(SCORE_LIMITS)
    if unknown_scores:
        raise ValueError(f"Unknown score dimensions: {sorted(unknown_scores)}")
    score: dict[str, int] = {}
    for key in QUALITATIVE_SCORES:
        value = supplied_score.get(key)
        if type(value) is not int or not 0 <= value <= SCORE_LIMITS[key]:
            raise ValueError(f"Invalid {key} score")
        score[key] = value
    calculated_scores = {
        "company_priority": 5,
        "geography": geography_points(row["location"], row.get("country")),
        "recency": RECENCY_POINTS[calculated_freshness],
    }
    for key, calculated in calculated_scores.items():
        supplied = supplied_score.get(key)
        if supplied is not None and supplied != calculated:
            raise ValueError(f"Supplied {key} score is inconsistent with policy")
        score[key] = calculated
    score = {key: score[key] for key in SCORE_LIMITS}
    total = sum(score.values())
    category = fit_category(total)
    _validate_supplied(row, "fit_score", total)
    _validate_supplied(row, "fit_category", category)
    row["fit_score"] = total
    row["fit_category"] = category
    row["score_breakdown"] = score

    source_type, source_detail = normalize_source(row)
    row["source_type"] = source_type
    row["source_detail"] = source_detail
    row.setdefault("status", "new")
    if row["status"] not in REVIEW_STATUSES:
        raise ValueError(f"Invalid review status: {row['status']}")
    if row.get("open_status") == "OPEN" and not all(
        row.get(key) for key in ["verified_at", "open_evidence", "source_file"]
    ):
        raise ValueError("OPEN requires a verification timestamp and saved source evidence")
    row["updated_at"] = now_iso()
    return {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
        for key, value in row.items()
    }


def identity_keys(row: dict) -> dict[str, tuple]:
    result = {
        "canonical_url": (row["canonical_url"],),
        "title_location": (
            str(row["company"]).casefold(), row["normalized_title"], row["normalized_location"]
        ),
    }
    if row.get("job_id") is not None:
        result["company_job_id"] = (str(row["company"]).casefold(), str(row["job_id"]))
    return result


def find_matches(db: sqlite3.Connection, row: dict) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM jobs WHERE canonical_url = ? OR "
        "(company = ? COLLATE NOCASE AND job_id IS NOT NULL AND job_id = ?) OR "
        "(company = ? COLLATE NOCASE AND normalized_title = ? AND normalized_location = ?)",
        (row["canonical_url"], row["company"], row.get("job_id"), row["company"],
         row["normalized_title"], row["normalized_location"]),
    ).fetchall()


def upsert(db: sqlite3.Connection, row: dict) -> tuple[str, int]:
    """Match all three identity rules and preserve the user's review status."""
    matches = find_matches(db, row)
    ids = {match["id"] for match in matches}
    if len(ids) > 1:
        raise ValueError("Conflicting duplicate identities: review existing rows before merging")
    if not matches:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        cursor = db.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(row.values()))
        return "inserted", int(cursor.lastrowid)
    old = dict(matches[0])
    old_source_type = old.get("source_type") or normalize_source(old)[0]
    if row.get("source_type") == "AGGREGATOR" and old_source_type != "AGGREGATOR":
        return "retained official source", int(old["id"])
    row["date_discovered"] = min(old["date_discovered"], row["date_discovered"])
    row["status"] = old["status"]
    assignments = ", ".join(f"{key} = ?" for key in row)
    db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*row.values(), old["id"]))
    return "updated", int(old["id"])


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {column[1] for column in db.execute(f"PRAGMA table_info({table})")}


def migrate(db: sqlite3.Connection, schema_path: Path | None = None) -> None:
    """Create the current schema or add nullable fields to a legacy database."""
    schema_path = schema_path or SCHEMA_PATH
    db.executescript(schema_path.read_text(encoding="utf-8"))
    columns = _table_columns(db, "jobs")
    additions = {
        "salary_notes": "TEXT",
        "source_type": "TEXT",
        "source_detail": "TEXT",
        "company_archetype": "TEXT",
        "diversity_exception": "TEXT",
        "evidence_digest_file": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            reason_code TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS review_events_job_id_created_at
        ON review_events(job_id, created_at DESC, id DESC);
        """
    )
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def export_rows(db: sqlite3.Connection, rows: Iterable[sqlite3.Row], output: Path) -> int:
    rows = list(rows)
    columns = [column[1] for column in db.execute("PRAGMA table_info(jobs)")]
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(["NULL" if row[key] is None else row[key] for key in columns] for row in rows)
    return len(rows)


def export(db: sqlite3.Connection, output: Path) -> int:
    rows = db.execute("SELECT * FROM jobs ORDER BY fit_score DESC, company, title").fetchall()
    return export_rows(db, rows, output)


def set_review_status(
    db: sqlite3.Connection,
    job_id: int,
    status: str,
    reason_code: str | None = None,
    note: str | None = None,
) -> None:
    if status not in REVIEW_STATUSES:
        raise ValueError(f"Invalid review status: {status}")
    if reason_code is not None and reason_code not in REVIEW_REASONS:
        raise ValueError(f"Invalid review reason: {reason_code}")
    timestamp = now_iso()
    result = db.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, timestamp, job_id)
    )
    if result.rowcount != 1:
        raise ValueError("Unknown local job id")
    db.execute(
        "INSERT INTO review_events(job_id, status, reason_code, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, status, reason_code, note, timestamp),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "import", "export", "status"])
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "jobs.db")
    parser.add_argument("--csv", type=Path, default=PROJECT_ROOT / "jobs.csv")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--job-id", type=int, help="Local SQLite id for the status command")
    parser.add_argument("--status", choices=sorted(REVIEW_STATUSES))
    parser.add_argument("--reason", choices=sorted(REVIEW_REASONS))
    parser.add_argument("--note")
    args = parser.parse_args()
    with closing(sqlite3.connect(args.db)) as db:
        db.row_factory = sqlite3.Row
        migrate(db)
        if args.command == "import":
            if args.input is None:
                parser.error("import requires a reviewed JSON input file")
            records = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("Expected a JSON array of reviewed job records")
            columns = _table_columns(db, "jobs") - {"id"}
            as_of = args.as_of or datetime.now(TIMEZONE).date()
            outcomes = []
            ids = []
            for record in records:
                row = prepare(record, as_of)
                if set(row) - columns:
                    raise ValueError(f"Unknown input fields: {set(row) - columns}")
                outcome, job_id = upsert(db, row)
                outcomes.append(outcome)
                ids.append(job_id)
            if args.expected_count is not None and len(set(ids)) != args.expected_count:
                raise ValueError(f"Expected {args.expected_count} unique imported jobs; found {len(set(ids))}")
            print(json.dumps({"records": len(records), "unique_jobs": len(set(ids)), "outcomes": outcomes}))
        elif args.command == "export":
            print(f"Exported {export(db, args.csv)} jobs to {args.csv}")
        elif args.command == "status":
            if args.job_id is None or args.status is None:
                parser.error("status requires --job-id and --status")
            set_review_status(db, args.job_id, args.status, args.reason, args.note)
            print(f"Set job {args.job_id} to {args.status}")
        else:
            print(f"Initialized {args.db}")
        db.commit()


if __name__ == "__main__":
    main()
