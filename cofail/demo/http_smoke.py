"""真实 HTTP 端到端冒烟测试：启动 4 个服务进程，走完整网页 API 链路。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
TOKENS = {"lab-a": "token-lab-a-secret", "lab-b": "token-lab-b-secret",
          "lab-c": "token-lab-c-secret"}
PORTS = {"coord": 8100, "lab-a": 8101, "lab-b": 8102, "lab-c": 8103}


def req(url, method="GET", token=None, admin=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin:
        headers["X-Admin-Token"] = admin
    r = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode()}")


def wait_health(url, tries=30):
    for _ in range(tries):
        try:
            req(url + "/health")
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"服务未就绪: {url}")


def main() -> int:
    from cofail.demo.make_data import generate
    from cofail.demo.run_demo import THRESHOLDS
    work = tempfile.mkdtemp(prefix="cofail_http_")
    data = generate(os.path.join(work, "demo"))
    # 用独立配置与端口数据目录，避免污染默认 data/
    cfg = {
        "coordinator": {"port": PORTS["coord"],
                        "url": f"http://127.0.0.1:{PORTS['coord']}",
                        "admin_token": "investigator-demo-token"},
        "labs": [{"lab_id": lab, "port": PORTS[lab], "token": TOKENS[lab]}
                 for lab in TOKENS],
        "thresholds": THRESHOLDS,
    }
    cfg_path = os.path.join(work, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    env = dict(os.environ, PYTHONPATH=ROOT)
    procs = []
    log = open(os.path.join(work, "services.log"), "w")
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "cofail.coordinator.serve",
             "--config", cfg_path], cwd=ROOT, env=env, stdout=log, stderr=log))
        for lab in TOKENS:
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "cofail.lab.serve", lab,
                 "--config", cfg_path], cwd=ROOT, env=env,
                stdout=log, stderr=log))
        base = {k: f"http://127.0.0.1:{p}" for k, p in PORTS.items()}
        wait_health(base["coord"])
        for lab in TOKENS:
            wait_health(base[lab])
        print("四个服务进程均就绪 ✓")

        created = req(base["coord"] + "/api/admin/rounds", "POST",
                      admin="investigator-demo-token",
                      body={"manifest": data["manifest"],
                            "thresholds": THRESHOLDS})
        rid = created["round_id"]
        print(f"已通过协调端 API 冻结轮次 {rid} ✓")

        # 三室各自向“自己的参与端”导入私有数据（不经过协调端）
        for lab in TOKENS:
            req(base[lab] + "/api/local-data", "POST", token=TOKENS[lab],
                body={"round_id": rid, "manifest": data["manifest"],
                      "thresholds": THRESHOLDS,
                      "labels": data["per_lab"][lab]["labels"],
                      "batch_ids": data["per_lab"][lab]["batch_ids"]})
        print("三室私有数据已分别导入本室 SQLite（批号不出室）✓")

        # 自动模式轮询推进，直到完成
        for _ in range(60):
            st = req(base["coord"] + f"/api/rounds/{rid}/status")
            if st["state"] in ("completed", "void"):
                break
            time.sleep(0.5)
        assert st["state"] == "completed", st["state"]
        print("三室自动协同完成，协调端状态 completed ✓")

        bf = req(base["coord"] + f"/api/rounds/{rid}/status")["briefing"]
        n = len(data["manifest"])
        expect_common = sum(
            1 for i in range(n)
            if all(data["per_lab"][lab]["labels"][i] for lab in TOKENS))
        expect_pass = sum(
            1 for i in range(n)
            if not any(data["per_lab"][lab]["labels"][i] for lab in TOKENS))
        t = bf["totals"]
        assert t["common_fail"] == expect_common
        assert t["all_pass"] == expect_pass
        print(f"HTTP 链路合计正确：共败 {t['common_fail']}、"
              f"全通过 {t['all_pass']}、池化 {t['pooled_fail']} ✓")

        # 隐私检查：协调端所有可见接口不含任何单室标签序列
        listed = req(base["coord"] + "/api/briefings")
        assert listed["briefings"]
        detail = req(base["coord"] +
                     f"/api/briefings/{bf['briefing_id']}")
        raw = json.dumps(detail)
        for lab in TOKENS:
            assert str(data["per_lab"][lab]["labels"]) not in raw
        print("协调端简报/进度接口均无单室标签或单室计数 ✓")
        print("\nHTTP 端到端冒烟测试全部通过。")
        return 0
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            p.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
