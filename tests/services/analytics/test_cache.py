"""The finished analysis is served from the store until its sources move."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from dtos.analytics import InvestorAnalyticsResponse
from models.analytics_cache import InvestorAnalyticsCache
from models.stock import StockAccount
from services.analytics.cache import KIND_INVESTOR, KIND_YEARLY, cached_payload, source_digest
from services.encryption import encrypt_data, hash_index
from services.stock_transaction import create_eur_deposit

USER = "user_cache"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _account(session, master_key, uuid="acc_cache"):
    session.add(
        StockAccount(
            uuid=uuid,
            user_uuid_bidx=hash_index(USER, master_key),
            name_enc=encrypt_data("PEA", master_key),
            account_type_enc=encrypt_data("PEA", master_key),
        )
    )
    session.commit()


def _payload(days=730):
    return {
        "period_start": datetime(2024, 1, 1).date(),
        "period_end": datetime(2026, 1, 1).date(),
        "days": days,
        "benchmark_asset_key": "IE00B4L5Y983",
        "verdict": "Un verdict.",
        "signals": [{"block": "fees", "label": "Frais", "tone": "good", "eur": Decimal("-12.5")}],
    }


class _Counter:
    def __init__(self, payload=None):
        self.calls = 0
        self.payload = payload or _payload()

    def __call__(self):
        self.calls += 1
        return dict(self.payload)


@pytest.fixture(autouse=True)
def _thirty_days():
    with patch("services.analytics.cache.cache_days", return_value=30):
        yield


def test_the_second_read_is_served_from_the_store(session, master_key):
    build = _Counter()

    first = cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)
    second = cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW + timedelta(days=3))

    assert build.calls == 1
    assert first["cached"] is False and second["cached"] is True
    assert first["computed_at"] == NOW
    assert second["computed_at"] == NOW
    assert first["valid_until"] == NOW + timedelta(days=30)
    # Types survive the round trip: a Decimal comes back a Decimal, a date a date.
    assert second["days"] == 730
    assert second["period_start"] == first["period_start"]
    assert second["signals"][0]["eur"] == Decimal("-12.5")
    assert isinstance(second["signals"][0]["eur"], Decimal)


def test_a_new_transaction_moves_the_digest_and_rebuilds(session, master_key):
    _account(session, master_key)
    build = _Counter()
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)
    before = source_digest(session, USER, master_key, KIND_INVESTOR)

    create_eur_deposit(session, "acc_cache", Decimal("100"), datetime(2026, 1, 2, 10), master_key)

    assert source_digest(session, USER, master_key, KIND_INVESTOR) != before
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)
    assert build.calls == 2


def test_an_expired_row_is_rebuilt(session, master_key):
    build = _Counter()
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)

    late = cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW + timedelta(days=31))

    assert build.calls == 2
    assert late["cached"] is False
    assert late["computed_at"] == NOW + timedelta(days=31)


def test_refresh_rebuilds_whatever_the_store_holds(session, master_key):
    build = _Counter()
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)

    forced = cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW + timedelta(days=1), refresh=True)

    assert build.calls == 2
    assert forced["cached"] is False
    row = session.get(InvestorAnalyticsCache, (hash_index(USER, master_key), KIND_INVESTOR))
    assert row.built_at.replace(tzinfo=UTC) == NOW + timedelta(days=1)


def test_the_kinds_do_not_share_a_row(session, master_key):
    investor = _Counter()
    yearly = _Counter({"benchmark_asset_key": "X", "benchmark_name": "X", "years": []})
    from dtos.analytics import YearlyPerformanceResponse

    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, investor, now=NOW)
    cached_payload(session, USER, master_key, KIND_YEARLY, YearlyPerformanceResponse, yearly, now=NOW)
    cached_payload(session, USER, master_key, KIND_YEARLY, YearlyPerformanceResponse, yearly, now=NOW)

    assert investor.calls == 1 and yearly.calls == 1


def test_a_changed_benchmark_rebuilds(session, master_key):
    from models.user import UserSettings

    settings = UserSettings(user_uuid_bidx=hash_index(USER, master_key))
    session.add(settings)
    session.commit()
    build = _Counter()
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)

    settings.benchmark_asset_key = "FR0000000001"
    session.add(settings)
    session.commit()
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)

    assert build.calls == 2


def test_a_disabled_cache_stores_nothing(session, master_key):
    build = _Counter()
    with patch("services.analytics.cache.cache_days", return_value=0):
        first = cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)
        cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, build, now=NOW)

    assert build.calls == 2
    assert first["valid_until"] is None
    assert session.get(InvestorAnalyticsCache, (hash_index(USER, master_key), KIND_INVESTOR)) is None


def test_the_stored_row_is_ciphertext(session, master_key):
    cached_payload(session, USER, master_key, KIND_INVESTOR, InvestorAnalyticsResponse, _Counter(), now=NOW)

    row = session.get(InvestorAnalyticsCache, (hash_index(USER, master_key), KIND_INVESTOR))
    assert "verdict" not in row.content_enc and "IE00B4L5Y983" not in row.content_enc
