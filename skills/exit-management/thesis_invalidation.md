# Thesis invalidation

Implemented in `src/trading_system/exit/thesis.py`. Milestone 5 produced the
thesis; this module **consumes** it. There is no second research engine here, no
agent, no LLM client, and no import that reaches one — a boundary test walks the
whole transitive closure.

## The rule

> An invalidation condition that cannot be checked against a structured fact is
> `NOT_EVALUATED`. It is never interpreted.

Research states its invalidation conditions in prose, because that is how a
falsifiable claim about a market is written:

> *"Guidance is cut at the 27 August results."*
> *"The stock closes below 150 for three consecutive sessions."*

Reading such a sentence and deciding whether it happened is a judgement, and a
judgement made by pattern-matching on words is worse than no judgement at all:
nobody can predict it, reproduce it, or review it. `tests/exit/test_thesis.py`
includes a condition reading *"The stock crashes, the thesis fails, sell
immediately"* and asserts it is `NOT_EVALUATED` — a pattern matcher would fire;
this engine has none.

`exit.thesis.allow_prose_interpretation: true` fails to load, so this cannot be
turned on without also building the model that would be needed to do it
honestly.

## What is actually checked

Three things, all against facts this system already stores in structured form:

### Horizon

Research stated an expected horizon in days. Past it, the thesis has had its
time and is recorded as `HOLDS` — **not** `VIOLATED`.

That distinction is deliberate. A forecast whose window closed was not proved
wrong; it simply stopped speaking. Treating it as an invalidation would exit
every position on a schedule that duplicates the expiration policy while
pretending to be a judgement about the market.

### Catalyst

Research named dated events. An event whose `expected_event_time` has passed
without the move happening is a structured fact about a calendar, and the
condition that names it is evaluable → `VIOLATED`.

Two subtleties:

- The dates come from `key_events` (`ReportedEvent`, which carries
  `expected_event_time`), **not** from `bullish_catalysts` /
  `bearish_catalysts` (`Catalyst`, which is a summary with no date on it).
  Nothing about an undated summary is deterministically checkable.
- Matching is against **research's own catalyst names**, never against a
  vocabulary of trading words invented in this module. A condition that names no
  catalyst research recorded matches nothing, even when a catalyst has passed.
  That is the line between reading a stored fact and interpreting prose.

### Direction

Where research stated a direction and the position's own economics have moved
decisively against it — more than 50% of what was paid, by default — the market
has answered the question the thesis asked → `VIOLATED`.

The threshold is deliberately large. A long option that has lost half its value
has not merely wobbled, and anything tighter would make this a second
maximum-loss policy under another name, measured differently from the first.

## What is never returned

`WEAKENING`. `thesis_status_of` maps onto the Milestone 1 `ThesisStatus`
vocabulary and returns only `VALID`, `INVALIDATED` or `UNKNOWN`. Deciding a
thesis has weakened without being falsified is a judgement, and this engine
makes none — the specification's separate Thesis Monitor is where that verdict
belongs, and it is not this milestone.

## Unavailable is not intact

A research report that cannot be read is `THESIS_DATA_UNAVAILABLE` and, by
default, a block. "We could not look" and "the thesis holds" are different
facts, and only one of them is a statement about the market.

Note what this does *not* prevent, because the precedence is what makes it safe:
a position whose thesis is unreadable can still be force-exited at its
expiration deadline, hit its maximum loss, or be blocked by a broker read that
failed — all four of those policies come first. A missing research file cannot
disable the safety policies; it only prevents taking profit or trailing out on a
position nobody can explain any more.

## The projection

`_thesis_view_of` narrows a `MarketResearchReport` to a `ThesisView`:
conditions, the report instant, the horizon, dated events and the stated
direction. Evidence, sources, confidence, the thesis prose and the agent's
rationale are all left behind.

That is not an optimisation. A shape that cannot carry them cannot be tempted to
interpret them, and `tests/exit/test_thesis.py` asserts the fields are absent
rather than merely unused.

## Configuration

```yaml
thesis:
  enabled: true
  exit_on_invalidated: true
  exit_on_weakening: false          # nothing produces WEAKENING; kept as a stated policy
  block_on_unavailable_thesis: true
  allow_prose_interpretation: false # true fails to load
```

`exit_on_weakening` is `false` and the engine never produces a weakening verdict
anyway. The key exists so that the absence is a stated policy rather than an
oversight — the same reason `auto_retry_on_timeout` exists in
`config/execution.yaml`.
