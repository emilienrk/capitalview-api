from models.enums import AssetType
from models.market import MarketAsset
from services.analytics.labels import label_of, resolve_asset_labels


def _asset(session, asset_key, symbol=None, name=None):
    asset = MarketAsset(
        asset_key=asset_key, symbol=symbol, name=name, asset_type=AssetType.STOCK
    )
    session.add(asset)
    session.commit()
    return asset


def test_a_known_key_resolves_to_its_name_and_ticker(session):
    _asset(session, "FR0000120073", symbol="AIR.PA", name="Air Liquide")

    labels = resolve_asset_labels(session, ["FR0000120073"])

    assert labels["FR0000120073"].name == "Air Liquide"
    assert labels["FR0000120073"].symbol == "AIR.PA"
    # The ISIN stays the key: it is the join, not the display.
    assert labels["FR0000120073"].asset_key == "FR0000120073"


def test_an_unknown_key_falls_back_to_itself(session):
    labels = resolve_asset_labels(session, ["FR0011550185"])

    assert labels["FR0011550185"].name == "FR0011550185"
    assert labels["FR0011550185"].symbol == "FR0011550185"


def test_a_name_less_asset_falls_back_to_its_ticker(session):
    _asset(session, "IE00B4L5Y983", symbol="IWDA.AS", name=None)

    labels = resolve_asset_labels(session, ["IE00B4L5Y983"])

    assert labels["IE00B4L5Y983"].name == "IWDA.AS"


def test_no_keys_asks_nothing(session):
    assert resolve_asset_labels(session, []) == {}
    assert resolve_asset_labels(session, None) == {}


def test_duplicate_and_empty_keys_are_ignored(session):
    labels = resolve_asset_labels(session, ["FR0000120073", "FR0000120073", "", None])

    assert list(labels) == ["FR0000120073"]


def test_label_of_never_returns_none_for_a_missing_key():
    assert label_of({}, "FR0013412020").name == "FR0013412020"


def test_a_label_serialises_to_the_three_fields_the_ui_reads(session):
    _asset(session, "FR0000120073", symbol="AIR.PA", name="Air Liquide")

    payload = resolve_asset_labels(session, ["FR0000120073"])["FR0000120073"].as_dict()

    assert payload == {
        "asset_key": "FR0000120073",
        "symbol": "AIR.PA",
        "name": "Air Liquide",
    }
