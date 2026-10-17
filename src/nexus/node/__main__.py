import argparse
import asyncio
import os
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
    args = parser.parse_args()
    try:
        config = load_config(args.config, NodeServiceConfig, {
            "NEXUS_NODE_DATA_DIR": "data_dir", "NEXUS_CORE_URL": "core_url",
        })
        local = NodeConfig.model_validate_json(config.model_dump_json(include=set(NodeConfig.model_fields)))
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
