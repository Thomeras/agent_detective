"""Output-contract schema checking and inference.

Scoring validates a node's output with a dependency-free JSON Schema SUBSET
(services/worker/worker/scoring.py::validate_json_schema): `type`, `required`,
`properties`, `items`, `enum` — every other keyword is IGNORED. The project has
no jsonschema dependency, so "is this schema valid?" is the wrong question;
the only question that matters is "will the engine actually enforce anything
with it?". A schema built out of keywords the engine ignores validates fine
against draft-07 and still passes every output ever produced — a 0.35-weight
scoring channel that says yes to everything, which is worse than the null
component you get with no contract at all. So this module mirrors the engine's
subset and refuses anything it cannot enforce.

Inference is deliberately timid for the same reason. It reads stored run
payloads and reports only what the samples literally show: a key is required
when it appeared in EVERY usable sample, its type is the type that was
observed, and that is all. No enums, no formats, no ranges, no nested
constraints, and no schema at all when the samples share nothing.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Keywords validate_json_schema acts on. Anything else is silently ignored by
# the engine, so accepting it here would promise enforcement that never happens.
ENFORCED_KEYWORDS = frozenset({"type", "required", "properties", "items", "enum"})

# Documentation, not constraints: ignored by the engine but not worth warning about.
METADATA_KEYWORDS = frozenset({"$schema", "$id", "title", "description"})

JSON_TYPE_NAMES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


@dataclass
class SchemaCheck:
    problems: list[str] = field(default_factory=list)
    ignored_keywords: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _type_names(schema: dict[str, Any]) -> set[str] | None:
    kw = schema.get("type")
    if isinstance(kw, str):
        return {kw}
    if isinstance(kw, list):
        return {t for t in kw if isinstance(t, str)}
    return None


def _check_subschema(schema: Any, path: str, check: SchemaCheck) -> bool:
    """Append findings for one subschema; returns True if it enforces anything."""
    if not isinstance(schema, dict):
        check.problems.append(f"{path}: must be a JSON object, got {type(schema).__name__}")
        return False

    enforces = False
    if "type" in schema:
        type_kw = schema["type"]
        names: set[str] | None
        if isinstance(type_kw, str):
            names = {type_kw}
        elif isinstance(type_kw, list) and type_kw and all(isinstance(t, str) for t in type_kw):
            names = set(type_kw)
        else:
            check.problems.append(f"{path}.type: must be a type name or a non-empty list of them")
            names = None
        unknown = sorted((names or set()) - JSON_TYPE_NAMES)
        if unknown:
            check.problems.append(f"{path}.type: unknown JSON type(s) {', '.join(unknown)}")
        elif names:
            enforces = True

    allows_object = (_type_names(schema) or set()) & {"object"}
    allows_array = (_type_names(schema) or set()) & {"array"}

    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            check.problems.append(f"{path}.enum: must be a non-empty list of allowed values")
        else:
            enforces = True

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            check.problems.append(f"{path}.required: must be a list of property names")
        elif required:
            # The engine checks required/properties only when the instance IS a
            # dict, so without "object" in `type` a plain string output slips
            # through untested — the exact silent pass this table exists to stop.
            if not allows_object:
                check.problems.append(
                    f'{path}.required is only enforced on objects: add "type": "object"'
                )
            else:
                enforces = True

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            check.problems.append(f"{path}.properties: must be a JSON object")
        else:
            if properties and not allows_object:
                check.problems.append(
                    f'{path}.properties is only enforced on objects: add "type": "object"'
                )
            for key, subschema in properties.items():
                if _check_subschema(subschema, f"{path}.properties.{key}", check) and allows_object:
                    enforces = True

    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            # Tuple-form items (a list) is ignored outright by the engine.
            check.problems.append(f"{path}.items: must be a single JSON object schema")
        else:
            if not allows_array:
                check.problems.append(
                    f'{path}.items is only enforced on arrays: add "type": "array"'
                )
            elif _check_subschema(items, f"{path}.items", check):
                enforces = True

    for keyword in schema:
        if keyword not in ENFORCED_KEYWORDS and keyword not in METADATA_KEYWORDS:
            check.ignored_keywords.append(f"{path}.{keyword}")

    return enforces


def check_contract_schema(schema: Any) -> SchemaCheck:
    """Structural check against the engine's enforceable subset."""
    check = SchemaCheck()
    enforces = _check_subschema(schema, "json_schema", check)
    if check.ok and not enforces:
        check.problems.append(
            "this schema constrains nothing the scoring engine enforces, so every "
            "output would score 1.0 on the schema channel; a contract that always "
            "passes is worse than no contract"
        )
    return check


# --- Inference from stored payloads --------------------------------------


@dataclass
class Inference:
    runs_examined: int
    runs_with_output: int
    usable_samples: int
    keys: list[dict[str, Any]]
    schema: dict[str, Any] | None
    reason: str | None


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool is an int subclass; check it first
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _reconcile(observed: set[str]) -> tuple[str | list[str] | None, str]:
    """The one type the samples agree on, or None with the reason they do not."""
    concrete = observed - {"null"}
    if {"integer", "number"} <= concrete:
        concrete = concrete - {"integer"}  # every integer is a number
    if not concrete:
        return None, "always null — nothing to constrain"
    if len(concrete) > 1:
        return None, f"conflicting types: {', '.join(sorted(concrete))}"
    (only,) = concrete
    if "null" in observed:
        return [only, "null"], f"{only}, sometimes null"
    return only, f"always {only}"


def infer_schema(outputs: list[str | None], min_samples: int) -> Inference:
    """Propose a schema from stored outputs, or say why none can be proposed."""
    examined = len(outputs)
    with_output = [text for text in outputs if text is not None and text.strip()]

    samples: list[dict[str, Any]] = []
    for text in with_output:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue  # not JSON: a contract cannot be derived from it
        if isinstance(parsed, dict):
            samples.append(parsed)

    usable = len(samples)
    base = {
        "runs_examined": examined,
        "runs_with_output": len(with_output),
        "usable_samples": usable,
    }
    if usable < min_samples:
        return Inference(
            **base,
            keys=[],
            schema=None,
            reason=(
                f"{usable} of {examined} run(s) produced a payload that parses as a "
                f"JSON object; {min_samples} are required before a shape observed "
                "that few times can be called a contract"
            ),
        )

    presence: dict[str, int] = defaultdict(int)
    observed: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        for key, value in sample.items():
            presence[key] += 1
            observed[key].add(_json_type(value))

    keys: list[dict[str, Any]] = []
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key in sorted(presence):
        resolved, note = _reconcile(observed[key])
        is_required = presence[key] == usable
        # A type read off SOME of the samples can be wrong for the rest, and a
        # wrong property type scores a perfectly good output 0.0 — a defect
        # manufactured by the contract. Only keys every sample carried are
        # typed here; the others are reported for a human to add deliberately.
        included = is_required and resolved is not None
        if included:
            properties[key] = {"type": resolved}
        if is_required:
            required.append(key)
        keys.append(
            {
                "key": key,
                "present_in": presence[key],
                "observed_types": sorted(observed[key]),
                "required": is_required,
                "type": resolved if included else None,
                "included": included,
                "note": note,
            }
        )

    if not required:
        return Inference(
            **base,
            keys=keys,
            schema=None,
            reason=(
                f"no key appeared in all {usable} usable sample(s), so the only "
                "schema these payloads support is one that accepts any object — "
                "that would score every output 1.0 on the schema channel"
            ),
        )

    schema: dict[str, Any] = {"type": "object", "required": required}
    if properties:
        schema["properties"] = properties
    return Inference(**base, keys=keys, schema=schema, reason=None)
