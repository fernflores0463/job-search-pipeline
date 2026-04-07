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
  ai_reasoning  TEXT,
  regex_score   INTEGER,
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

-- ─────────────────────────────────────────────────────────
-- Idempotent migrations for additive columns
-- ─────────────────────────────────────────────────────────
-- CREATE TABLE IF NOT EXISTS above is a no-op when the table already
-- exists, so columns added after the original schema must also be
-- declared as ALTER TABLE ... ADD COLUMN IF NOT EXISTS for existing
-- databases to pick them up on redeploy. All statements below are
-- additive, nullable, and safe to re-run.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_reasoning TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS regex_score  INTEGER;

-- ─────────────────────────────────────────────────────────
-- import_batches: tracks every AI CSV import submission
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS import_batches (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  import_date              VARCHAR(100) NOT NULL,   -- matches jobs.import_date
  mode                     VARCHAR(10)  NOT NULL,   -- 'live' | 'batch'
  status                   VARCHAR(20)  NOT NULL,   -- 'queued'|'running'|'completed'|'failed'|'canceled'
  location                 VARCHAR(255),
  csv_filename             VARCHAR(255),
  anthropic_batch_id       VARCHAR(128),
  batch_processing_status  VARCHAR(32),
  total                    INTEGER NOT NULL DEFAULT 0,
  progress                 INTEGER NOT NULL DEFAULT 0,
  added                    INTEGER NOT NULL DEFAULT 0,
  scored_ai                INTEGER NOT NULL DEFAULT 0,
  scored_fallback          INTEGER NOT NULL DEFAULT 0,
  regex_agree              INTEGER NOT NULL DEFAULT 0,
  ai_promoted              INTEGER NOT NULL DEFAULT 0,
  ai_demoted               INTEGER NOT NULL DEFAULT 0,
  estimated_cost           NUMERIC(10,4) NOT NULL DEFAULT 0,
  request_counts           JSONB,                   -- {"processing":n,"succeeded":n,...}
  pending_jobs             JSONB,                   -- filtered new_jobs list for restart recovery
  message                  TEXT,
  last_error               TEXT,
  stopped                  BOOLEAN NOT NULL DEFAULT FALSE,
  created_at               TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMP NOT NULL DEFAULT NOW(),
  started_at               TIMESTAMP,
  finished_at              TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_batches_status     ON import_batches(status);
CREATE INDEX IF NOT EXISTS idx_import_batches_created_at ON import_batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_import_batches_import_dt  ON import_batches(import_date);
CREATE INDEX IF NOT EXISTS idx_import_batches_anthropic  ON import_batches(anthropic_batch_id);
