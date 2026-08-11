# Data directory

Working storage for market data, research inputs and reproducibility artifacts.

Directory *structure* is tracked in Git; directory *contents* are not (see
[.gitignore](../.gitignore)). Nothing here is source code, and nothing here may
contain credentials.

Three distinctions do most of the work, and confusing any of them corrupts the
historical record:

> **`raw` is not `normalized`.** Raw is what the provider said. Normalized is
> what we made of it.
>
> **`cache` is not `historical`.** Cache is disposable. History is evidence.
>
> **`latest` is not point-in-time truth.** The newest value is not what the
> system knew last Tuesday.

## Layout

### `raw/`

Original provider responses, stored as received. Written once, never edited,
never normalised in place. If a provider returns something malformed or
implausible, this is what proves it did.

```
raw/<DATA_TYPE>/<KEY>/<retrieved>-<hash>.json
```

Broker responses are stored as the *adapter's* snapshot rather than the wire
object: `ib_async` types are confined to `broker/ibkr/` by design. Nothing has
been repaired or filled in at that point — only translated.

### `normalized/`

The canonical internal representation, derived from `raw/`. Regenerating
`normalized/` from `raw/` is deterministic, which is what makes a normalisation
bug fixable after the fact.

```
normalized/<DATA_TYPE>/<KEY>/<as_of>-<hash>.json
```

### `cache/`

Temporary, reusable data that can be regenerated at any time and deleted
without loss. Nothing here is authoritative.

Anything served from the cache is **relabelled `CACHED`** before it is handed
on. A quote that was realtime when it was stored is not realtime when it is
replayed, and the origin has to say so. An entry past its TTL is a miss, not a
stale hit: during a provider outage the cache returns nothing rather than
yesterday's price wearing today's timestamp.

### `historical/`

The append-only ledger, one file per data type and key, plus the collectors'
bookkeeping.

```
historical/<DATA_TYPE>/<KEY>.jsonl
historical/_collection_state/<PROVIDER>/<DATA_TYPE>/<KEY>.json
```

Each ledger line records one event:

| Event                 | Meaning                                                     |
| --------------------- | ----------------------------------------------------------- |
| `SNAPSHOT_CREATED`    | New content was stored                                       |
| `SNAPSHOT_REOBSERVED` | The provider returned data identical to the newest snapshot  |
| `COLLECTION_FAILED`   | An attempt produced no usable data                           |

The ledger is appended to and never rewritten, so a failed collection cannot
erase the evidence of an earlier successful one. It doubles as the index for
point-in-time queries.

`_collection_state/` is mutable bookkeeping — last attempt, last success, last
error, counts — deliberately *not* the historical record. Its job is to let
tomorrow's collector find yesterday's gap.

### `snapshots/`

Immutable point-in-time captures used for research, decisions, backtesting and
reproducibility. Written once and never modified.

```
snapshots/<DATA_TYPE>/<KEY>/<as_of>-<snapshot_id>.json
```

A snapshot's id is *derived*, not generated: data type, key, provider, schema
version, `as_of` and a hash of the payload. The same response collected twice
produces the same id, which is what makes deduplication and integrity checking
possible. Reading a snapshot back re-verifies its hash; a file that changed on
disk raises rather than being returned.

**Never mix `cache/` and `snapshots/`.** Cache is disposable; a snapshot is
evidence. If a snapshot can be silently regenerated with different content, no
past decision can be audited.

### `universe/`

Universe-selection runs (Milestone 4). Stored here rather than under
`snapshots/` because they are not provider data: a run is a *decision record*
built from provider data, and mixing the two would blur what the data layer
vouches for.

```
universe/runs/<generated>-<run_id>.json
universe/history.jsonl
```

The same discipline as `snapshots/` and `historical/`, for the same reasons. A
run file is written once; writing different content to an existing id raises
rather than overwriting. `history.jsonl` is appended to and never rewritten, so
a later failed run cannot erase the evidence of an earlier successful one.

Each run records what was considered, which assets survived the deterministic
pre-filter, how they were ranked, which model ranked them (provider, model,
prompt version and the prompt's own fingerprint), the exact model response, and
every data snapshot the decisions rest on. Selected assets carry provenance;
rejected assets carry a machine-readable reason. There is no third category.

The `run_id` is derived from the instant, the configuration and the snapshots
consumed — so re-running over unchanged inputs is idempotent, while a run over
newly collected data at the same instant is correctly a different run.

Read one with the CLI rather than by hand:

```bash
python -m trading_system.cli universe history
python -m trading_system.cli universe show
python -m trading_system.cli universe explain --run-id <ID> --symbol SPY
```

## Required metadata

Every snapshot carries:

| Field                 | Meaning                                                   |
| --------------------- | --------------------------------------------------------- |
| `snapshot_id`         | Deterministic identifier                                   |
| `as_of`               | The instant the data describes                             |
| `retrieved_at`        | When *this system* actually fetched it                     |
| `source_timestamp`    | The timestamp the provider itself attached                 |
| `provider`            | Who actually produced it                                   |
| `source_tier`         | `TIER_1` … `TIER_4` trust ranking                          |
| `data_origin`         | `BROKER_REALTIME` / `BROKER_DELAYED` / `PROVIDER_*` / `HISTORICAL` / `CACHED` / `SIMULATED` / `UNAVAILABLE` |
| `schema_version`      | Version of the canonical record shapes                     |
| `application_version` | Version of the code that wrote it                          |
| `config_version`      | Version of the configuration in force                      |
| `data_quality`        | The full eight-dimension quality report                    |
| `payload_hash`        | Deterministic hash of the records                          |

All timestamps are timezone-aware and stored in UTC.

## Data quality

Quality is **not** a single boolean. Eight dimensions fail independently:

```
transport_valid   schema_valid       source_valid       timestamp_valid
freshness_valid   completeness_valid plausibility_valid consistency_valid
```

plus a derived `research_usable`, and a list of machine-readable `issues`.

A record can be transport-valid, schema-valid and source-valid while still
being economically impossible — and real IBKR paper validation produced exactly
that for an SPY volume field. In that case the value is **preserved verbatim**,
`plausibility_valid` goes false, `SUSPICIOUS_VOLUME` is recorded, and
`research_usable` goes false.

Nothing is ever corrected, smoothed, swapped or dropped. A crossed quote
(`bid > ask`) is flagged, not un-crossed. A zero price is flagged, not nulled.
Future consumers filter on `research_usable`; auditors read the raw value.

Thresholds live in [`config/data.yaml`](../config/data.yaml), never in code.

## Look-ahead bias

Historical analysis may only use information that was available at the instant
being evaluated. A record is visible at time T only if **every** clock it
carries has passed T — and above all only if we had already retrieved it:

```
retrieved_at <= T   and   as_of <= T
and (source_timestamp is None or source_timestamp <= T)
and (published_at     is None or published_at     <= T)
and (effective_at     is None or effective_at     <= T)
```

Retrieval binds. A filing published in 2019 that we first downloaded this
morning is invisible to any reconstruction of last week.

Future-dated *content* is fine and expected — an earnings date next month is
what a calendar is for. What is forbidden is the record being visible before it
was known, which is why corporate events gate on `announced_at` rather than on
`event_time`.

## Inspecting a snapshot

```bash
# What is stored, and what state it is in
python -m trading_system.cli data status

# The latest snapshot's provenance and version stamps
python -m trading_system.cli data snapshot --symbol SPY
python -m trading_system.cli data snapshot --symbol SPY --type OPTION_CHAIN

# What the system knew at a past instant
python -m trading_system.cli data snapshot --symbol SPY --as-of 2026-08-10T14:30:00+00:00

# The full quality verdict
python -m trading_system.cli data quality --symbol SPY

# The append-only ledger
python -m trading_system.cli data history --symbol SPY
```

Raw JSON is readable directly — every artifact is indented and key-sorted:

```bash
jq '.records[0] | {provider: .source.provider, origin: .source.origin, bid, ask}' \
  data/snapshots/MARKET_QUOTE/SPY/*.json | head

jq -r '.event + " " + .recorded_at' data/historical/MARKET_QUOTE/SPY.jsonl
```

## Accumulation

The project deliberately does not require an expensive multi-year options
dataset up front. It starts with free sources and grows this directory forward
over time. Coverage starting at zero is the expected initial condition, not a
fault — `data status` reports it as `NO_COVERAGE` rather than as a gap, and
nothing is ever backfilled silently.
