/**
 * Eightfold Career Page Scraper — Chrome Console Script
 *
 * Usage:
 *   1. Open a company's Eightfold careers page in Chrome
 *      (e.g., https://starbucks.eightfold.ai/careers?query=Software+Engineer)
 *   2. Scroll down to load all the jobs you want (or click "Load More")
 *   3. Open Developer Tools (Cmd+Option+I) → Console tab
 *   4. Paste this entire script and press Enter
 *   5. A CSV file will download automatically
 *   6. Run: python scrape_careers_page.py downloaded_file.csv --company "Company Name"
 *
 * The CSV matches the same format as LinkedIn exports so it flows
 * right into the existing pipeline.
 */

(async function scrapeEightfold() {
  console.log("🔍 Scanning for job cards...");

  // Eightfold renders job cards in various containers
  const cards = document.querySelectorAll(
    '[data-test-id="position-card"], ' +
    '.position-card, ' +
    '[class*="PositionCard"], ' +
    '[class*="position-card"], ' +
    '[role="listitem"], ' +
    'article[class*="job"], ' +
    'div[class*="careers-list"] > div, ' +
    'div[class*="job-list"] > div'
  );

  if (cards.length === 0) {
    // Fallback: look for any repeated pattern with job-like links
    const links = document.querySelectorAll('a[href*="/careers/"][href*="pid="], a[href*="/jobs/"]');
    if (links.length === 0) {
      console.error("❌ No job cards found. Make sure the page has fully loaded and jobs are visible.");
      console.log("💡 Try scrolling down to load more jobs, then run this script again.");
      return;
    }
    console.log(`Found ${links.length} job links (fallback mode)`);
  } else {
    console.log(`Found ${cards.length} job cards`);
  }

  const jobs = [];
  const seen = new Set();

  // Strategy 1: Extract from structured cards
  for (const card of cards) {
    const titleEl = card.querySelector(
      'h3, h4, [class*="title"], [class*="Title"], [data-test-id="position-title"], a'
    );
    const linkEl = card.querySelector('a[href*="pid="], a[href*="/careers/"], a[href*="/jobs/"]') || card.closest('a');
    const locationEl = card.querySelector(
      '[class*="location"], [class*="Location"], [data-test-id="position-location"]'
    );

    const title = titleEl ? titleEl.textContent.trim() : '';
    const link = linkEl ? linkEl.href : '';
    const location = locationEl ? locationEl.textContent.trim() : '';

    if (title && !seen.has(title + link)) {
      seen.add(title + link);
      jobs.push({ title, link, location });
    }
  }

  // Strategy 2: Fallback to link-based extraction
  if (jobs.length === 0) {
    const links = document.querySelectorAll('a[href*="pid="], a[href*="/careers/"], a[href*="/jobs/"]');
    for (const link of links) {
      const title = link.textContent.trim();
      const href = link.href;
      if (title && title.length > 5 && title.length < 200 && !seen.has(title + href)) {
        seen.add(title + href);
        // Try to find location near the link
        const parent = link.closest('div, li, article, tr');
        let location = '';
        if (parent) {
          const locEl = parent.querySelector('[class*="location"], [class*="Location"]');
          if (locEl) location = locEl.textContent.trim();
        }
        jobs.push({ title, link: href, location });
      }
    }
  }

  console.log(`📋 Extracted ${jobs.length} jobs`);

  if (jobs.length === 0) {
    console.error("❌ Could not extract any jobs. The page structure may have changed.");
    return;
  }

  // Now fetch descriptions for each job
  console.log("📖 Fetching descriptions (this may take a moment)...");

  for (let i = 0; i < jobs.length; i++) {
    const job = jobs[i];
    if (!job.link) continue;

    try {
      const resp = await fetch(job.link);
      const html = await resp.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');

      // Try to extract description
      const descEl = doc.querySelector(
        '[class*="description"], [class*="Description"], ' +
        '[data-test-id="description"], ' +
        '#job-description, .job-description, ' +
        'article, main, .content'
      );

      job.description = descEl ? descEl.textContent.trim().substring(0, 5000) : '';
      console.log(`  [${i + 1}/${jobs.length}] ${job.title} — ${job.description.length} chars`);
    } catch (e) {
      job.description = '';
      console.log(`  [${i + 1}/${jobs.length}] ${job.title} — failed to fetch`);
    }

    // Small delay to be polite
    if (i < jobs.length - 1) {
      await new Promise(r => setTimeout(r, 300));
    }
  }

  // Build CSV in the same format as LinkedIn exports
  const headers = ["Job Title", "Company", "Location", "Description", "Application Link", "Job Link", "Salary", "Work Type", "Posted Date", "Applicants"];
  const companyName = document.title.replace(/careers?\s*(at|-)?\s*/gi, '').replace(/\s*[-|].*/, '').trim() || window.location.hostname.split('.')[0];

  let csv = headers.join(",") + "\n";
  for (const job of jobs) {
    const escape = (s) => '"' + (s || '').replace(/"/g, '""').replace(/\n/g, ' ') + '"';
    csv += [
      escape(job.title),
      escape(companyName),
      escape(job.location),
      escape(job.description),
      escape(job.link),
      escape(job.link),
      escape(''),
      escape(''),
      escape(''),
      escape(''),
    ].join(",") + "\n";
  }

  // Download CSV
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const timestamp = new Date().toISOString().split("T")[0];
  a.href = url;
  a.download = `careers_${companyName.toLowerCase().replace(/\s+/g, '_')}_${timestamp}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.log(`\n✅ Done! Downloaded CSV with ${jobs.length} jobs.`);
  console.log(`\n📌 Next steps:`);
  console.log(`   1. Move the CSV: cp ~/Downloads/careers_*.csv ~/Documents/job_search_2026/job_postings_dump/csv_imports/`);
  console.log(`   2. Process it:   cd ~/Documents/job_search_2026 && python process_new_postings.py job_postings_dump/csv_imports/careers_*.csv`);
  console.log(`   3. Or use:       python scrape_careers_page.py ~/Downloads/careers_*.csv --company "${companyName}"`);
})();
