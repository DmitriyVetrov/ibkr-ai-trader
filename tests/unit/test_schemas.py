"""The workflow-boundary schemas exist, load, and are themselves valid."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

#: The workflow boundaries the specification requires a schema for
#: (specification section 25). Declared here rather than derived from the
#: directory listing: a test that reads the answer off disk proves nothing.
REQUIRED_SCHEMAS = (
    "universe_selection",
    "research_report",
    "strategy_decision",
    "purchase_card",
    "allocation_decision",
    "risk_decision",
    "order_intent",
    "execution_result",
    "position_snapshot",
    "exit_decision",
    "trade_snapshot",
)


@pytest.mark.unit
def test_schema_directory_exists(schemas_dir: Path) -> None:
    assert schemas_dir.is_dir()


@pytest.mark.unit
def test_exactly_the_required_schemas_are_present(schemas_dir: Path) -> None:
    found = {path.stem for path in schemas_dir.glob("*.json")}
    assert found == set(REQUIRED_SCHEMAS)


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_valid_json(name: str, schemas_dir: Path) -> None:
    payload = json.loads((schemas_dir / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_a_valid_json_schema(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """An invalid schema would silently accept everything it was meant to reject."""
    Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_is_self_describing(name: str, load_schema: Callable[[str], dict[str, Any]]) -> None:
    schema = load_schema(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(f"{name}.json")
    assert schema["title"]
    assert schema["description"]


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_forbids_unexpected_top_level_fields(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    """Extra fields from an agent are a contract violation, not a bonus."""
    assert load_schema(name)["additionalProperties"] is False


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_schema_declares_required_fields(
    name: str, load_schema: Callable[[str], dict[str, Any]]
) -> None:
    assert load_schema(name)["required"]


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_SCHEMAS)
def test_internal_refs_resolve(name: str, load_schema: Callable[[str], dict[str, Any]]) -> None:
    """Every $ref points at a definition that exists in the same file."""
    schema = load_schema(name)
    defs = set(schema.get("$defs", {}))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                assert ref.startswith("#/$defs/"), f"{name}: unsupported $ref {ref}"
                assert ref.removeprefix("#/$defs/") in defs, f"{name}: dangling {ref}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)


@pytest.mark.unit
def test_money_is_never_a_json_number(schemas_dir: Path) -> None:
    """Monetary values serialise as strings so exact decimals survive JSON."""
    for path in schemas_dir.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        for def_name in ("money", "nullable_money"):
            definition = schema.get("$defs", {}).get(def_name)
            if definition is None:
                continue
            types = definition["type"]
            types = [types] if isinstance(types, str) else types
            assert "number" not in types, f"{path.name}: {def_name} allows a JSON number"
            assert "string" in types
