from __future__ import annotations

from typing import Any

from problem_locator.interfaces.mcp_server import _REQUESTS


LEGACY_COMPOSITE_INPUTS = {
    ("problem_locator_create_case", "problem_spec"),
    ("problem_locator_create_case", "initial_user_facts"),
    ("problem_locator_submit_supplement", "inputs"),
}
SCALAR_TYPES = {"boolean", "integer", "number", "string"}


def _is_nullable_scalar(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    if schema_type in SCALAR_TYPES:
        return "$ref" not in schema and "$defs" not in schema
    variants = schema.get("anyOf")
    if not isinstance(variants, list) or not variants:
        return False
    types = {variant.get("type") for variant in variants if isinstance(variant, dict)}
    return (
        "null" in types
        and types - {"null"} <= SCALAR_TYPES
        and all(
            isinstance(variant, dict)
            and "$ref" not in variant
            and "$defs" not in variant
            and "properties" not in variant
            and "items" not in variant
            and "additionalProperties" not in variant
            and variant.get("type") in SCALAR_TYPES | {"null"}
            for variant in variants
        )
    )


def _is_flat_property(schema: dict[str, Any]) -> bool:
    if "$ref" in schema or "$defs" in schema:
        return False
    if _is_nullable_scalar(schema):
        return True
    if schema.get("type") != "array":
        return False
    items = schema.get("items")
    return isinstance(items, dict) and _is_nullable_scalar(items)


def test_only_the_three_approved_historical_inputs_are_nonflat() -> None:
    detected: set[tuple[str, str]] = set()

    for tool_name, request_type in _REQUESTS.items():
        schema = request_type.model_json_schema(mode="validation")
        assert schema.get("type") == "object"
        properties = schema.get("properties")
        assert isinstance(properties, dict)
        for property_name, property_schema in properties.items():
            assert isinstance(property_schema, dict)
            path = (tool_name, property_name)
            if not _is_flat_property(property_schema):
                detected.add(path)

    assert detected == LEGACY_COMPOSITE_INPUTS


def test_legacy_allowlist_paths_still_have_the_expected_composite_shapes() -> None:
    schemas = {
        tool_name: request_type.model_json_schema(mode="validation")
        for tool_name, request_type in _REQUESTS.items()
    }

    create = schemas["problem_locator_create_case"]
    assert "$ref" in create["properties"]["problem_spec"]
    assert create["properties"]["initial_user_facts"]["type"] == "array"
    assert "$ref" in create["properties"]["initial_user_facts"]["items"]

    submit = schemas["problem_locator_submit_supplement"]
    inputs = submit["properties"]["inputs"]
    assert inputs["type"] == "object"
    assert inputs["additionalProperties"]["type"] == "string"
