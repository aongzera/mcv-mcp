"""Entry point: `mcv-mcp` runs the MCP server; `--selftest` verifies the setup."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def _selftest(full: bool) -> int:
    from .client import MCVClient
    from .config import load_config
    from .store import Store
    from .sync import Syncer

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()

    print(f"state dir : {config.state_dir}")
    print(f"username  : {config.username or '(not set)'}")
    if not config.has_credentials:
        print("\nMCV_USERNAME / MCV_PASSWORD are not set.")
        print("Copy .env.example to .env and fill in your Chula SSO credentials.")
        return 2

    store = Store(config.db_path)
    with MCVClient(config) as client:
        client.login()
        print("login     : ok")
        semesters = client.get_semesters()
        print(f"semesters : {', '.join(semesters) or '(none found)'}")

        result = Syncer(client, store, config).sync_once(full=full)
        print(f"sync      : {'ok' if result.get('ok') else 'FAILED'}")
        if not result.get("ok"):
            print(f"error     : {result.get('error')}")
        print(json.dumps(result.get("counts", {}), indent=2))

        for table in ("courses", "assignments", "materials", "announcements", "grades"):
            n = store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            print(f"{table:<13}: {n}")

    return 0 if result.get("ok") else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcv-mcp", description="MyCourseVille MCP server")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="log in, sync once, and print what was found, then exit",
    )
    parser.add_argument(
        "--full", action="store_true", help="with --selftest, sync every semester"
    )
    args = parser.parse_args()

    if args.selftest:
        sys.exit(_selftest(args.full))

    from .server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
