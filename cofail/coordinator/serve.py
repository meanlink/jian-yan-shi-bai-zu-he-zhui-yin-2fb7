"""协调端启动入口：python -m cofail.coordinator.serve"""
from __future__ import annotations

import argparse
import json
import os

from .app import serve

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "demo", "config.json"))
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    serve({
        "port": cfg["coordinator"]["port"],
        "db_path": os.path.join(ROOT, "data", "coordinator.sqlite3"),
        "lab_tokens": {x["lab_id"]: x["token"] for x in cfg["labs"]},
        "admin_token": cfg["coordinator"]["admin_token"],
    })


if __name__ == "__main__":
    main()
