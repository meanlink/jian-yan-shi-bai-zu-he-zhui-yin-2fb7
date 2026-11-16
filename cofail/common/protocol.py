"""协议常量、状态机、单元（规则格）构造与 MPC 计算辅助函数。"""
from __future__ import annotations

from .crypto import FIELD_P, fadd, fmul, fsub

LAB_A, LAB_B, LAB_C = "lab-a", "lab-b", "lab-c"
ALL_LABS = [LAB_A, LAB_B, LAB_C]

# 轮次状态：创建即冻结成员/样本/阈值；按顺序推进；少一家永不推进
ST_FROZEN = "frozen"          # 等待三家加入
ST_TRIPLE = "triple"          # Beaver 三元组分发（密封）
ST_INPUT = "input"            # 输入分享（密封）
ST_MULT = "mult"              # 乘法（共败 AND，密封）
ST_AGGREGATE = "aggregate"    # 掩码计数上报
ST_COMPLETED = "completed"
ST_VOID = "void"

STEPS_ORDER = [ST_FROZEN, ST_TRIPLE, ST_INPUT, ST_MULT, ST_AGGREGATE,
               ST_COMPLETED]
# 每一步期望收到的包类型
STEP_PACKAGE_KIND = {
    ST_FROZEN: "join",
    ST_TRIPLE: "triple",
    ST_INPUT: "input",
    ST_MULT: "mult",
    ST_AGGREGATE: "aggregate",
}
# 密封步骤：协调端只见密文信封
SEALED_STEPS = {ST_TRIPLE, ST_INPUT, ST_MULT}

TERMINAL_STATES = {ST_COMPLETED, ST_VOID}


def expected_kind(state: str) -> str | None:
    return STEP_PACKAGE_KIND.get(state)


def next_state(state: str) -> str:
    return {
        ST_FROZEN: ST_TRIPLE,
        ST_TRIPLE: ST_INPUT,
        ST_INPUT: ST_MULT,
        ST_MULT: ST_AGGREGATE,
        ST_AGGREGATE: ST_COMPLETED,
    }[state]


# ---------- 规则单元 ----------
def cell_key(attrs: dict) -> str:
    """规则单元的规范化键（样本公开属性组合）。"""
    return "|".join(f"{k}={attrs[k]}" for k in sorted(attrs))


def _subsets(keys: tuple, max_depth: int) -> list[tuple]:
    out: list[tuple] = []
    for mask in range(1, 1 << len(keys)):
        sel = tuple(k for i, k in enumerate(keys) if mask & (1 << i))
        if len(sel) <= max_depth:
            out.append(sel)
    return out


def build_cells(manifest: list[dict], rule_attrs: list[str],
                max_depth: int) -> list[str]:
    """由冻结样本清单生成规范化、有序的全部规则单元（含空集以外的组合）。"""
    cells: set[str] = set()
    attr_keys = tuple(sorted(rule_attrs))
    for sel in _subsets(attr_keys, max_depth):
        for s in manifest:
            cells.add(cell_key({k: s["attrs"][k] for k in sel}))
    return sorted(cells)


def sample_cells(attrs: dict, rule_attrs: list[str],
                 max_depth: int) -> list[str]:
    attr_keys = tuple(sorted(k for k in rule_attrs if k in attrs))
    out = []
    for sel in _subsets(attr_keys, max_depth):
        out.append(cell_key({k: attrs[k] for k in sel}))
    return out


# ---------- Beaver 三元组重组与乘法 ----------
def reconstruct_triples(shares: list[list[tuple[int, int, int]]]
                        ) -> list[tuple[int, int, int]]:
    """汇总三家发来的三元组分享，得到本端持有的 (a_i,b_i,c_i) 列表。"""
    t = len(shares[0])
    out = []
    for j in range(t):
        a = b = c = 0
        for piece in shares:
            aj, bj, cj = piece[j]
            a = fadd(a, aj)
            b = fadd(b, bj)
            c = fadd(c, cj)
        out.append((a, b, c))
    return out


def beaver_open_local(x: int, a: int, y: int, b: int) -> tuple[int, int]:
    """本端计算开放值 d_i=x_i-a_i、e_i=y_i-b_i（公开后求和还原 d、e）。"""
    return fsub(x, a), fsub(y, b)


def beaver_result_share(c_i: int, d: int, e: int,
                        x_i: int, y_i: int, idx: int) -> int:
    """由开放后的 d、e 计算 (x*y)_i = c_i + d*y_i + e*x_i - idx*d*e。

    idx 为参与端编号（0/1/2），保证三家之和恰为 x*y。
    """
    term = fadd(c_i, fmul(d, y_i))
    term = fadd(term, fmul(e, x_i))
    if idx:
        term = fsub(term, fmul(idx, fmul(d, e)))
    return term


def field_sum(parts: list[int]) -> int:
    r = 0
    for v in parts:
        r = fadd(r, v)
    return r


def decode_count(v: int, n_samples: int) -> int:
    """域元素还原为非负整数计数（真实计数远小于 p，取最小非负代表元）。"""
    return v % FIELD_P
