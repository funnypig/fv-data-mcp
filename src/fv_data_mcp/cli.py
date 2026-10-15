from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import uvicorn

from fv_data_mcp.auth import Memberships, Principal
from fv_data_mcp.config import Settings
from fv_data_mcp.server import create_app
from fv_data_mcp.telemetry import configure


async def manage(args: argparse.Namespace, settings: Settings) -> None:
    """Execute operator-only membership administration or access-record backup.

    Args:
        args: Parsed command and its subject or backup-blob identifier.
        settings: Operator configuration with Azure storage endpoints.

    Raises:
        SystemExit: The backend, backup, or required endpoint is unsuitable.
        AzureError: An Azure operation fails; previously completed writes remain.
    """
    if settings.access_backend != "azure":
        raise SystemExit("Membership administration requires FV_ACCESS_BACKEND=azure.")
    members = Memberships(settings)
    try:
        if args.command in {"grant", "suspend"}:
            await members.set_access(
                Principal(settings.auth0_issuer, args.subject), args.command == "grant"
            )
            print(json.dumps({"operation": args.command, "status": "ok"}))
        elif args.command in {"backup-members", "restore-members"}:
            from azure.storage.blob.aio import BlobServiceClient

            if not settings.blob_endpoint:
                raise SystemExit("Set FV_BLOB_ENDPOINT to the configured backup storage endpoint.")
            if args.command == "restore-members":
                async with BlobServiceClient(
                    settings.blob_endpoint, credential=members.credential
                ) as blobs:
                    blob = blobs.get_blob_client("access-backups", args.blob)
                    properties = await blob.get_blob_properties()
                    if properties.size > 1_000_000:
                        raise SystemExit("Backup exceeds the supported size limit.")
                    download = await blob.download_blob()
                    records = json.loads(await download.readall())
                if not isinstance(records, list) or any(
                    not isinstance(record, dict)
                    or record.get("issuer") != settings.auth0_issuer
                    or not isinstance(record.get("subject"), str)
                    or not record["subject"]
                    for record in records
                ):
                    raise SystemExit("Invalid backup or issuer mismatch; no users were restored.")
                for record in records:
                    await members.set_access(Principal(record["issuer"], record["subject"]), False)
                print(json.dumps({"status": "ok", "restored_disabled": len(records)}))
                return
            records = [
                {
                    key: entity[key]
                    for key in ("PartitionKey", "RowKey", "issuer", "subject", "enabled")
                }
                async for entity in members.table.list_entities()
            ]
            name = datetime.now(UTC).strftime("members/%Y-%m-%dT%H-%M-%SZ.json")
            async with BlobServiceClient(
                settings.blob_endpoint, credential=members.credential
            ) as blobs:
                await blobs.get_blob_client("access-backups", name).upload_blob(
                    json.dumps(records), overwrite=False
                )
            print(json.dumps({"status": "ok", "blob": name, "records": len(records)}))
    finally:
        await members.close()


def main() -> None:
    """Parse CLI arguments, configure telemetry, and serve or administer the service."""
    parser = argparse.ArgumentParser(prog="fv-data-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    for name in ("grant", "suspend"):
        command = commands.add_parser(name)
        command.add_argument(
            "--subject", required=True, help="Exact Auth0 user_id/sub, not an email address"
        )
    commands.add_parser("backup-members")
    restore = commands.add_parser("restore-members")
    restore.add_argument(
        "--blob", required=True, help="Backup blob name; restored users are always disabled"
    )
    args = parser.parse_args()
    settings = Settings()
    providers = configure(settings)
    try:
        if args.command == "serve":
            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                proxy_headers=False,
                access_log=False,
                log_config=None,
                limit_concurrency=64,
                timeout_keep_alive=5,
            )
        else:
            asyncio.run(manage(args, settings))
    finally:
        for provider in providers:
            provider.shutdown()


if __name__ == "__main__":
    main()
