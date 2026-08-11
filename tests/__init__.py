"""Test suites.

A package so that every suite's ``conftest`` has a unique module path — two
top-level ``conftest`` modules are ambiguous to mypy — and so the ``tests.*``
mypy override applies to all of them.
"""
