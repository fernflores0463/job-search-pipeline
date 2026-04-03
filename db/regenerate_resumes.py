#!/usr/bin/env python3
"""
Regenerate resume_text for all jobs using the current config.json bullet pool.

Run this after updating config.json with new/corrected experience bullets.
It re-runs pick_bullets + customize_skills + generate_resume_txt for every
job in the database and updates the resume_text column.

Usage:
  # Dry run (default) — shows what would change without writing
  docker exec -it <container> python3 db/regenerate_resumes.py

  # Apply changes
  docker exec -it <container> python3 db/regenerate_resumes.py --apply
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db import init_pool, get_conn, put_conn  # noqa: E402
from process_new_postings import (  # noqa: E402
    pick_bullets, customize_skills, generate_resume_txt,
)


def main():
    apply = "--apply" in sys.argv

    init_pool()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, company, title, description, resume_text "
            "FROM jobs ORDER BY company, title"
        )
        rows = cur.fetchall()
        print(f"Total jobs in DB: {len(rows)}")

        updated = 0
        skipped = 0
        for job_id, company, title, description, old_resume in rows:
            if not description:
                skipped += 1
                continue

            job = {
                "company": company or "",
                "title": title or "",
                "description": description,
            }
            bullets = pick_bullets(description, title or "")
            langs, frameworks, misc = customize_skills(description)
            new_resume = generate_resume_txt(
                job, bullets, langs, frameworks, misc
            )

            if new_resume == old_resume:
                skipped += 1
                continue

            updated += 1
            if not apply and updated <= 5:
                print(f"\n--- {company} | {title} ---")
                # Show first 3 lines of new resume as preview
                preview = "\n".join(new_resume.split("\n")[:3])
                print(f"  Preview: {preview}...")

            if apply:
                cur.execute(
                    "UPDATE jobs SET resume_text = %s WHERE id = %s",
                    (new_resume, job_id)
                )

        if apply:
            conn.commit()
            print(f"\nDone! Updated {updated} resumes, "
                  f"skipped {skipped} (unchanged or no description).")
        else:
            print(f"\nDRY RUN — {updated} resumes would be updated, "
                  f"{skipped} unchanged.")
            print("Run with --apply to execute.")

    finally:
        put_conn(conn)


if __name__ == "__main__":
    main()
