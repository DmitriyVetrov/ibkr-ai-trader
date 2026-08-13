"""Profit and loss attribution. The position ledger itself ships as ``positions/``.

Specification section 3 names this package for the position repository, the
position service and P&L. The first two were delivered in Milestone 9 as
:mod:`trading_system.positions`, so that the package, the ``positions`` CLI
group and the ``tests/positions`` suite share one name — the same choice the
strategy agent makes in shipping as ``strategy_selector`` rather than the
specification's ``options_strategist``.

What remains here is ``pnl``: attribution over closed trades, which needs the
realised profit and loss the evaluation milestone introduces. Empty until then
by design — a stub that pretends to work is worse than an absent module
(specification section 48.3).
"""
