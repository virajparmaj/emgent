import json
from contextlib import contextmanager
from pathlib import Path

from employment_agent import run_manager


def record(
    index=1,
    company=None,
    title=None,
    location="Chicago, IL",
    canonical_url=None,
    job_id=None,
    source_file=None,
    source_type="GREENHOUSE",
    date_posted="2026-10-02",
):
    company = company or f"Company {index}"
    title = title or f"Risk Analyst {index}"
    canonical_url = canonical_url or f"https://job-boards.greenhouse.io/company/jobs/{1000 + index}"
    return {
        "company": company,
        "company_priority_tier": None,
        "title": title,
        "role_family": "CREDIT_RISK",
        "team_function": None,
        "location": location,
        "country": "US",
        "work_arrangement": None,
        "job_url": canonical_url,
        "canonical_url": canonical_url,
        "ats_source": None,
        "source_type": source_type,
        "source_detail": None,
        "company_archetype": "consumer lending and BNPL",
        "diversity_exception": None,
        "job_id": job_id,
        "date_posted": date_posted,
        "date_discovered": "2026-10-04",
        "salary_minimum": None,
        "salary_maximum": None,
        "salary_currency": None,
        "salary_notes": None,
        "required_years_experience": None,
        "preferred_years_experience": None,
        "required_skills": None,
        "preferred_skills": None,
        "education_requirements": None,
        "visa_work_authorization_language": None,
        "short_job_summary": "Credit risk analytics role.",
        "key_responsibilities": None,
        "why_it_fits": "Relevant statistical and risk analysis.",
        "potential_concerns": None,
        "open_status": "OPEN",
        "verified_at": "2026-10-04T08:00:00-05:00",
        "open_evidence": "A job-specific application link was present.",
        "source_file": source_file,
        "evidence_digest_file": None,
        "score_breakdown": {
            "statistics_ml": 24,
            "finance_risk": 22,
            "seniority": 13,
            "technical": 8,
            "company_priority": None,
            "geography": None,
            "recency": None,
        },
        "score_explanation": "Explicit qualitative component review.",
        "status": "new",
    }


@contextmanager
def isolated_runs(root: Path):
    old_root, old_runs = run_manager.ROOT, run_manager.RUNS
    run_manager.ROOT = root.resolve()
    run_manager.RUNS = root.resolve() / "runs"
    try:
        yield
    finally:
        run_manager.ROOT, run_manager.RUNS = old_root, old_runs


def populate_run(run_dir: Path, records: list[dict]) -> None:
    for index, item in enumerate(records, 1):
        evidence = run_dir / "evidence" / f"job-{index}.json"
        evidence.write_text(json.dumps({"markdown": "job evidence", "links": [item["job_url"]]}), encoding="utf-8")
        item["source_file"] = evidence.relative_to(run_manager.ROOT).as_posix()
    (run_dir / "jobs.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
