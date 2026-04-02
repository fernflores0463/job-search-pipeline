# Job Search Pipeline

A personal job search automation system that processes LinkedIn CSV exports, scores and filters postings against your tech stack, generates tailored resumes, and serves an interactive dashboard with an AI assistant.

## Features

- **Scoring & Filtering**: Automatically scores job postings based on tech keyword matches, seniority level, and company tier. Filters out staffing agencies, wrong seniority levels, and irrelevant specializations.
- **Resume Tailoring**: Selects and orders resume bullets based on keyword overlap with each job's description. Generates a tailored resume for every qualifying position.
- **Interactive Dashboard**: Shows all jobs with status tracking, notes, batch history, a Sankey flow visualization, and an application pipeline board.
- **CSV Upload**: Upload LinkedIn CSV exports directly through the dashboard — no local Python install needed. Jobs are scored and imported server-side.
- **AI Assistant**: Chat with a local Ollama model about any job posting — fit analysis, resume suggestions, interview prep.
- **Careers Scraper**: Scrape Greenhouse, Lever, and Ashby job boards directly into the pipeline.
- **Login Protection**: Single-password session auth with HTTP-only cookies. Password stored in AWS Parameter Store — changeable without a rebuild.
- **Config-Driven**: All personal data (name, LinkedIn URL, resume bullets, scoring weights) lives in `config.json`. The code ships with no personal information.

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

# 2. Start Postgres + the server with hot reload
docker compose -f docker-compose.dev.yml up

# 3. Open http://localhost:8080
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
- `tech_keywords`: Map of regex patterns to point values.
- `tier_thresholds`: `strong` (default: 13) and `match` (default: 7) thresholds.
- `top_companies`: Company name substrings that get a +2 bonus.

### `filters`
Controls which jobs are excluded:
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
process_new_postings.py (local)      or      Dashboard CSV Upload (remote)
  |-- Filter (company, title, role)            |-- Same filter/score logic
  |-- Score (tech + level + company)           |-- Inserts directly to PostgreSQL
  |-- Tier (Strong / Match / Weak)             '-- Resume text stored in DB
  |-- Generate resumes/ per job
  '-- Write metadata to JSON
       |
       v
dashboard/server.py (port 8080)
  |-- GET  /               -> dashboard.html
  |-- GET  /login          -> login page
  |-- POST /api/login      -> set session cookie
  |-- GET  /api/logout     -> clear session
  |-- GET  /api/jobs-data  -> all jobs + state merged
  |-- POST /api/import-csv -> upload LinkedIn CSV
  |-- GET  /api/batch-stats
  |-- POST /api/state      -> save job state
  |-- GET  /api/plans      -> application plans
  |-- POST /api/plans      -> save plans
  |-- GET  /api/config     -> candidate name + LinkedIn URL
  |-- POST /api/ai-chat    -> Ollama AI assistant
  |-- POST /api/scrape-careers -> scrape a careers page
  '-- POST /api/run-live-check -> check if postings are still open
```

---

## Database

PostgreSQL schema lives in `db/schema.sql`. Five tables:

| Table | Contents |
|---|---|
| `jobs` | Job metadata — title, company, score, tier, links |
| `job_state` | Per-job status, notes, timestamps, live status |
| `application_plans` | Named batches of jobs to apply to |
| `application_plan_jobs` | Jobs within each plan |
| `company_cache` | Company summaries and logo URLs |

DB credentials are read from environment variables (`DB_HOST`, `DB_PASSWORD`, etc.) or pulled from AWS Parameter Store automatically in production.

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

For local dev, set the `DASHBOARD_PASSWORD` environment variable instead. If neither is set, auth is bypassed entirely (open access).

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
