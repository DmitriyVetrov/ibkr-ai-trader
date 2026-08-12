# Strategy specifications

One document per configured strategy. Each says what the strategy *is*, when it
may be used, what data it needs, and what it must never do.

**These are specifications, not autonomous traders, and not the source of
truth for any number.** Executable rules live in exactly two places:

| Where | What |
| --- | --- |
| `config/strategies/<name>.yaml` | policy: DTE window, target delta, offsets, liquidity floors, price bounds, exit policy |
| `src/trading_system/strategies/<name>.py` | structure: how many legs, which rights, which strike relationship |

A number written into one of these documents would be a second copy of a
decision, and second copies drift. Where a value matters, these documents name
the configuration key rather than the value — so a policy change shows up in a
diff of the file that governs it, and never leaves a stale figure behind in
prose.

The registry (`src/trading_system/strategies/registry.py`) resolves the two
sources together and refuses to build if a strategy's configuration widens a
limit in `config/risk.yaml` or describes a different structure from the one its
module defines.

## What every strategy shipped today has in common

- **Long premium only.** Every leg is bought. Nothing here sells an option,
  and no configuration can make it: the leg actions are asserted in code.
- **One position.** A multi-leg strategy is entered, monitored and exited as a
  unit. `allow_independent_leg_exit` is `false` in every shipped specification.
- **The AI never chooses the contract.** The strategy agent picks *which
  strategy*; the deterministic contract selector picks *which contract*. The
  agent is never shown a chain, and its response has no field for a strike, an
  expiration, a quantity or a price.
- **No sizing.** Nothing in this stage decides quantity or money. A selection
  ends at one unit of the structure and its estimated cost; the risk and
  allocation engines decide the rest.
