import argparse
import asyncio
import os
import json
import sys
import threading
from pathlib import Path

from pydantic import SecretStr

from nexus.node.config import NodeConfig
from nexus.node.link import NodeClient
from nexus.node.runtime import NodeRuntime
from nexus.node.service_config import NodeServiceConfig
from nexus.service_config import load_config


async def run_client(client, parent_stdin=False):
    stop = asyncio.Event()
    if parent_stdin:
        loop = asyncio.get_running_loop()
        def parent_closed():
            # This inherited private pipe can only request shutdown, never work.
            while line := sys.stdin.buffer.readline(128):
                if line.strip() == b'shutdown':
                    break
            if not loop.is_closed():
                loop.call_soon_threadsafe(stop.set)
        threading.Thread(target=parent_closed, daemon=True).start()
    await client.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS Windows Node")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--parent-stdin", action="store_true", help="Stop cleanly when the desktop parent requests shutdown or closes its pipe")
    parser.add_argument("--print-policy-digests", action="store_true", help="Print configured command/UI policy digests without starting Node")
    args = parser.parse_args()
    try:
        config = load_config(args.config, NodeServiceConfig, {
            "NEXUS_NODE_DATA_DIR": "data_dir", "NEXUS_CORE_URL": "core_url",
        })
        local = NodeConfig.model_validate_json(config.model_dump_json(include=set(NodeConfig.model_fields)))
        if args.print_policy_digests:
            print(json.dumps({"command_digests":{key:value.digest() for key,value in local.commands.items()},"interaction_digests":{key:value.digest() for key,value in local.interaction_targets.items()},'plugin_digest':local.plugin_digest()},sort_keys=True))
            return
        token = SecretStr(os.environ.get("NEXUS_NODE_TOKEN", ""))
        with NodeRuntime(local) as runtime:
            client = NodeClient(config, runtime, token)
            asyncio.run(run_client(client, args.parent_stdin))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        raise SystemExit("Node configuration or startup failed") from None


if __name__ == "__main__":
    main()
