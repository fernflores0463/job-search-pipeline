-- Job Search Pipeline - PostgreSQL Schema
-- Run this once after creating the database

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS jobs (
  id            VARCHAR(12) PRIMARY KEY,
  company       VARCHAR(255),
  title         VARCHAR(255),
  location      VARCHAR(255),
  work_type     VARCHAR(50),
  salary        VARCHAR(255),
  posted_date   VARCHAR(50),
  import_date   VARCHAR(100),
  description   TEXT,
  score         INTEGER,
  tier          VARCHAR(50),
  job_link      TEXT,
  apply_link    TEXT,
  resume_text   TEXT,
  resume_s3_key TEXT,
  created_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_state (
  job_id              VARCHAR(12) PRIMARY KEY REFERENCES jobs(id),
  status              VARCHAR(50) DEFAULT 'New',
  notes               TEXT,
  applicants          VARCHAR(100),
  live_status         VARCHAR(20),
  live_status_checked TIMESTAMP,
  timestamps          JSONB DEFAULT '{}',
  pdf_path            TEXT,
  updated_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS application_plans (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title      VARCHAR(255),
  date       VARCHAR(20),
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS application_plan_jobs (
  plan_id UUID REFERENCES application_plans(id) ON DELETE CASCADE,
  job_id  VARCHAR(12) REFERENCES jobs(id),
  notes   TEXT,
  PRIMARY KEY (plan_id, job_id)
);

CREATE TABLE IF NOT EXISTS company_cache (
  company_name VARCHAR(255) PRIMARY KEY,
  summary      TEXT,
  logo_url     TEXT,
  industry     VARCHAR(255),
  cached_at    TIMESTAMP DEFAULT NOW()
);

-- Index for common query patterns
CREATE INDEX IF NOT EXISTS idx_jobs_import_date ON jobs(import_date);
CREATE INDEX IF NOT EXISTS idx_jobs_tier ON jobs(tier);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_job_state_status ON job_state(status);
