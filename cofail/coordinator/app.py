"""协调端服务：只保存密文信封与三室合计，绝不保存任何单室计数。

- 成员/样本/阈值在创建轮次时冻结，任何后续提交不得改变冻结口径；
- 每一步必须三家到齐才原子推进，少一家永远不出结果；
- 任一家退出 → 整轮作废，已收密封包随轮次失效，晚到包不复活；
- 重试包按内容哈希幂等处理，同室异包、跨轮包一律拒收；
- 不提供任何两轮差分接口，单室计数在任何 API 中都不可读。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common.crypto import FIELD_P, fadd, sha256_hex
from ..common.protocol import (
    ALL_LABS, SEALED_STEPS, ST_AGGREGATE, ST_FROZEN, ST_INPUT, ST_MULT,
    ST_TRIPLE, ST_VOID, build_cells, expected_kind, next_state,
)
from ..common.rules import briefing_trace_note, build_briefing, mine_rules
from ..common.storage import Store


class CoordinatorApp:
    def __init__(self, db_path: str, tokens: dict[str, str]):
        self.store = Store(db_path)
        self.tokens = tokens          # lab_id -> bearer token
        self.lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS rounds(
                round_id TEXT PRIMARY KEY, created_at REAL, state TEXT,
                frozen_json TEXT, void_reason TEXT)""")
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS packages(
                round_id TEXT, step TEXT, lab_id TEXT, kind TEXT,
                payload_json TEXT, content_hash TEXT, received_at REAL,
                PRIMARY KEY(round_id, step, lab_id))""")
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS briefings(
                briefing_id TEXT PRIMARY KEY, round_id TEXT,
                created_at REAL, content_json TEXT, trace_note TEXT)""")

    # ---------- 轮次创建（冻结成员、样本、阈值） ----------
    def create_round(self, manifest: list[dict], thresholds: dict) -> dict:
        with self.lock:
            rid = "r_" + uuid.uuid4().hex[:12]
            required = {"min_support", "min_common_rate",
                        "min_pooled_rate", "rule_attrs"}
            missing = required - set(thresholds)
            if missing:
                raise ValueError(f"阈值缺少字段: {sorted(missing)}")
            if not manifest:
                raise ValueError("样本清单为空")
            for s in manifest:
                if "sample_id" not in s or "attrs" not in s:
                    raise ValueError("样本必须包含 sample_id 与 attrs")
                for attr in thresholds["rule_attrs"]:
                    if attr not in s["attrs"]:
                        raise ValueError(f"样本 {s['sample_id']} 缺少属性 {attr}")
            fp = sha256_hex(json.dumps(
                manifest, sort_keys=True, ensure_ascii=False).encode())
            frozen = {
                "members": ALL_LABS[:],           # 成员创建即冻结为三室
                "manifest": manifest,             # 仅公开属性，无批号无标签
                "manifest_fingerprint": fp,
                "thresholds": thresholds,
                "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.store.exec(
                "INSERT INTO rounds VALUES(?,?,?,?,?)",
                (rid, time.time(), ST_FROZEN, Store.dumps(frozen), None))
            return {"round_id": rid, "state": ST_FROZEN, "frozen": frozen}

    def _round(self, rid: str):
        row = self.store.one("SELECT * FROM rounds WHERE round_id=?", (rid,))
        if not row:
            raise ValueError("轮次不存在（跨轮包拒收）")
        return row

    # ---------- 收包（幂等 + 严格校验） ----------
    def receive_package(self, lab_id: str, pkg: dict) -> dict:
        with self.lock:
            rid = pkg.get("round_id", "")
            if not rid:
                raise ValueError("包缺少 round_id")
            row = self._round(rid)   # 不存在即跨轮/伪造，直接拒收
            state = row["state"]
            if state == ST_VOID:
                raise ValueError("该轮已作废，任何包不再受理（晚包不复活）")
            if state == "completed":
                raise ValueError("该轮已完成，拒收新包")
            if lab_id not in ALL_LABS or pkg.get("lab_id") != lab_id:
                raise ValueError("包归属与认证身份不符（同室异包拒收）")
            kind = expected_kind(state)
            if pkg.get("kind") != kind:
                raise ValueError(
                    f"当前步骤 {state} 只收 {kind}，收到 {pkg.get('kind')}")
            chash = sha256_hex(Store.dumps(pkg).encode())
            existing = self.store.one(
                "SELECT content_hash FROM packages WHERE round_id=? AND step=?"
                " AND lab_id=?", (rid, state, lab_id))
            if existing:
                if existing["content_hash"] == chash:
                    return {"status": "duplicate_ignored", "step": state}
                raise ValueError("同一室同一步骤提交了不同内容（同室异包拒收）")

            self._validate_payload(rid, state, lab_id, pkg, row)
            self.store.exec(
                "INSERT INTO packages VALUES(?,?,?,?,?,?,?)",
                (rid, state, lab_id, kind, Store.dumps(pkg), chash,
                 time.time()))
            return self._maybe_advance(rid, state, row)

    def _validate_payload(self, rid, state, lab_id, pkg, row):
        frozen = Store.loads(row["frozen_json"])
        n = len(frozen["manifest"])
        if state == ST_FROZEN:
            if pkg["sample_count"] != n:
                raise ValueError("样本数量与冻结清单不一致")
            if not isinstance(pkg.get("dh_pub"), int):
                raise ValueError("join 包缺少 DH 公钥")
            if pkg.get("manifest_fingerprint") != \
                    frozen["manifest_fingerprint"]:
                raise ValueError("join 包承诺的清单指纹与冻结轮次不符")
            from ..lab.mpc import verify_commitment
            if not verify_commitment(pkg):
                raise ValueError("join 包轮次承诺验证失败（跨轮/伪造包拒收）")
            return
        if state in SEALED_STEPS:
            env = pkg.get("envelopes", {})
            peers = [x for x in ALL_LABS if x != lab_id]
            if set(env.keys()) != set(peers):
                raise ValueError("密封信封必须恰好发给另外两家")
            for peer, e in env.items():
                if set(e.keys()) != {"payload", "tag"}:
                    raise ValueError("信封结构非法")
            return
        if state == ST_AGGREGATE:
            cells = build_cells(
                frozen["manifest"], frozen["thresholds"]["rule_attrs"],
                frozen["thresholds"].get("max_depth", 1))
            expect_cells = ["__ALL__"] + cells
            for key in ("common", "allpass", "pooled_masked"):
                if set(pkg[key].keys()) != set(expect_cells):
                    raise ValueError(f"{key} 单元集合与冻结口径不一致")
                if any(not isinstance(v, int) or not (0 <= v < FIELD_P)
                       for v in pkg[key].values()):
                    raise ValueError(f"{key} 含非法域元素")

    def _maybe_advance(self, rid: str, state: str, row) -> dict:
        got = {r["lab_id"] for r in self.store.query(
            "SELECT lab_id FROM packages WHERE round_id=? AND step=?",
            (rid, state))}
        if got != set(ALL_LABS):
            return {"status": "accepted_waiting", "have": sorted(got),
                    "need": 3, "step": state}
        new_state = next_state(state)
        self.store.exec("UPDATE rounds SET state=? WHERE round_id=?",
                        (new_state, rid))
        if new_state == "completed":
            self._complete(rid, row)
        return {"status": "advanced", "step": state, "new_state": new_state}

    # ---------- 密封信封路由（协调端只转发，不能解密） ----------
    def envelopes_for(self, rid: str, step: str, lab_id: str) -> dict:
        with self.lock:
            row = self._round(rid)
            if step not in SEALED_STEPS:
                raise ValueError("该步骤没有密封信封")
            rows = self.store.query(
                "SELECT lab_id, payload_json FROM packages"
                " WHERE round_id=? AND step=?", (rid, step))
            out = {}
            for r in rows:
                if r["lab_id"] == lab_id:
                    continue
                pkg = Store.loads(r["payload_json"])
                if lab_id in pkg["envelopes"]:
                    out[r["lab_id"]] = pkg["envelopes"][lab_id]
            return {"envelopes": out, "available_from": sorted(out.keys())}

    # ---------- 完成：只在三室合计上挖掘规则 ----------
    def _complete(self, rid: str, row):
        frozen = Store.loads(row["frozen_json"])
        th = frozen["thresholds"]
        manifest = frozen["manifest"]
        cells = build_cells(manifest, th["rule_attrs"],
                            th.get("max_depth", 1))
        all_cells = ["__ALL__"] + cells
        rows = self.store.query(
            "SELECT lab_id, payload_json FROM packages"
            " WHERE round_id=? AND step=?", (rid, ST_AGGREGATE))
        assert len(rows) == 3                       # 少一家不会进入完成态
        per_cell = {}
        for cell in all_cells:
            common = allpass = pooled = 0
            for r in rows:
                pkg = Store.loads(r["payload_json"])
                common = fadd(common, pkg["common"][cell])
                allpass = fadd(allpass, pkg["allpass"][cell])
                pooled = fadd(pooled, pkg["pooled_masked"][cell])
            # 成对掩码在三家求和后抵消；秘密分享求和还原三室联合计数
            d = len(manifest) if cell == "__ALL__" else sum(
                1 for s in manifest
                if cell in {ck for ck in _cells_of(s, th)})
            per_cell[cell] = {"D": d, "F": pooled, "C": common,
                              "P": allpass}
        rules = mine_rules(per_cell, len(manifest), th)
        pubs = {}
        for r in self.store.query(
                "SELECT lab_id, payload_json FROM packages"
                " WHERE round_id=? AND step=?", (rid, ST_FROZEN)):
            pkg = Store.loads(r["payload_json"])
            pubs[r["lab_id"]] = sha256_hex(str(pkg["dh_pub"]).encode())[:16]
        briefing = build_briefing(
            rid, frozen["manifest_fingerprint"], th, pubs, rules,
            per_cell, len(manifest))
        self.store.exec("INSERT INTO briefings VALUES(?,?,?,?,?)",
                        (briefing["briefing_id"], rid, time.time(),
                         Store.dumps(briefing),
                         briefing_trace_note(briefing)))

    # ---------- 退出：整轮作废 ----------
    def exit_round(self, lab_id: str, rid: str, reason: str) -> dict:
        with self.lock:
            row = self._round(rid)
            if row["state"] in ("completed", ST_VOID):
                return {"status": row["state"], "note": "终态轮次无需退出"}
            self.store.exec(
                "UPDATE rounds SET state=?, void_reason=? WHERE round_id=?",
                (ST_VOID, f"{lab_id}: {reason or '主动退出'}", rid))
            return {"status": ST_VOID}

    # ---------- 查询（进度对成员公开；任何接口都不返回单室计数） ----------
    def status(self, rid: str) -> dict:
        row = self._round(rid)
        frozen = Store.loads(row["frozen_json"])
        steps = [ST_FROZEN, ST_TRIPLE, ST_INPUT, ST_MULT, ST_AGGREGATE]
        progress = []
        for step in steps:
            labs = [r["lab_id"] for r in self.store.query(
                "SELECT lab_id FROM packages WHERE round_id=? AND step=?",
                (rid, step))]
            progress.append({"step": step, "received": sorted(labs),
                             "complete": len(labs) == 3})
        out = {"round_id": rid, "state": row["state"],
               "void_reason": row["void_reason"], "frozen": frozen,
               "members": [r["lab_id"] for r in self.store.query(
                   "SELECT DISTINCT lab_id FROM packages"
                   " WHERE round_id=? AND step=?", (rid, ST_FROZEN))],
               "member_pubkeys": [
                   {"lab_id": r["lab_id"],
                    "dh_pub": Store.loads(r["payload_json"])["dh_pub"]}
                   for r in self.store.query(
                       "SELECT lab_id, payload_json FROM packages"
                       " WHERE round_id=? AND step=?", (rid, ST_FROZEN))],
               "progress": progress}
        if row["state"] == "completed":
            out["briefing"] = self.briefing_for_round(rid)
        return out

    def list_rounds(self) -> dict:
        return {"rounds": [
            {"round_id": r["round_id"], "state": r["state"],
             "created_at": r["created_at"]}
            for r in self.store.query(
                "SELECT * FROM rounds ORDER BY created_at DESC")]}

    def briefing_for_round(self, rid: str) -> dict:
        row = self.store.one(
            "SELECT content_json FROM briefings WHERE round_id=?", (rid,))
        if not row:
            raise ValueError("该轮没有简报（未完成或已作废）")
        return Store.loads(row["content_json"])

    def list_briefings(self) -> dict:
        return {"briefings": [
            {"briefing_id": r["briefing_id"], "round_id": r["round_id"],
             "created_at": r["created_at"],
             "trace_note": Store.loads(r["trace_note"])}
            for r in self.store.query(
                "SELECT * FROM briefings ORDER BY created_at DESC")]}


def _cells_of(sample: dict, th: dict) -> list[str]:
    from ..common.protocol import sample_cells
    return sample_cells(sample["attrs"], th["rule_attrs"],
                        th.get("max_depth", 1))


# ---------- HTTP ----------
class _Handler(BaseHTTPRequestHandler):
    app: CoordinatorApp = None

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _auth_lab(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        for lab_id, tok in self.app.tokens.items():
            if token == tok:
                return lab_id
        return None

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/":
            return self._serve("index.html", "text/html")
        if self.path == "/app.js":
            return self._serve("app.js", "application/javascript")
        if self.path == "/styles.css":
            return self._serve("styles.css", "text/css")
        try:
            if self.path == "/api/rounds":
                return self._json(200, self.app.list_rounds())
            if self.path == "/api/briefings":
                return self._json(200, self.app.list_briefings())
            if self.path.startswith("/api/rounds/") and \
                    self.path.endswith("/status"):
                rid = self.path.split("/")[3]
                return self._json(200, self.app.status(rid))
            if "/envelopes/" in self.path:
                lab = self._auth_lab()
                if not lab:
                    return self._json(401, {"error": "未授权"})
                # /api/rounds/<rid>/envelopes/<step>/<lab>
                parts = self.path.split("/")
                rid, step, target = parts[3], parts[5], parts[6]
                if target != lab:
                    return self._json(403, {"error": "只能取自己的信封"})
                return self._json(
                    200, self.app.envelopes_for(rid, step, lab))
            if self.path.startswith("/api/briefings/"):
                bid = self.path.rsplit("/", 1)[1]
                row = self.app.store.one(
                    "SELECT content_json FROM briefings WHERE briefing_id=?",
                    (bid,))
                if not row:
                    return self._json(404, {"error": "简报不存在"})
                return self._json(200, Store.loads(row["content_json"]))
            self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/admin/rounds":
                # 演示环境的发起口令；生产中应换成调查员账号体系
                if self.headers.get("X-Admin-Token") != \
                        self.app.tokens.get("__admin__"):
                    return self._json(401, {"error": "发起轮次需要调查员口令"})
                return self._json(
                    200, self.app.create_round(body["manifest"],
                                               body["thresholds"]))
            lab = self._auth_lab()
            if not lab:
                return self._json(401, {"error": "未授权"})
            if self.path == "/api/packages":
                return self._json(200, self.app.receive_package(lab, body))
            if self.path.startswith("/api/rounds/") and \
                    self.path.endswith("/exit"):
                rid = self.path.split("/")[3]
                return self._json(
                    200, self.app.exit_round(lab, rid, body.get("reason", "")))
            self._json(404, {"error": "not found"})
        except KeyError as e:
            self._json(400, {"error": f"缺少字段 {e}"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _serve(self, name, ctype):
        path = os.path.join(os.path.dirname(__file__), "static", name)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)


def serve(config: dict):
    app = CoordinatorApp(config["db_path"], config["lab_tokens"])
    app.tokens["__admin__"] = config["admin_token"]
    _Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", config["port"]), _Handler)
    print(f"协调端监听 http://127.0.0.1:{config['port']}")
    httpd.serve_forever()
