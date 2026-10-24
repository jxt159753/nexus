"""One bounded DeepSeek Search/Fetch request, without retries or direct crawling."""

import asyncio
from dataclasses import dataclass

import httpx2

from nexus.contracts.extensions import parse_strict_json
from nexus.contracts.web import WebSearchArguments, WebFetchArguments, public_url
from nexus.contracts.extensions import canonical


@dataclass(frozen=True)
class WebReply:
    data: dict | None = None
    error: str | None = None
    # None means the response did not establish whether this request was billed.
    billable: bool | None = None


def bounded(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def web_request(intent, arguments):
    if intent == "web_search":
        return "search", {**WebSearchArguments.model_validate(arguments).model_dump(),"need_content":False}
    if intent == "web_fetch":
        return "fetch", WebFetchArguments.model_validate(arguments).model_dump()
    raise ValueError("unsupported web operation")


class DeepSeekWebProvider:
    def __init__(self, key, *, transport=None):
        self.key, self.transport = key, transport

    async def invoke(self, intent: str, arguments: dict) -> WebReply:
        operation,body=web_request(intent,arguments)
        billable = None
        try:
            async with asyncio.timeout(10):
                async with httpx2.AsyncClient(timeout=10, follow_redirects=False,
                                             trust_env=False, transport=self.transport) as client:
                    async with client.stream("POST", "https://api.deepseek.com/v1/tools/"+operation,
                                             content=canonical(body).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+self.key.get_secret_value()}) as response:
                        if response.status_code != 200:
                            code = {401:"WEB_AUTH_REQUIRED",402:"WEB_QUOTA_EXCEEDED",403:"WEB_URL_DENIED",404:"WEB_PAGE_UNAVAILABLE",429:"WEB_RATE_LIMITED"}.get(response.status_code,"WEB_REQUEST_FAILED")
                            return WebReply(error=code, billable=False)
                        raw = bytearray()
                        async for part in response.aiter_bytes():
                            if len(raw)+len(part)>262144:
                                return WebReply(error="WEB_RESPONSE_LIMIT")
                            raw.extend(part)
            payload = parse_strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            if operation == "fetch":
                content, title = payload["content"], payload["title"]
                if not isinstance(content,str) or not isinstance(title,str):
                    raise ValueError("invalid page")
                billable = bool(content)
                text = bounded(content,8192)
                return WebReply(data={"url":body["url"],"title":bounded(title,256),"content":text,"truncated":text!=content},billable=billable)
            results = payload["results"]
            if not isinstance(results,list) or len(results)>50:
                raise ValueError("invalid search")
            billable = bool(results)
            sources = []
            for item in results[:body["count"]]:
                if not isinstance(item,dict) or any(not isinstance(item.get(key),str) for key in ("url","title","snippet")):
                    raise ValueError("invalid source")
                sources.append({"url":public_url(item["url"]),"title":bounded(item["title"],256),"snippet":bounded(item["snippet"],700)})
            return WebReply(data={"sources":sources,"truncated":len(results)>len(sources)},billable=billable)
        except TimeoutError:
            return WebReply(error="WEB_TIMEOUT")
        except httpx2.HTTPError:
            return WebReply(error="WEB_OUTCOME_UNKNOWN")
        except (ValueError,KeyError,TypeError,UnicodeError,RecursionError):
            return WebReply(error="WEB_RESPONSE_INVALID",billable=billable)
