"""Bounded validation evidence without model arguments or exception messages."""

from pydantic import ValidationError


def argument_error(index, name, arguments, schema, error):
    fields = set(schema.get("properties", {}))
    issues = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_input=False, include_context=False)[:4]:
            location = [part for part in item["loc"] if isinstance(part, str) and part in fields]
            issues.append({"field": ".".join(location) or "$", "type": item["type"]})
    else:
        issues.append({"field": "$", "type": "invalid_json_or_arguments"})
    result = {"index": index, "tool": name, "error_code": "INVALID_TOOL_ARGUMENTS", "issues": issues}
    if name == "write_file" and isinstance(arguments, dict) and isinstance(arguments.get("content"), str):
        # Byte limits cannot be represented by JSON Schema maxLength (characters).
        result.update(content_utf8_bytes=len(arguments["content"].encode("utf-8")), max_content_utf8_bytes=12000)
        if result["content_utf8_bytes"] > 12000:
            result["error_code"] = "FILE_CONTENT_TOO_LARGE"
    return result
