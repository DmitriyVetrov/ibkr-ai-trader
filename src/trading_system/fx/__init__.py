"""Explicit foreign exchange between the account's currency and the traded one.

The account holds capital in one currency; a campaign trades instruments quoted
in another. This package is the *only* connection between the two, and it is
deliberately narrow:

.. code-block:: text

    account / budget currency          EUR
              |
              |   FxRate, captured with the balance it converts
              v
    target / trading currency          USD
              |
              v
    risk  ->  allocation  ->  position sizing  ->  order validation

Two things are absent by design. There is **no default rate**: a pair nobody
quoted converts to nothing at all, and a caller that wants a figure has to ask
a conversion whose status is ``VALID``. And there is **no parity shortcut**:
this package cannot be configured, persuaded or defaulted into treating two
different currencies as one unit of account, which is the failure it was built
to remove.

Everything here is pure. Rates arrive as captured state - today from IBKR's own
per-currency ``ExchangeRate`` rows, which ride along with the account summary
already read at capture time - so no engine downstream needs a broker to
convert, and no conversion can hide a live request behind itself.
"""

from __future__ import annotations

from trading_system.fx.convert import convert
from trading_system.fx.models import FxConversion, FxRate, FxRateTable

__all__ = ["FxConversion", "FxRate", "FxRateTable", "convert"]
