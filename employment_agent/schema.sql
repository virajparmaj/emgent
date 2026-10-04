CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    company_priority_tier TEXT,
    title TEXT NOT NULL,
    role_family TEXT NOT NULL CHECK (role_family IN (
        'CREDIT_RISK', 'FRAUD_RISK', 'FINANCIAL_ANALYTICS', 'PRODUCT_ANALYTICS',
        'BUSINESS_STRATEGY', 'DATA_ANALYTICS', 'DATA_SCIENCE', 'APPLIED_AI',
        'AI_PRODUCT', 'QUANT_FINANCE', 'EMBEDDED_ML', 'OTHER'
    )),
    team_function TEXT,
    location TEXT NOT NULL,
    country TEXT,
    work_arrangement TEXT,
    job_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    ats_source TEXT,
    source_type TEXT CHECK (source_type IS NULL OR source_type IN (
        'COMPANY_CAREERS', 'GREENHOUSE', 'LEVER', 'ASHBY', 'WORKDAY',
        'SUCCESSFACTORS', 'AGGREGATOR', 'OTHER'
    )),
    source_detail TEXT,
    company_archetype TEXT,
    diversity_exception TEXT,
    job_id TEXT,
    date_posted TEXT,
    date_discovered TEXT NOT NULL,
    freshness TEXT NOT NULL CHECK (freshness IN (
        'VERY_NEW', 'NEW', 'RECENT', 'ACTIVE', 'OLD', 'UNKNOWN'
    )),
    salary_minimum REAL,
    salary_maximum REAL,
    salary_currency TEXT,
    salary_notes TEXT,
    required_years_experience TEXT,
    preferred_years_experience TEXT,
    required_skills TEXT,
    preferred_skills TEXT,
    education_requirements TEXT,
    visa_work_authorization_language TEXT,
    short_job_summary TEXT,
    key_responsibilities TEXT,
    why_it_fits TEXT,
    potential_concerns TEXT,
    fit_score INTEGER NOT NULL CHECK (fit_score BETWEEN 0 AND 100),
    fit_category TEXT NOT NULL CHECK (fit_category IN (
        'EXCEPTIONAL', 'STRONG', 'REVIEW', 'BORDERLINE', 'SKIP'
    )),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
        'new', 'review', 'apply', 'applied', 'interview', 'rejected', 'closed', 'skip'
    )),
    open_status TEXT NOT NULL CHECK (open_status IN ('OPEN', 'UNVERIFIED', 'CLOSED')),
    verified_at TEXT,
    open_evidence TEXT,
    source_file TEXT,
    evidence_digest_file TEXT,
    score_breakdown TEXT NOT NULL,
    score_explanation TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_location TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (company, normalized_title, normalized_location)
);
-- ATS IDs are unique within an employer; unrelated companies can reuse numeric IDs.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_company_job_id
ON jobs(company COLLATE NOCASE, job_id) WHERE job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'new', 'review', 'apply', 'applied', 'interview', 'rejected', 'closed', 'skip'
    )),
    reason_code TEXT CHECK (reason_code IS NULL OR reason_code IN (
        'GREAT_FIT', 'TOO_SENIOR', 'WRONG_LOCATION', 'TOO_ENGINEERING_HEAVY',
        'WEAK_DOMAIN_FIT', 'MISSING_REQUIRED_SKILLS', 'STALE_OR_CLOSED',
        'COMPANY_NOT_INTERESTING', 'DUPLICATE', 'OTHER'
    )),
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS review_events_job_id_created_at
ON review_events(job_id, created_at DESC, id DESC);
