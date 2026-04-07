# Job Search Pipeline

A personal job search automation system that processes LinkedIn CSV exports, scores and filters postings against your tech stack, generates tailored resumes, and serves an interactive dashboard with an AI assistant.

## Features

- **Regex Scoring & Filtering**: Automatically scores job postings based on tech keyword matches, seniority level, and company tier. Filters out staffing agencies, wrong seniority levels, and irrelevant specializations.
- **AI Scoring (Claude Haiku)**: Optional second import path that scores every job 1–10 using Claude Haiku against your candidate profile. Stores AI reasoning per job. Falls back to regex scoring per-job on API errors — imports never abort.
- **AI vs Regex Comparison**: During AI imports, the regex score is also computed for free. Inline chips (`AI 18 ↑ Rx 12`) appear on every job's reasoning block, and a live agreement summary card shows how often the two systems agree.
- **Resume Tailoring**: Selects and orders resume bullets based on keyword overlap with each job's description. Generates a tailored resume for every qualifying position.
- **Interactive Dashboard**: Shows all jobs with status tracking, notes, batch history, a Sankey flow visualization, and an application pipeline board.
- **CSV Upload (Regex)**: Upload LinkedIn CSV exports directly through the dashboard — no local Python install needed. Jobs are scored and imported server-side.
- **CSV Upload (AI)**: Upload the same CSV through the "🤖 AI Import CSV" panel. Jobs are scored by Claude Haiku in parallel (2 workers) with automatic 429 retry and exponential backoff.
- **Anthropic Batch API mode**: Opt-in checkbox on the AI import panel submits all jobs as a single Anthropic batch request (50% cost discount, async). Dashboard polls every 30s; inserts all jobs when the batch ends.
- **Real-time Import Status (SSE)**: The AI import panel subscribes to `/api/ai-import-stream` (Server-Sent Events) instead of polling. Status updates are pushed in real time. On page refresh, the dashboard automatically reattaches the stream if an import is still running.
- **AI Resume Assistant**: Chat with Claude directly on any job's detail page — fit analysis, bullet suggestions, interview prep. Uses prompt caching for cost efficiency.
- **AI Job Import**: Paste a job URL and let Claude extract all structured fields automatically.
- **Careers Scraper**: Scrape Greenhouse, Lever, and Ashby job boards directly into the pipeline.
- **Live Status Checker**: Checks whether job postings are still open (not 404 or expired).
- **Login Protection**: Single-password session auth with HTTP-only cookies. Password stored in AWS Parameter Store — changeable without a rebuild.
- **Config-Driven**: All personal data (name, LinkedIn URL, resume bullets, scoring weights, filter lists) lives in `config.json`. The code ships with no personal information.

---

## Architecture

```
GitHub (main branch)
       |  push
       v
GitHub Actions CI/CD
  |-- Build Docker image
  |-- Push to ECR
  '-- SSH deploy to EC2
       |
       v
EC2 + nginx (HTTPS, TLS 1.3, rate limiting)
  '-- Docker: dashboard/server.py -> port 8080
       |
       v
RDS PostgreSQL 15
  '-- 5 tables: jobs, job_state, application_plans,
                application_plan_jobs, company_cache
```

---

## Local Development

### Quick Start

```bash
# 1. Clone and configure
git clone <your-repo-url>
cd job-search-pipeline
cp config.example.json config.json
# Edit config.json with your name, LinkedIn URL, bullets, and scoring weights

# 2. Add your Anthropic API key (needed for AI import features)
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # gitignored

# 3. Start Postgres + the server with hot reload
docker compose -f docker-compose.dev.yml up

# 4. Open http://localhost:8080
# No password required locally.
```

### How It Works

`docker-compose.dev.yml` runs two containers:

| Container | What it does |
|---|---|
| **db** | Postgres 15 — schema auto-applied on first start via `docker-entrypoint-initdb.d` |
| **server** | Your source code mounted as volumes, watched by `watchmedo` for auto-restart |

Edit any `.py` file and the server restarts automatically — no rebuild needed.

### Useful Commands

```bash
# Start (foreground — see logs, Ctrl+C to stop everything)
docker compose -f docker-compose.dev.yml up

# Start in background
docker compose -f docker-compose.dev.yml up -d

# View logs
docker compose -f docker-compose.dev.yml logs -f server

# Reset database (deletes all local data)
docker compose -f docker-compose.dev.yml down -v
docker compose -f docker-compose.dev.yml up
```

Local data is fully isolated — nothing you do locally affects production or the dev environment.

### Without Docker

If you prefer running Python directly (requires a Postgres instance):

```bash
# Start just the database
docker compose -f docker-compose.dev.yml up db

# Run the server natively
DB_HOST=localhost DB_PASSWORD=localdev python3 dashboard/server.py
```

---

## Dev Branch Deploys

Push to any `dev/*` branch to deploy a preview environment on the same EC2 instance.

```bash
git checkout -b dev/my-feature
# ... make changes ...
git push origin dev/my-feature
```

GitHub Actions builds the image, deploys it to **port 8081** on the EC2 instance, and **auto-kills the container after 1 hour** to save costs. The dev environment:

- Uses a separate `jobsearch_dev` database (isolated from production)
- Has no login required (auth bypassed)
- Schema is applied automatically on each deploy
- Is accessible at `http://<EC2-IP>:8081`

When you're satisfied, merge to `main` to trigger the production deploy.

### One-Time Setup (before first dev deploy)

```bash
# 1. Create the dev database on RDS
# SSH to EC2, then run:
docker run --rm -e PGPASSWORD=<db-password> postgres:15 \
  psql -h <RDS-HOST> -U <db-user> -d <prod-db-name> \
  -c "CREATE DATABASE jobsearch_dev;"

# 2. Open port 8081 in the EC2 security group
aws ec2 authorize-security-group-ingress \
  --group-id <security-group-id> \
  --protocol tcp --port 8081 \
  --cidr 0.0.0.0/0
```

---

## Configuration Guide

All personal data lives in `config.json` (gitignored). Copy `config.example.json` to get started.

### `candidate`
```json
{
  "candidate": {
    "name": "Your Name",
    "linkedin_url": "https://www.linkedin.com/in/your-profile/",
    "summary": "A software engineer with experience at ...",
    "skills": "Go, Java, JavaScript, ..."
  }
}
```

### `experience`
Your work history, organized by company key. Each entry has a `display_name`, a `context` string (used in the AI prompt), a `bullet_limit` (max bullets per resume from this employer), and your `bullets`:
```json
{
  "experience": {
    "COMPANY_A": {
      "display_name": "Company A",
      "context": "Company A (Go, React, Microservices)",
      "bullet_limit": 5,
      "bullets": [
        "Built backend services using Go...",
        "Led migration of gRPC services..."
      ]
    }
  }
}
```

### `scoring`
Controls how jobs are scored and tiered:
- `tech_keywords`: Map of regex patterns to point values (used by the regex baseline).
- `tier_thresholds`: `strong` (default: 13) and `match` (default: 7) thresholds for the 0–24 legacy score range.
- `top_companies`: Company name substrings that get a +2 bonus on the regex score.

### `filters`
Controls which jobs are excluded before scoring:
- `exclude_companies_containing`: Substrings — any company whose name contains one is filtered out (e.g., `"staffing"`, `"recruiting"`).
- `exclude_title_keywords`: Job title keywords to reject (e.g., `"principal"`, `"intern"`).
- `exclude_role_keywords`: Description keywords for wrong specializations (e.g., `"mobile"`, `"devops"`).

### `bullet_keyword_pairs`
A list of `[description_pattern, bullet_keyword]` pairs that drive bullet selection. When a job description matches `description_pattern`, bullets containing `bullet_keyword` score higher.

### `skills_template`
Default skills section for generated resumes:
```json
{
  "skills_template": {
    "languages": "Go, Java, ...",
    "frameworks": "Spring Boot, React, ...",
    "misc": "Git, Docker, Kubernetes, ..."
  }
}
```

---

## Pipeline Workflow

```
LinkedIn CSV export
       |
       v
┌──────────────────────────────────┬────────────────────────────────────────┐
│  Regex Import (free, instant)    │  AI Import (Claude Haiku, ~$0.001/job) │
│  POST /api/import-csv            │  POST /api/import-csv-ai               │
│                                  │    ?mode=live  (default)               │
│  Filter → Regex Score → Insert   │    ?mode=batch (50% off, async)        │
│                                  │                                        │
│                                  │  Filter → AI Score + Regex Baseline    │
│                                  │        → Compare → Insert              │
└──────────────────────────────────┴────────────────────────────────────────┘
       |
       v
dashboard/server.py (port 8080)
  |-- GET  /                      -> dashboard.html
  |-- GET  /login                 -> login page
  |-- POST /api/login             -> set session cookie
  |-- GET  /api/logout            -> clear session
  |-- GET  /api/jobs-data         -> all jobs + state merged
  |-- GET  /api/batch-stats       -> import batch history
  |-- POST /api/import-csv        -> regex CSV upload
  |-- GET  /api/import-status     -> regex import progress
  |-- POST /api/import-csv-ai     -> AI CSV upload (live or batch mode)
  |-- GET  /api/ai-import-status  -> AI import progress (one-shot poll)
  |-- GET  /api/ai-import-stream  -> AI import progress (SSE stream)
  |-- POST /api/ai-import-stop    -> graceful stop (live mode only)
  |-- POST /api/state             -> save job state
  |-- GET  /api/plans             -> application plans
  |-- POST /api/plans             -> save plans
  |-- GET  /api/config            -> candidate name + LinkedIn URL
  |-- POST /api/ai-chat           -> Ollama AI assistant
  |-- POST /api/scrape-careers    -> scrape a careers page
  |-- GET  /api/scrape-status     -> scrape progress
  '-- POST /api/run-live-check    -> check if postings are still open
```

---

## AI Scoring

### How it works

The "🤖 AI Import CSV" panel accepts the same LinkedIn CSV as the regular import. Instead of the regex scorer, every qualifying job is sent to **Claude Haiku** with a system prompt containing your candidate profile and full bullet pool. Haiku returns:

```json
{ "fit_score": 8, "tier": "Strong Match", "reasoning": "Strong Go + gRPC + Kafka overlap..." }
```

The fit score (1–10) is mapped to the 0–24 legacy range so tiers are consistent with regex-scored batches. AI reasoning is stored in the `ai_reasoning` column and shown as a purple block on every job detail view.

The regex score is also computed for every AI-imported job (free, local), enabling the inline comparison chip and the AI vs Regex agreement summary.

### Scoring tiers

| AI fit score | Legacy score | Tier |
|---|---|---|
| 8–10 | 18–24 | Strong Match |
| 5–7 | 12–17 | Match |
| 1–4 | 0–11 | Weak Match |

Entry-level / new-grad roles are capped at **Match** (score 5) regardless of tech overlap — seniority mismatch is factored in by the AI prompt.

### Rate limiting

Anthropic Tier 1 allows 50,000 input tokens per minute. Each scoring call uses ~2,200 tokens (system prompt + job description). The server uses **2 parallel workers** with **exponential backoff** on 429 errors (5s → 10s → 20s, up to 3 retries). Jobs that still fail after retries fall back to regex scoring and are marked as fallback in the status panel.

To score faster, upgrade your Anthropic account to Tier 2 (100K ITPM) and increase `max_workers` in `_run_import_csv_ai`.

### Batch API mode

Checking "Use Batch API (50% off, async)" submits all jobs as a single Anthropic Message Batch instead of parallel live calls. The server polls Anthropic every 30s and inserts all results when the batch ends. Key differences from live mode:

| | Live mode | Batch mode |
|---|---|---|
| Cost | ~$0.00093/job | ~$0.00047/job |
| Speed | 40–90s for ~150 jobs | Minutes to hours |
| DB inserts | All at once after scoring | All at once after batch ends |
| Rate limit risk | Low (2 workers + backoff) | None (Anthropic handles it) |
| Stop button | Yes | No (batch runs at Anthropic) |

### Cost estimates

| Scenario | Cost |
|---|---|
| Per job (live) | ~$0.00093 |
| Per job (batch) | ~$0.00047 |
| 150-job CSV (live) | ~$0.14 |
| 150-job CSV (batch) | ~$0.07 |
| Full 925-job CSV (no dedup hits, live) | ~$0.86 |

Prompt caching makes the first call in a batch more expensive; subsequent calls within a 5-minute window benefit from cached system prompt tokens (~90% discount on the cached portion).

---

## Database

PostgreSQL schema lives in `db/schema.sql`. Five tables:

| Table | Contents |
|---|---|
| `jobs` | Job metadata — title, company, score, tier, links, `ai_reasoning`, `regex_score` |
| `job_state` | Per-job status, notes, timestamps, live status |
| `application_plans` | Named batches of jobs to apply to |
| `application_plan_jobs` | Jobs within each plan |
| `company_cache` | Company summaries and logo URLs |

DB credentials are read from environment variables (`DB_HOST`, `DB_PASSWORD`, etc.) or pulled from AWS Parameter Store automatically in production.

### Additive migrations

New columns are added at the bottom of `db/schema.sql` as idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. Safe to re-run on existing databases — all new columns are nullable.

### One-time migration from JSON files
If you have existing data in the legacy JSON files:
```bash
DB_HOST=<host> DB_PASSWORD=<pw> python3 db/migrate_json_to_pg.py
```

---

## CSV Format

The pipeline expects LinkedIn's job export CSV format:

| Column | Description |
|---|---|
| `Job Title` | Position title |
| `Company` | Employer name |
| `Location` | Job location |
| `Work Type` | Remote / Hybrid / On-site |
| `Salary` | Salary range (if listed) |
| `Posted Date` | Date posted |
| `Applicants` | Applicant count (if listed) |
| `Description` | Full job description text |
| `Application Link` | Direct apply URL |
| `Job Link` | LinkedIn job URL |
| `Search Location` | (Optional) Metro area used as batch label |

A sample file with fictional job postings is included at `sample_data/sample_jobs.csv`.

---

## Production Deployment (AWS)

The app runs on EC2 behind nginx with a TLS certificate from Let's Encrypt. CI/CD is fully automated via GitHub Actions.

### Infrastructure
| Resource | Detail |
|---|---|
| EC2 | t3.micro, Amazon Linux 2023 |
| RDS | PostgreSQL 15, t3.micro |
| ECR | Docker image registry |
| IAM | OIDC role for GitHub Actions (ECR push only); EC2 instance role (SSM + ECR pull) |
| nginx | HTTPS redirect, TLS 1.2/1.3, rate limiting on `/api/login` |

### CI/CD

| Branch | What happens |
|---|---|
| `main` | Build + push to ECR + deploy to production (port 8080) |
| `dev/*` | Build + push to ECR + deploy to dev environment (port 8081, auto-kills in 1 hour) |
| Any other | Lint + syntax check only (no deploy) |

### Setting the dashboard password
The password lives in AWS Parameter Store — never in code or config files:
```bash
aws ssm put-parameter \
  --name "/job-search/dashboard-password" \
  --value "your-strong-password-here" \
  --type SecureString \
  --overwrite

# Restart the container to load the new password
ssh -i <key.pem> <ec2-user>@<EC2-IP> "docker compose restart server"
```

Password resolution order at startup:
1. `DASHBOARD_PASSWORD` env var — if explicitly set (even to empty string), use it; empty = auth bypassed
2. AWS Parameter Store `/job-search/dashboard-password` — used automatically in production via EC2 instance role
3. If neither resolves, auth is bypassed (open access)

The dev container sets `DASHBOARD_PASSWORD=""` explicitly so it never picks up the production password from Parameter Store.

### Setting the Anthropic API key
The key lives in AWS Parameter Store alongside the dashboard password:
```bash
aws ssm put-parameter \
  --name "/job-search/anthropic-api-key" \
  --value "sk-ant-..." \
  --type SecureString \
  --overwrite
```
The server loads it at startup from Parameter Store automatically (no restart needed if the container is already running — set it before the first deploy or restart the container after adding it). For local dev, put it in a gitignored `.env` file:
```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```
`docker-compose.dev.yml` picks up `.env` automatically via `env_file`.

### Manual deploy (without CI/CD)
```bash
# 1. Build and push to ECR
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-west-2.amazonaws.com

docker build --platform linux/amd64 \
  -t <account-id>.dkr.ecr.us-west-2.amazonaws.com/job-search-pipeline:latest .
docker push <account-id>.dkr.ecr.us-west-2.amazonaws.com/job-search-pipeline:latest

# 2. Pull and restart on EC2
ssh -i <key.pem> ec2-user@<EC2-IP> \
  "docker compose pull server && docker compose up -d --force-recreate server"
```

### Deploy nginx config changes
After editing `infra/nginx.conf`, copy it to the server and reload:
```bash
scp -i <key.pem> infra/nginx.conf <ec2-user>@<EC2-IP>:/etc/nginx/conf.d/jobs.conf
ssh -i <key.pem> <ec2-user>@<EC2-IP> "sudo nginx -t && sudo systemctl reload nginx"
```

---

## Optional Tools

### `scrape_careers_page.py`
Scrapes job listings from Greenhouse, Lever, and Ashby career pages. The dashboard's "Import URL" button calls this under the hood.
```bash
python3 scrape_careers_page.py https://boards.greenhouse.io/yourcompany
```

### `eightfold_scraper_console.js`
A browser console script for scraping Eightfold.ai-based job boards that require JavaScript rendering.
1. Open the careers page in Chrome
2. Open DevTools Console
3. Paste and run the script
4. Copy the output JSON

### `update_applicants.py`
Standalone script that fetches updated applicant counts without starting the dashboard server.
```bash
python3 update_applicants.py
```

### Cowork AI Scorer (`job_search_2026/cowork-ai-scorer/`)
A standalone CLI version of the AI scoring pipeline designed to run inside a Claude Cowork session or locally. No database, no web UI — CSV in, scored CSV out.

```bash
cd cowork-ai-scorer
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python3 score_csv.py ~/Downloads/linkedin_jobs.csv
```

Output CSV adds: `ai_fit_score`, `ai_legacy_score`, `ai_tier`, `ai_reasoning`, `regex_score`, `regex_tier`, `comparison` (agree / ai_promoted / ai_demoted / fallback), `ai_error`.

See `cowork-ai-scorer/README.md` for full usage, options, and cost estimates.

---

## AI Assistant

The dashboard includes a chat interface powered by a local [Ollama](https://ollama.ai) model.

```bash
# Install Ollama: https://ollama.ai
ollama pull qwen2.5:14b   # recommended model
```

The AI can analyze job-resume fit, suggest which bullets to emphasize, generate interview prep questions, and compare roles side-by-side.

---

## License

MIT
