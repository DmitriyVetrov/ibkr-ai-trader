"""Fixtures shared by the integration suites.

Deliberately thin. Almost every file here builds its own wiring, because an
integration test that inherited a service from somewhere else would be testing
a composition nobody in the system actually performs.

The exception is the orphan-cleanup wiring: ``tests/integration/test_orphan_cleanup.py``
drives exactly the same service graph as ``tests/cleanup``, and a second copy of
it would be a second thing to keep in step with the service. Registering the
fixtures here rather than importing them into the test module keeps the names
out of that module, where they would shadow the parameters that request them.
"""

from __future__ import annotations

from tests.cleanup.conftest import (  # noqa: F401  (registered as fixtures, used by name)
    cleanup_disabled_config,
    cleanup_enabled_config,
    clock,
    make_service,
    settings,
)
