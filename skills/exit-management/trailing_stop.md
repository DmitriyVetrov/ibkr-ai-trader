# The trailing stop

A four-state machine, not a mutable price. Implemented in
`src/trading_system/exit/trailing.py`; every function there is pure, so a level
can be *reproduced* from stored artifacts long after the exit it caused.

```
INACTIVE   below the activation threshold; no level exists
   |  gain over entry cost reaches activation_return_pct
ARMED      first level set, from the observation that armed it
   |  carried across evaluations
ACTIVE     level ratchets upward with the peak, never downward
   |  observed price <= level
TRIGGERED  terminal; the crossing observation is recorded
```

## The invariant

```
favourable price rises   ->  peak rises, level may rise
favourable price falls   ->  peak unchanged, level UNCHANGED
```

A level that followed the position down would not be a stop. It would guarantee
the position is never sold, however much of its peak it had given back, and it
would fail **silently** — no error, no log line, just a position that never
exits.

Enforced three times, deliberately:

1. `TrailingStopRecord` refuses a level above its peak.
2. `observe` has no branch that lowers a peak or a level. The monotonicity is
   structural rather than checked.
3. `exit.trailing.allow_level_to_fall: true` fails to load.

`tests/exit/test_trailing.py` walks a price series and asserts the recorded
levels come back sorted.

## Why it is a record and not a number

"The trailing level is 2.10" answers none of the questions an operator asks
after an exit. The record answers all of them:

| field | the question it answers |
|---|---|
| `activated_at`, `activation_quote` | when did this arm, and at what price? |
| `peak_quote`, `peak_at` | how good did it get? |
| `stop_quote`, `level_updated_at` | where is the line, and when did it last move? |
| `trigger_quote`, `triggered_at` | what observation crossed it? |
| `state` | is it armed, running, or finished? |

`trigger_quote` is required on a `TRIGGERED` record by a model validator,
because it is the whole explanation of the exit and the moment it stops being
reconstructible is the moment it is not written down.

## Units

Everything is in the broker's **quoted** terms — 6.05, not 605.00. That is what
a market observation and a limit price are in, and doing the multiplier
conversion on every comparison would be a factor of 100 waiting to be forgotten.

Percentages are unaffected either way: both sides of every ratio carry the same
multiplier, so the activation threshold and the trail distance mean the same
thing in either unit.

Percentages reach `Decimal` through `str`, always. `Decimal(0.3)` is
0.299999999999999988897769…, and a trail distance that is not the number in the
configuration file is one nobody can reconcile against the file.

## Restart safety

The state is written to `data/exit/trailing/<position_id>.json` after every
observation that moved it, and read back on every evaluation. Nothing lives in
process memory.

The failure this prevents is specific: a monitor that kept the peak in memory
would, after a restart, re-arm from wherever the price happens to be. A position
that ran from 6.00 to 12.00 and fell back to 9.00 would restart its trail at
9.00 and set a level at 6.30 — giving back everything the original level was
there to protect, with nothing in any log to say it had happened.

`tests/exit/test_trailing.py::test_a_restart_does_not_restart_the_trail_from_the_current_price`
asserts exactly that, by round-tripping through the repository rather than by
copying the object.

## The one deliberately mutable record

Every other artifact in this milestone is immutable. The trailing record is
overwritten in place, and the exception is worth stating: a trailing stop is one
continuously-updated fact about a position, and an immutable file per
observation would produce thousands of near-identical records for a level that
moved three times.

What *is* immutable is the history. Every movement — armed, level raised,
triggered — is an appended `PositionLifecycleEvent` carrying the peak, the level
and the observation, so the explanation of an exit survives even though the
state itself is a current value.

## Idempotency

An observation that moves nothing returns the record **unchanged** — no clock
stamp, no counter, no detail rewrite. That is not tidiness: a re-run over
unchanged state must produce a byte-identical evaluation artifact, and a
trailing record that ticked a counter on every look would break it.

`observations` therefore counts observations that *moved* the trail, and that we
looked at all is recorded in the lifecycle history, where it belongs.

## Configuration

Global envelope in `config/exit.yaml`:

```yaml
trailing:
  enabled: true
  activation_return_pct: 25.0     # gain over entry cost that arms the trail
  trail_distance_pct: 40.0        # the WIDEST any strategy may use
  min_improvement_pct: 1.0        # how far the peak must improve to move the level
  allow_level_to_fall: false      # true fails to load
```

Per-strategy narrowing in `config/strategies/*.yaml`:

```yaml
exit_policy:
  trailing_stop_pct: 30.0         # must be <= the global trail_distance_pct
```

**A smaller distance is narrower**: it gives back less of the peak. A larger one
widens a global safety boundary and fails to load, naming the strategy and the
limit.
