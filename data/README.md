# Data directory

Working storage for market data, research inputs and reproducibility artifacts.

Directory *structure* is tracked in Git; directory *contents* are not (see
[.gitignore](../.gitignore)). Nothing here is source code, and nothing here may
contain credentials.

## Layout

### `raw/`

Original provider responses, stored verbatim as received. Never edited, never
normalised in place. If a provider changes its response shape, the raw record is
what proves what was actually returned at the time.

### `normalized/`

The canonical internal representation, derived from `raw/`. Regenerating
`normalized/` from `raw/` must be deterministic — that is what makes a
normalisation bug fixable after the fact.

### `cache/`

Temporary, reusable data that can be regenerated at any time and can be deleted
without loss. Nothing here is authoritative.

### `historical/`

Long-lived historical datasets accumulated over the life of the project:
underlying prices, option chains, IV, Greeks, volume and open interest.

The project deliberately does not require an expensive multi-year options
dataset up front. It starts with free sources and grows this directory over
time.

### `snapshots/`

Immutable point-in-time captures used for research, decisions, backtesting and
reproducibility. A snapshot is written once and never modified.

**Never mix `cache/` and `snapshots/`.** Cache is disposable; a snapshot is
evidence. If a snapshot can be silently regenerated with different content, no
past decision can be audited.

## Required metadata

Every persisted snapshot carries:

| Field                | Meaning                                                  |
| -------------------- | -------------------------------------------------------- |
| `as_of_timestamp`    | The instant the data describes                            |
| `source_timestamp`   | The timestamp the provider itself attached                |
| `retrieved_timestamp`| When this system actually fetched it                      |
| `source_id`          | Which provider/source produced it                         |
| `data_quality`       | `OK` / `DEGRADED` / `STALE` / `UNUSABLE`                   |

All timestamps are timezone-aware and stored in UTC.

## Look-ahead bias

Historical analysis may only use information that was available at the
historical timestamp being evaluated. The three separate timestamps above exist
precisely so that "what did we know, and when did we know it" is answerable —
`retrieved_timestamp` after the evaluated instant means the record must be
excluded from that evaluation.
