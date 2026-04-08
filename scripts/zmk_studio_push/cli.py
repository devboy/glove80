"""Command-line interface for zmk-studio-push."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import flows


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zmk_studio_push",
        description="Push/pull ZMK keymaps at runtime via Studio RPC.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # push
    push = subparsers.add_parser(
        "push",
        help="Parse a keymap file and push all bindings to the device.",
    )
    push.add_argument("--keymap", type=Path, required=True)
    push.add_argument("--backup-dir", type=Path, default=Path("backups"))
    push.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default="auto",
        help="Transport preference (default: auto-detect USB then BLE).",
    )
    push.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff without applying any changes.",
    )
    push.add_argument(
        "--expected-key-count",
        type=int,
        default=flows.TOUCAN_KEY_COUNT,
        help="Expected number of key positions per layer (default: 42 for Toucan).",
    )
    push.add_argument(
        "--keyboard",
        default="toucan",
        help="Keyboard name for backup filenames.",
    )
    push.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite unsaved changes pending on the device "
            "(use with care — usually means in-flight Studio web UI edits)."
        ),
    )

    # pull
    pull = subparsers.add_parser(
        "pull",
        help="Fetch the current keymap and write a JSON snapshot.",
    )
    pull.add_argument("--output-dir", type=Path, default=Path("backups"))
    pull.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default="auto",
    )
    pull.add_argument("--keyboard", default="toucan")

    # restore
    restore = subparsers.add_parser(
        "restore",
        help="Push a previously-pulled JSON snapshot back to the device.",
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--backup-dir", type=Path, default=Path("backups"))
    restore.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default="auto",
    )
    restore.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be restored without applying.",
    )
    restore.add_argument("--keyboard", default="toucan")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.command == "push":
            asyncio.run(
                flows.push(
                    keymap_path=args.keymap,
                    backup_dir=args.backup_dir,
                    transport=args.transport,
                    dry_run=args.dry_run,
                    keyboard=args.keyboard,
                    expected_key_count=args.expected_key_count,
                    force=args.force,
                )
            )
        elif args.command == "pull":
            asyncio.run(
                flows.pull(
                    output_dir=args.output_dir,
                    transport=args.transport,
                    keyboard=args.keyboard,
                )
            )
        elif args.command == "restore":
            asyncio.run(
                flows.restore(
                    backup_path=args.backup,
                    backup_dir=args.backup_dir,
                    transport=args.transport,
                    dry_run=args.dry_run,
                    keyboard=args.keyboard,
                )
            )
        else:  # pragma: no cover
            parser.error(f"Unknown command: {args.command}")
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.error("%s", exc)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
