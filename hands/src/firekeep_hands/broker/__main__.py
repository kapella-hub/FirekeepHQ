"""`firekeep-hands-broker run | install-autostart | uninstall-autostart | status`.

The entry point exists so the broker is a process of its own. Note what is
absent and must stay absent: there is no `approve` sub-command. A human at a
terminal is not the human at the keyboard — a command anything with shell
access could run would undo the whole point of watching for real keystrokes.
"""
from __future__ import annotations

import argparse
import sys

from . import autostart
from .client import BrokerClient
from .server import run as run_broker


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="firekeep-hands-broker",
        description="Firekeep Hands approval broker — the local gate on protected steps.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the broker in the foreground (blocks)")
    sub.add_parser("install-autostart", help="start the broker at login, and now")
    sub.add_parser("uninstall-autostart", help="stop starting it at login, and stop it now")
    sub.add_parser("status", help="report whether the broker is running")
    args = parser.parse_args(argv)

    if args.command == "run":
        return run_broker(argv)

    if args.command == "install-autostart":
        try:
            autostart.install()
        except Exception as exc:  # noqa: BLE001 - the caller only needs the reason
            print(f"firekeep-hands-broker: could not install autostart: {exc}", file=sys.stderr)
            return 1
        print("firekeep-hands-broker: autostart installed and broker started")
        return 0

    if args.command == "uninstall-autostart":
        try:
            autostart.uninstall()
        except Exception as exc:  # noqa: BLE001
            print(f"firekeep-hands-broker: could not remove autostart: {exc}", file=sys.stderr)
            return 1
        print("firekeep-hands-broker: autostart removed and broker stopped")
        return 0

    client = BrokerClient.from_disk()
    health = client.health() if client is not None else None
    if health is None:
        print("firekeep-hands-broker: not running")
        return 1
    listeners = health.get("listeners") or {}
    print(
        f"firekeep-hands-broker: running on 127.0.0.1:{client.port} · "
        f"chord {health.get('chord', '?')} ({listeners.get('chord', '?')}) · "
        f"phone {listeners.get('phone', '?')} · {health.get('pending', 0)} pending"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
