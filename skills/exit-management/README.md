# Working on exit management and the position lifecycle (Milestone 10)

Development guidance for the milestone that closes a position. Read
[CLAUDE.md](../../CLAUDE.md) first; this is the detail behind the summary there.

Milestone 10 introduces **no agent**, so there is deliberately no
`.claude/agents/` entry for it — exactly as in Milestones 7, 8 and 9. Whether to
sell an option is a safety decision, and a deterministic engine that can be
replayed is worth more than a persuasive one that cannot.

## The shape of it

```
Milestone 9 position reality       what the broker actually holds
      |
open strategy positions            from CONFIRMED FILLS only
      |
stored point-in-time quotes        repository only; no live request
      |
ExitPolicyEngine                   pure, deterministic, no model
      |
WAIT / EXIT / BLOCK                immutable evaluation + decision
      |
ExitRequest -> Milestone 8         the ONLY path to an exit order
      |
Milestone 9 reconciliation         what actually happened
      |
CLOSED / STILL OPEN / UNKNOWN
```

Three milestones, three questions, and collapsing any two is the failure this
package is shaped to prevent:

| milestone | answers |
|---|---|
| **M10** | should this existing position be closed? |
| **M8** | how do we send the exit order? |
| **M9** | what actually happened at the broker? |

## The one rule in the engine

> The **first** policy in precedence order that does not say `WAIT` decides.

That is the whole combination rule, and two tempting alternatives are both
wrong.

**A later block does not veto an earlier exit.** A position one day from expiry
whose research report cannot be read still force-exits. The thesis is a
secondary signal the expiration policy does not depend on, and letting a missing
file suppress a force-exit would mean the most important policy in the milestone
could be disabled by deleting something unrelated to it.

**An earlier block still beats a later exit, and it is the same rule.** A
position at its take-profit whose quantity the broker disputes blocks rather
than sells, because `POSITION_CONSISTENCY` is first and `TAKE_PROFIT` is ninth —
the profit figure was computed from a quantity nobody confirmed. Nothing special
is needed for this; it falls out of the ordering.

The precedence, printed by `exit validate` and asserted by a test:

```
1  POSITION_CONSISTENCY   does this exist, and do we and the broker agree?
2  BROKER_OBSERVATION     was the broker actually read?
3  EXECUTION_STATE        is an exit already working, or unresolved?
4  CONTRACT_VALIDITY      do we hold what an exit order needs?
5  EXPIRATION             how long is left, on the exchange's calendar?
6  DATA_QUALITY           is the price everything else rests on usable?
7  MAX_LOSS               has it lost more of its declared maximum than policy permits?
8  THESIS                 has the research thesis been invalidated?
9  TAKE_PROFIT            has it made enough?
10 TRAILING_STOP          has it given back enough of its peak?
```

Two outcomes **short-circuit** it, before the later policies are consulted at
all. Both are `WAIT` reasons, and both mean *there is nothing here to decide*:

- `POSITION_CLOSED` — the broker holds none of the structure. Its return, its
  remaining time and its maximum loss are all arithmetic over zero, and the
  money policies would report an unavailable risk basis and block a position
  that is simply gone.
- `EXIT_ALREADY_SUBMITTED` — an exit order is working. Continuing would evaluate
  the take-profit against a position that is being sold and record an `EXIT`
  verdict for it.

## The distinctions everything rests on

Each looks like one thing and is two, and every one has a test that fails
loudly:

| looks the same | is not |
|---|---|
| `UNKNOWN` exit | `FAILED` exit — one may be live right now |
| a failed broker read | an empty account |
| "no bid quoted" | "the bid is zero" |
| a partly-held straddle | a closed position |
| a prose invalidation condition | a checkable one |
| an acknowledged exit | a closed position |
| a blocked position | a position that may not be force-exited |

That last row is the subtle one. A block is **re-derived on every evaluation**
from the conditions that caused it; it is not a memory. `BLOCKED` is deliberately
*not* in `EXIT_SUBMISSION_BLOCKED_STATES`, so a position blocked once — because a
research file was unreadable, say — can still be force-exited at its expiration
deadline. What must never be retried is a *submission whose outcome is unknown*,
and `EXIT_SUBMITTED` / `EXIT_UNKNOWN` are what express that.

## What to do when you…

### …add an exit policy

1. Add a member to `ExitPolicyKind` **and** to `EXIT_POLICY_PRECEDENCE`. A test
   asserts the two agree, and the position you choose in the list is a safety
   decision: everything above it can veto it, and it can veto everything below.
2. Add its reason codes to `ExitReasonCode`, and classify each into
   `EXIT_WAIT_REASONS` or `EXIT_TRIGGER_REASONS`. Anything unclassified becomes
   a block, which is safe but invisible — `tests/exit/test_models.py` asserts
   the three sets partition the enum.
3. Write the policy as a **pure function** in `exit/policies.py` (or its own
   module if it carries machinery, as expiration, trailing and thesis do). No
   clock, no broker, no repository. Return an `ExitPolicyOutcome` with
   `measured` and `threshold` filled in: an operator asking "how close was it"
   deserves both numbers.
4. Wire it into `ExitPolicyEngine._run_policies`. The dictionary is keyed by
   kind and read back in precedence order, so the executed order and the
   reviewable list cannot drift.
5. If it needs a new trigger reason, extend `_M1_EXIT_REASON` in
   `exit/models.py` — or add a member to the Milestone 1 `ExitReason` and to
   `schemas/exit_decision.json`, as `TAKE_PROFIT` did. Forcing a new trigger
   onto an existing member would record every take-profit as a risk breach.

### …add a configuration value

Global limits go in `config/exit.yaml`; per-strategy narrowing goes in
`config/strategies/*.yaml` under `exit_policy`. Then:

1. Add the field to the right `_ConfigModel` in `infrastructure/settings.py`.
   They are `extra="forbid"`, so a typo fails loudly.
2. If it is a **safety** value, add a narrowing check to
   `SystemConfig._strategies_never_widen_exit_limits` — and be explicit about
   which direction is narrower. Getting it backwards silently enforces the
   inverse of the intended property, so state the reason in the message and
   test **both** directions.
3. Add it to `effective_policy` in `exit/validation.py` and to the `scopes` map,
   so a stored decision records which layer supplied the value. A reader must
   never have to infer that an absent key means "global".
4. Add a row to `configuration_report`, so `exit validate` prints it.

Widening is a **load failure**, never a clamp. A clamped limit is a limit nobody
can see, and here that would mean nobody could see how long a position is
actually allowed to be held.

### …change what an exit is valued at

`exit.data_quality.quote_field` names it, and `BID` is the shipped answer
because closing a long option is a **sale**: the bid is the price a seller can
actually get. `MID` is a fair value nobody is obliged to trade at, and `LAST` is
a print that may be hours old and on the wrong side of the spread.

There is deliberately no fallback. A missing bid is `QUOTE_FIELD_UNAVAILABLE`,
and `allow_quote_field_substitution: true` fails to load — substituting the ask,
the last print or the price we paid would value the position at something no
seller could get, and the trailing level, the take-profit and the maximum-loss
figure would all inherit the invention.

### …add a lifecycle state

Add it to `PositionLifecycleState`, give it edges in
`ALLOWED_LIFECYCLE_TRANSITIONS`, and ask two questions about every edge:

- Does it permit a **second submission**? If so it must not exist. There is no
  edge from `EXIT_UNKNOWN` to anything that sends.
- Does it leave `CLOSED`? Nothing may. If the broker later reports contracts
  under those ids, that is a new position or a reconciliation finding, and
  either is better than a record that silently reopened.

### …touch the trailing stop

The invariant is that the level **never falls**. It is enforced three times, and
all three are deliberate: the model validator refuses a level above its peak,
`observe` has no branch that lowers one, and
`exit.trailing.allow_level_to_fall: true` fails to load. A stop that followed a
position down would fail *silently* — no error, no log line, just a position
that never exits.

State is persisted and reloaded on every evaluation rather than held in memory,
which is what makes the restart guarantee real. Anything you add to
`TrailingStopRecord` that changes on an unremarkable observation will break
idempotency: a re-run over unchanged state must produce a byte-identical
artifact, so `observe` returns the record **unchanged** when nothing moved.

## What this milestone deliberately does not do

- **It does not schedule anything.** `ExitService.monitor` is a callable
  operation that is safe to run repeatedly; nothing calls it on a cadence.
  `config/schedules.yaml` still describes jobs no process executes.
- **It does not return `WEAKENING`.** Deciding a thesis has weakened without
  being falsified is a judgement, and this engine makes none. The
  specification's separate Thesis Monitor is a later milestone.
- **It does not interpret prose.** An invalidation condition that cannot be
  checked against a structured fact is `NOT_EVALUATED`, and
  `allow_prose_interpretation: true` fails to load. Only three things are
  checkable today: a research horizon that has closed, a dated catalyst that has
  passed, and a decisive move against a stated direction. That is a
  deliberately small set — a thesis monitor that claimed to evaluate ten
  conditions and pattern-matched nine would be worse than one that says it
  evaluated one.
- **It does not compute profit and loss.** An exit's proceeds never move a
  reservation: `CLOSE` executions are excluded from reservation accounting
  entirely, because returning capital to a campaign needs a realised figure
  and that is Milestone 11's.
- **It does not adopt, hedge or complete anything.** A partly-held structure
  blocks and is reported. Nothing here completes a straddle.
- **It does not model assignment or exercise.** This system holds long options,
  which are never assigned. An expiration that has already passed blocks: an
  expired option cannot be sold, and an order for one is not an exit.

## Further reading

- [trailing_stop.md](trailing_stop.md) — the state machine and its invariant
- [expiration.md](expiration.md) — why the date has to be the exchange's
- [thesis_invalidation.md](thesis_invalidation.md) — what is checked, and what is refused
