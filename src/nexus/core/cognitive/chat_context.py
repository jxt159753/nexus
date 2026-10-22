"""Deterministic context projections; durable tool evidence is never rewritten."""

import copy
import json
import math

from nexus.contracts.extensions import canonical
from nexus.core.models.chat import build_request
from nexus.core.security.types import AccessDenied


def request_size(config, messages, **options):
    return len(canonical(build_request(config, messages, **options)).encode("utf-8"))


def estimated_tokens(value):
    """Dependency-free text estimate, not a provider tokenizer or cost receipt."""
    image_count = 0

    def text_surface(item):
        nonlocal image_count
        if isinstance(item, dict):
            if item.get("type") == "image_url":
                image_count += 1
                return {"type": "image_url", "notice": "bounded image snapshot"}
            return {key: text_surface(child) for key, child in item.items()}
        if isinstance(item, list):
            return [text_surface(child) for child in item]
        return item

    value = text_surface(value)
    text = canonical(value)
    ascii_count = sum(ord(char) < 128 for char in text)
    # deepseek-flash currently documents at most 1024 input tokens per image.
    return math.ceil(ascii_count / 4 + (len(text) - ascii_count) / 1.5) + 16 + image_count * 1024


def pressure(config, messages, **options):
    request = build_request(config, messages, **options)
    tokens = estimated_tokens(request)
    window = config.chat_context_window_tokens
    threshold = min(config.chat_input_tokens * config.chat_compaction_threshold_percent // 100,
                    window * config.chat_compaction_threshold_percent // 100,
                    window - request["max_tokens"] - config.chat_compaction_headroom_tokens)
    size = len(canonical(request).encode("utf-8"))
    return {"estimated_tokens": tokens, "threshold_tokens": threshold, "request_bytes": size,
            "pressured": tokens >= threshold or size >= config.chat_input_bytes * config.chat_compaction_threshold_percent // 100,
            "fits": tokens <= min(config.chat_input_tokens, window-request["max_tokens"])
                    and size <= config.chat_input_bytes}


def prune_tools(messages):
    """First DSH-style model-free pass; keep all native call/result pairs."""
    output = copy.deepcopy(messages)
    for item in output:
        if item.get("role") == "tool":
            result = json.loads(item["content"])
            if isinstance(result, dict):
                item["content"] = canonical(project_result(result, 4096))
    return output


def excerpt(text, limit):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "\n[…中间内容已裁剪；完整结果见工具执行记录…]\n"
    remaining = limit - len(marker.encode("utf-8"))
    head = remaining * 3 // 4
    return raw[:head].decode("utf-8", errors="ignore") + marker + raw[-(remaining-head):].decode("utf-8", errors="ignore")


def project_result(result, limit):
    """Shorten only the data payload, retaining outcome, errors and verification."""
    raw = canonical(result)
    if len(raw.encode("utf-8")) <= limit or not {"data", "data_excerpt"}.intersection(result):
        return result
    value = copy.deepcopy(result)
    data = value.pop("data", None)
    value["data_excerpt"] = excerpt(canonical(data) if "data" in result else result["data_excerpt"], limit)
    # Citation locators remain exact even if the excerpt cuts through a URL.
    if isinstance(data, dict):
        if isinstance(data.get("url"), str):
            value["source_url"] = data["url"]
        if isinstance(data.get("sources"), list):
            value["sources"] = [{key: item[key] for key in ("title", "url") if isinstance(item.get(key),str)} for item in data["sources"][:5] if isinstance(item,dict)]
    value["context_projection"] = {"truncated": True, "original_bytes": result.get("context_projection",{}).get("original_bytes",len(raw.encode("utf-8"))),
                                   "notice": "仅为完整 JSON 数据的首尾摘录，不能当作完整数据或用于全量统计；完整结果保留在执行记录。"}
    return value


def history_message(records):
    return {"role":"user","content":"较早对话的确定性摘录（不完整，原文按 turn_id 保留）：\n"+canonical(list(reversed(records)))}


def fit_work_context(config, messages, catalog, *, allow_tools=True, work_limit=4, generate_title=False, history_summary=None):
    """Drop whole old chat rounds, then excerpt results; preserve every call/id."""
    output = copy.deepcopy(messages)
    options = {"allow_tools": allow_tools, "work_catalog": catalog, "work_limit": work_limit, "generate_title": generate_title}

    def fits():
        return request_size(config, output, **options) <= config.chat_input_bytes

    if not fits() and history_summary and output and output[0]==history_message(history_summary):
        del output[0]

    while not fits() and len(output) > 2:
        # Only plain historical pairs can precede another user message.
        # Selected memories/current input and the entire native call chain stay.
        first, second = output[:2]
        if (first.get("role") != "user" or second.get("role") != "assistant"
                or second.get("tool_calls") or not any(item.get("role") == "user" for item in output[2:])):
            break
        del output[:2]
    for limit in (4096, 1024):
        for index, message in enumerate(output):
            if fits():
                return output
            if message.get("role") != "tool":
                continue
            # Re-project from the complete original, never a previous excerpt.
            original = messages[len(messages)-len(output)+index]
            result = json.loads(original["content"])
            if isinstance(result, dict):
                message["content"] = canonical(project_result(result, limit))
    if not fits():
        raise AccessDenied("MODEL_INPUT_LIMIT")
    return output
