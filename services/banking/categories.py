"""
The user's categories, and which of them each screen offers.

One table for every screen. A category remembers where it was created — a
declared cashflow, Banque, or the AI — and that origin only decides where it is
offered, never what it means:

- with AI categorisation on, Banque and the real cashflow offer their own and
  the AI's, while the declared cashflow offers its own, so the AI's naming
  never floods what the user typed by hand;
- with it off, everything is offered everywhere, the free-text categories of
  the declared cashflows included.

Turning the switch moves nothing: only what is offered changes.

`Cashflow.category` stays free text. A cashflow's category picked in Banque is
materialised then, as a row of origin `cashflow`; a Banque category picked for
a cashflow is simply written as its name.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import AvailableCategory, CategoryNature, CategoryOrigin, CategoryScope
from models.banking import BankCategory, BankCategoryRule
from models.cashflow import Cashflow
from models.enums import FlowType
from services.encryption import decrypt_data, encrypt_data, hash_index

MAX_NAME_LENGTH = 60

_OFFERED_WITH_AI = {
    CategoryScope.BANK: frozenset({CategoryOrigin.BANK, CategoryOrigin.AI}),
    CategoryScope.PLANNED: frozenset({CategoryOrigin.CASHFLOW}),
}


class CategoryNotFoundError(LookupError):
    """No category of this user has this id."""


class CategoryNameTakenError(ValueError):
    """Another category of this user already reads as this name."""


class InvalidCategoryNameError(ValueError):
    """The name is empty or too long."""


@dataclass(frozen=True)
class Category:
    uuid: str
    name: str
    nature: CategoryNature
    origin: CategoryOrigin


def name_key(name: str) -> str:
    """A name as compared for uniqueness: case, accents and spacing folded."""
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def load_categories(session: Session, user_uuid: str, master_key: str) -> dict[str, Category]:
    rows = session.exec(
        select(BankCategory).where(BankCategory.user_uuid_bidx == hash_index(user_uuid, master_key))
    ).all()
    return {row.uuid: _read(row, master_key) for row in rows}


def create_category(
    session: Session,
    user_uuid: str,
    master_key: str,
    name: str,
    nature: CategoryNature,
    origin: CategoryOrigin,
) -> Category:
    user_bidx = hash_index(user_uuid, master_key)
    name = _valid_name(name)
    name_bidx = hash_index(name_key(name), master_key)
    if _row_named(session, user_bidx, name_bidx) is not None:
        raise CategoryNameTakenError(name)
    row = BankCategory(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data(name, master_key),
        name_bidx=name_bidx,
        nature_enc=encrypt_data(nature.value, master_key),
        origin_enc=encrypt_data(origin.value, master_key),
    )
    session.add(row)
    session.commit()
    return _read(row, master_key)


def rename_category(
    session: Session, user_uuid: str, master_key: str, category_id: str, name: str
) -> Category:
    row = _owned_row(session, user_uuid, master_key, category_id)
    name = _valid_name(name)
    name_bidx = hash_index(name_key(name), master_key)
    other = _row_named(session, row.user_uuid_bidx, name_bidx)
    if other is not None and other.uuid != row.uuid:
        raise CategoryNameTakenError(name)
    row.name_enc = encrypt_data(name, master_key)
    row.name_bidx = name_bidx
    session.add(row)
    session.commit()
    return _read(row, master_key)


def set_category_nature(
    session: Session, user_uuid: str, master_key: str, category_id: str, nature: CategoryNature
) -> Category:
    row = _owned_row(session, user_uuid, master_key, category_id)
    row.nature_enc = encrypt_data(nature.value, master_key)
    session.add(row)
    session.commit()
    return _read(row, master_key)


def delete_category(session: Session, user_uuid: str, master_key: str, category_id: str) -> None:
    """Delete a category and the rules filing into it.

    An operation's own override naming it is left in place and reads as
    uncategorised from then on: finding those rows would mean decrypting every
    operation, which carry no user column to narrow the search.
    """
    row = _owned_row(session, user_uuid, master_key, category_id)
    rules = session.exec(
        select(BankCategoryRule).where(BankCategoryRule.user_uuid_bidx == row.user_uuid_bidx)
    ).all()
    for rule in rules:
        if decrypt_data(rule.category_ref_enc, master_key) == category_id:
            session.delete(rule)
    session.delete(row)
    session.commit()


def materialize_cashflow_category(session: Session, user_uuid: str, master_key: str, name: str) -> Category:
    """The category row for a declared cashflow's category, created on first use.

    Its nature follows the cashflows carrying that name, by majority: income
    when most of them are inflows, an expense otherwise.
    """
    user_bidx = hash_index(user_uuid, master_key)
    key = name_key(name)
    existing = _row_named(session, user_bidx, hash_index(key, master_key))
    if existing is not None:
        return _read(existing, master_key)
    flows = [flow for text, flow in _cashflow_categories(session, user_bidx, master_key) if name_key(text) == key]
    if not flows:
        raise CategoryNotFoundError(name)
    return create_category(
        session, user_uuid, master_key, name, _nature_of_flows(flows), CategoryOrigin.CASHFLOW,
    )


def available_categories(
    session: Session, user_uuid: str, master_key: str, scope: CategoryScope, ai_enabled: bool
) -> list[AvailableCategory]:
    """The categories `scope` offers, one per name, sorted by name."""
    user_bidx = hash_index(user_uuid, master_key)
    offered: dict[str, AvailableCategory] = {}
    origins = _OFFERED_WITH_AI[scope] if ai_enabled else frozenset(CategoryOrigin)
    for category in load_categories(session, user_uuid, master_key).values():
        if category.origin in origins:
            offered[name_key(category.name)] = AvailableCategory(
                id=category.uuid, name=category.name, nature=category.nature, origin=category.origin,
            )

    if not ai_enabled or scope is CategoryScope.PLANNED:
        by_name: dict[str, tuple[str, list[FlowType]]] = {}
        for text, flow in _cashflow_categories(session, user_bidx, master_key):
            key = name_key(text)
            if key and key not in offered:
                by_name.setdefault(key, (text.strip(), []))[1].append(flow)
        for key, (text, flows) in by_name.items():
            offered[key] = AvailableCategory(
                name=text, nature=_nature_of_flows(flows), origin=CategoryOrigin.CASHFLOW,
            )

    return sorted(offered.values(), key=lambda c: name_key(c.name))


def _cashflow_categories(session: Session, user_bidx: str, master_key: str) -> list[tuple[str, FlowType]]:
    rows = session.exec(
        select(Cashflow.category_enc, Cashflow.flow_type_enc).where(Cashflow.user_uuid_bidx == user_bidx)
    ).all()
    return [
        (decrypt_data(category, master_key), FlowType(decrypt_data(flow_type, master_key)))
        for category, flow_type in rows
    ]


def _nature_of_flows(flows: list[FlowType]) -> CategoryNature:
    counts = Counter(flows)
    return CategoryNature.INCOME if counts[FlowType.INFLOW] > counts[FlowType.OUTFLOW] else CategoryNature.EXPENSE


def _valid_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME_LENGTH:
        raise InvalidCategoryNameError(name)
    return name


def _row_named(session: Session, user_bidx: str, name_bidx: str) -> BankCategory | None:
    return session.exec(
        select(BankCategory).where(
            sa.and_(BankCategory.user_uuid_bidx == user_bidx, BankCategory.name_bidx == name_bidx)
        )
    ).first()


def _owned_row(session: Session, user_uuid: str, master_key: str, category_id: str) -> BankCategory:
    row = session.get(BankCategory, category_id)
    if row is None or row.user_uuid_bidx != hash_index(user_uuid, master_key):
        raise CategoryNotFoundError(category_id)
    return row


def _read(row: BankCategory, master_key: str) -> Category:
    return Category(
        uuid=row.uuid,
        name=decrypt_data(row.name_enc, master_key),
        nature=CategoryNature(decrypt_data(row.nature_enc, master_key)),
        origin=CategoryOrigin(decrypt_data(row.origin_enc, master_key)),
    )
