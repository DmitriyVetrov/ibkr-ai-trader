"""The source trust policy, read from ``config/sources.yaml``.

There is exactly **one** source-ranking system in this project and this module
reads it — it does not define a second one. ``config/sources.yaml`` lists the
domains and identifiers belonging to each tier, and everything here is a
consequence of that file.

Two jobs:

* **Resolve a tier.** A record arrives from the data layer already carrying the
  tier its provider declared. Where the configured policy recognises the
  source's identifier or name, the configured tier wins — that file is the
  project's considered judgement about who to trust, and a provider should not
  be able to promote itself by stamping ``TIER_1`` on its own output. Where the
  policy says nothing, the record's own tier stands, because rewriting an
  unrecognised source to the bottom tier would be a judgement nobody made.
* **Order and count.** Evidence is preferred by tier when a window has to be
  truncated, and the confidence policy needs to know the best tier a report
  actually rests on. A low-tier source is never discarded; it simply cannot
  license a ``HIGH``.

Matching is deliberately simple: case-insensitive substring against the
source's identifier (usually a URL) and its display name. A domain like
``reuters.com`` matches ``https://www.reuters.com/technology/...`` and the name
``Reuters`` matches an entry ``reuters.com`` only through its stem. Anything
cleverer would be a second ranking system wearing a disguise.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_system.domain.enums import SourceTier
from trading_system.infrastructure.settings import SourcesConfig
from trading_system.research.models import (
    ResearchSourcePolicySnapshot,
    tier_rank,
)

__all__ = ["SourceTrustPolicy"]


@dataclass(frozen=True, slots=True)
class _TierEntry:
    tier: SourceTier
    pattern: str
    #: The pattern's leading label, e.g. ``reuters`` for ``reuters.com``. Used
    #: so a display name of "Reuters" matches a domain entry without the entry
    #: having to be duplicated in two forms.
    stem: str


class SourceTrustPolicy:
    """Classifies a source into a trust tier, per ``config/sources.yaml``."""

    def __init__(self, config: SourcesConfig) -> None:
        self._config = config
        entries: list[_TierEntry] = []
        for tier, patterns in (
            (SourceTier.TIER_1, config.tier_1),
            (SourceTier.TIER_2, config.tier_2),
            (SourceTier.TIER_3, config.tier_3),
            (SourceTier.TIER_4, config.tier_4),
        ):
            for raw in patterns:
                pattern = raw.strip().lower()
                if not pattern:
                    continue
                entries.append(_TierEntry(tier=tier, pattern=pattern, stem=_stem(pattern)))
        # Most trusted first, then longest pattern: a specific entry should win
        # over a broad one, and ties must not depend on dict ordering.
        self._entries = sorted(entries, key=lambda e: (tier_rank(e.tier), -len(e.pattern)))

    @property
    def config(self) -> SourcesConfig:
        return self._config

    @property
    def require_source_attribution(self) -> bool:
        return self._config.require_source_attribution

    @property
    def min_sources_per_report(self) -> int:
        return self._config.min_sources_per_report

    def classify(self, *, identifier: str | None, name: str | None) -> SourceTier | None:
        """The configured tier for this source, or ``None`` if unrecognised.

        ``None`` is a real answer and is never collapsed into ``TIER_4``: "the
        policy does not list this source" and "the policy considers this source
        general web" are different statements.
        """
        haystacks = [value.lower() for value in (identifier, name) if value]
        if not haystacks:
            return None
        for entry in self._entries:
            for haystack in haystacks:
                if entry.pattern in haystack or (entry.stem and entry.stem in haystack):
                    return entry.tier
        return None

    def resolve(
        self, *, declared: SourceTier, identifier: str | None, name: str | None
    ) -> SourceTier:
        """The tier to record for a source the data layer already tiered.

        The configured policy wins where it recognises the source; otherwise
        the declared tier stands. A provider cannot promote itself by declaring
        a tier the policy disagrees with, and an unlisted source is not demoted
        by a judgement nobody made.
        """
        configured = self.classify(identifier=identifier, name=name)
        return configured if configured is not None else declared

    def snapshot(self) -> ResearchSourcePolicySnapshot:
        """Echo the policy in force into the stored artifact."""
        return ResearchSourcePolicySnapshot(
            config_version=self._config.config_version,
            require_source_attribution=self._config.require_source_attribution,
            min_sources_per_report=self._config.min_sources_per_report,
            tier_1_count=len(self._config.tier_1),
            tier_2_count=len(self._config.tier_2),
            tier_3_count=len(self._config.tier_3),
            tier_4_count=len(self._config.tier_4),
        )


def _stem(pattern: str) -> str:
    """The label a domain-style pattern is known by, e.g. ``reuters``.

    Returns an empty string for multi-word entries such as ``investor
    relations`` or ``general web``, which are already prose and must be matched
    whole rather than by a leading fragment.
    """
    if " " in pattern:
        return ""
    label = pattern.split(".", 1)[0]
    return label if len(label) >= 4 else ""
