"""Core terminal workflow for bounded, auditable job research."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import jobs, run_manager


DEFAULT_FOCUS = [
    "credit risk analytics", "fraud risk analytics", "financial modeling",
    "decision science", "product analytics fintech", "data science financial services",
    "quantitative analysis", "applied AI financial data", "embedded ML",
]
DEFAULT_LOCATIONS = [
    "Chicago", "New York", "Remote US", "San Diego or Los Angeles", "Bay Area",
    "Boston", "Washington DC", "Seattle",
]
DEFAULT_ARCHETYPES = [
    "consumer lending and BNPL", "payments", "credit and fraud infrastructure",
    "banks and credit unions", "B2B spend and treasury", "asset management and market data",
    "insurance and insurtech", "risk modeling and decisioning", "newer fintech",
    "established financial institution",
]
PREFERRED_COMPANIES = [
    "Ramp", "Mercury", "Jeeves", "Navan", "Avant", "Upstart", "Enova", "Discover",
    "Affirm", "M1", "Morningstar", "Zest AI", "Pagaya", "Chime", "SoFi", "Ocrolus",
    "OnePay", "Brex",
]
DEFAULT_BUDGETS = {"search": 20, "scrape": 30, "map": 4, "crawl": 2, "interact": 2}
SOURCE_HOSTS = {
    "greenhouse.io": "GREENHOUSE", "lever.co": "LEVER", "ashbyhq.com": "ASHBY",
    "myworkdayjobs.com": "WORKDAY", "successfactors.com": "SUCCESSFACTORS",
}


def timestamp() -> str:
    return jobs.now_iso()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_fingerprint(spec: dict) -> str:
    material = {
        "operation": spec["operation"],
        "parameters": spec.get("parameters", {}),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def feedback_summary(db_path: Path) -> dict:
    if not db_path.exists():
        return {"event_count": 0, "groups": {}, "proposed_adaptations": []}
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "review_events" not in tables:
            return {"event_count": 0, "groups": {}, "proposed_adaptations": []}
        rows = db.execute(
            "SELECT e.reason_code, e.status, j.company, j.role_family, j.team_function, j.location "
            "FROM review_events e JOIN jobs j ON j.id = e.job_id ORDER BY e.created_at"
        ).fetchall()
    finally:
        if "db" in locals():
            db.close()
    groups = {
        "company": dict(Counter(row["company"] for row in rows)),
        "role_family": dict(Counter(row["role_family"] for row in rows)),
        "domain": dict(Counter(row["team_function"] or row["role_family"] for row in rows)),
        "location": dict(Counter(row["location"] for row in rows)),
        "seniority_concern": {
            "TOO_SENIOR": sum(row["reason_code"] == "TOO_SENIOR" for row in rows),
            "OTHER": sum(row["reason_code"] not in {None, "TOO_SENIOR"} for row in rows),
        },
        "reason": dict(Counter(row["reason_code"] or "NO_REASON" for row in rows)),
        "status": dict(Counter(row["status"] for row in rows)),
    }
    adaptations = []
    reasons = groups["reason"]
    if reasons.get("TOO_SENIOR", 0):
        adaptations.append("Increase early-career and explicit 0-3 year query terms")
    if reasons.get("WRONG_LOCATION", 0):
        adaptations.append("Tighten location clauses to the plan's reviewed locations")
    if reasons.get("WEAK_DOMAIN_FIT", 0):
        adaptations.append("Increase credit, risk, payments, and financial-data responsibility terms")
    if reasons.get("TOO_ENGINEERING_HEAVY", 0):
        adaptations.append("Reduce engineering-title queries and emphasize analysis and decision science")
    return {"event_count": len(rows), "groups": groups, "proposed_adaptations": adaptations}


def create_plan(
    run_date: date,
    scope: str,
    target_count: int = 10,
    focus: list[str] | None = None,
    locations: list[str] | None = None,
    freshness_days: int = 30,
    company_cap: int = 2,
    budgets: dict[str, int] | None = None,
    secondary_track_count: int = 2,
    include_companies: list[str] | None = None,
    exclude_companies: list[str] | None = None,
    cache_age_hours: int = 24,
    zero_yield_stop: int = 4,
) -> Path:
    if not 1 <= target_count <= 10:
        raise ValueError("The current search policy authorizes at most ten jobs")
    if not 0 <= secondary_track_count <= target_count:
        raise ValueError("secondary_track_count must be between zero and target_count")
    if freshness_days < 0 or company_cap < 1 or cache_age_hours < 0 or zero_yield_stop < 1:
        raise ValueError("Plan limits must be non-negative and company/zero-yield limits must be positive")
    actual_budgets = dict(DEFAULT_BUDGETS)
    actual_budgets.update(budgets or {})
    if any(type(value) is not int or value < 0 for value in actual_budgets.values()):
        raise ValueError("Every operation budget must be a non-negative integer")
    run_dir = run_manager.create_run(run_date, scope, target_count)
    feedback = feedback_summary(run_manager.ROOT / "jobs.db")
    plan = {
        "version": 1,
        "run_id": run_dir.name,
        "date": run_date.isoformat(),
        "scope": scope,
        "target_count": target_count,
        "focus": focus or DEFAULT_FOCUS,
        "locations": locations or DEFAULT_LOCATIONS,
        "freshness_days": freshness_days,
        "company_cap": company_cap,
        "company_goal": min(8, target_count),
        "operation_budgets": actual_budgets,
        "secondary_track_count": secondary_track_count,
        "include_companies": include_companies or [],
        "exclude_companies": exclude_companies or [],
        "company_archetypes": DEFAULT_ARCHETYPES,
        "same_run_cache_age_hours": cache_age_hours,
        "zero_yield_stop": zero_yield_stop,
        "network_execution": "explicit_execute_flag_only",
        "feedback_summary": feedback,
        "created_at": timestamp(),
    }
    write_json(run_dir / "plan.json", plan)
    return run_dir


def load_plan(run_dir: Path) -> dict:
    path = run_dir / "plan.json"
    if not path.is_file():
        raise ValueError(f"Missing structured plan: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _search_spec(
    query: str, purpose: str, focus: str, geography: str, archetype: str, index: int,
    freshness_days: int,
) -> dict:
    parameters = {"query": query, "limit": 10, "sources": "web", "country": "US"}
    if freshness_days <= 1:
        parameters["tbs"] = "qdr:d"
    elif freshness_days <= 7:
        parameters["tbs"] = "qdr:w"
    elif freshness_days <= 31:
        parameters["tbs"] = "qdr:m"
    elif freshness_days <= 366:
        parameters["tbs"] = "qdr:y"
    spec = {
        "operation": "search",
        "parameters": parameters,
        "context": {
            "purpose": purpose, "role_family": focus, "geography": geography,
            "company_archetype": archetype,
            "track": "secondary" if re.search(r"embedded|edge ai|tinyml|inference", focus, re.I) else "primary",
        },
    }
    fingerprint = request_fingerprint(spec)
    spec["fingerprint"] = fingerprint
    spec["output"] = f"discovery/search-{index:03d}-{fingerprint[:10]}.json"
    return spec


def generate_discovery_plan(run_dir: Path) -> dict:
    plan = load_plan(run_dir)
    maximum = plan["operation_budgets"]["search"]
    focuses = plan["focus"]
    locations = plan["locations"]
    archetypes = plan["company_archetypes"]
    excluded = {company.casefold() for company in plan.get("exclude_companies", [])}
    specs = []

    primary_focuses = [focus for focus in focuses if not re.search(r"embedded|edge ai|tinyml|inference", focus, re.I)]
    secondary_focuses = [focus for focus in focuses if focus not in primary_focuses] or ["embedded ML"]
    primary_focuses = primary_focuses or ["data analytics financial services"]
    secondary_query_count = round(
        maximum * plan.get("secondary_track_count", 0) / max(1, plan.get("target_count", 10))
    )
    schedule = [primary_focuses[index % len(primary_focuses)] for index in range(maximum)]
    if secondary_query_count:
        step = maximum / secondary_query_count
        for index in range(secondary_query_count):
            position = min(maximum - 1, round(index * step + step / 2 - 0.5))
            schedule[position] = secondary_focuses[index % len(secondary_focuses)]

    broad_limit = 0 if maximum == 0 else max(1, maximum - min(maximum // 5, 4) - min(maximum // 5, 4))
    for index in range(broad_limit):
        focus = schedule[len(specs)]
        geography = locations[index % len(locations)]
        archetype = archetypes[index % len(archetypes)]
        query = f'"{focus}" {geography} {archetype} jobs Python SQL early career'
        specs.append(_search_spec(
            query, "broad_role_first", focus, geography, archetype, len(specs) + 1,
            plan["freshness_days"],
        ))

    ats_domains = ["job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "myworkdayjobs.com"]
    for index, domain in enumerate(ats_domains[:max(0, min(4, maximum - len(specs)))]):
        focus = schedule[len(specs)]
        geography = locations[index % len(locations)]
        query = f'site:{domain} "{focus}" {geography} United States'
        specs.append(_search_spec(
            query, "official_ats", focus, geography, "mixed", len(specs) + 1,
            plan["freshness_days"],
        ))

    companies = plan.get("include_companies") or PREFERRED_COMPANIES
    for company in companies:
        if len(specs) >= maximum or company.casefold() in excluded:
            continue
        focus = schedule[len(specs)]
        geography = locations[len(specs) % len(locations)]
        query = f'"{company}" careers "{focus}" {geography}'
        specs.append(_search_spec(
            query, "company_specific", focus, geography, "explicit_company", len(specs) + 1,
            plan["freshness_days"],
        ))

    unique: list[dict] = []
    seen = set()
    for spec in specs:
        if spec["fingerprint"] not in seen:
            unique.append(spec)
            seen.add(spec["fingerprint"])
    request_plan = {
        "run_id": plan["run_id"], "generated_at": timestamp(), "execute_by_default": False,
        "request_count": len(unique), "requests": unique,
    }
    write_json(run_dir / "discovery" / "request-plan.json", request_plan)
    for spec in unique:
        append_jsonl(run_dir / "usage.jsonl", usage_event(spec, "planned"))
    return request_plan


class CommandRunner:
    """Subprocess seam used only after the caller supplies explicit execution authorization."""

    def run(self, command: list[str], output_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)


def build_command(spec: dict, output_path: Path) -> list[str]:
    operation = spec["operation"]
    parameters = spec.get("parameters", {})
    if operation == "search":
        command = [
            "firecrawl", "search", parameters["query"], "--limit", str(parameters.get("limit", 10)),
            "--sources", parameters.get("sources", "web"), "--country", parameters.get("country", "US"),
        ]
        if parameters.get("tbs"):
            command.extend(["--tbs", parameters["tbs"]])
        command.extend(["--json", "--output", str(output_path)])
        return command
    if operation == "scrape":
        command = ["firecrawl", "scrape", parameters["url"], "--format", parameters.get("formats", "markdown,links")]
        if parameters.get("only_main_content", True):
            command.append("--only-main-content")
        command.extend(["--json", "--output", str(output_path)])
        return command
    if operation == "map":
        return [
            "firecrawl", "map", parameters["url"], "--search", parameters["search"],
            "--limit", str(parameters.get("limit", 25)), "--wait", "--json", "--output", str(output_path),
        ]
    if operation == "crawl":
        return [
            "firecrawl", "crawl", parameters["url"], "--include-paths", parameters["include_paths"],
            "--limit", str(parameters.get("limit", 25)), "--max-depth", str(parameters.get("max_depth", 2)),
            "--wait", "--pretty", "--output", str(output_path),
        ]
    if operation == "interact":
        code = (
            "JSON.stringify({title:document.title,url:location.href,text:document.body.innerText.slice(0,12000),"
            "links:[...document.querySelectorAll('a')].map(a=>({text:a.innerText,href:a.href})).slice(0,250)})"
        )
        return [
            "firecrawl", "interact", "--scrape-id", parameters["scrape_id"], "--code", code,
            "--node", "--json", "--output", str(output_path),
        ]
    raise ValueError(f"Unsupported Firecrawl operation: {operation}")


def usage_event(spec: dict, status: str, **extra: Any) -> dict:
    return {
        "operation": spec["operation"],
        "request_fingerprint": spec.get("fingerprint") or request_fingerprint(spec),
        "timestamp": timestamp(), "status": status, "duration_seconds": None,
        "success": status in {"successful", "cached"}, "cache_status": "hit" if status == "cached" else "miss",
        "result_count": None, "page_count": None, "output_path": spec.get("output"),
        "reported_credits": None, "error_category": None, **extra,
    }


def _recursive_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None:
                return child
            found = _recursive_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recursive_value(child, keys)
            if found is not None:
                return found
    return None


def _result_metrics(path: Path) -> tuple[int | None, int | None, float | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, None
    urls = extract_url_items(value)
    pages = _recursive_value(value, {"total", "pageCount", "pages"})
    credits = _recursive_value(value, {"creditsUsed", "credits"})
    return len(urls) if urls else None, pages if isinstance(pages, int) else None, credits


def _cached_event(run_dir: Path, spec: dict, cache_hours: int) -> dict | None:
    cutoff = datetime.now(jobs.TIMEZONE) - timedelta(hours=cache_hours)
    fingerprint = spec.get("fingerprint") or request_fingerprint(spec)
    for event in reversed(read_jsonl(run_dir / "usage.jsonl")):
        if event.get("request_fingerprint") != fingerprint or event.get("status") not in {"successful", "cached"}:
            continue
        try:
            event_time = datetime.fromisoformat(event["timestamp"])
        except (KeyError, ValueError):
            continue
        output = run_dir / str(event.get("output_path") or "")
        if event_time >= cutoff and output.is_file() and event.get("output_sha256") == file_hash(output):
            return event
    return None


def _attempt_count(run_dir: Path, operation: str) -> int:
    return sum(
        event.get("operation") == operation and event.get("status") == "attempted"
        for event in read_jsonl(run_dir / "usage.jsonl")
    )


def execute_request(run_dir: Path, spec: dict, plan: dict, runner: CommandRunner) -> dict:
    operation = spec["operation"]
    if operation not in DEFAULT_BUDGETS:
        raise ValueError(f"Unknown operation type: {operation}")
    spec = dict(spec)
    spec["fingerprint"] = spec.get("fingerprint") or request_fingerprint(spec)
    cached = _cached_event(run_dir, spec, plan["same_run_cache_age_hours"])
    if cached:
        event = usage_event(
            spec, "cached", result_count=cached.get("result_count"), page_count=cached.get("page_count"),
            reported_credits=0, output_sha256=cached.get("output_sha256"),
        )
        append_jsonl(run_dir / "usage.jsonl", event)
        return event
    budget = plan["operation_budgets"][operation]
    if _attempt_count(run_dir, operation) >= budget:
        raise ValueError(f"{operation} budget exhausted ({budget})")
    output = run_dir / spec["output"]
    output.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(run_dir / "usage.jsonl", usage_event(spec, "attempted"))
    command = build_command(spec, output)
    started = time.monotonic()
    try:
        result = runner.run(command, output)
    except subprocess.TimeoutExpired:
        event = usage_event(spec, "failed", duration_seconds=round(time.monotonic() - started, 3), error_category="TIMEOUT")
    except OSError:
        event = usage_event(spec, "failed", duration_seconds=round(time.monotonic() - started, 3), error_category="COMMAND_UNAVAILABLE")
    else:
        duration = round(time.monotonic() - started, 3)
        if result.returncode != 0 or not output.is_file():
            event = usage_event(
                spec, "failed", duration_seconds=duration, error_category="COMMAND_FAILED",
                error_message=(result.stderr or result.stdout or "")[-500:],
            )
        else:
            try:
                json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                event = usage_event(
                    spec, "failed", duration_seconds=duration, error_category="INVALID_JSON_OUTPUT",
                )
            else:
                result_count, page_count, credits = _result_metrics(output)
                event = usage_event(
                    spec, "successful", duration_seconds=duration, result_count=result_count,
                    page_count=page_count, reported_credits=credits, output_sha256=file_hash(output),
                )
    append_jsonl(run_dir / "usage.jsonl", event)
    return event


def extract_url_items(value: Any) -> list[dict]:
    """Find URL-bearing search result objects without assuming one CLI response version."""
    found: list[dict] = []
    if isinstance(value, dict):
        url = value.get("url") or value.get("link")
        if isinstance(url, str):
            found.append(value)
        for child in value.values():
            found.extend(extract_url_items(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(extract_url_items(child))
    unique = []
    seen = set()
    for item in found:
        url = item.get("url") or item.get("link")
        marker = (url, item.get("title"))
        if marker not in seen:
            unique.append(item)
            seen.add(marker)
    return unique


def execute_discovery(run_dir: Path, runner: CommandRunner | None = None) -> dict:
    plan = load_plan(run_dir)
    request_plan = generate_discovery_plan(run_dir)
    runner = runner or CommandRunner()
    known_urls = set()
    master_urls = _master_urls(run_manager.ROOT / "jobs.db")
    zero_yield = 0
    executed = []
    stopped = False
    for spec in request_plan["requests"]:
        event = execute_request(run_dir, spec, plan, runner)
        executed.append(event)
        new_count = 0
        if event["status"] in {"successful", "cached"}:
            output = run_dir / spec["output"]
            try:
                response = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                response = {}
            for item in extract_url_items(response):
                try:
                    url = jobs.canonicalize(item.get("url") or item.get("link"))
                except ValueError:
                    continue
                if url in master_urls or source_type_for_url(url) == "AGGREGATOR":
                    continue
                if url not in known_urls:
                    known_urls.add(url)
                    new_count += 1
        zero_yield = zero_yield + 1 if new_count == 0 else 0
        if zero_yield >= plan["zero_yield_stop"]:
            stopped = True
            break
    return {"requests_executed": len(executed), "stopped_for_zero_yield": stopped, "events": executed}


def source_type_for_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    for suffix, source_type in SOURCE_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return source_type
    aggregator_hosts = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com"}
    if any(host == name or host.endswith("." + name) for name in aggregator_hosts):
        return "AGGREGATOR"
    return "COMPANY_CAREERS"


def _master_urls(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {row[0] for row in db.execute("SELECT canonical_url FROM jobs")}
    except sqlite3.DatabaseError:
        return set()
    finally:
        db.close()


def triage(run_dir: Path, db_path: Path | None = None) -> dict:
    request_plan_path = run_dir / "discovery" / "request-plan.json"
    if not request_plan_path.is_file():
        raise ValueError("Run discover first to create discovery/request-plan.json")
    request_plan = json.loads(request_plan_path.read_text(encoding="utf-8"))
    plan = load_plan(run_dir)
    excluded_companies = {company.casefold() for company in plan.get("exclude_companies", [])}
    master_urls = _master_urls(db_path or run_manager.ROOT / "jobs.db")
    candidates: dict[str, dict] = {}
    invalid = []
    for spec in request_plan["requests"]:
        output = run_dir / spec["output"]
        if not output.is_file():
            continue
        try:
            response = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            invalid.append({"output": spec["output"], "reason": "INVALID_JSON"})
            continue
        for item in extract_url_items(response):
            raw_url = item.get("url") or item.get("link")
            try:
                canonical = jobs.canonicalize(raw_url)
            except (TypeError, ValueError):
                invalid.append({"url": raw_url, "reason": "INVALID_OR_NON_HTTPS_URL"})
                continue
            candidate = candidates.setdefault(canonical, {
                "candidate_id": hashlib.sha256(canonical.encode()).hexdigest()[:16],
                "canonical_url": canonical,
                "discovered_urls": [],
                "title_hint": item.get("title"),
                "company_hint": item.get("company"),
                "snippet": item.get("description") or item.get("snippet") or item.get("markdown"),
                "source_type_hint": source_type_for_url(canonical),
                "provenance": [],
            })
            if raw_url not in candidate["discovered_urls"]:
                candidate["discovered_urls"].append(raw_url)
            candidate["provenance"].append({
                "request_fingerprint": spec["fingerprint"], "purpose": spec["context"]["purpose"],
                "output": spec["output"],
            })
    for candidate in candidates.values():
        candidate["already_in_master"] = candidate["canonical_url"] in master_urls
        if str(candidate.get("company_hint") or "").casefold() in excluded_companies:
            candidate["decision"] = "rejected"
            candidate["decision_reason"] = "EXCLUDED_COMPANY"
        elif candidate["already_in_master"]:
            candidate["decision"] = "rejected"
            candidate["decision_reason"] = "ALREADY_TRACKED"
        elif candidate["source_type_hint"] == "AGGREGATOR":
            candidate["decision"] = "needs_canonical_url"
            candidate["decision_reason"] = "AGGREGATOR_DISCOVERY_ONLY"
        else:
            candidate["decision"] = "retained"
            candidate["decision_reason"] = "CANONICAL_VERIFICATION_REQUIRED"
        source_rank = 3 if candidate["source_type_hint"] != "AGGREGATOR" else 0
        snippet = (candidate.get("snippet") or "").casefold()
        relevance = sum(term in snippet for term in ("risk", "credit", "data", "analytic", "model", "python", "sql"))
        candidate["verification_rank"] = source_rank * 10 + relevance
        candidate["verified"] = False
        candidate["open_status"] = None
    ordered = sorted(candidates.values(), key=lambda item: (-item["verification_rank"], item["canonical_url"]))
    ledger = {
        "generated_at": timestamp(), "source_request_plan": "discovery/request-plan.json",
        "counts": {
            "unique": len(ordered),
            "retained": sum(item["decision"] == "retained" for item in ordered),
            "needs_canonical_url": sum(item["decision"] == "needs_canonical_url" for item in ordered),
            "rejected": sum(item["decision"] == "rejected" for item in ordered),
            "invalid": len(invalid),
        },
        "candidates": ordered, "invalid_results": invalid,
    }
    write_json(run_dir / "candidates.json", ledger)
    return ledger


def _slug(candidate: dict) -> str:
    base = jobs.normalize(candidate.get("title_hint") or "posting").replace(" ", "-")[:40] or "posting"
    return f"{base}-{candidate['candidate_id'][:8]}"


def _verification_specs(candidate: dict) -> list[dict]:
    slug = _slug(candidate)
    url = candidate["canonical_url"]
    parts = urlsplit(url)
    root = f"https://{parts.netloc}"
    parent_path = parts.path.rsplit("/", 1)[0] or "/"
    raw = [
        {
            "operation": "scrape",
            "parameters": {"url": url, "formats": "markdown,links", "only_main_content": True},
            "output": f"evidence/{slug}.json", "condition": "always",
        },
        {
            "operation": "scrape",
            "parameters": {"url": url, "formats": "markdown,links,rawHtml", "only_main_content": True},
            "output": f"evidence/{slug}-metadata.json", "condition": "direct scrape lacks required facts",
        },
        {
            "operation": "map",
            "parameters": {"url": root, "search": candidate.get("title_hint") or "job", "limit": 25},
            "output": f"discovery/{slug}-map.json", "condition": "canonical page remains unresolved",
        },
        {
            "operation": "crawl",
            "parameters": {"url": root, "include_paths": parent_path, "limit": 25, "max_depth": 2},
            "output": f"discovery/{slug}-crawl.json", "condition": "map and direct scrape are insufficient",
        },
        {
            "operation": "interact",
            "parameters": {"scrape_id": "FROM_DIRECT_SCRAPE"},
            "output": f"evidence/{slug}-interact.json", "condition": "final read-only availability fallback",
        },
    ]
    for spec in raw:
        spec["candidate_id"] = candidate["candidate_id"]
        spec["fingerprint"] = request_fingerprint(spec)
    return raw


def generate_verification_plan(run_dir: Path) -> dict:
    ledger_path = run_dir / "candidates.json"
    if not ledger_path.is_file():
        raise ValueError("Run triage first to create candidates.json")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    pipelines = []
    for candidate in ledger["candidates"]:
        if candidate["decision"] == "retained":
            if source_type_for_url(candidate["canonical_url"]) == "AGGREGATOR":
                raise ValueError(
                    f"Candidate {candidate['candidate_id']} needs an official canonical URL before verification"
                )
            pipelines.append({"candidate_id": candidate["candidate_id"], "requests": _verification_specs(candidate)})
    result = {
        "generated_at": timestamp(), "execute_by_default": False,
        "safety": "Read-only evidence retrieval; no form fields, submissions, login, accounts, or uploads",
        "pipelines": pipelines,
    }
    write_json(run_dir / "verification-plan.json", result)
    for pipeline in pipelines:
        for spec in pipeline["requests"]:
            append_jsonl(run_dir / "usage.jsonl", usage_event(spec, "planned"))
    return result


def _response_text(value: Any) -> str:
    if isinstance(value, dict):
        pieces = []
        for key in ("markdown", "text", "rawHtml", "html"):
            if isinstance(value.get(key), str):
                pieces.append(value[key])
        pieces.extend(_response_text(child) for child in value.values() if isinstance(child, (dict, list)))
        return "\n".join(piece for piece in pieces if piece)
    if isinstance(value, list):
        return "\n".join(_response_text(item) for item in value)
    return ""


def _response_links(value: Any) -> list[str]:
    links = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"links", "url", "href"}:
                if isinstance(child, str) and child.startswith("http"):
                    links.append(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str) and item.startswith("http"):
                            links.append(item)
                        elif isinstance(item, dict) and isinstance(item.get("href"), str):
                            links.append(item["href"])
            if isinstance(child, (dict, list)):
                links.extend(_response_links(child))
    elif isinstance(value, list):
        for child in value:
            links.extend(_response_links(child))
    return list(dict.fromkeys(links))


def assess_evidence(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sufficient": False, "closure_excerpt": None, "application_url": None, "scrape_id": None}
    text = _response_text(value)
    lowered = text.casefold()
    closure_patterns = ["job is no longer available", "position has been filled", "job has expired", "no longer accepting applications"]
    closure = next((pattern for pattern in closure_patterns if pattern in lowered), None)
    links = _response_links(value)
    apply_link = next((link for link in links if re.search(r"apply|application", link, re.I)), None)
    scrape_id = _recursive_value(value, {"scrapeId", "scrape_id", "id"})
    sufficient = len(text.strip()) >= 400 and bool(closure or apply_link)
    return {
        "sufficient": sufficient,
        "closure_excerpt": closure,
        "application_url": apply_link,
        "scrape_id": scrape_id if isinstance(scrape_id, str) else None,
    }


def _fact(value: Any = None, basis: str = "unknown", evidence: list[dict] | None = None) -> dict:
    return {"value": value, "basis": basis, "evidence": evidence or []}


def generate_digest(run_dir: Path, candidate: dict, source_path: Path | None, assessment: dict) -> Path:
    response = json.loads(source_path.read_text(encoding="utf-8")) if source_path else {}
    metadata = response.get("metadata") if isinstance(response, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    text = _response_text(response)
    source_relative = source_path.relative_to(run_manager.ROOT).as_posix() if source_path else None
    excerpt = re.sub(r"\s+", " ", text).strip()[:320]
    source_evidence = [{"path": source_relative, "pointer": "markdown", "excerpt": excerpt}] if excerpt else []
    fields = {
        "canonical_url": _fact(candidate["canonical_url"], "calculated"),
        "company": _fact(),
        "title": _fact(metadata.get("title"), "explicit_source_fact" if metadata.get("title") else "unknown", source_evidence if metadata.get("title") else []),
        "location": _fact(), "date_posted": _fact(), "job_id": _fact(),
        "source_type": _fact(source_type_for_url(candidate["canonical_url"]), "calculated"),
        "required_experience": _fact(), "preferred_experience": _fact(),
        "required_skills": _fact(), "preferred_skills": _fact(), "education": _fact(),
        "work_arrangement": _fact(), "salary": _fact(), "work_authorization_language": _fact(),
        "scoring_responsibilities": _fact(),
        "closure_evidence": _fact(assessment.get("closure_excerpt"), "explicit_source_fact" if assessment.get("closure_excerpt") else "unknown", source_evidence if assessment.get("closure_excerpt") else []),
        "application_path_evidence": _fact(assessment.get("application_url"), "explicit_source_fact" if assessment.get("application_url") else "unknown"),
        "extraction_timestamp": _fact(timestamp(), "calculated"),
        "canonical_source_path": _fact(source_relative, "calculated" if source_path else "unknown"),
        "canonical_source_sha256": _fact(file_hash(source_path) if source_path else None, "calculated" if source_path else "unknown"),
    }
    digest = {
        "version": 1, "candidate_id": candidate["candidate_id"],
        "classification_values": ["explicit_source_fact", "calculated", "reviewer_judgment", "unknown"],
        "fields": fields,
        "notes": "Unknown fields remain null until supported by canonical evidence or explicit reviewer judgment.",
    }
    output = run_dir / "evidence-digests" / f"{candidate['candidate_id']}.json"
    write_json(output, digest)
    return output


def execute_verification(run_dir: Path, runner: CommandRunner | None = None) -> dict:
    plan = load_plan(run_dir)
    verification_plan = generate_verification_plan(run_dir)
    ledger = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    by_id = {candidate["candidate_id"]: candidate for candidate in ledger["candidates"]}
    runner = runner or CommandRunner()
    outcomes = []
    budget_stops = []
    for pipeline in verification_plan["pipelines"]:
        candidate = by_id[pipeline["candidate_id"]]
        source_path = None
        assessment = {"sufficient": False, "scrape_id": None}
        for position, spec in enumerate(pipeline["requests"]):
            if position > 0 and assessment.get("sufficient"):
                break
            if spec["operation"] == "interact":
                if not assessment.get("scrape_id"):
                    continue
                spec = dict(spec)
                spec["parameters"] = {"scrape_id": assessment["scrape_id"]}
                spec["fingerprint"] = request_fingerprint(spec)
            try:
                event = execute_request(run_dir, spec, plan, runner)
            except ValueError as exc:
                if "budget exhausted" not in str(exc):
                    raise
                budget_stops.append({"candidate_id": candidate["candidate_id"], "message": str(exc)})
                break
            outcomes.append(event)
            if event["status"] not in {"successful", "cached"}:
                continue
            output = run_dir / spec["output"]
            if spec["operation"] == "scrape":
                source_path = output
                assessment = assess_evidence(output)
        digest_path = generate_digest(run_dir, candidate, source_path, assessment)
        candidate["evidence_digest_file"] = digest_path.relative_to(run_manager.ROOT).as_posix()
        if source_path and source_path.is_file():
            candidate["canonical_source_file"] = source_path.relative_to(run_manager.ROOT).as_posix()
            candidate["availability_evidence"] = {
                "closure": assessment.get("closure_excerpt"),
                "application_path": assessment.get("application_url"),
            }
    write_json(run_dir / "candidates.json", ledger)
    return {
        "request_events": outcomes,
        "budget_stops": budget_stops,
        "digests": sum("evidence_digest_file" in item for item in ledger["candidates"]),
    }


def review_list(db_path: Path, statuses: list[str] | None = None) -> list[dict]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        query = "SELECT id, company, title, location, fit_score, fit_category, status, open_status FROM jobs"
        parameters: tuple = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(statuses)
        query += " ORDER BY fit_score DESC, company, title"
        return [dict(row) for row in db.execute(query, parameters)]
    finally:
        db.close()


def review_show(db_path: Path, job_id: int) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown local job id")
        tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = []
        if "review_events" in tables:
            events = [dict(event) for event in db.execute(
                "SELECT status, reason_code, note, created_at FROM review_events WHERE job_id = ? ORDER BY id",
                (job_id,),
            )]
        result = dict(row)
        result["review_events"] = events
        return result
    finally:
        db.close()


def review_set(db_path: Path, job_id: int, status: str, reason: str | None, note: str | None) -> None:
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        jobs.migrate(db)
        jobs.set_review_status(db, job_id, status, reason, note)
        db.commit()
