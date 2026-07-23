from __future__ import annotations

import argparse
import json

from app.database import check_db, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="ProducersCenter backend maintenance")
    parser.add_argument("command", choices=("init-db", "check-db"))
    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
        print(json.dumps(check_db(), ensure_ascii=False))
        return

    if args.command == "check-db":
        print(json.dumps(check_db(), ensure_ascii=False))


if __name__ == "__main__":
    main()
