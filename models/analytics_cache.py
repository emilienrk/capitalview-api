"""
Investor analytics, kept between reads.

The analysis page replays the whole stock ledger, backfills the market history
of every line held and runs thousands of permutation draws. None of that
changes between two visits unless the ledger or the settings did, and the
prices only drift. So the finished payload is kept here, encrypted under the
user's Master Key like everything else about them, with a digest of what it
was built from: a reader whose digest no longer matches rebuilds before
reading, and a payload older than its validity is rebuilt too, so the price
drift is bounded (services/analytics/cache.py).

Written on read, never by a job: the server never holds a Master Key outside
a request, so no background process could compute — or even open — a row.
"""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import TEXT, Column
from sqlmodel import Field, SQLModel


class InvestorAnalyticsCache(SQLModel, table=True):
    """One finished analytics payload per user and kind ("investor", "yearly")."""

    __tablename__ = "investor_analytics_cache"
    __table_args__ = {"extend_existing": True}

    user_uuid_bidx: str = Field(sa_column=Column(TEXT, primary_key=True, nullable=False))
    kind: str = Field(sa_column=Column(sa.String(20), primary_key=True, nullable=False))
    # Digest of the ledger rows and settings the payload was derived from.
    source_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    # JSON of the response payload, Decimals and dates as strings.
    content_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    built_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    # Past this instant the payload is rebuilt even on an unchanged digest:
    # prices move without touching any row the digest reads.
    valid_until: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
