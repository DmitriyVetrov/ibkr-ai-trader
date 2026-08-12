"""Fixtures for the per-strategy suites.

The option-market builders are the ones the contract-selection suite already
uses, re-exported here rather than copied. A second copy would drift, and these
suites exist precisely to check that each strategy behaves the way its
specification says against the *same* market the selector tests use.

pytest does not share fixtures between sibling directories, so importing the
fixture functions into this conftest is what registers them here.
"""

from __future__ import annotations

from tests.contract_selection.conftest import (
    FURTHER,
    NEAR_TARGET,
    REFERENCE,
    SELECTION_NOW,
    STRIKES,
    TOO_FAR,
    TOO_NEAR,
    build_option_quotes,
    data_repo,
    make_decision,
    make_selector,
    priced_chain,
    registry,
    select,
    selection_clock,
    selection_now,
    store_chain,
    store_option_quotes,
    store_quote_records,
    store_underlying_quote,
)

__all__ = [
    "FURTHER",
    "NEAR_TARGET",
    "REFERENCE",
    "SELECTION_NOW",
    "STRIKES",
    "TOO_FAR",
    "TOO_NEAR",
    "build_option_quotes",
    "data_repo",
    "make_decision",
    "make_selector",
    "priced_chain",
    "registry",
    "select",
    "selection_clock",
    "selection_now",
    "store_chain",
    "store_option_quotes",
    "store_quote_records",
    "store_underlying_quote",
]
