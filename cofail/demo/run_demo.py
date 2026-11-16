"""三室端到端演示（不依赖服务进程，直接驱动应用对象 + 真实密封协议）。

用法：
  python -m cofail.demo.run_demo            # 正常全流程 + 异常场景演练
  python -m cofail.demo.run_demo --happy    # 只跑正常全流程
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

from ..common.protocol import ALL_LABS
from ..coordinator.app import CoordinatorApp
from ..lab.app import LabApp
from .make_data import generate

ROOT = os.path.dirname(os.path.dirname(__file__))
TOKENS = {lab: f"token-{lab}-secret" for lab in ALL_LABS}
THRESHOLDS = {
    "min_support": 6,
    "min_common_rate": 0.35,
    "min_pooled_rate": 0.4,
    "rule_attrs": ["reagent_lot", "instrument", "temperature_band"],
    "max_depth": 1,
}


def _build_stack(tmpdir: str, auto: bool = False):
    coord = CoordinatorApp(os.path.join(tmpdir, "coord.db"), dict(TOKENS))
    coord.tokens["__admin__"] = "admin"
    labs = {}
    for lab in ALL_LABS:
        cfg = {"lab_id": lab, "token": TOKENS[lab],
               "coordinator_url": "http://unused", "db_path":
               os.path.join(tmpdir, f"{lab}.db"), "auto": False}
        app = LabApp(cfg)
        # 直连，不走 HTTP
        app._post = lambda path, body, a=app: _coord_post(coord, a, path, body)
        app._get = lambda path, a=app: _coord_get(coord, a, path)
        labs[lab] = app
    return coord, labs


def _coord_post(coord: CoordinatorApp, lab: LabApp, path: str, body: dict):
    if path == "/api/packages":
        return coord.receive_package(lab.lab_id, body)
    if path.endswith("/exit"):
        rid = path.split("/")[3]
        return coord.exit_round(lab.lab_id, rid, body.get("reason", ""))
    raise RuntimeError(path)


def _coord_get(coord: CoordinatorApp, lab: LabApp, path: str):
    if path == "/api/rounds":
        return coord.list_rounds()
    if path.startswith("/api/rounds/") and path.endswith("/status"):
        return coord.status(path.split("/")[3])
    if "/envelopes/" in path:
        parts = path.split("/")
        return coord.envelopes_for(parts[3], parts[5], parts[6])
    raise RuntimeError(path)


def _drive_until_done(labs: dict, rid: str, max_ticks: int = 200):
    """模拟并发轮询：每 tick 每家各尝试推进一步，直到完成/作废。"""
    for _ in range(max_ticks):
        for lab in labs.values():
            lab.advance(rid)
        states = {lab.progress(rid)["coordinator_state"] for lab in labs.values()}
        assert len(states) == 1
        if states.pop() in ("completed", "void"):
            return
    raise RuntimeError("演示未能在限定步数内收敛")


def _import_private(labs: dict, rid: str, manifest: list[dict], per_lab: dict):
    for lab in ALL_LABS:
        labs[lab].import_local_data({
            "round_id": rid,
            "manifest": manifest,
            "thresholds": THRESHOLDS,
            "labels": per_lab[lab]["labels"],
            "batch_ids": per_lab[lab]["batch_ids"],
        })


def happy_path(tmpdir: str, data: dict) -> str:
    print("\n=== 场景 1：三室齐全的完整联合分析 ===")
    coord, labs = _build_stack(tmpdir)
    created = coord.create_round(data["manifest"], THRESHOLDS)
    rid = created["round_id"]
    print(f"轮次 {rid} 已冻结：成员 {created['frozen']['members']}，"
          f"样本 {len(data['manifest'])} 个，阈值 {THRESHOLDS['min_support']}/"
          f"{THRESHOLDS['min_common_rate']}/{THRESHOLDS['min_pooled_rate']}")
    _import_private(labs, rid, data["manifest"], data["per_lab"])
    _drive_until_done(labs, rid)

    status = coord.status(rid)
    assert status["state"] == "completed"
    bf = status["briefing"]
    t = bf["totals"]
    print(f"完成。三室合计：共败 {t['common_fail']}，"
          f"池化判败 {t['pooled_fail']}，全通过 {t['all_pass']}（仍在分母中）")
    hits = [r for r in bf["rules"] if r["hit"]]
    print(f"命中规则 {len(hits)} 条：")
    for r in hits:
        print(f"  - {r['cell']}: 分母 {r['denominator']}，"
              f"共败率 {r['common_rate']:.2f}，池化败率 {r['pooled_rate']:.2f}，"
              f"lift(共败) {r['lift_common']}")
    # 与明文事实核对
    n = len(data["manifest"])
    expect_common = sum(
        1 for i in range(n)
        if all(data["per_lab"][lab]["labels"][i] for lab in ALL_LABS))
    expect_allpass = sum(
        1 for i in range(n)
        if not any(data["per_lab"][lab]["labels"][i] for lab in ALL_LABS))
    expect_pooled = sum(sum(data["per_lab"][lab]["labels"]) for lab in ALL_LABS)
    assert t["common_fail"] == expect_common
    assert t["all_pass"] == expect_allpass
    assert t["pooled_fail"] == expect_pooled
    print("MPC 合计与明文核对一致 ✓（协调端全程未接触单室计数）")
    print(f"简报追溯 ID: {bf['briefing_id']}，"
          f"清单指纹 {bf['manifest_fingerprint'][:16]}…")
    return rid


def exit_scenario(tmpdir: str, data: dict):
    print("\n=== 场景 2：一室退出 → 整轮作废，晚包不复活 ===")
    coord, labs = _build_stack(tmpdir)
    rid = coord.create_round(data["manifest"], THRESHOLDS)["round_id"]
    _import_private(labs, rid, data["manifest"], data["per_lab"])
    labs["lab-a"].advance(rid)
    labs["lab-b"].advance(rid)          # 两家加入，第三家未动
    st = coord.status(rid)
    assert st["state"] == "frozen"
    print("仅两家加入时状态保持 frozen，不出任何结果 ✓")
    coord.exit_round("lab-c", rid, "设备故障退出")
    assert coord.status(rid)["state"] == "void"
    print("lab-c 退出 → 轮次作废 ✓")
    try:
        labs["lab-a"].advance(rid)
    except RuntimeError as e:
        print(f"作废后迟到的包被拒收：{e} ✓")
    try:
        coord.briefing_for_round(rid)
    except ValueError as e:
        print(f"作废轮次无简报：{e} ✓")


def reject_scenario(tmpdir: str, data: dict):
    print("\n=== 场景 3：同室异包 / 跨轮包 / 重试验证 ===")
    coord, labs = _build_stack(tmpdir)
    r1 = coord.create_round(data["manifest"], THRESHOLDS)["round_id"]
    r2 = coord.create_round(data["manifest"], THRESHOLDS)["round_id"]
    _import_private(labs, r1, data["manifest"], data["per_lab"])
    _import_private(labs, r2, data["manifest"], data["per_lab"])

    # 1) 正常加入 r1，然后用同内容重试（幂等，不重新生成密钥/份额）
    labs["lab-a"].advance(r1)
    again = labs["lab-a"].get_package(r1, "frozen")
    resp = coord.receive_package("lab-a", again)
    assert resp["status"] == "duplicate_ignored"
    print("同内容重试被识别为 duplicate_ignored，不重复计数 ✓")

    # 2) 同室异包：篡改已收包内容后再投，拒收
    tampered = json.loads(json.dumps(again))
    tampered["dh_pub"] += 1
    try:
        coord.receive_package("lab-a", tampered)
        raise AssertionError("同室异包应当被拒收")
    except ValueError as e:
        print(f"同室异包拒收：{e} ✓")

    # 3) 跨轮包：另一轮 r2 的 join 包谎称属于 r1，拒收
    labs["lab-a"].join_round(r2, coord.status(r2)["frozen"])
    foreign = json.loads(json.dumps(labs["lab-a"].get_package(r2, "frozen")))
    foreign["round_id"] = r1
    try:
        coord.receive_package("lab-a", foreign)
        raise AssertionError("跨轮包应当被拒收")
    except ValueError as e:
        print(f"跨轮包拒收：{e} ✓")


def resume_scenario(tmpdir: str, data: dict):
    print("\n=== 场景 4：中断恢复 —— 重启参与端后原轮继续，无重复计数 ===")
    coord, labs = _build_stack(tmpdir)
    rid = coord.create_round(data["manifest"], THRESHOLDS)["round_id"]
    _import_private(labs, rid, data["manifest"], data["per_lab"])
    labs["lab-a"].advance(rid)
    labs["lab-b"].advance(rid)
    labs["lab-c"].advance(rid)                 # 三家加入 → triple
    labs["lab-a"].advance(rid)                 # 发出三元组信封后“断电”
    before = coord.status(rid)["progress"][1]["received"]
    # 重新 new 一个 LabApp（同一 SQLite 文件）模拟进程重启
    cfg = {"lab_id": "lab-a", "token": TOKENS["lab-a"],
           "coordinator_url": "http://unused",
           "db_path": os.path.join(tmpdir, "lab-a.db"), "auto": False}
    reborn = LabApp(cfg)
    reborn._post = lambda path, body: _coord_post(coord, reborn, path, body)
    reborn._get = lambda path: _coord_get(coord, reborn, path)
    assert rid in reborn.states, "本地轮次状态已从 SQLite 恢复"
    reborn.advance(rid)                        # 应重放原包而非重新生成
    after = coord.status(rid)["progress"][1]["received"]
    assert before == after and len(after) == 1
    print("重启后状态从 SQLite 恢复，原包重放、计数无重复 ✓")
    labs["lab-a"] = reborn
    _drive_until_done(labs, rid)
    assert coord.status(rid)["state"] == "completed"
    print("恢复后整轮顺利完成 ✓")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--happy", action="store_true")
    args = ap.parse_args()
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    data = generate(data_dir)
    with tempfile.TemporaryDirectory() as tmp:
        happy_path(tmp, data)
        if not args.happy:
            exit_scenario(tmp, data)
            reject_scenario(tmp, data)
            resume_scenario(tmp, data)
    print("\n全部演示场景结束。")


if __name__ == "__main__":
    main()
