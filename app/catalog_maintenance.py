"""Stopped-service catalog recovery commands for the single-merchant pilot.

This is intentionally an operator CLI, not an API endpoint.  It restores only
catalog rows from a validated catalog snapshot and never copies or rewinds the
SQLite database, so order history and approved commercial snapshots survive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import database


def _existing_database(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise argparse.ArgumentTypeError("--db must name an existing SQLite database file")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List or restore catalog-only pilot recovery snapshots."
    )
    parser.add_argument("--db", required=True, type=_existing_database, help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-backups", help="List validated catalog recovery points")
    restore = commands.add_parser("restore", help="Restore one catalog recovery point")
    restore.add_argument("--backup-id", required=True, help="Backup ID shown by list-backups")
    restore.add_argument("--actor", required=True, help="Named operator performing the restore")
    restore.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="Required acknowledgement that the one-replica application is stopped",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path = args.db
    try:
        if args.command == "list-backups":
            rows = database.list_catalog_backups(db_path)
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        if not args.confirm_service_stopped:
            raise ValueError(
                "Refusing restore: stop the one-replica application first, then pass "
                "--confirm-service-stopped. Restart it after restore so its catalog matcher reloads."
            )
        # Bring a legitimate older pilot DB through its forward migrations
        # while the service is stopped before touching recovery metadata.
        database.initialize_runtime_database(db_path)
        result = database.restore_catalog_backup(
            args.backup_id, actor=args.actor, db_path=db_path
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Catalog recovery command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
