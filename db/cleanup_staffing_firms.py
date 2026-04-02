#!/usr/bin/env python3
"""
One-time cleanup: remove jobs from staffing firms already in the database.

Usage:
  # Against local dev DB
  DB_HOST=localhost DB_PASSWORD=localdev python3 db/cleanup_staffing_firms.py

  # Against production (from EC2)
  DB_HOST=<rds-host> DB_PASSWORD=<pw> python3 db/cleanup_staffing_firms.py

  # Dry run (default) — prints what would be deleted without touching the DB
  DB_HOST=localhost DB_PASSWORD=localdev python3 db/cleanup_staffing_firms.py

  # Actually delete
  DB_HOST=localhost DB_PASSWORD=localdev python3 db/cleanup_staffing_firms.py --apply
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db import init_pool, get_conn, put_conn  # noqa: E402


def load_exclusion_lists():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(config_path) as f:
        config = json.load(f)
    companies = config["filters"]["exclude_companies_containing"]
    desc_patterns = config["filters"].get("exclude_description_patterns", [])
    return companies, desc_patterns


def is_excluded(company, description, companies, desc_patterns):
    cl = company.lower().strip()
    if any(ex in cl for ex in companies):
        return True, "company"
    dl = description.lower() if description else ""
    if any(pat in dl for pat in desc_patterns):
        return True, "description"
    return False, None


def main():
    apply = "--apply" in sys.argv

    companies, desc_patterns = load_exclusion_lists()
    print(f"Loaded {len(companies)} company patterns, {len(desc_patterns)} description patterns")

    init_pool()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, company, title, description FROM jobs")
        rows = cur.fetchall()
        print(f"Total jobs in DB: {len(rows)}")

        to_delete = []
        for job_id, company, title, description in rows:
            excluded, reason = is_excluded(company or "", description or "", companies, desc_patterns)
            if excluded:
                to_delete.append((job_id, company, title, reason))

        if not to_delete:
            print("No staffing firm jobs found in DB. Nothing to clean.")
            return

        print(f"\nFound {len(to_delete)} staffing firm jobs to remove:\n")
        for job_id, company, title, reason in sorted(to_delete, key=lambda x: x[1].lower()):
            print(f"  [{reason:11s}] {company:40s} | {title}")

        if not apply:
            print(f"\nDRY RUN — {len(to_delete)} jobs would be deleted.")
            print("Run with --apply to execute the cleanup.")
            return

        ids = [row[0] for row in to_delete]
        cur.execute("DELETE FROM application_plan_jobs WHERE job_id = ANY(%s)", (ids,))
        plan_jobs_deleted = cur.rowcount
        cur.execute("DELETE FROM job_state WHERE job_id = ANY(%s)", (ids,))
        state_deleted = cur.rowcount
        cur.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))
        jobs_deleted = cur.rowcount
        conn.commit()

        print(f"\nDeleted {jobs_deleted} jobs, {state_deleted} job_state rows, {plan_jobs_deleted} plan_job rows.")

    finally:
        put_conn(conn)


if __name__ == "__main__":
    main()
