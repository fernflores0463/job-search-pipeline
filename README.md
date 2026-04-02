# Job Search Pipeline

A personal job search automation system that processes LinkedIn CSV exports, scores and filters postings against your tech stack, generates tailored resumes, and serves an interactive dashboard with an AI assistant.

## Features

- **Scoring & Filtering**: Automatically scores job postings based on tech keyword matches, seniority level, and company tier. Filters out staffing agencies, wrong seniority levels, and irrelevant specializations.
- **Resume Tailoring**: Selects and orders your resume bullets based on keyword overlap with each job's description. Generates a `resume.txt` and `info.txt` for every qualifying position.
- **Interactive Dashboard**: A single-file HTML dashboard showing all jobs with status tracking, notes, batch history, and a Sankey flow visualization.
- **AI Assistant**: Chat with a local Ollama model about any job posting — get fit analysis, resume suggestions, and interview prep.
- **Careers Scraper**: Scrape Greenhouse, Lever, and Ashby job boards directly into the pipeline.
- **Config-Driven**: All personal data (name, LinkedIn URL, resume bullets, scoring weights) lives in `config.json`. The code ships with no personal information.

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/your-username/job-search-pipeline.git
cd job-search-pipeline

# 2. Copy the example config and fill in your details
cp config.example.json config.json
# Edit config.json with your name, LinkedIn URL, experience bullets, and scoring preferences

# 3. Test with the sample data
python3 process_new_postings.py sample_data/sample_jobs.csv

# 4. Start the dashboard
cd dashboard && python3 server.py

# 5. Open http://localhost:8080 in your browser
```

## Configuration Guide

All personal data lives in `config.json` (gitignored). Copy `config.example.json` to get started.

### `candidate`
Your basic information:
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
Your work history, organized by company key. Each entry has a `display_name`, a `context` string (used in the AI prompt), a `bullet_limit` (max bullets from this employer per resume), and your `bullets`:
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
- `tech_keywords`: Map of regex patterns to point values. Jobs accumulate points for each keyword found in the description.
- `tier_thresholds`: `strong` (default: 13) and `match` (default: 7) thresholds.
- `top_companies`: Company name substrings that get a +2 bonus.

### `filters`
Controls which jobs are excluded:
- `exclude_companies_containing`: Substrings — any company whose name contains one of these is filtered out (e.g., `"staffing"`, `"recruiting"`).
- `exclude_title_keywords`: Job title keywords to reject (e.g., `"principal"`, `"intern"`).
- `exclude_role_keywords`: Description/title keywords for wrong specializations (e.g., `"mobile"`, `"devops"`).

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

## Pipeline Workflow

```
LinkedIn CSV export
       │
       ▼
process_new_postings.py
  ├── Filter (company, title, role type)
  ├── Score (tech keywords + level bonus + company bonus)
  ├── Tier (Strong Match / Match / Weak Match)
  ├── Generate resumes/  (resume.txt + info.txt per job)
  ├── Save metadata  (resumes/all_jobs_metadata.json)
  └── Rebuild dashboard.html
       │
       ▼
dashboard/server.py
  ├── Serve dashboard.html
  ├── /api/state  (read/write job status, notes, timestamps)
  ├── /api/config  (name + LinkedIn URL for the frontend)
  ├── /api/ai-chat  (Ollama AI assistant)
  ├── /api/scrape-careers  (scrape a careers page)
  └── /api/run-live-check  (check if postings are still open)
```

## CSV Format

The pipeline expects LinkedIn's job export CSV format with these columns:

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
| `Search Location` | (Optional) Metro area used in batch label |

A sample file with 5 fictional job postings is included at `sample_data/sample_jobs.csv`.

## Optional Tools

### `scrape_careers_page.py`
Scrapes job listings directly from Greenhouse, Lever, and Ashby career pages. The dashboard's "Scrape Careers" button calls this under the hood.

```bash
python3 scrape_careers_page.py https://boards.greenhouse.io/yourcompany
```

### `eightfold_scraper_console.js`
A browser console script for scraping Eightfold.ai-based job boards (Wayfair, Boeing, etc.) that require JavaScript rendering.

1. Open the company's careers page in Chrome
2. Open DevTools Console
3. Paste and run the script
4. Copy the output JSON

### `update_applicants.py`
Standalone script that curls LinkedIn for updated applicant counts without starting the full dashboard server.

```bash
python3 update_applicants.py
```

## AI Assistant

The dashboard includes a chat interface powered by a local [Ollama](https://ollama.ai) model. The AI has full context about the job posting, your tailored resume, and your complete bullet pool.

**Setup:**
```bash
# Install Ollama: https://ollama.ai
ollama pull qwen2.5:14b   # recommended model
```

The AI can:
- Analyze job-resume fit and explain the score
- Suggest which bullets to emphasize or reword
- Generate interview prep questions tailored to the specific role
- Compare two roles side-by-side

## Screenshots

_Add screenshots of the dashboard here._

## License

MIT
