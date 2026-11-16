"""三室演示数据生成（确定性，便于复现）。

公开部分（可进冻结清单）：sample_id + 三个公开属性。
私有部分（只写入各室自己的 SQLite / 数据文件）：批号 batch_id 与逐批判败标签。
共败规则埋点：
  R1 试剂批次 R-7  → 三室高概率同败
  R2 仪器 INSP-2   → 三室高概率同败
  R3 高温带 T-H    → 较弱共败信号
其余样本以低噪声背景生成，全通过样本仍然存在并计入分母。
"""
from __future__ import annotations

import json
import os
import random

REAGENTS = ["R-1", "R-3", "R-5", "R-7", "R-9"]
INSTRUMENTS = ["INSP-1", "INSP-2", "INSP-3"]
TEMP_BANDS = ["T-L", "T-M", "T-H"]
LABS = ["lab-a", "lab-b", "lab-c"]

N_SAMPLES = 60
SEED = 20261116


def _label(rng: random.Random, attrs: dict, lab_idx: int) -> int:
    base = 0.05
    if attrs["reagent_lot"] == "R-7":
        base = 0.80
    if attrs["instrument"] == "INSP-2":
        base = max(base, 0.65)
    if attrs["temperature_band"] == "T-H":
        base = max(base, 0.40)
    # 各室轻微系统性差异，但不破坏联合信号
    base += [0.0, 0.03, -0.03][lab_idx]
    return 1 if rng.random() < min(base, 0.97) else 0


def generate(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(SEED)
    manifest = []
    per_lab = {lab: {"batch_ids": [], "labels": []} for lab in LABS}
    for i in range(N_SAMPLES):
        attrs = {
            "reagent_lot": rng.choice(REAGENTS),
            "instrument": rng.choice(INSTRUMENTS),
            "temperature_band": rng.choice(TEMP_BANDS),
        }
        sid = f"S{i+1:04d}"
        manifest.append({"sample_id": sid, "attrs": attrs})
        for k, lab in enumerate(LABS):
            per_lab[lab]["batch_ids"].append(f"{lab[-1].upper()}-B{i+1:04d}")
            per_lab[lab]["labels"].append(_label(rng, attrs, k))

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    for lab, data in per_lab.items():
        with open(os.path.join(out_dir, f"{lab}.private.json"), "w") as f:
            json.dump({"lab_id": lab, **data}, f, ensure_ascii=False, indent=2)

    # 控制台只输出聚合层面的事实核对，不导出单室明细
    common = sum(
        1 for i in range(N_SAMPLES)
        if all(per_lab[lab]["labels"][i] for lab in LABS))
    allpass = sum(
        1 for i in range(N_SAMPLES)
        if not any(per_lab[lab]["labels"][i] for lab in LABS))
    pooled = sum(sum(per_lab[lab]["labels"]) for lab in LABS)
    print(f"演示数据已生成: {N_SAMPLES} 个样本")
    print(f"  三室同败(共败) {common} | 三室全通过 {allpass} |"
          f" 池化判败次数 {pooled}（分母始终为 {N_SAMPLES}）")
    return {"manifest": manifest, "per_lab": per_lab}


if __name__ == "__main__":
    here = os.path.dirname(__file__)
    generate(os.path.join(here, "data"))
