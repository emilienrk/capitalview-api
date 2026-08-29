"""GET /market/currencies — the one list the web app builds its pickers from."""
from fastapi.testclient import TestClient

from main import app
from models.currency import BASE_CURRENCY, CURRENCY_CODES, NO_CURRENCY, SUPPORTED_CURRENCIES


def test_the_route_serves_every_supported_currency():
    body = TestClient(app).get("/market/currencies").json()

    assert body["base"] == BASE_CURRENCY
    assert {c["code"] for c in body["currencies"]} == CURRENCY_CODES
    assert all(c["name"] for c in body["currencies"])


def test_the_base_currency_is_offered_first():
    """The picker's default. A list that buried it would make the ordinary case
    the one the user has to hunt for."""
    body = TestClient(app).get("/market/currencies").json()

    assert body["currencies"][0]["code"] == BASE_CURRENCY


def test_no_currency_is_never_offered():
    """XXX is ISO 4217's "no currency", and it is exactly what Boursorama returns
    on the account resource — offering it would let a balance be read in it."""
    assert NO_CURRENCY not in CURRENCY_CODES


def test_the_crypto_alias_still_points_at_the_same_set():
    """~50 call sites across crypto, community and imports read FIAT_ASSET_KEYS.
    It is now an alias; this is what catches the two drifting apart again."""
    from dtos.crypto import FIAT_ASSET_KEYS

    assert FIAT_ASSET_KEYS is CURRENCY_CODES


def test_every_code_is_a_well_formed_iso_alphabetic_code():
    for currency in SUPPORTED_CURRENCIES:
        assert len(currency.code) == 3 and currency.code.isalpha() and currency.code.isupper()
