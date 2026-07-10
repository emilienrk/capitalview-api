"""Platform import services (CSV exports from exchanges, brokers, banks).

Importing this package populates the parser registry: every parser module
must be listed here so its ``@register`` decorator runs.
"""

from services.imports import (
    bank_csv,  # noqa: F401
    binance,  # noqa: F401
    coinbase,  # noqa: F401
    degiro,  # noqa: F401
    generic_csv,  # noqa: F401
    kraken,  # noqa: F401
    trade_republic,  # noqa: F401
)
