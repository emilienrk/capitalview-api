"""Human labels for the asset keys the analytics reason about.

The analytics work on `asset_key` — an ISIN, and the only identifier stable
enough to join prices, transactions and holdings. It is also unreadable: nobody
recognises their portfolio in FR0000120073. The reference data already carries a
name and a ticker, so the report resolves both once and ships them alongside the
key rather than making the UI guess.

The key never leaves the payload: it stays the technical join, the label is what
gets displayed. When reference data is missing a name the ticker stands in, and
when both are missing the key does — a row is never dropped for lack of a label.
"""

from dataclasses import dataclass

from sqlmodel import Session, select

from models.market import MarketAsset


@dataclass(frozen=True)
class AssetLabel:
    asset_key: str
    symbol: str
    name: str

    def as_dict(self) -> dict:
        return {"asset_key": self.asset_key, "symbol": self.symbol, "name": self.name}


def _fallback(asset_key: str) -> AssetLabel:
    return AssetLabel(asset_key=asset_key, symbol=asset_key, name=asset_key)


def resolve_asset_labels(session: Session, asset_keys) -> dict[str, AssetLabel]:
    """Return {asset_key: AssetLabel} for every key asked, in one query."""
    keys = [key for key in dict.fromkeys(asset_keys or ()) if key]
    if not keys:
        return {}

    rows = session.exec(
        select(MarketAsset.asset_key, MarketAsset.symbol, MarketAsset.name).where(
            MarketAsset.asset_key.in_(keys)
        )
    ).all()

    known = {
        asset_key: AssetLabel(
            asset_key=asset_key,
            symbol=(symbol or asset_key).strip() or asset_key,
            name=(name or symbol or asset_key).strip() or asset_key,
        )
        for asset_key, symbol, name in rows
    }
    return {key: known.get(key) or _fallback(key) for key in keys}


def label_of(labels: dict[str, AssetLabel], asset_key: str) -> AssetLabel:
    """The label for a key, falling back to the key itself when unresolved."""
    return labels.get(asset_key) or _fallback(asset_key)
