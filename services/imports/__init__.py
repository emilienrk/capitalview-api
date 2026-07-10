"""Platform import services (CSV exports from exchanges, brokers, banks).

Importing this package populates the parser registry: every parser module
must be listed here so its ``@register`` decorator runs.
"""

from services.imports import binance  # noqa: F401
from services.imports import coinbase  # noqa: F401
from services.imports import kraken  # noqa: F401
