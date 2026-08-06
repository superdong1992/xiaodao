from __future__ import annotations

from typing import Any

from problem_locator.interfaces.mcp_server import _REQUESTS


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


def test_all_public_mcp_inputs_are_flat_without_exceptions() -> None:
    for tool_name, request_type in _REQUESTS.items():
        schema = request_type.model_json_schema(mode="validation")
        assert schema.get("type") == "object"
        assert "$defs" not in schema, tool_name
        properties = schema.get("properties")
        assert isinstance(properties, dict)
        for property_name, property_schema in properties.items():
            assert isinstance(property_schema, dict)
            assert _is_flat_property(property_schema), (
                tool_name,
                property_name,
                property_schema,
            )
