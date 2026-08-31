"""
Job run model.

One row per execution of a background job — the nightly cron and the history
rebuilds. Without it nothing records that a job started, so "did last night's
job run, and what did it do" had no answer, and a rebuild that died between its
DELETE and its recomputation left a truncated wealth curve nobody could see.

Operational telemetry, stored in cleartext: it carries identifiers, counters and
durations, never an amount or anything else decrypted.
"""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Column, TEXT
from sqlmodel import Field, SQLModel


class JobStatus:
    """Lifecycle of one run. Plain strings so adding one needs no migration."""
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class JobRun(SQLModel, table=True):
    """A single execution of a background job."""

    __tablename__ = "job_runs"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    job_name: str = Field(sa_column=Column(sa.String(60), nullable=False, index=True))
    # Which user a per-user job ran for; null for the cluster-wide cron. No
    # foreign key on purpose: deleting a user must not erase the record that a
    # job ran, and a dangling opaque uuid carries nothing personal.
    user_uuid: str | None = Field(default=None, sa_column=Column(sa.String, nullable=True, index=True))
    started_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, index=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )
    status: str = Field(sa_column=Column(sa.String(10), nullable=False, index=True))
    # Whatever the job wants to count: prices updated, snapshots written. Generic
    # sa.JSON rather than JSONB — nothing here is ever queried inside.
    counters: dict | None = Field(default=None, sa_column=Column(sa.JSON, nullable=True))
    error: str | None = Field(default=None, sa_column=Column(TEXT, nullable=True))
