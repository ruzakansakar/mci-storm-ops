"""A tiny JSON-schema checker (just the parts the agent contracts use), so we need no extra package.

Supported: type (one name or a list), enum, required, properties, additionalProperties (false), items, minItems, maxItems,
minimum, maximum, minLength, maxLength.
"""

TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
KEYWORDS = {"type", "enum", "required", "properties", "additionalProperties", "items", "minItems", "maxItems", "minimum",
            "maximum", "minLength", "maxLength"}


def _is(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def check_schema(schema: dict, where: str = "output") -> list[str]:
    """Problems with the schema itself (unknown keywords or types)."""
    problems = []
    if not isinstance(schema, dict):
        return [f"{where} must be a mapping"]
    for key in schema:
        if key not in KEYWORDS:
            problems.append(f"{where}: unsupported keyword '{key}'")
    t = schema.get("type")
    for name in ([t] if isinstance(t, str) else t or []):
        if name not in TYPES:
            problems.append(f"{where}: unknown type '{name}'")
    for k, sub in (schema.get("properties") or {}).items():
        problems += check_schema(sub, f"{where}.{k}")
    if "items" in schema:
        problems += check_schema(schema["items"], f"{where}[]")
    return problems


def validate(value, schema: dict, path: str = "output") -> list[str]:
    """Plain-English problems with `value` (an empty list means it matches)."""
    errors = []
    t = schema.get("type")
    if t is not None:
        names = [t] if isinstance(t, str) else t
        if not any(_is(value, n) for n in names):
            return [f"{path} must be {' or '.join(names)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} is above {schema['maximum']}")
    if isinstance(value, dict):
        for r in schema.get("required", []):
            if r not in value:
                errors.append(f"{path} is missing '{r}'")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path} has a field '{k}' that is not in the contract")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} has more than {schema['maxItems']} items")
        if "items" in schema:
            for i, v in enumerate(value):
                errors += validate(v, schema["items"], f"{path}[{i}]")
    return errors


def sample(schema: dict):
    """The smallest value that matches the schema (used by tests and by `scripts.contracts` checks)."""
    t = schema.get("type")
    t = t[0] if isinstance(t, list) else t
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties", {})
        return {k: sample(props[k]) for k in schema.get("required", []) if k in props}
    if t == "array":
        return [sample(schema["items"])] * schema.get("minItems", 0) if "items" in schema else []
    if t == "string":
        return "x" * max(1, schema.get("minLength", 1))
    if t == "integer":
        return max(0, schema.get("minimum", 0))
    if t == "number":
        return float(max(0, schema.get("minimum", 0)))
    if t == "boolean":
        return False
    return None
