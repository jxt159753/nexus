"""Test-only commands and blocking registry injection; never shipped with Node."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nexus.contracts.commands import command_digest
from nexus.contracts.messages import ListDirectoryExecute, SystemInfoExecute
from nexus.contracts.task import ListDirectoryArguments, SystemInfoArguments
from nexus.node.config import NodeConfig


def configuration(path, **changes):
    return NodeConfig(node_id=changes.pop("node_id", uuid4()), data_dir=path,
                      allowed_tools=changes.pop("allowed_tools", frozenset({"system.info@1", "filesystem.list@1"})), **changes)


def command(config, *, directory=False, **changes):
    values = dict(session_id=uuid4(), task_id=uuid4(), attempt_id=uuid4(),
                  command_digest="a" * 64, grant_id=uuid4(),
                  expires_at=datetime.now(UTC) + timedelta(minutes=1),
                  execution_timeout_seconds=10)
    values.update(changes)
    if directory:
        item = ListDirectoryExecute(tool="filesystem.list", tool_version=1,
                                    arguments=ListDirectoryArguments(root_id="workspace", limit=100), **values)
    else:
        item = SystemInfoExecute(tool="system.info", tool_version=1, arguments=SystemInfoArguments(), **values)
    return sign(item, config.node_id)


def sign(item, node_id):
    return item.model_copy(update={"command_digest": command_digest(item, node_id)})


def blocking_worker(marker):
    # Patch only the test process's registry, after the production worker gate.
    return f'''
import os, time
from pathlib import Path
from nexus.node.tools import registry
def block(command, config):
    Path({json.dumps(str(marker))}).write_text(str(os.getpid()))
    time.sleep(60)
registry.execute_tool = block
from nexus.node.worker import main
raise SystemExit(main())
'''
