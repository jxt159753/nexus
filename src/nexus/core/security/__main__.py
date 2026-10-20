"""Offline management: credentials enter via environment, never command arguments/output."""

import argparse
import asyncio
import os
import sqlite3
from uuid import UUID

from pydantic import SecretStr

from nexus.core.security.service import SecurityService
from nexus.core.storage.database import Database
from nexus.settings import load_settings
from nexus.contracts.tools import TOOL_ARGUMENT_MODELS


async def manage(args: argparse.Namespace) -> None:
    database = Database(load_settings().data_dir / "core.sqlite3")
    await database.initialize()
    security = SecurityService(database)
    if args.operation in {"register-owner", "register-node", "rotate"}:
        token = SecretStr(os.environ.get("NEXUS_ENROLL_TOKEN", ""))
        if args.operation == "rotate":
            await security.rotate_credential(args.identity_id, token)
        else:
            await security.register_identity(args.identity_id, "owner" if args.operation == "register-owner" else "node", token)
    elif args.operation == "revoke":
        await security.revoke_identity(args.identity_id)
    else:
        await security.set_grant(args.identity_id, args.tool, root_id=args.root_id, enabled=not args.disable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("register-owner", "register-node", "rotate", "revoke", "grant"):
        command = commands.add_parser(operation)
        command.add_argument("identity_id", type=UUID)
        if operation == "grant":
            command.add_argument("--tool", choices=tuple(key.split("@")[0] for key in TOOL_ARGUMENT_MODELS), required=True)
            command.add_argument("--root-id", default="")
            command.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(manage(args))
    except (ValueError, PermissionError, LookupError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Management failed ({type(exc).__name__}); check local configuration.\n")
    print(f"Completed {args.operation}: {args.identity_id}")


if __name__ == "__main__":
    main()
