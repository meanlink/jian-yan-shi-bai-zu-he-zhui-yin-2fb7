"""参与端本地 MPC 逻辑（批号与逐批标签不出本室）。

协议每一步的明文只存在于参与端本地；跨室数据一律用成对密钥密封，
协调端仅能转发密文信封。

密码结构（素数域 p=2^61-1 上的加法秘密分享）：
  1. TRIPLE  预生成“三输入 AND 相关随机量”：随机比特 a,b,c 及其
     ab/ac/bc/abc 的三份加法分享（生成者本地明文计算，无需事后修正）。
  2. INPUT   本室 0/1 标签切三份分享密封分发，批号本身不出室。
  3. MULT    每个样本做两个三输入 AND：xyz（共败）与 (1-x)(1-y)(1-z)
     （全通过），仅需一轮密封的开放值交换。
  4. AGGREGATE 本端按规则单元汇总份额；池化败数用成对掩码上报。
单份秘密分享在域上均匀随机；掩码三家求和抵消 —— 协调端只能还原合计。
"""
from __future__ import annotations

import json

from ..common.crypto import (
    FIELD_P, dh_keypair, dh_shared, fadd, fmul, frand, fsub,
    open_envelope, pair_keys, seal, sha256_hex,
)
from ..common.protocol import (
    ALL_LABS, ST_AGGREGATE, ST_INPUT, ST_MULT, ST_TRIPLE,
    field_sum, sample_cells,
)

# 每个样本的三输入 AND 门数：共败 xyz、全通过 (1-x)(1-y)(1-z)
GATES_PER_SAMPLE = 2
# 相关随机量包含的 7 个量：a, b, c, ab, ac, bc, abc
CORR_FIELDS = 7


# ---------- 轮次初始化 ----------
def round_commitment(dh_priv: int, round_id: str,
                     manifest_fingerprint: str, lab_id: str) -> dict:
    """Schnorr 式知识证明：证明 dh_pub 的持有者对本轮冻结指纹负责。

    协调端只凭 dh_pub 与证明即可验证，私钥不离开参与端；
    挑战 e 绑定 round_id 与清单指纹，跨轮改包无法通过验证。
    """
    import hashlib
    from ..common.crypto import MODP_G, MODP_P
    k = frand()
    r = pow(MODP_G, k, MODP_P)
    e = int(hashlib.sha256(
        f"{round_id}|{manifest_fingerprint}|{lab_id}|{r}".encode()
    ).hexdigest(), 16) % MODP_P
    s = (k - dh_priv * e) % (MODP_P - 1)
    return {"r": r, "s": s, "e": e}


def verify_commitment(pkg: dict) -> bool:
    """协调端独立验证 join 包的 Schnorr 承诺（无需任何私钥）。"""
    import hashlib
    from ..common.crypto import MODP_G, MODP_P
    try:
        com = pkg["commitment"]
        r, s, e = com["r"], com["s"], com["e"]
        expect_e = int(hashlib.sha256(
            f"{pkg['round_id']}|{pkg['manifest_fingerprint']}|"
            f"{pkg['lab_id']}|{r}".encode()).hexdigest(), 16) % MODP_P
        if e != expect_e:
            return False
        # g^s · pub^e == r
        lhs = (pow(MODP_G, s, MODP_P) *
               pow(pkg["dh_pub"], e, MODP_P)) % MODP_P
        return lhs == r
    except (KeyError, TypeError, ValueError):
        return False


def init_round_state(round_id: str, lab_id: str, peers: list[str],
                     n_samples: int) -> dict:
    priv, pub = dh_keypair()
    return {
        "round_id": round_id,
        "lab_id": lab_id,
        "peers": peers,
        "n_samples": n_samples,
        "dh_priv": priv,
        "dh_pub": pub,
        "pair_keys": {},          # peer -> [enc_hex, mac_hex]
        "done_steps": [],
    }


def derive_pair_keys(state: dict, peer_pubkeys: dict) -> None:
    """收齐三家 DH 公钥后派生与每个对端独立的成对密钥（协调端无法获知）。"""
    for peer in state["peers"]:
        shared = dh_shared(state["dh_priv"], peer_pubkeys[peer])
        enc, mac = pair_keys(shared, state["round_id"], state["lab_id"], peer)
        state["pair_keys"][peer] = [enc.hex(), mac.hex()]


def _keys(state: dict, peer: str) -> tuple[bytes, bytes]:
    enc, mac = state["pair_keys"][peer]
    return bytes.fromhex(enc), bytes.fromhex(mac)


def _aad(round_id: str, step: str, frm: str, to: str) -> bytes:
    return f"{round_id}:{step}:{frm}->{to}".encode()


def seal_for(state: dict, step: str, to: str, plaintext: bytes) -> dict:
    enc, mac = _keys(state, to)
    return seal(enc, mac, _aad(state["round_id"], step,
                               state["lab_id"], to), plaintext)


def open_from(state: dict, step: str, frm: str, env: dict) -> bytes:
    enc, mac = _keys(state, frm)
    return open_envelope(enc, mac,
                         _aad(state["round_id"], step, frm,
                              state["lab_id"]), env)


def _share3(value: int, my_idx: int, peers: list[str]
            ) -> tuple[int, dict[str, int]]:
    """切成三份加法分享，parts 的下标对应全局实验室编号（ALL_LABS）。"""
    s0, s1 = frand(), frand()
    parts = [s0, s1, fsub(value, fadd(s0, s1))]
    out = {peer: parts[ALL_LABS.index(peer)] for peer in peers}
    return parts[my_idx], out


# ---------- 步骤 1：三输入 AND 相关随机量 ----------
def triple_step(state: dict) -> dict:
    """门 g 的相关随机量固定由 g%3 号实验室生成；本端只生成自己负责的门。

    每个门 7 个量（a,b,c,ab,ac,bc,abc），生成者本地明文计算后切三份，
    三份分享分别自留与密封发出。
    """
    n = state["n_samples"]
    my_idx = ALL_LABS.index(state["lab_id"])
    peers = state["peers"]
    n_gates = GATES_PER_SAMPLE * n
    my_corr, peer_payload = [], {p: [] for p in peers}
    state["gate_owner"] = [g % 3 for g in range(n_gates)]
    for g in range(n_gates):
        if g % 3 != my_idx:
            my_corr.append(None)          # 非本端生成的门，待对端份额填入
            for p in peers:
                peer_payload[p].append(None)
            continue
        a, b, c = frand(), frand(), frand()
        values = (a, b, c, fmul(a, b), fmul(a, c), fmul(b, c),
                  fmul(fmul(a, b), c))
        mine_row, peer_rows = [], {p: [] for p in peers}
        for v in values:
            mine, outs = _share3(v, my_idx, peers)
            mine_row.append(mine)
            for p in peers:
                peer_rows[p].append(outs[p])
        my_corr.append(mine_row)
        for p in peers:
            peer_payload[p].append(peer_rows[p])
    state["my_corr"] = my_corr
    return {
        peer: seal_for(state, ST_TRIPLE, peer,
                       json.dumps(peer_payload[peer]).encode())
        for peer in peers
    }


def consume_triples(state: dict, incoming: dict[str, dict]) -> None:
    """汇总三家分发的相关随机量，按门得到本端持有的份额。"""
    n_gates = GATES_PER_SAMPLE * state["n_samples"]
    held = [row[:] if row else [0] * CORR_FIELDS
            for row in state["my_corr"]]
    for frm, env in incoming.items():
        raw = json.loads(open_from(state, ST_TRIPLE, frm, env))
        for g in range(n_gates):
            if raw[g] is None:
                continue
            for f in range(CORR_FIELDS):
                held[g][f] = fadd(held[g][f], raw[g][f])
    state["corr"] = held


# ---------- 步骤 2：标签输入分享 ----------
def input_step(state: dict, labels: list[int]) -> dict:
    """把本室逐批 0/1 标签切三份加法分享密封发出；批号不出室。"""
    assert len(labels) == state["n_samples"]
    my_idx = ALL_LABS.index(state["lab_id"])
    shares_me, by_peer = [], {p: [] for p in state["peers"]}
    for x in labels:
        mine, outs = _share3(x, my_idx, state["peers"])
        shares_me.append(mine)
        for p in state["peers"]:
            by_peer[p].append(outs[p])
    state["label_share"] = shares_me
    state["label_hash"] = sha256_hex(bytes(labels))
    return {
        peer: seal_for(state, ST_INPUT, peer,
                       json.dumps(by_peer[peer]).encode())
        for peer in state["peers"]
    }


def consume_inputs(state: dict, incoming: dict[str, dict]) -> None:
    shares = {state["lab_id"]: state["label_share"]}
    for frm, env in incoming.items():
        shares[frm] = json.loads(open_from(state, ST_INPUT, frm, env))
    state["x_share"] = shares[ALL_LABS[0]]
    state["y_share"] = shares[ALL_LABS[1]]
    state["z_share"] = shares[ALL_LABS[2]]


# ---------- 步骤 3：三输入 AND（共败 / 全通过），一轮密封交换 ----------
def _and3_correction(corr: list[int], d: int, e: int, f: int,
                     my_idx: int) -> int:
    """已知公开差 d=x-a、e=y-b、f=z-c，由相关量份额求 (x*y*z) 的份额。

    xyz=(a+d)(b+e)(c+f)，8 项中 7 项可由相关随机量的份额直接相加：
      abc, ab·f, ae·c, ae·f, db·c, db·f, de·c
    末项 def 是公开常数，约定由 0 号参与端承担。
    """
    a, b, c, ab, ac, bc, abc = corr
    r = abc                                   # abc
    r = fadd(r, fmul(ab, f))                  # ab·f
    r = fadd(r, fmul(ac, e))                  # ac·e（=ae·c）
    r = fadd(r, fmul(a, fmul(e, f)))          # a·ef
    r = fadd(r, fmul(bc, d))                  # bc·d（=db·c）
    r = fadd(r, fmul(b, fmul(d, f)))          # b·df（=db·f）
    r = fadd(r, fmul(c, fmul(d, e)))          # c·de（=de·c）
    if my_idx == 0:
        r = fadd(r, fmul(fmul(d, e), f))      # 公开常数 def
    return r


def mult_step(state: dict) -> dict:
    """计算两个三输入 AND 的开放差份额（d_i,e_i,f_i），密封交换。

    d_i=x_i-a_i 本身就是加法分享的差，三家求和即公开 d，无需再切份；
    单看 d_i 不泄露 x_i（a_i 是均匀随机分享）。密封仅为防协调端。
    """
    n = state["n_samples"]
    my_idx = ALL_LABS.index(state["lab_id"])
    # 公开常数 1 的加法分享：0 号持 1，其余持 0，三家之和恰为 1
    one_share = 1 if my_idx == 0 else 0
    opens_me, by_peer = [], {p: [] for p in state["peers"]}
    open_plan = []   # (sample, gate, 输入份额三元组)
    for i in range(n):
        xi, yi, zi = (state["x_share"][i], state["y_share"][i],
                      state["z_share"][i])
        open_plan.append((i, 0, (xi, yi, zi)))
        open_plan.append((i, 1, (fsub(one_share, xi),
                                 fsub(one_share, yi),
                                 fsub(one_share, zi))))
    for i, g, (xi, yi, zi) in open_plan:
        corr = state["corr"][GATES_PER_SAMPLE * i + g]
        a, b, c = corr[0], corr[1], corr[2]
        opens_me.append([fsub(xi, a), fsub(yi, b), fsub(zi, c)])
    payload = json.dumps(opens_me).encode()
    state["opens_me"] = opens_me
    state["open_plan"] = open_plan
    return {peer: seal_for(state, ST_MULT, peer, payload)
            for peer in state["peers"]}


def consume_mult(state: dict, incoming: dict[str, dict]) -> None:
    """重构公开差并完成两个 AND，得到每样本共败/全通过的本端份额。"""
    n = state["n_samples"]
    my_idx = ALL_LABS.index(state["lab_id"])
    parts = {state["lab_id"]: state["opens_me"]}
    for frm, env in incoming.items():
        parts[frm] = json.loads(open_from(state, ST_MULT, frm, env))
    common_share, allpass_share = [], []
    for i in range(n):
        for g in (0, 1):
            k = GATES_PER_SAMPLE * i + g
            d = field_sum([parts[lab][k][0] for lab in ALL_LABS])
            e = field_sum([parts[lab][k][1] for lab in ALL_LABS])
            f = field_sum([parts[lab][k][2] for lab in ALL_LABS])
            r = _and3_correction(state["corr"][k], d, e, f, my_idx)
            (common_share if g == 0 else allpass_share).append(r)
    state["common_share"] = common_share
    state["allpass_share"] = allpass_share


# ---------- 步骤 4：按规则单元汇总并掩码 ----------
def aggregate_step(state: dict, labels: list[int],
                   sample_attrs: list[dict], rule_attrs: list[str],
                   max_depth: int, cells: list[str]) -> dict:
    """生成本端上报包：共败/全通过份额按单元求和；池化败数成对掩码。"""
    from ..common.crypto import mask_vector
    n = state["n_samples"]
    all_key = "__ALL__"   # 样本层全集单元：每样本恰计入一次，用于总计与基线
    c_sum = {c: 0 for c in cells}
    p_sum = {c: 0 for c in cells}
    f_local = {c: 0 for c in cells}
    c_sum[all_key] = p_sum[all_key] = f_local[all_key] = 0
    for i, attrs in enumerate(sample_attrs):
        # 全集单元：样本层计数，不随属性单元重复
        c_sum[all_key] = fadd(c_sum[all_key], state["common_share"][i])
        p_sum[all_key] = fadd(p_sum[all_key], state["allpass_share"][i])
        f_local[all_key] += labels[i]
        for ck in sample_cells(attrs, rule_attrs, max_depth):
            c_sum[ck] = fadd(c_sum[ck], state["common_share"][i])
            p_sum[ck] = fadd(p_sum[ck], state["allpass_share"][i])
            f_local[ck] += labels[i]

    all_cells = [all_key] + cells
    # 成对掩码：lo 加、hi 减，三家求和后掩码整体抵消
    f_masked = dict(f_local)
    # 掩码密钥：双方由各自成对 enc/mac 密钥再派生一次，保证两端一致
    for peer in state["peers"]:
        enc_hex, mac_hex = state["pair_keys"][peer]
        mask_key = sha256_hex(bytes.fromhex(enc_hex)
                              + bytes.fromhex(mac_hex)).encode()
        a, b = sorted((state["lab_id"], peer))
        m = mask_vector(mask_key, state["round_id"], ST_AGGREGATE,
                        all_cells, a, b)
        sign = 1 if state["lab_id"] == a else -1
        for ck in all_cells:
            f_masked[ck] = (f_masked[ck] + sign * m[ck]) % FIELD_P

    return {
        "common": {c: c_sum[c] for c in all_cells},
        "allpass": {c: p_sum[c] for c in all_cells},
        "pooled_masked": {c: f_masked[c] for c in all_cells},
        "label_hash": state["label_hash"],
    }
