"""参与端启动入口：python -m cofail.lab.serve <lab-a|lab-b|lab-c> [--no-auto]"""
from __future__ import annotations

import argparse
import json
import os

from .app import serve

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lab_id", choices=["lab-a", "lab-b", "lab-c"])
    ap.add_argument("--no-auto", action="store_true", help="关闭自动推进")
    ap.add_argument("--config", default=os.path.join(ROOT, "demo", "config.json"))
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    lab_cfg = next(x for x in cfg["labs"] if x["lab_id"] == args.lab_id)
    lab_cfg["coordinator_url"] = cfg["coordinator"]["url"]
    lab_cfg["auto"] = not args.no_auto
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    lab_cfg["db_path"] = os.path.join(
        ROOT, "data", f"{args.lab_id}.sqlite3")
    serve(lab_cfg)


if __name__ == "__main__":
    main()
