"""规则挖掘：在三室合计计数上按既有口径筛选规则。

口径（所有计数均为三室联合协议输出，协调端看不到任何单室计数）：
- 分母 D：单元内样本数，全通过样本仍计入分母。
- 共败数 C：同一批号在三室内均判败的样本数（标签 AND，由 MPC 计算）。
- 合计败数 F：三室判败次数之和（池化口径）。
- 共败率 = C / D；池化败率 = F / (3D)。
"""
from __future__ import annotations

import json
import secrets
import time

from .protocol import decode_count


def mine_rules(per_cell: dict, n_samples: int, thresholds: dict) -> list[dict]:
    """per_cell: {cell: {"D": int, "F": field-int, "C": field-int, "P": field-int}}。

    "__ALL__" 是样本层全集单元（每样本计一次），只用于总计/基线，不作为规则。
    """
    min_support = int(thresholds["min_support"])
    min_common_rate = float(thresholds["min_common_rate"])
    min_pooled_rate = float(thresholds["min_pooled_rate"])

    overall = per_cell["__ALL__"]
    total_c = decode_count(overall["C"], n_samples)
    total_f = decode_count(overall["F"], n_samples)
    base_common = total_c / n_samples if n_samples else 0.0
    base_pooled = total_f / (3 * n_samples) if n_samples else 0.0

    rules = []
    for cell, v in sorted(per_cell.items()):
        if cell == "__ALL__":
            continue
        d = v["D"]
        f = decode_count(v["F"], n_samples)
        c = decode_count(v["C"], n_samples)
        p = decode_count(v["P"], n_samples)
        if d < min_support:
            continue
        common_rate = c / d
        pooled_rate = f / (3 * d)
        hit = (common_rate >= min_common_rate
               and pooled_rate >= min_pooled_rate)
        rules.append({
            "cell": cell,
            "denominator": d,                 # 含全通过样本
            "common_fail": c,                 # 三室同败
            "pooled_fail": f,                 # 三室判败次数
            "all_pass": p,
            "common_rate": round(common_rate, 4),
            "pooled_rate": round(pooled_rate, 4),
            "lift_common": round(common_rate / base_common, 3)
            if base_common else None,
            "lift_pooled": round(pooled_rate / base_pooled, 3)
            if base_pooled else None,
            "hit": hit,
        })
    rules.sort(key=lambda r: (not r["hit"], -r["common_rate"], -r["denominator"]))
    return rules


def build_briefing(round_id: str, manifest_hash: str, thresholds: dict,
                   member_pubkeys: dict, rules: list[dict],
                   per_cell: dict, n_samples: int) -> dict:
    """生成可追溯简报：记录冻结指纹、成员公钥指纹与完整规则口径。"""
    overall = per_cell["__ALL__"]
    totals = {
        "samples": n_samples,
        "common_fail": decode_count(overall["C"], n_samples),
        "pooled_fail": decode_count(overall["F"], n_samples),
        "all_pass": decode_count(overall["P"], n_samples),
    }
    return {
        "briefing_id": "bf_" + secrets.token_hex(8),
        "round_id": round_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "manifest_fingerprint": manifest_hash,
        "thresholds": thresholds,
        "member_key_fingerprints": member_pubkeys,
        "totals": totals,
        "rules": rules,
    }


def briefing_trace_note(briefing: dict) -> str:
    """简报追溯说明（写入调查档案时使用）。"""
    return json.dumps({
        "briefing_id": briefing["briefing_id"],
        "round_id": briefing["round_id"],
        "manifest_fingerprint": briefing["manifest_fingerprint"],
        "member_key_fingerprints": briefing["member_key_fingerprints"],
        "rule_hits": [r["cell"] for r in briefing["rules"] if r["hit"]],
    }, ensure_ascii=False, indent=2)
