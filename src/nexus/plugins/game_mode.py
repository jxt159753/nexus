"""Desktop resource preference; Core still authorizes and journals plugin calls.

The desktop shell owns detection/media/notifications. Both explicit user controls
write one atomic preference, so the last completed selection wins. Its status is
an observation, never an authorization or proof that an emulator has stopped.
"""
import os
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import StrictBool, StrictInt, StrictStr

from nexus.contracts.base import StrictContract
from nexus.node.process import ExecutionStopped


class Preference(StrictContract):
    mode: Literal['auto', 'on', 'off']
    request_id: UUID


class DesktopStatus(Preference):
    active: StrictBool
    reason: StrictStr
    updated_at_ms: StrictInt
    error: StrictStr = ''


def preference(directory: Path) -> Preference | None:
    path = directory / 'game-mode.json'
    return Preference.model_validate_json(path.read_bytes()) if path.exists() else None


def status(directory: Path) -> dict:
    selected = preference(directory)
    path = directory / 'game-mode-status.json'
    observed = DesktopStatus.model_validate_json(path.read_bytes()) if path.exists() else None
    available = bool(observed and 0 <= time.time()*1000-observed.updated_at_ms < 10000)
    applied = bool(available and selected and selected.request_id == observed.request_id and not observed.error)
    return {'mode': selected.mode if selected else 'auto',
            'active': observed.active if available else None,
            'available': available, 'applied': applied,
            'reason': observed.reason if available else 'desktop_unavailable',
            'error': observed.error if observed else '',
            'verification': 'desktop_reported' if applied else 'not_applied'}


def blocked(directory: Path) -> bool:
    selected = preference(directory)
    if selected is None or selected.mode == 'off':
        return False
    if selected.mode == 'on':
        return True
    path = directory / 'game-mode-status.json'
    # Preserve an active veto until the shell observes the end of full screen.
    # Restarting a Node must not reopen a phone during a game.
    return path.exists() and DesktopStatus.model_validate_json(path.read_bytes()).active


def execute(directory: Path, name: str, value: dict, deadline: float, cancel: threading.Event) -> dict:
    before = status(directory)
    if name == 'status':
        return before
    if name != 'set_mode':
        raise ValueError('unknown game mode operation')
    if not before['available']:
        raise ValueError('请先打开支持游戏模式的 NEXUS Windows 客户端')
    selected = Preference(mode=value['mode'], request_id=uuid4())
    temporary = directory / f'.game-mode-{selected.request_id}.tmp'
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(selected.model_dump_json())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / 'game-mode.json')
    finally:
        temporary.unlink(missing_ok=True)
    until = min(deadline, time.monotonic()+3)
    while time.monotonic() < until:
        if cancel.wait(.05):
            raise ExecutionStopped('cancelled')
        latest = preference(directory)
        if latest.request_id != selected.request_id:
            return {**status(directory), 'notice': '选择已被另一项用户操作更新，请以当前状态为准。'}
        result = status(directory)
        if result['applied']:
            return {**result, 'notice': '桌面已应用；虚拟手机若正在运行，将正常关闭并保留数据，实际停止需查询虚拟手机状态。'}
    return {**status(directory), 'notice': '已保存选择，正在等待桌面应用；尚不能确认所有功能已停止。'}
