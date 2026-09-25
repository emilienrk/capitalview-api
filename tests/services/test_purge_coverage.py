"""
Every table is either wiped by `purge_account` or deliberately kept.

`test_deleting_the_account_leaves_no_row_behind` counts rows on a hand-written
list of tables, so a new table forgotten in the purge is forgotten there too and
the test stays green. This one reads the schema itself: a new table fails here
until it is wiped or listed below with its reason.
"""

import ast
import inspect
import textwrap

from sqlmodel import SQLModel

import models  # noqa: F401 — registers every table on the metadata
from services import account_data

# Tables holding no row that belongs to a user.
NOT_USER_DATA = {
    "market_assets": "shared market catalogue",
    "market_price_history": "shared price cache",
    "rate_limit_hits": "opaque HMAC of ip and action, expires on its own",
    "job_runs": "kept on purpose: a job ran, and an opaque uuid says nothing personal",
}


def _tables_purged() -> set[str]:
    """Every table a model named inside `purge_account` maps to."""
    source = textwrap.dedent(inspect.getsource(account_data.purge_account))
    names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
    tables = set()
    for name in names:
        candidate = getattr(account_data, name, None)
        if isinstance(candidate, type) and issubclass(candidate, SQLModel) and hasattr(candidate, "__table__"):
            tables.add(candidate.__tablename__)
    # The user row itself goes through `session.delete(user)`.
    return tables | {"users"}


def test_every_table_is_purged_or_deliberately_kept():
    missing = set(SQLModel.metadata.tables) - _tables_purged() - set(NOT_USER_DATA)
    assert not missing, (
        f"not wiped by purge_account: {sorted(missing)} — wipe them, or add them to "
        "NOT_USER_DATA with the reason they hold nothing of the user's"
    )


def test_the_exceptions_still_exist():
    assert set(NOT_USER_DATA) <= set(SQLModel.metadata.tables)
