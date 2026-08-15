"""Live-trading readiness and the acceptance gate (Milestone 12).

The milestone that answers one question — *is this system safe and
operationally complete enough to proceed to the next trading mode?* — and
answers it with evidence rather than with booleans.

.. code-block:: text

    COLLECTORS  (impure: git, toolchain, config, stores, broker, HTTP probes)
          |
          v
    EvidenceBundle          frozen, captured, immutable
          |
          v
    evaluate()              PURE: no broker, no LLM, no docker, no socket
          |
          v
    ReadinessAssessment     one criterion at a time, each with its evidence
          |
          v
    immutable run under data/readiness/

Three properties hold, and each has tests that fail loudly:

* **Readiness reports; it never enables.** Nothing here can change
  ``TRADING_MODE``, ``LIVE_TRADING_CONFIRMED``,
  ``LIVE_READINESS_CHECKLIST_SIGNED_OFF``, ``execution.enabled`` or
  ``IBKR_READ_ONLY``. There is no ``readiness == true -> enable execution``
  path, and ``tests/readiness/test_boundaries.py`` walks the transitive import
  graph to prove the evaluator cannot reach one.
* **Every PASS has evidence.** A criterion with no evidence is ``NOT_TESTED``,
  never ``PASS``. Evidence outside its freshness window is ``STALE``. Evidence
  that contradicts the criterion is ``FAIL``. Evidence that does not settle the
  question is ``UNKNOWN``, and ``UNKNOWN`` never satisfies a blocking
  criterion.
* **There is no ``READY_FOR_LIVE``.** The final authorisation is a human
  control expressed through guards that already exist. The strongest thing this
  package can conclude is ``READY_FOR_LIVE_REVIEW``, which is a request for a
  person to look.

Imports are deferred through ``__getattr__`` for the same reason
``execution/__init__.py`` defers its service: ``collectors`` reaches a broker
(read-only, through the Milestone 9 positions service) and an eager re-export
would put that in the import graph of anything that merely names a readiness
type — including the evaluator, whose whole contract is that it cannot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "READINESS_CRITERIA",
    "CriterionDefinition",
    "EvidenceBundle",
    "EvidenceRecord",
    "LiveReadinessSignoff",
    "ReadinessAssessment",
    "ReadinessCriterion",
    "ReadinessRun",
    "ReadinessService",
    "assessment_identifier",
    "criterion",
    "evaluate",
    "evidence_identifier",
    "run_identifier",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.readiness.criteria import READINESS_CRITERIA, CriterionDefinition, criterion
    from trading_system.readiness.evaluator import evaluate
    from trading_system.readiness.evidence import (
        EvidenceBundle,
        EvidenceRecord,
        evidence_identifier,
    )
    from trading_system.readiness.models import (
        LiveReadinessSignoff,
        ReadinessAssessment,
        ReadinessCriterion,
        ReadinessRun,
        assessment_identifier,
        run_identifier,
    )
    from trading_system.readiness.service import ReadinessService


_LAZY: dict[str, str] = {
    "READINESS_CRITERIA": "trading_system.readiness.criteria",
    "CriterionDefinition": "trading_system.readiness.criteria",
    "criterion": "trading_system.readiness.criteria",
    "evaluate": "trading_system.readiness.evaluator",
    "EvidenceBundle": "trading_system.readiness.evidence",
    "EvidenceRecord": "trading_system.readiness.evidence",
    "evidence_identifier": "trading_system.readiness.evidence",
    "LiveReadinessSignoff": "trading_system.readiness.models",
    "ReadinessAssessment": "trading_system.readiness.models",
    "ReadinessCriterion": "trading_system.readiness.models",
    "ReadinessRun": "trading_system.readiness.models",
    "assessment_identifier": "trading_system.readiness.models",
    "run_identifier": "trading_system.readiness.models",
    "ReadinessService": "trading_system.readiness.service",
}


def __getattr__(name: str) -> Any:
    """Resolve the package's public names lazily.

    ``ReadinessService`` builds collectors, and one collector reaches a
    read-only broker. Re-exporting it eagerly would put a broker in the import
    graph of every module that merely names ``ReadinessAssessment`` — which
    includes the evaluator, whose boundary test forbids exactly that.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
