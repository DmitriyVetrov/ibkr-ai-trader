"""Simulated broker used by default in tests and dry runs.

Deterministic and offline: identical inputs always produce identical output.
Everything it returns is stamped ``SIMULATOR`` so simulated state can never be
mistaken for real broker state.
"""

from trading_system.broker.simulator.broker import (
    SimulatedBroker,
    SimulatedBrokerState,
    default_simulated_state,
)
from trading_system.broker.simulator.market import (
    SIMULATED_SOURCE,
    simulated_option_chain,
    simulated_quote,
    simulated_reference_price,
)

__all__ = [
    "SIMULATED_SOURCE",
    "SimulatedBroker",
    "SimulatedBrokerState",
    "default_simulated_state",
    "simulated_option_chain",
    "simulated_quote",
    "simulated_reference_price",
]
