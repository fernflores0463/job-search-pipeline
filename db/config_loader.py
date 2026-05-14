"""
db/config_loader.py — Config loading with S3 fallback

Resolution order for loading:
  1. Local config.json (fast path for local dev and bind-mounted prod files)
  2. AWS S3 s3://<CONFIG_S3_BUCKET>/config/config.json (production fallback)

Use save_config() to persist changes — it writes locally (if the file exists /
is writable) AND pushes to S3 so the authoritative copy stays in sync.

Environment variables:
  CONFIG_S3_BUCKET   — S3 bucket name (default: fernflores-job-search-resumes)
  AWS_REGION         — AWS region      (default: us-west-2)
"""

import json
import os
import sys

_AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
_S3_BUCKET = os.environ.get("CONFIG_S3_BUCKET", "fernflores-job-search-resumes")
_S3_KEY = "config/config.json"

# Resolved at import time so all callers share the same base path
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config.json")


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _s3_client():
    import boto3
    return boto3.client("s3", region_name=_AWS_REGION)


def _load_from_s3() -> dict:
    """Fetch config JSON from S3. Raises on failure."""
    s3 = _s3_client()
    obj = s3.get_object(Bucket=_S3_BUCKET, Key=_S3_KEY)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _push_to_s3(cfg: dict) -> None:
    """Push config dict to S3."""
    s3 = _s3_client()
    s3.put_object(
        Bucket=_S3_BUCKET,
        Key=_S3_KEY,
        Body=json.dumps(cfg, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """
    Load config, preferring the local file for speed.
    Falls back to S3 when running in a container without a bind-mounted file
    (e.g. after EC2 is replaced or a fresh deployment).

    Exits with a helpful message only if both paths fail.
    """
    # Fast path: local file exists
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH) as f:
            return json.load(f)

    # S3 fallback
    try:
        s3_path = f"s3://{_S3_BUCKET}/{_S3_KEY}"
        print(f"[config] config.json not found locally — loading from {s3_path} ...", flush=True)
        cfg = _load_from_s3()
        print("[config] Loaded from S3.", flush=True)
        return cfg
    except Exception as e:
        print(f"[config] ERROR: config.json not found and S3 fetch failed: {e}")
        print("  • For local dev: cp config.example.json config.json")
        print(f"  • For production: ensure s3://{_S3_BUCKET}/{_S3_KEY} exists")
        sys.exit(1)


def save_config(cfg: dict) -> None:
    """
    Persist config changes.

    • Writes to the local file when it already exists (so the running process
      sees the update immediately via the bind-mounted path).
    • Always pushes to S3 so the authoritative copy stays in sync and survives
      EC2 replacement.

    S3 failures are logged but do NOT crash — the local file is ground truth
    for the currently running container.
    """
    # Write locally only if the file already exists (don't create it in
    # environments that intentionally rely on S3, e.g. fresh containers)
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")

    # Push to S3
    try:
        _push_to_s3(cfg)
        print("[config] Config pushed to S3.", flush=True)
    except Exception as e:
        print(f"[config] WARNING: S3 push failed: {e}", flush=True)


def upload_current_config() -> None:
    """
    One-shot helper: read the local config.json and push it to S3.

    Run from the repo root:
        python3 -c 'from db.config_loader import upload_current_config; upload_current_config()'
    """
    if not os.path.exists(_CONFIG_PATH):
        print(f"ERROR: {_CONFIG_PATH} not found.")
        sys.exit(1)
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    patterns = len(cfg.get("filters", {}).get("exclude_companies_containing", []))
    s3_path = f"s3://{_S3_BUCKET}/{_S3_KEY}"
    print(f"[config] Uploading config.json to {s3_path} ({patterns} exclusion patterns)...")
    _push_to_s3(cfg)
    print(f"[config] Done.")
