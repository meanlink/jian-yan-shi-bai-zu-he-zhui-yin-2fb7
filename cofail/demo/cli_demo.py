"""命令行演示客户端：在四个运行中的服务间驱动一轮完整联合分析。

用法：
  python3 -m cofail.demo.cli_demo
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .make_data import generate
from .run_demo import THRESHOLDS

HERE = os.path.dirname(__file__)
COORD = "http://127.0.0.1:8100"
LABS = {
    "lab-a": ("http://127.0.0.1:8101", "token-lab-a-secret"),
    "lab-b": ("http://127.0.0.1:8102", "token-lab-b-secret"),
    "lab-c": ("http://127.0.0.1:8103", "token-lab-c-secret"),
}
ADMIN = "investigator-demo-token"


def call(url, method="GET", token=None, admin=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin:
        headers["X-Admin-Token"] = admin
    req = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"请求失败 {url}: {e.read().decode()}")


def main():
    data = generate(os.path.join(HERE, "data"))
    created = call(COORD + "/api/admin/rounds", "POST", admin=ADMIN,
                   body={"manifest": data["manifest"],
                         "thresholds": THRESHOLDS})
    rid = created["round_id"]
    print(f"[协调端] 已冻结轮次 {rid}，成员 {created['frozen']['members']}")

    for lab, (base, token) in LABS.items():
        call(base + "/api/local-data", "POST", token=token, body={
            "round_id": rid, "manifest": data["manifest"],
            "thresholds": THRESHOLDS,
            "labels": data["per_lab"][lab]["labels"],
            "batch_ids": data["per_lab"][lab]["batch_ids"]})
        print(f"[{lab}] 私有数据已导入本室库（批号/标签不出室）")

    print("[参与端] 自动模式推进中…")
    for _ in range(60):
        st = call(f"{COORD}/api/rounds/{rid}/status")
        if st["state"] in ("completed", "void"):
            break
        time.sleep(0.5)
    if st["state"] != "completed":
        raise SystemExit(f"轮次未完成：{st['state']}")

    bf = st["briefing"]
    t = bf["totals"]
    print("\n===== 联合分析简报（仅三室合计）=====")
    print(f"样本分母 {t['samples']}（含全通过 {t['all_pass']}）")
    print(f"三室同败(共败) {t['common_fail']} ｜ 池化判败 {t['pooled_fail']}")
    print(f"简报可追溯 ID：{bf['briefing_id']} ｜ 清单指纹："
          f"{bf['manifest_fingerprint'][:16]}…")
    print("\n命中规则：")
    for r in bf["rules"]:
        if r["hit"]:
            print(f"  {r['cell']}: 分母 {r['denominator']}，"
                  f"共败率 {r['common_rate']:.2f}，lift {r['lift_common']}")


if __name__ == "__main__":
    main()
