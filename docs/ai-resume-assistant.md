# AI Resume Assistant

The AI Resume Assistant is an inline feature on each job's detail page that uses the Claude API to analyze resumes against job descriptions, suggest improvements, and help with interview preparation.

## Overview

Unlike the floating AI chat popup (which uses a local Ollama model), the Resume Assistant is embedded directly in the job detail page's right column. It provides pre-built prompts for common resume review tasks and supports follow-up questions with full conversation history.

## Setup

### API Key

The feature requires an Anthropic API key. Store it in AWS Parameter Store:

```bash
aws ssm put-parameter \
  --name "/job-search/anthropic-api-key" \
  --value "sk-ant-..." \
  --type SecureString \
  --overwrite
```

For local development, set the `ANTHROPIC_API_KEY` environment variable instead:

```bash
ANTHROPIC_API_KEY=sk-ant-... docker compose -f docker-compose.dev.yml up
```

If no key is configured, the assistant displays an error message when a prompt is clicked.

## Pre-built Prompts

| Button | What it does |
|--------|-------------|
| **Analyze Fit** | Rates resume-to-job fit from 1-10. Identifies which requirements are covered and where gaps exist. References specific resume bullets. |
| **Tailor Bullets** | Suggests specific bullet rewordings, reorderings, or substitutions from the full bullet pool to improve keyword alignment. Shows before/after for each change. |
| **Review PDF** | Extracts text from the uploaded PDF resume and compares it against the job description. Identifies missing keywords and rates targeting accuracy. Only appears when a PDF has been uploaded. |
| **Interview Prep** | Generates 5 technical and 5 behavioral interview questions based on the job description. Includes suggested answers that reference specific experience. |
| **Cover Letter** | Drafts a concise 2-paragraph cover letter tailored to the position. |

## Architecture

### API Endpoint

`POST /api/ai-review`

**Request:**
```json
{
  "message": "Analyze this job posting against my resume...",
  "job_id": "abc123",
  "history": [],
  "include_pdf": false
}
```

**Response:**
```json
{
  "response": "**Fit Score: 8/10**\n\n..."
}
```

### Context Building

The system prompt includes:
1. Candidate name, skills, and experience context (from `config.json`)
2. Full bullet pool from all past employers
3. Target job posting (company, title, location, salary, full description)
4. Current tailored resume text (generated during CSV import scoring)
5. Uploaded PDF resume text (only when `include_pdf: true`)

### PDF Text Extraction

When `include_pdf: true` is set in the request:
1. Server looks up the job's `pdf_path` from the job state
2. Uses PyPDF2 to extract text from the PDF file
3. Appends extracted text to the system prompt as a separate section

## Model

The assistant uses `claude-sonnet-4-20250514` via the Anthropic Python SDK. The model is hardcoded (not user-selectable) since the Claude API is billed per token and only one model tier is needed for resume analysis.

## Relationship to Existing AI Chat

| Feature | AI Chat Popup | AI Resume Assistant |
|---------|--------------|-------------------|
| **Location** | Floating panel (any view) | Inline on job detail page |
| **Backend** | Local Ollama (qwen2.5) | Claude API (Anthropic) |
| **Purpose** | General Q&A about jobs | Focused resume analysis |
| **Prompts** | 4 quick prompts | 5 task-specific prompts |
| **PDF support** | No | Yes (text extraction) |
