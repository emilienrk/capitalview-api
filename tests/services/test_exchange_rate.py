"""Tests for the exchange rate service."""

from decimal import Decimal
from unittest.mock import patch, MagicMock

from services.exchange_rate import (
    get_exchange_rate,
    convert_amount,
    convert_position_to_eur,
    convert_account_to_eur,
    _cache,
    _FALLBACK_USD_EUR,
)
from dtos import PositionResponse, AccountSummaryResponse


def _clear_cache():
    """Helper to reset the in-memory cache between tests."""
    _cache.clear()


class TestGetExchangeRate:
    def setup_method(self):
        _clear_cache()

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_returns_live_rate(self, mock_fetch):
        mock_fetch.return_value = Decimal("0.91")
        rate = get_exchange_rate("USD", "EUR")
        assert rate == Decimal("0.91")
        mock_fetch.assert_called_once_with("USD", "EUR")

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_caches_rate(self, mock_fetch):
        mock_fetch.return_value = Decimal("0.91")
        rate1 = get_exchange_rate("USD", "EUR")
        rate2 = get_exchange_rate("USD", "EUR")
        assert rate1 == rate2
        # Called only once thanks to caching
        mock_fetch.assert_called_once()

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_fallback_when_fetch_fails(self, mock_fetch):
        mock_fetch.return_value = None
        rate = get_exchange_rate("USD", "EUR")
        assert rate == _FALLBACK_USD_EUR

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_inverse_fallback(self, mock_fetch):
        """When direct USD→EUR fails, try EUR→USD and invert."""
        mock_fetch.side_effect = [None, Decimal("1.09")]
        rate = get_exchange_rate("USD", "EUR")
        expected = Decimal("1") / Decimal("1.09")
        assert round(rate, 6) == round(expected, 6)

    def test_same_currency_returns_one(self):
        rate = get_exchange_rate("EUR", "EUR")
        assert rate == Decimal("1")

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_eur_to_usd_fallback(self, mock_fetch):
        mock_fetch.return_value = None
        rate = get_exchange_rate("EUR", "USD")
        expected = Decimal("1") / _FALLBACK_USD_EUR
        assert round(rate, 6) == round(expected, 6)


class TestConvertAmount:
    def setup_method(self):
        _clear_cache()

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_convert_usd_to_eur(self, mock_fetch):
        mock_fetch.return_value = Decimal("0.90")
        result = convert_amount(Decimal("100"), "USD", "EUR")
        assert result == Decimal("90.00")

    def test_convert_same_currency(self):
        result = convert_amount(Decimal("100"), "EUR", "EUR")
        assert result == Decimal("100")

    @patch("services.exchange_rate._fetch_rate_yahoo")
    def test_convert_zero(self, mock_fetch):
        mock_fetch.return_value = Decimal("0.90")
        result = convert_amount(Decimal("0"), "USD", "EUR")
        assert result == Decimal("0")


class TestConvertPositionToEur:
    def test_converts_monetary_fields(self):
        pos = PositionResponse(
            symbol="BTC",
            name="Bitcoin",
            total_amount=Decimal("1"),
            average_buy_price=Decimal("30000"),
            total_invested=Decimal("30000"),
            total_fees=Decimal("10"),
            fees_percentage=Decimal("0.03"),
            currency="USD",
            current_price=Decimal("50000"),
            current_value=Decimal("50000"),
            profit_loss=Decimal("20000"),
            profit_loss_percentage=Decimal("66.67"),
        )
        result = convert_position_to_eur(pos, Decimal("0.90"))

        assert result.currency == "EUR"
        assert result.total_invested == Decimal("27000.00")
        assert result.total_fees == Decimal("9.00")
        assert result.average_buy_price == Decimal("27000.0000")
        assert result.current_price == Decimal("45000.0000")
        assert result.current_value == Decimal("45000.00")
        assert result.profit_loss == Decimal("18000.00")
        # Non-monetary fields preserved
        assert result.symbol == "BTC"
        assert result.name == "Bitcoin"
        assert result.fees_percentage == Decimal("0.03")
        assert result.profit_loss_percentage == Decimal("66.67")

    def test_handles_none_market_data(self):
        pos = PositionResponse(
            symbol="XYZ",
            total_amount=Decimal("1"),
            average_buy_price=Decimal("100"),
            total_invested=Decimal("100"),
            total_fees=Decimal("0"),
            fees_percentage=Decimal("0"),
            currency="USD",
        )
        result = convert_position_to_eur(pos, Decimal("0.90"))
        assert result.current_price is None
        assert result.current_value is None
        assert result.profit_loss is None


class TestConvertAccountToEur:
    def test_converts_account_and_positions(self):
        pos = PositionResponse(
            symbol="BTC",
            total_amount=Decimal("1"),
            average_buy_price=Decimal("30000"),
            total_invested=Decimal("30000"),
            total_fees=Decimal("10"),
            fees_percentage=Decimal("0.03"),
            currency="USD",
            current_price=Decimal("50000"),
            current_value=Decimal("50000"),
            profit_loss=Decimal("20000"),
        )
        account = AccountSummaryResponse(
            account_id="acc-1",
            account_name="Test Crypto",
            account_type="CRYPTO",
            total_invested=Decimal("30010"),
            total_fees=Decimal("10"),
            currency="USD",
            current_value=Decimal("50000"),
            profit_loss=Decimal("19990"),
            profit_loss_percentage=Decimal("66.6"),
            positions=[pos],
        )
        result = convert_account_to_eur(account, Decimal("0.90"))

        assert result.currency == "EUR"
        assert result.total_invested == Decimal("27009.00")
        assert result.total_fees == Decimal("9.00")
        assert result.current_value == Decimal("45000.00")
        assert result.profit_loss == Decimal("17991.00")
        # Percentage preserved
        assert result.profit_loss_percentage == Decimal("66.6")
        # Positions also converted
        assert result.positions[0].currency == "EUR"
        assert result.positions[0].total_invested == Decimal("27000.00")
