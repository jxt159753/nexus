import argparse
from pathlib import Path

import uvicorn

from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS Core identity and Node Link")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config, CoreConfig, {
            "NEXUS_DATA_DIR": "data_dir", "NEXUS_HOST": "host", "NEXUS_PORT": "port",
        })
        app = create_app(config)
        server = uvicorn.Config(app, host=config.host, port=config.port, workers=1,
            ws="websockets-sansio", ws_max_size=config.max_message_bytes,
            ws_max_queue=16, ws_per_message_deflate=False,
            ssl_certfile=None if config.tls_cert is None else str(config.tls_cert),
            ssl_keyfile=None if config.tls_key is None else str(config.tls_key),
            proxy_headers=False, access_log=False, log_level="warning",
            limit_concurrency=64, timeout_graceful_shutdown=5)
        uvicorn.Server(server).run()
    except (OSError, ValueError):
        raise SystemExit("Core configuration or startup failed") from None


if __name__ == "__main__":
    main()
