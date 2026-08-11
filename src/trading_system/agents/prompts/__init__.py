"""Agent prompts, shipped with the package.

Prompts live here rather than in ``.claude/`` because they are part of the
*trading runtime*, not the development environment: the container image
installs the package and has no repository checkout to read from. The Claude
Code subagent definition under ``.claude/agents/`` mirrors the same boundaries
for development use; a test asserts the two cannot drift on the safety-critical
statements.

Every prompt is fingerprinted on load. The declared ``prompt_version`` in
configuration says what an operator *intended*; the fingerprint says what the
file actually contained. A prompt edited without a version bump is exactly the
change that would otherwise leave no trace in a stored trade record.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files

__all__ = ["load_prompt", "prompt_fingerprint"]


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """Read a prompt shipped with the package.

    Raises :class:`FileNotFoundError` rather than returning a default: an agent
    running on a silently empty prompt would still produce confident-looking
    output, with none of the boundaries the prompt exists to state.
    """
    resource = files("trading_system.agents.prompts").joinpath(f"{name}.md")
    if not resource.is_file():
        raise FileNotFoundError(f"agent prompt not found: {name}.md")
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=8)
def prompt_fingerprint(name: str) -> str:
    """SHA-256 of a prompt's exact bytes, truncated for readability."""
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:32]
