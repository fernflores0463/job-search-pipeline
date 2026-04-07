"""
Shared PostgreSQL connection pool.

Credential resolution order:
  1. Environment variables: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT
  2. AWS Parameter Store: /job-search/db-* (used in production on EC2)
"""

import os
import logging
from psycopg2 import pool

logger = logging.getLogger(__name__)

_connection_pool = None


def _get_param(name):
    """Fetch a single SSM Parameter Store value."""
    import boto3
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    resp = ssm.get_parameter(Name=name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def _load_credentials():
    """Return DB credentials from env vars or Parameter Store."""
    host = os.environ.get("DB_HOST")
    if host:
        return {
            "host": host,
            "dbname": os.environ.get("DB_NAME", "jobsearch"),
            "user": os.environ.get("DB_USER", "jobsearch"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "port": int(os.environ.get("DB_PORT", 5432)),
        }

    # Fall back to Parameter Store (production)
    logger.info("DB_HOST not set — loading credentials from Parameter Store")
    return {
        "host": _get_param("/job-search/db-host"),
        "dbname": _get_param("/job-search/db-name"),
        "user": _get_param("/job-search/db-user"),
        "password": _get_param("/job-search/db-password"),
        "port": 5432,
    }


def init_pool(minconn=1, maxconn=10):
    """Initialize the connection pool. Call once at server startup."""
    global _connection_pool
    if _connection_pool is not None:
        return
    creds = _load_credentials()
    _connection_pool = pool.ThreadedConnectionPool(minconn, maxconn, **creds)
    logger.info("Database connection pool initialized (host=%s, db=%s)", creds["host"], creds["dbname"])


def get_conn():
    """Get a connection from the pool."""
    if _connection_pool is None:
        init_pool()
    return _connection_pool.getconn()


def put_conn(conn):
    """Return a connection to the pool."""
    if _connection_pool:
        _connection_pool.putconn(conn)


class Db:
    """Context manager for safe connection checkout/return."""

    def __enter__(self):
        self.conn = get_conn()
        self.conn.autocommit = False
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        put_conn(self.conn)
        return False
