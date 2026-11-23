"""JSON Schema subset used to lint ledger files.

Why a subset: the runtime is the Python stdlib, and the four schemas in
`schema/` only need types, enums, $ref, and basic combinators.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

from mil import CONTRACT
from mil.paths import schema_dir

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class SchemaIssue:
    __slots__ = ("path", "message", "keyword")

    def __init__(self, path: str, message: str, keyword: str = "") -> None:
        self.path = path
        self.message = message
        self.keyword = keyword

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def load_schema(name: str) -> dict[str, Any]:
    """Load one JSON Schema from `schema/` (the single source of truth)."""
    filename = name if name.endswith(".schema.json") else f"{name}.schema.json"
    path = schema_dir() / filename
    if not path.is_file():
        raise FileNotFoundError(f"missing schema file: {path}")
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: schema root must be a JSON object")
    return data


def validate(instance: Any, schema: dict[str, Any], prefix: str = "$") -> list[SchemaIssue]:
    return _Validator(schema).validate(instance, schema, prefix)


def contract_from_config_schema() -> str:
    schema = load_schema("config.schema.json")
    return schema["properties"]["contract"]["const"]


class _Validator:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root

    def validate(self, instance: Any, schema: Any, path: str) -> list[SchemaIssue]:
        if schema is True:
            return []
        if schema is False:
            return [SchemaIssue(path, "value is not allowed", "false")]
        if not isinstance(schema, dict):
            return [SchemaIssue(path, "schema is not an object", "schema")]
        issues: list[SchemaIssue] = []
        if "$ref" in schema:
            issues.extend(self.validate(instance, self._resolve(schema["$ref"], path), path))
            if issues and len(schema) == 1:
                return issues
        if "type" in schema:
            issues.extend(self._check_type(instance, schema["type"], path))
            if issues:
                return issues
        if "const" in schema and instance != schema["const"]:
            issues.append(SchemaIssue(path, f"expected {schema['const']!r}", "const"))
        if "enum" in schema and instance not in schema["enum"]:
            issues.append(SchemaIssue(path, f"expected one of {schema['enum']!r}", "enum"))
        if "pattern" in schema and isinstance(instance, str):
            if re.search(schema["pattern"], instance) is None:
                issues.append(SchemaIssue(path, f"does not match {schema['pattern']}", "pattern"))
        if "format" in schema and isinstance(instance, str):
            issues.extend(self._check_format(instance, schema["format"], path))
        if "minLength" in schema and isinstance(instance, str) and len(instance) < schema["minLength"]:
            issues.append(SchemaIssue(path, f"shorter than {schema['minLength']}", "minLength"))
        if "minimum" in schema and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if instance < schema["minimum"]:
                issues.append(SchemaIssue(path, f"less than {schema['minimum']}", "minimum"))
        if "minItems" in schema and isinstance(instance, list) and len(instance) < schema["minItems"]:
            issues.append(SchemaIssue(path, f"needs at least {schema['minItems']} items", "minItems"))
        if "uniqueItems" in schema and schema["uniqueItems"] and isinstance(instance, list):
            seen = []
            for item in instance:
                if item in seen:
                    issues.append(SchemaIssue(path, f"duplicate item {item!r}", "uniqueItems"))
                else:
                    seen.append(item)
        if isinstance(instance, dict) and (
            "properties" in schema or "additionalProperties" in schema or "required" in schema
        ):
            issues.extend(self._check_object(instance, schema, path))
        if isinstance(instance, list) and "items" in schema:
            item_schema = schema["items"]
            for i, item in enumerate(instance):
                issues.extend(self.validate(item, item_schema, f"{path}[{i}]"))
        if "anyOf" in schema:
            nested = [self.validate(instance, sub, path) for sub in schema["anyOf"]]
            if all(nested):
                issues.append(SchemaIssue(path, "does not match any allowed shape", "anyOf"))
        if "oneOf" in schema:
            ok = [issues_ for issues_ in (self.validate(instance, sub, path) for sub in schema["oneOf"]) if not issues_]
            if len(ok) != 1:
                issues.append(SchemaIssue(path, "expected to match exactly one shape", "oneOf"))
        if "dependentRequired" in schema and isinstance(instance, dict):
            for trigger, needed in schema["dependentRequired"].items():
                if trigger in instance:
                    for key in needed:
                        if key not in instance:
                            issues.append(
                                SchemaIssue(path, f"{key!r} is required when {trigger!r} is set", "dependentRequired")
                            )
        return issues

    def _check_type(self, instance: Any, expected: Any, path: str) -> list[SchemaIssue]:
        types = expected if isinstance(expected, list) else [expected]
        for name in types:
            if _matches_type(instance, name):
                return []
        return [SchemaIssue(path, f"expected type {expected!r}, got {_type_name(instance)}", "type")]

    def _check_format(self, instance: str, fmt: str, path: str) -> list[SchemaIssue]:
        if fmt == "date":
            if not DATE_RE.fullmatch(instance):
                return [SchemaIssue(path, "expected YYYY-MM-DD", "format")]
            year, month, day = (int(p) for p in instance.split("-"))
            try:
                datetime.date(year, month, day)
            except ValueError:
                return [SchemaIssue(path, "invalid calendar date", "format")]
        return []

    def _check_object(self, instance: dict[str, Any], schema: dict[str, Any], path: str) -> list[SchemaIssue]:
        issues: list[SchemaIssue] = []
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                issues.append(SchemaIssue(path, f"missing {key!r}", "required"))
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                issues.extend(self.validate(value, props[key], f"{path}.{key}"))
            elif extra is False:
                issues.append(SchemaIssue(path, f"unknown field {key!r}", "additionalProperties"))
            elif isinstance(extra, dict):
                issues.extend(self.validate(value, extra, f"{path}.{key}"))
        return issues

    def _resolve(self, ref: str, path: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"{path}: unsupported $ref {ref!r}")
        cur: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cur, dict) or part not in cur:
                raise ValueError(f"{path}: unresolved $ref {ref!r}")
            cur = cur[part]
        if not isinstance(cur, dict):
            raise ValueError(f"{path}: $ref {ref!r} is not an object schema")
        return cur


def _matches_type(instance: Any, name: str) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    return False


def _type_name(instance: Any) -> str:
    if instance is None:
        return "null"
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if isinstance(instance, list):
        return "array"
    if isinstance(instance, dict):
        return "object"
    if isinstance(instance, str):
        return "string"
    return type(instance).__name__


def assert_bundled_contract() -> str:
    """Return CONTRACT after checking schema/config.schema.json agrees."""
    from_schema = contract_from_config_schema()
    if from_schema != CONTRACT:
        raise RuntimeError(
            f"schema/config.schema.json contract {from_schema!r} != mil.CONTRACT {CONTRACT!r}"
        )
    return CONTRACT
