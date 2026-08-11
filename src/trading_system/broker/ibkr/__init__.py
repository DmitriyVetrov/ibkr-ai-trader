"""IBKR adapter: connection, state reads, market data and reconciliation.

The only package permitted to know that IBKR exists. ``ib_async`` is imported
lazily inside :mod:`trading_system.broker.ibkr.client`, so importing this
package — and running the unit test suite — does not require the library or a
gateway.

Everything crossing back out is a domain model; no IBKR type escapes here.
"""

from trading_system.broker.ibkr.client import IBKRBroker
from trading_system.broker.ibkr.reconciliation import Reconciler

__all__ = ["IBKRBroker", "Reconciler"]
