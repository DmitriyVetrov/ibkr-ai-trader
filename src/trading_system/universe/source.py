"""Loading the candidate pool.

The source is configuration, never a list literal in Python: "which assets were
even considered on date T" is part of reconstructing a decision, and a symbol
list embedded in code changes without a review trail.

Four of the seven source kinds are deliberately **not implemented**, and that is
a correctness decision rather than a gap in effort. Producing an S&P 500 or
NASDAQ-100 constituent list from memory would be inventing data — the same
prohibition that stops an agent citing a source it did not retrieve. An index
universe that is quietly wrong by five names yields research nobody can trust,
so those kinds fail loudly and say what they would need instead.
"""

from __future__ import annotations

from pathlib import Path

from trading_system.domain.enums import UniverseSourceKind
from trading_system.infrastructure.settings import UniverseSourceConfig, project_root
from trading_system.universe.models import UniverseSourceRef

__all__ = [
    "IMPLEMENTED_KINDS",
    "UniverseSourceError",
    "load_symbols",
    "source_reference",
]


class UniverseSourceError(RuntimeError):
    """The candidate pool could not be loaded, and will not be approximated."""


#: Kinds that resolve to a real symbol list today.
IMPLEMENTED_KINDS: frozenset[UniverseSourceKind] = frozenset(
    {
        UniverseSourceKind.STATIC,
        UniverseSourceKind.CUSTOM,
        UniverseSourceKind.FILE,
    }
)

#: What each unimplemented kind would actually need. Named explicitly so the
#: error tells an operator how to proceed rather than only that it stopped.
_REQUIREMENTS: dict[UniverseSourceKind, str] = {
    UniverseSourceKind.SP500: (
        "a retrieved, dated constituent list from an authoritative source; "
        "writing one from memory would be invented data"
    ),
    UniverseSourceKind.NASDAQ100: (
        "a retrieved, dated constituent list from an authoritative source; "
        "writing one from memory would be invented data"
    ),
    UniverseSourceKind.ETF_UNIVERSE: (
        "a retrieved ETF listing with an issuer and inception date per fund"
    ),
    UniverseSourceKind.IBKR_DISCOVERED: (
        "the IBKR scanner API, which is a different broker capability from the "
        "quote and chain reads Milestone 2 validated"
    ),
}


def _normalise(symbol: str) -> str:
    """Canonical form of a ticker: upper case, trimmed.

    Applied before deduplication so that ``aapl``, ``AAPL `` and ``AAPL`` are
    one candidate rather than three.
    """
    return symbol.strip().upper()


def load_symbols(config: UniverseSourceConfig, *, root: Path | None = None) -> list[str]:
    """Resolve the configured source to a list of symbols.

    Order is preserved and duplicates are removed here, keeping the first
    occurrence: the pre-filter reports duplicates as a rejection reason, and
    doing the collapse in one place keeps that report honest.
    """
    if config.kind not in IMPLEMENTED_KINDS:
        requirement = _REQUIREMENTS.get(config.kind, "an authoritative source")
        raise UniverseSourceError(
            f"universe source kind {config.kind.value} is not implemented: it requires "
            f"{requirement}. Configure a STATIC, CUSTOM or FILE source instead — the "
            f"system does not approximate a constituent list."
        )

    if config.kind is UniverseSourceKind.FILE:
        raw = _read_file(config, root=root)
    else:
        raw = list(config.symbols)

    seen: set[str] = set()
    symbols: list[str] = []
    for entry in raw:
        symbol = _normalise(entry)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def raw_symbols(config: UniverseSourceConfig, *, root: Path | None = None) -> list[str]:
    """Every configured symbol, normalised but with duplicates intact.

    The pre-filter needs the un-collapsed list to report ``DUPLICATE_SYMBOL``
    rather than silently having fewer candidates than the configuration lists.
    """
    if config.kind not in IMPLEMENTED_KINDS:
        return []
    entries = (
        _read_file(config, root=root) if config.kind is UniverseSourceKind.FILE else config.symbols
    )
    return [_normalise(entry) for entry in entries if _normalise(entry)]


def _read_file(config: UniverseSourceConfig, *, root: Path | None) -> list[str]:
    """Read a newline-delimited symbol file. ``#`` starts a comment."""
    location = (config.location or "").strip()
    path = Path(location)
    if not path.is_absolute():
        path = (root or project_root()) / path
    if not path.is_file():
        raise UniverseSourceError(
            f"universe source file not found: {path}. The configured source names it, "
            f"so an absent file is a configuration error rather than an empty universe."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UniverseSourceError(f"cannot read universe source file {path}: {exc}") from exc
    return [line.split("#", 1)[0] for line in text.splitlines()]


def source_reference(
    config: UniverseSourceConfig, symbols: list[str] | None = None
) -> UniverseSourceRef:
    """Build the stamped reference recorded on every run."""
    return UniverseSourceRef(
        kind=config.kind,
        name=config.name,
        version=config.version,
        symbol_count=len(symbols) if symbols is not None else len(config.symbols),
        location=config.location,
    )
