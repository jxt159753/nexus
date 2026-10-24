"""Bounded incremental speech text. Code remains visible in the ordinary chat."""

import re
from collections.abc import AsyncIterator


async def speech_segments(source: AsyncIterator[str], limit: int = 1000) -> AsyncIterator[str]:
    pending, spoken, fenced, announced = "", 0, False, False
    async for delta in source:
        pending += delta
        while pending:
            # Keep a partial fence until the following delta supplies the rest.
            if pending in {"`", "``"}:
                break
            fence = pending.find("```")
            if fenced:
                if fence < 0:
                    pending = pending[-2:]
                    break
                pending, fenced = pending[fence + 3:], False
                continue
            if fence == 0:
                pending, fenced = pending[3:], True
                if not announced and spoken + 15 < limit:
                    announced = True
                    spoken += 15
                    yield "代码和详细内容请看聊天界面。"
                continue
            boundary = next((i + 1 for i, char in enumerate(pending)
                             if char in "。！？!?；;\n" or char in "，,：:" and i >= 8), 0)
            if fence > 0:
                boundary = min(boundary or fence, fence)
            if not boundary and len(pending) >= 80:
                boundary = 80
            if not boundary:
                break
            piece, pending = pending[:boundary], pending[boundary:]
            piece = re.sub(r"https?://\S+", "链接见界面", piece)
            piece = re.sub(r"[*#_`>]|\[|\]", "", piece).strip()
            piece = piece[:max(0, limit - spoken)]
            if piece:
                spoken += len(piece)
                yield piece
        if spoken >= limit:
            # Still consume the original chat to its actual terminal state.
            pending = ""
    if pending.strip() and not fenced and spoken < limit:
        yield re.sub(r"[*#_`>]", "", pending).strip()[:limit - spoken]
