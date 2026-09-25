"""
Rate limit model.

One row per accepted request, in a sliding window. Shared state on purpose: a
per-process counter lets `uvicorn --workers 4` accept four times the limit, and
resets on every redeploy.

The bucket is an opaque HMAC of "<ip>:<action>", never the address itself: the
limiter only ever needs equality, and an IP is personal data this table has no
business keeping in the clear.
"""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Column
from sqlmodel import Field, SQLModel


class RateLimitHit(SQLModel, table=True):
    """A single request counted against a bucket."""

    __tablename__ = "rate_limit_hits"
    __table_args__ = (
        # Every read is "this bucket, inside this window", and the purge walks
        # hit_at; one composite index serves both.
        sa.Index("ix_rate_limit_hits_bucket_hit_at", "bucket", "hit_at"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    bucket: str = Field(sa_column=Column(sa.String(64), nullable=False))
    hit_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
