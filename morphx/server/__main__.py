"""Run the central server: python -m morphx.server"""

import argparse
import logging
from pathlib import Path

import uvicorn

from morphx.server.app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="morphx-server", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    parser.add_argument(
        "--db", default="data/server.sqlite3", help="SQLite file, kept across restarts"
    )
    parser.add_argument(
        "--demo", action="store_true", help="add buttons to the dashboard that simulate an outage"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.info("database %s", Path(args.db).resolve())
    uvicorn.run(create_app(args.db, demo=args.demo), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
