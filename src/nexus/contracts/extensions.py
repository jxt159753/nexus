"""Strict, immutable data contracts for generated Tool and Skill releases.

No module here imports or executes candidate code. JSON Schema is a deliberately
small, bounded subset: no remote references, regular expressions, floats or
unknown keywords. The Core remains the authority for identity and revision.
"""

import hashlib
import json
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.base import StrictContract

CAPABILITY_ID = re.compile(r"^user\.[a-z][a-z0-9_]{2,62}$")
STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DIGEST = r"^[0-9a-f]{64}$"
MAX_RELEASE_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 32 * 1024
MAX_MARKDOWN_BYTES = 16 * 1024
MAX_INPUT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 32 * 1024
MAX_CASES = 20

TOOL_PROFILE = "directory_entry_stats_v1"
SKILL_PROFILE = "skill_steps_v1"
BUILTIN_TOOLS = {"system.info", "filesystem.list"}
RESULT_FIELDS = {
    "system.info": frozenset({"os_name", "os_version", "architecture", "python_version"}),
    "filesystem.list": frozenset({"root_id", "entries", "truncated"}),
}

_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minimum", "maximum", "minLength", "maxLength",
    "minItems", "maxItems", "title", "description",
}
_SCHEMA_TYPES = {"object", "array", "string", "integer", "boolean", "null"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = item
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("NaN/Infinity is not valid JSON")


def parse_strict_json(data: str | bytes) -> Any:
    """One JSON value, unique keys, no NaN/Infinity, bounded UTF-8 input."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_RELEASE_BYTES:
        raise ValueError("JSON input exceeds release limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("JSON must be UTF-8") from None
    try:
        return json.loads(text, object_pairs_hook=_object_no_duplicates, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc


def check_json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Reject floats and excessively deep/large values before they reach a schema."""
    nodes = nodes if nodes is not None else [0]
    nodes[0] += 1
    if depth > 8 or nodes[0] > 4096:
        raise ValueError("JSON value exceeds structural limits")
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str and len(value.encode("utf-8")) > 4096:
            raise ValueError("string value exceeds 4096 bytes")
        return
    if isinstance(value, list):
        if len(value) > 200:
            raise ValueError("array value exceeds 200 items")
        for item in value:
            check_json_value(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        if len(value) > 32 or not all(type(key) is str for key in value):
            raise ValueError("object value exceeds field limits")
        if len(canonical(value).encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("object value exceeds byte limit")
        for item in value.values():
            check_json_value(item, depth=depth + 1, nodes=nodes)
        return
    raise ValueError("only finite JSON primitives are accepted")


def validate_schema(value: Any, *, where: str, depth: int = 0, count: list[int] | None = None) -> dict:
    """Validate and return a normalized copy of the allowed schema subset."""
    count = count if count is not None else [0]
    count[0] += 1
    if depth > 6 or count[0] > 256:
        raise ValueError(f"{where}: schema exceeds depth/node limits")
    if type(value) is not dict:
        raise ValueError(f"{where}: schema must be an object")
    unknown = set(value) - _SCHEMA_KEYS
    if unknown:
        raise ValueError(f"{where}: unsupported schema keyword")
    kind = value.get("type")
    if kind not in _SCHEMA_TYPES:
        raise ValueError(f"{where}: unsupported schema type")
    normalized: dict[str, Any] = {"type": kind}
    if "title" in value:
        title = value["title"]
        if type(title) is not str or not 1 <= len(title) <= 100:
            raise ValueError(f"{where}: invalid title")
        normalized["title"] = title
    if "description" in value:
        description = value["description"]
        if type(description) is not str or len(description.encode("utf-8")) > 1000:
            raise ValueError(f"{where}: invalid description")
        normalized["description"] = description
    if "enum" in value:
        enum = value["enum"]
        if type(enum) is not list or not 1 <= len(enum) <= 32:
            raise ValueError(f"{where}: enum must have 1-32 values")
        for item in enum:
            if item is not None and type(item) not in {bool, int, str}:
                raise ValueError(f"{where}: enum values must be JSON primitives")
        normalized["enum"] = list(enum)
    if "const" in value:
        const = value["const"]
        if const is not None and type(const) not in {bool, int, str}:
            raise ValueError(f"{where}: const must be a JSON primitive")
        normalized["const"] = const
    if kind == "object":
        properties = value.get("properties", {})
        if type(properties) is not dict or len(properties) > 32:
            raise ValueError(f"{where}: object properties must be an object of at most 32 fields")
        if not all(type(key) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) for key in properties):
            raise ValueError(f"{where}: invalid property name")
        normalized["properties"] = {
            key: validate_schema(item, where=f"{where}.{key}", depth=depth + 1, count=count)
            for key, item in properties.items()
        }
        required = value.get("required", [])
        if type(required) is not list or len(required) > 32 or not all(type(item) is str for item in required):
            raise ValueError(f"{where}: required must be a string list")
        if len(set(required)) != len(required) or not set(required) <= set(properties):
            raise ValueError(f"{where}: required must name unique declared properties")
        normalized["required"] = list(required)
        additional = value.get("additionalProperties", False)
        if additional is not False:
            raise ValueError(f"{where}: additionalProperties must be false")
        normalized["additionalProperties"] = False
        if set(value) & {"items", "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength"}:
            raise ValueError(f"{where}: scalar/array keyword on object")
    elif kind == "array":
        if "items" not in value:
            raise ValueError(f"{where}: array requires items")
        normalized["items"] = validate_schema(value["items"], where=f"{where}[]", depth=depth + 1, count=count)
        for key, limit in (("minItems", 200), ("maxItems", 200)):
            if key in value:
                item = value[key]
                if type(item) is not int or not 0 <= item <= limit:
                    raise ValueError(f"{where}: invalid {key}")
                normalized[key] = item
        if normalized.get("minItems", 0) > normalized.get("maxItems", 200):
            raise ValueError(f"{where}: minItems exceeds maxItems")
        if set(value) & {"properties", "required", "additionalProperties", "minimum", "maximum", "minLength", "maxLength"}:
            raise ValueError(f"{where}: object/scalar keyword on array")
    elif kind == "string":
        for key, limit in (("minLength", 4096), ("maxLength", 4096)):
            if key in value:
                item = value[key]
                if type(item) is not int or not 0 <= item <= limit:
                    raise ValueError(f"{where}: invalid {key}")
                normalized[key] = item
        if normalized.get("minLength", 0) > normalized.get("maxLength", 4096):
            raise ValueError(f"{where}: minLength exceeds maxLength")
        if set(value) & {"items", "properties", "required", "additionalProperties", "minimum", "maximum", "minItems", "maxItems"}:
            raise ValueError(f"{where}: structural keyword on string")
    elif kind == "integer":
        for key in ("minimum", "maximum"):
            if key in value:
                item = value[key]
                if type(item) is not int or not -(10**9) <= item <= 10**9:
                    raise ValueError(f"{where}: invalid {key}")
                normalized[key] = item
        if normalized.get("minimum", -(10**9)) > normalized.get("maximum", 10**9):
            raise ValueError(f"{where}: minimum exceeds maximum")
        if set(value) & {"items", "properties", "required", "additionalProperties", "minLength", "maxLength", "minItems", "maxItems"}:
            raise ValueError(f"{where}: structural keyword on integer")
    else:
        if set(value) & {"items", "properties", "required", "additionalProperties", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
            raise ValueError(f"{where}: structural keyword on primitive")
    return normalized


def validate_instance(schema: dict, value: Any, *, path: str = "$") -> None:
    check_json_value(value)
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict:
            raise ValueError(f"{path}: object required")
        properties = schema.get("properties", {})
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"{path}: unknown field")
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ValueError(f"{path}: required field missing")
        for key, item in value.items():
            validate_instance(properties[key], item, path=f"{path}.{key}")
    elif kind == "array":
        if type(value) is not list:
            raise ValueError(f"{path}: array required")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 200):
            raise ValueError(f"{path}: array length outside limits")
        for index, item in enumerate(value):
            validate_instance(schema["items"], item, path=f"{path}[{index}]")
    elif kind == "string":
        if type(value) is not str:
            raise ValueError(f"{path}: string required")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 4096):
            raise ValueError(f"{path}: string length outside limits")
    elif kind == "integer":
        if type(value) is not int:
            raise ValueError(f"{path}: integer required")
        if value < schema.get("minimum", -(10**9)) or value > schema.get("maximum", 10**9):
            raise ValueError(f"{path}: integer outside limits")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{path}: boolean required")
    elif kind == "null":
        if value is not None:
            raise ValueError(f"{path}: null required")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise ValueError(f"{path}: value is not in enum")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"{path}: value differs from const")


class ExtensionRequestCreate(StrictContract):
    request_id: UUID
    kind: Literal["tool", "skill"]
    need: StrictStr = Field(min_length=1, max_length=4000)
    capability_id: StrictStr | None = Field(default=None, min_length=8, max_length=70)
    node_id: UUID | None = None
    parent_version_id: UUID | None = None

    @model_validator(mode="after")
    def bounded(self):
        if not self.need.strip() or len(self.need.encode("utf-8")) > 12000:
            raise ValueError("bounded nonempty need required")
        if self.capability_id is not None and not CAPABILITY_ID.fullmatch(self.capability_id):
            raise ValueError("capability identity must use the user namespace")
        return self


class ExtensionGenerateConsent(StrictContract):
    preview_digest: StrictStr = Field(pattern=f"^{DIGEST[1:-1]}$")
    allow_model: StrictBool = False


class StepReference(StrictContract):
    step: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    field: Literal["entries", "truncated", "root_id", "os_name", "os_version", "architecture", "python_version"]


class InputReference(StrictContract):
    field: StrictStr = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class SkillStep(StrictContract):
    id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    tool: Literal["system.info", "filesystem.list"] | None = None
    tool_version: Literal[1] | None = None
    tool_version_id: UUID | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_shape(self):
        if (self.tool is None) != (self.tool_version is None):
            raise ValueError("tool and tool_version must be supplied together")
        if (self.tool is None) == (self.tool_version_id is None):
            raise ValueError("exactly one of builtin tool or extension tool version is required")
        check_json_value(self.arguments)
        if len(canonical(self.arguments).encode("utf-8")) > 4096:
            raise ValueError("step arguments exceed 4 KiB")
        for name, value in self.arguments.items():
            if type(name) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise ValueError("invalid argument name")
            if isinstance(value, dict):
                if set(value) == {"$ref"}:
                    StepReference.model_validate(value["$ref"])
                elif set(value) == {"$input"}:
                    InputReference.model_validate(value["$input"])
                else:
                    raise ValueError("only explicit $ref objects are accepted as structured arguments")
        return self

    def references(self) -> list[StepReference]:
        result = []
        for value in self.arguments.values():
            if isinstance(value, dict) and set(value) == {"$ref"}:
                result.append(StepReference.model_validate(value["$ref"]))
        return result


class ExtensionManifest(StrictContract):
    kind: Literal["tool", "skill"]
    capability_id: StrictStr = Field(min_length=8, max_length=70)
    description: StrictStr = Field(min_length=10, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    verification_profile: Literal["directory_entry_stats_v1", "skill_steps_v1"]
    dependencies: list[Literal["python-stdlib"]] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def check_identity(self):
        if not CAPABILITY_ID.fullmatch(self.capability_id):
            raise ValueError("capability identity must use the user namespace")
        validate_schema(self.input_schema, where="input_schema")
        validate_schema(self.output_schema, where="output_schema")
        if self.input_schema["type"] != "object" or self.output_schema["type"] != "object":
            raise ValueError("tool/skill input and output must be JSON objects")
        if self.kind == "tool" and self.verification_profile != TOOL_PROFILE:
            raise ValueError("unsupported tool verification profile")
        if self.kind == "skill" and self.verification_profile != SKILL_PROFILE:
            raise ValueError("unsupported skill verification profile")
        return self


class ToolCase(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=80)
    input: Any
    expect_output: Any | None = None
    expect_error: StrictBool = False

    @model_validator(mode="after")
    def bounded(self):
        check_json_value(self.input)
        if self.expect_output is not None:
            check_json_value(self.expect_output)
        return self


class SkillCase(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    expected_steps: list[StrictStr] = Field(max_length=3)


class ToolGeneration(StrictContract):
    kind: Literal["tool"]
    manifest: ExtensionManifest
    source: StrictStr = Field(min_length=1, max_length=MAX_SOURCE_BYTES)
    tests: list[ToolCase] = Field(default_factory=list, max_length=MAX_CASES)

    @model_validator(mode="after")
    def check_manifest(self):
        if self.manifest.kind != "tool":
            raise ValueError("manifest kind mismatch")
        if len(self.source.encode("utf-8")) > MAX_SOURCE_BYTES or "\x00" in self.source:
            raise ValueError("source exceeds its text budget")
        return self


class SkillGeneration(StrictContract):
    kind: Literal["skill"]
    manifest: ExtensionManifest
    markdown: StrictStr = Field(min_length=1, max_length=MAX_MARKDOWN_BYTES)
    steps: list[SkillStep] = Field(min_length=1, max_length=3)
    tests: list[SkillCase] = Field(default_factory=list, max_length=MAX_CASES)

    @model_validator(mode="after")
    def check_plan(self):
        if self.manifest.kind != "skill":
            raise ValueError("manifest kind mismatch")
        if len(self.markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise ValueError("markdown exceeds its text budget")
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step id")
        for index, step in enumerate(self.steps):
            for value in step.arguments.values():
                if isinstance(value, dict) and "$input" in value:
                    reference = InputReference.model_validate(value["$input"])
                    if reference.field not in self.manifest.input_schema.get("properties", {}):
                        raise ValueError("input reference must name a declared input field")
            for reference in step.references():
                if reference.step not in ids[:index]:
                    raise ValueError("references must point to an earlier declared step")
        return self


ExtensionGeneration = Annotated[ToolGeneration | SkillGeneration, Field(discriminator="kind")]
GENERATION = TypeAdapter(ExtensionGeneration)


class SandboxToolInput(StrictContract):
    entries: list[dict[str, Any]] = Field(max_length=200)


def environment_for(kind: str, sandbox: dict | None) -> dict:
    if kind == "skill":
        return {"type": "skill_standin", "controller": "trusted-skill-v1", "version": 1}
    if sandbox is None:
        return {"type": "sandbox", "runtime": "runsc", "image_digest": None, "available": False}
    return {"type": "sandbox", "runtime": "runsc", "image_digest": sandbox["image_digest"],
            "platform": sandbox.get("platform", "systrap"), "runsc_digest": sandbox.get("runsc_digest"),
            "policy": "runsc-host-limits-v2", "available": True}


def build_release(generation: ExtensionGeneration, *, capability_id: str, revision: int,
                  parent_version_id: UUID | None, environment: dict) -> tuple[dict, str, str]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "kind": generation.kind,
        "capability_id": capability_id,
        "revision": revision,
        "parent_version_id": str(parent_version_id) if parent_version_id else None,
        "manifest": generation.manifest.model_dump(mode="json"),
        "tests": [item.model_dump(mode="json") for item in generation.tests],
        "environment": environment,
    }
    if isinstance(generation, ToolGeneration):
        value["source"] = generation.source
    else:
        value["markdown"] = generation.markdown
        value["steps"] = [item.model_dump(mode="json") for item in generation.steps]
    if len(canonical(value).encode("utf-8")) > MAX_RELEASE_BYTES:
        raise ValueError("release exceeds 64 KiB")
    return value, digest_value(value), digest_value(environment)


def parse_generation(text: str) -> ExtensionGeneration:
    value = parse_strict_json(text)
    # Re-enter through JSON so UUID fields accept their canonical string form;
    # the duplicate-key/NaN checks above still run first.
    return GENERATION.validate_json(canonical(value))
