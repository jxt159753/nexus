import argparse
import asyncio
import os
import json
from pathlib import Path

from pydantic import SecretStr

from nexus.node.config import NodeConfig
from nexus.node.link import NodeClient
from nexus.node.runtime import NodeRuntime
from nexus.node.service_config import NodeServiceConfig
from nexus.service_config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS Windows Node")
    parser.add_argument("--config", type=Path, required=True)
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
            asyncio.run(client.run(asyncio.Event()))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        raise SystemExit("Node configuration or startup failed") from None


if __name__ == "__main__":
    main()
