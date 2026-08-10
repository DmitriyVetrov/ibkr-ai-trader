# Test fixtures

Recorded provider responses, agent outputs and market snapshots used by the
test suite. Ordinary unit tests read from here rather than making network
calls.

Fixtures are checked against *structural constraints* — enum membership,
bounds, required fields, presence of sources and invalidation conditions —
never against exact prose. An LLM that phrases the same correct answer
differently must not fail the suite.
