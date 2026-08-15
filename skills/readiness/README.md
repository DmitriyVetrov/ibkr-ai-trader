# Readiness and the acceptance gate (Milestone 12)

> **Readiness reports. It never enables.**

This package answers one question — *is this system safe and operationally
complete enough to proceed to the next trading mode?* — and answers it with
evidence rather than with booleans.

```
COLLECTORS  (impure: git, toolchain, config, stores, broker, HTTP probes)
      |
      v
EvidenceBundle          frozen, captured, immutable
      |
      v
evaluate()              PURE: no broker, no LLM, no Docker, no socket, no clock
      |
      v
ReadinessAssessment     one criterion at a time, each with its evidence
      |
      v
immutable run under data/readiness/
```

## What to do when …

### … adding a criterion

1. Add a member to `ReadinessCriterionId` in `domain/enums.py`. It is persisted
   into immutable assessments and enumerated in
   `schemas/readiness_criterion.json`, so renaming one later is a breaking
   change to a stored audit record.
2. Add a `CriterionDefinition` to `READINESS_CRITERIA` in `criteria.py`, naming
   its evidence slot and its freshness rule.
3. Write the predicate so that **an empty payload cannot pass**. A missing key
   is `UNKNOWN`; `dict.get(key, False)` turns silence into a defect, and
   `dict.get(key, True)` turns silence into a certification.
4. Decide whether it blocks paper, live review, or neither, in
   `config/readiness.yaml`. A criterion in neither list is advisory: still
   evaluated, still recorded, still shown, and unable to hold a gate shut.
5. `tests/readiness/test_criteria.py` sweeps every criterion for the two
   dangerous behaviours automatically. Add a case for the judgement itself.

### … adding a collector

Collectors live in `collectors.py` (or `observability_probe.py` for anything
that speaks HTTP). Three rules:

* **Never raise into the caller.** A run that crashed on its fourth probe would
  report nothing about the three that succeeded. Record the failure as an
  evidence record with `collected=False` and let the predicate judge it.
* **Never build a writable broker.** `build_broker` is read-only whatever the
  settings say; `build_execution_broker` has exactly one caller in this
  repository and it is not here.
* **Take the project root, not the data root.** Every service in this system
  resolves `data.storage.root` beneath the root it is given. Handing one the
  already-resolved data root nests the tree a second time and fails *silently*.

### … changing a freshness window

`config/readiness.yaml`. Two mechanisms, and they are not interchangeable:

* `freshness.revision_bound` — evidence that expires with the **code**. A test
  result belongs to the commit it ran against; three days old at an unchanged
  revision is perfectly good evidence, and one second old at a different
  revision is not evidence about this revision at all.
* `freshness.windows.*` — evidence that expires with the **clock**. A broker
  probe, a health report, a reconciliation, a daily figure.

### … relaxing a gate

Don't, without also changing what the gate *claims*. The failure mode of this
whole package is a report that looks exactly like a real assessment and
certifies nothing.

## Things this milestone deliberately does not do

* **It does not define `READY_FOR_LIVE`.** The strongest conclusion available
  is `READY_FOR_LIVE_REVIEW`, which is a request for a person to look. A level
  with the other name would eventually be read as the authorisation itself.
* **It does not enable anything.** `TRADING_MODE`, `LIVE_TRADING_CONFIRMED`,
  `LIVE_READINESS_CHECKLIST_SIGNED_OFF`, `execution.enabled` and
  `IBKR_READ_ONLY` are untouched, and `tests/readiness/test_boundaries.py`
  walks the transitive import graph to prove there is no path.
* **It does not submit orders.** Not even `readiness paper`, which checks the
  four authorisation gates and then points at
  `tests/integration/test_paper_execution.py` — the one audited order path,
  which runs through `execution/service.py`, the only caller of
  `build_execution_broker`. The first shape of the paper gate submitted its own
  order and thereby became a second caller; that broke a Milestone 8 invariant
  two boundary suites assert, and brief section 2 forbids weakening an existing
  gate.
* **It does not infer a signer.** `$USER` is whoever ran the process and a git
  `user.name` is a string anybody can set. An inferred signer is worse than
  none, because it looks like accountability.
* **It does not adopt, repair or clean up anything.** An orphan broker position
  is reported and left exactly where it is.

## Two Milestone 11 defects this milestone found

Both were found by the acceptance gate asking a log backend for a specific
trace id and getting nothing back — which is the whole argument for probing a
running system rather than reading its configuration.

1. **No OTLP log exporter existed.** `deploy/otel/collector.yaml` shipped a
   `logs` pipeline wired to Loki and `observability/otel.py` built only a
   tracer and a meter. The pipeline was correct and permanently unfed.
2. **Trace correlation never reached module-level loggers.** `get_logger`
   returned an eagerly-bound structlog logger, and `BoundLoggerLazyProxy.bind()`
   snapshots the processor chain as it stands at *import* time. The correlation
   processor is installed at *start-up*, after imports — so every
   `_logger = get_logger(__name__)` emitted lines with no `trace_id`. The lines
   still appeared and still looked right, which is exactly why nobody noticed.

`tests/observability/test_log_export.py` pins both.

## One environment finding worth keeping

The shipped `ib-gateway` image trusts only `127.0.0.1` for API access
(`jts.ini`'s `TrustedIPs`). A connection from the **host** to the published port
arrives from the Docker bridge address, is accepted at the TCP level and then
never answered — indistinguishable from a hang, and it survives a container
restart. `docker-compose.yml` solves it for the runtime with
`network_mode: "service:ib-gateway"`. Running a readiness broker probe from the
host against a containerised gateway will always time out; run it in the
gateway's network namespace.
