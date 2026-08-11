"""Raw provider payloads to the canonical representation.

Normalisation converts shape, never substance. It may rename a field, map a
provider enum onto ours, convert a timestamp to UTC or attach provenance. It
may not invent a missing value, repair an implausible one, or drop a source
identifier — the raw record must always still explain what the provider
actually said.
"""

from trading_system.data.normalizers.broker import (
    market_quote_from_broker,
    option_chain_from_broker,
    option_contract_from_broker,
)

__all__ = [
    "market_quote_from_broker",
    "option_chain_from_broker",
    "option_contract_from_broker",
]
