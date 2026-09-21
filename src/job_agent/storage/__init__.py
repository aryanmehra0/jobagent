"""Databases the agent writes for querying: fetched jobs, contacts and outreach."""

from job_agent.storage.jobs_db import JobsDatabase, sync_jobs_db

__all__ = ["JobsDatabase", "sync_jobs_db"]
