"""One bounded DeepSeek call for a Tool/Skill candidate. No retries or tools."""

import asyncio
import json

import httpx2
from pydantic import SecretStr

from nexus.core.models.deepseek import ENDPOINT, MODEL, ProviderFailure
from nexus.core.models.providers import ModelReply

MAX_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class DeepSeekExtensionProvider:
    def __init__(self, key: SecretStr, timeout_seconds: int, *, transport=None):
        self.key, self.timeout_seconds, self.transport = key, timeout_seconds, transport

    async def generate(self, messages: list[dict], *, max_output_tokens: int,
                       timeout_seconds: int | None = None) -> ModelReply:
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 4096:
            raise ProviderFailure("MODEL_BUDGET_INVALID")
        timeout = min(self.timeout_seconds, timeout_seconds or self.timeout_seconds)
        if timeout <= 0:
            raise ProviderFailure("MODEL_BUDGET_INVALID")
        value = {
            "model": MODEL,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ProviderFailure("MODEL_INPUT_LIMIT")
        try:
            async with asyncio.timeout(timeout):
                async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False,
                                             trust_env=False, transport=self.transport) as client:
                    async with client.stream(
                        "POST", ENDPOINT, json=value,
                        headers={"Authorization": "Bearer " + self.key.get_secret_value()},
                    ) as response:
                        if response.status_code != 200:
                            raise ProviderFailure("MODEL_REQUEST_FAILED")
                        raw = bytearray()
                        async for part in response.aiter_bytes():
                            if len(raw) + len(part) > MAX_RESPONSE_BYTES:
                                raise ProviderFailure("MODEL_RESPONSE_LIMIT")
                            raw.extend(part)
            data = json.loads(raw)
            usage = data["usage"]
            incoming, outgoing = usage["prompt_tokens"], usage["completion_tokens"]
            if (type(incoming) is not int or type(outgoing) is not int
                    or not 0 < incoming <= 16384 or not 0 <= outgoing <= max_output_tokens):
                raise ProviderFailure("MODEL_USAGE_INVALID")
            model = data.get("model")
            if not isinstance(model, str) or not model.startswith("deepseek-"):
                raise ProviderFailure("MODEL_RESPONSE_INVALID")
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ProviderFailure("MODEL_RESPONSE_INVALID")
            choice = choices[0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
            if (not isinstance(content, str) or len(content.encode("utf-8")) > MAX_RESPONSE_BYTES
                    or choice["message"].get("tool_calls") or finish not in {"stop", "length"}):
                raise ProviderFailure("MODEL_RESPONSE_INVALID")
            return ModelReply(content, incoming, outgoing, finish == "stop", model=model, finish_reason=finish)
        except (httpx2.HTTPError, TimeoutError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ProviderFailure("MODEL_REQUEST_FAILED") from None
