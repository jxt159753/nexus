"""Bounded validation evidence without model arguments or exception messages."""

from pydantic import ValidationError


def declared_fields(value):
    if isinstance(value, dict):
        names=set(value.get('properties',{}))
        for child in value.values():names.update(declared_fields(child))
        return names
    if isinstance(value,list):
        return set().union(*(declared_fields(child) for child in value))
    return set()


def argument_error(index, name, arguments, schema, error):
    fields = declared_fields(schema)
    issues = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_input=False)[:4]:
            location = [str(part) for part in item["loc"] if isinstance(part, str) and part in fields or type(part) is int and 0<=part<1000]
            issue={"field": ".".join(location) or "$", "type": item["type"]}
            # Only known static validator messages may become model feedback.
            # Arbitrary exception text can contain user arguments or secrets.
            message=str(item.get('ctx',{}).get('error',''))
            if message=='tool and tool_version must be supplied together':
                issue['hint']='A direct Skill step requires both tool and tool_version: 1.'
            issues.append(issue)
    else:
        issues.append({"field": "$", "type": "invalid_json_or_arguments"})
    result = {"index": index, "tool": name, "error_code": "INVALID_TOOL_ARGUMENTS", "issues": issues}
    if name == "write_file" and isinstance(arguments, dict) and isinstance(arguments.get("content"), str):
        # Byte limits cannot be represented by JSON Schema maxLength (characters).
        result.update(content_utf8_bytes=len(arguments["content"].encode("utf-8")), max_content_utf8_bytes=12000)
        if result["content_utf8_bytes"] > 12000:
            result["error_code"] = "FILE_CONTENT_TOO_LARGE"
    return result
