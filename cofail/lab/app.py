"""参与端服务：独立进程、独立端口、独立 SQLite，批号/标签不出本室。

断点恢复：每一步的本地状态在发送前落盘；重试时直接重放已生成的包，
绝不重复计数。同室异包（同一 step 内容漂移）与跨轮包一律拒收。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common.protocol import (
    ALL_LABS, ST_AGGREGATE, ST_FROZEN, ST_INPUT, ST_MULT, ST_TRIPLE,
    ST_VOID, STEPS_ORDER, expected_kind, next_state,
)
from ..common.storage import Store
from . import mpc


class LabApp:
    def __init__(self, config: dict):
        self.lab_id = config["lab_id"]
        self.token = config["token"]
        self.coordinator = config["coordinator_url"].rstrip("/")
        self.auto = config.get("auto", True)
        self.store = Store(config["db_path"])
        self._init_db()
        self.http = urllib.request
        # 运行中的轮次状态（round_id -> state dict），从库中恢复
        self.states: dict[str, dict] = {
            r["round_id"]: Store.loads(r["state_json"])
            for r in self.store.query("SELECT round_id, state_json FROM round_state")
        }
        self.lock = threading.RLock()

    def _init_db(self):
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS round_state(
                round_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL)""")
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS sent_packages(
                round_id TEXT, step TEXT, kind TEXT, payload TEXT,
                PRIMARY KEY(round_id, step))""")
        self.store.exec("""
            CREATE TABLE IF NOT EXISTS local_data(
                round_id TEXT PRIMARY KEY, manifest_hash TEXT,
                labels_json TEXT, attrs_json TEXT, cells_json TEXT)""")

    # ---------- 持久化 ----------
    def save_state(self, state: dict):
        self.store.exec(
            "INSERT OR REPLACE INTO round_state VALUES(?,?,?)",
            (state["round_id"], Store.dumps(state), time.time()))

    def save_package(self, round_id: str, step: str, kind: str, payload: dict):
        self.store.exec(
            "INSERT OR REPLACE INTO sent_packages VALUES(?,?,?,?)",
            (round_id, step, kind, Store.dumps(payload)))

    def get_package(self, round_id: str, step: str):
        row = self.store.one(
            "SELECT payload FROM sent_packages WHERE round_id=? AND step=?",
            (round_id, step))
        return Store.loads(row["payload"]) if row else None

    # ---------- 协调端通信 ----------
    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            self.coordinator + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.loads(e.read())["error"])

    def _get(self, path: str):
        req = urllib.request.Request(
            self.coordinator + path,
            headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # ---------- 轮次驱动 ----------
    def join_round(self, round_id: str, frozen: dict) -> dict:
        with self.lock:
            if round_id in self.states:
                return {"status": "already_joined"}
            local = self.store.one(
                "SELECT * FROM local_data WHERE round_id=?", (round_id,))
            if local is None:
                return {"status": "no_local_data",
                        "error": "本室尚未导入该轮样本数据，无法加入"}
            labels = Store.loads(local["labels_json"])
            attrs = Store.loads(local["attrs_json"])
            manifest = frozen["manifest"]
            if local["manifest_hash"] != frozen["manifest_fingerprint"]:
                return {"status": "manifest_mismatch",
                        "error": "冻结清单指纹与本室数据不一致，拒绝加入"}
            if len(labels) != len(manifest):
                return {"status": "size_mismatch"}
            state = mpc.init_round_state(
                round_id, self.lab_id,
                [x for x in ALL_LABS if x != self.lab_id], len(labels))
            self.states[round_id] = state
            self.save_state(state)
            # 用本轮 DH 私钥对“轮次+冻结指纹”做承诺：跨轮改包无法伪造
            commitment = mpc.round_commitment(
                state["dh_priv"], round_id,
                frozen["manifest_fingerprint"], self.lab_id)
            pkg = {"round_id": round_id, "lab_id": self.lab_id,
                   "kind": "join", "dh_pub": state["dh_pub"],
                   "manifest_fingerprint": frozen["manifest_fingerprint"],
                   "commitment": commitment,
                   "label_hash": mpc.sha256_hex(bytes(labels)),
                   "sample_count": len(labels)}
            self.save_package(round_id, ST_FROZEN, "join", pkg)
            self._post("/api/packages", pkg)
            return {"status": "joined"}

    def advance(self, round_id: str) -> dict:
        """按协调端当前状态推进一步；幂等，可安全重试。"""
        with self.lock:
            status = self._get(f"/api/rounds/{round_id}/status")
            state = status["state"]
            if state == ST_VOID:
                return {"status": "void"}
            if state == "completed":
                return {"status": "completed"}
            local = self.store.one(
                "SELECT * FROM local_data WHERE round_id=?", (round_id,))
            labels = Store.loads(local["labels_json"])
            attrs = Store.loads(local["attrs_json"])
            cells = Store.loads(local["cells_json"])

            if round_id not in self.states:
                # 本端尚未加入（或重启后状态缺失但 join 包未发）：先加入
                return self.join_round(round_id, status["frozen"])
            st = self.states[round_id]

            if state == ST_FROZEN:
                return self.join_round(round_id, status["frozen"])

            cached = self.get_package(round_id, state)
            if cached is not None:
                # 重试：重放原包，不重新生成、不重复计数
                self._post("/api/packages", cached)
                return {"status": "replayed", "step": state}

            if state == ST_TRIPLE:
                if not st["pair_keys"]:
                    self._after_join(round_id, st, status)
                envelopes = mpc.triple_step(st)
                self.save_state(st)
            elif state == ST_INPUT:
                mpc.consume_triples(st, self._collect_envelopes(
                    round_id, ST_TRIPLE, st))
                self.save_state(st)
                envelopes = mpc.input_step(st, labels)
            elif state == ST_MULT:
                mpc.consume_inputs(st, self._collect_envelopes(
                    round_id, ST_INPUT, st))
                self.save_state(st)
                envelopes = mpc.mult_step(st)
            elif state == ST_AGGREGATE:
                mpc.consume_mult(st, self._collect_envelopes(
                    round_id, ST_MULT, st))
                self.save_state(st)
                frozen = status["frozen"]
                pkg = mpc.aggregate_step(
                    st, labels, attrs,
                    frozen["thresholds"]["rule_attrs"],
                    frozen["thresholds"].get("max_depth", 1), cells)
                pkg.update({"round_id": round_id,
                            "lab_id": self.lab_id, "kind": "aggregate"})
                self.save_package(round_id, ST_AGGREGATE, "aggregate", pkg)
                self._post("/api/packages", pkg)
                return {"status": "submitted", "step": state}
            else:
                return {"status": "waiting", "state": state}
            pkg = {"round_id": round_id, "lab_id": self.lab_id,
                   "kind": expected_kind(state), "envelopes": envelopes}
            self.save_package(round_id, state, pkg["kind"], pkg)
            self._post("/api/packages", pkg)
            return {"status": "submitted", "step": state}

    def _after_join(self, round_id: str, st: dict, status: dict):
        pubs = {m["lab_id"]: m["dh_pub"]
                for m in status.get("member_pubkeys", [])}
        if len(pubs) < 3:
            raise RuntimeError("等待三家 DH 公钥到齐")
        mpc.derive_pair_keys(st, pubs)
        self.save_state(st)

    def _collect_envelopes(self, round_id: str, step: str, st: dict) -> dict:
        """从协调端取回本步发给本端的密封信封（协调端无法解密）。

        必须两个对端都到齐；未到齐时抛出等待异常，由调用方稍后安全重试，
        已发送的包不会重发、更不会重复计数。
        """
        data = self._get(
            f"/api/rounds/{round_id}/envelopes/{step}/{self.lab_id}")
        env = data["envelopes"]
        missing = [p for p in st["peers"] if p not in env]
        if missing:
            raise RuntimeError(f"等待对端密封包: {missing}")
        return env

    def exit_round(self, round_id: str, reason: str) -> dict:
        with self.lock:
            self._post("/api/rounds/{}/exit".format(round_id),
                       {"lab_id": self.lab_id, "reason": reason or "主动退出"})
            return {"status": "exited"}

    def progress(self, round_id: str) -> dict:
        status = self._get(f"/api/rounds/{round_id}/status")
        local = self.store.one(
            "SELECT manifest_hash FROM local_data WHERE round_id=?",
            (round_id,))
        return {
            "round_id": round_id,
            "coordinator_state": status["state"],
            "members": status.get("members", []),
            "have_local_data": local is not None,
            "manifest_hash": local["manifest_hash"] if local else None,
            "my_steps": [r["step"] for r in self.store.query(
                "SELECT step FROM sent_packages WHERE round_id=? ORDER BY rowid",
                (round_id,))],
        }

    def import_local_data(self, body: dict) -> dict:
        """导入本室私有数据：逐批标签与批号仅保存在本室 SQLite。"""
        from ..common.crypto import sha256_hex
        from ..common.protocol import build_cells
        round_id = body["round_id"]
        labels = [int(x) for x in body["labels"]]
        manifest = body["manifest"]
        if len(labels) != len(manifest):
            raise ValueError("标签数量必须等于样本数量")
        if body.get("batch_ids"):
            # 批号仅本地落盘，绝不参与任何对外消息
            self.store.exec(
                "CREATE TABLE IF NOT EXISTS batch_ids(round_id TEXT, idx INT,"
                " batch_id TEXT, PRIMARY KEY(round_id, idx))")
            self.store.exec("DELETE FROM batch_ids WHERE round_id=?",
                            (round_id,))
            for i, bid in enumerate(body["batch_ids"]):
                self.store.exec(
                    "INSERT INTO batch_ids VALUES(?,?,?)", (round_id, i, bid))
        attrs = [s["attrs"] for s in manifest]
        th = body["thresholds"]
        cells = build_cells(manifest, th["rule_attrs"],
                            th.get("max_depth", 1))
        fp = sha256_hex(json.dumps(
            manifest, sort_keys=True, ensure_ascii=False).encode())
        self.store.exec(
            "INSERT OR REPLACE INTO local_data VALUES(?,?,?,?,?)",
            (round_id, fp, Store.dumps(labels), Store.dumps(attrs),
             Store.dumps(cells)))
        return {"status": "imported", "samples": len(labels),
                "manifest_hash": fp}

    def auto_loop(self):
        while True:
            time.sleep(1.0)
            if not self.auto:
                continue
            try:
                rounds = self._get("/api/rounds")["rounds"]
                for r in rounds:
                    rid = r["round_id"]
                    if r["state"] in ("completed", "void"):
                        continue
                    if rid not in self.states:
                        local = self.store.one(
                            "SELECT 1 FROM local_data WHERE round_id=?", (rid,))
                        if not local:
                            continue
                    self.advance(rid)
            except Exception as exc:  # 网络抖动等：下一轮重试，不重复计数
                print(f"[{self.lab_id}] auto loop: {exc}")


# ---------- HTTP 入口（页面操作 + 本地数据导入） ----------
class _Handler(BaseHTTPRequestHandler):
    app: "LabApp" = None

    def log_message(self, *args):
        pass

    def _json(self, code: int, obj: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _auth(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.app.token}"

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "lab_id": self.app.lab_id})
        if self.path == "/":
            return self._serve("index.html", "text/html")
        if self.path == "/app.js":
            return self._serve("app.js", "application/javascript")
        if self.path == "/styles.css":
            return self._serve("styles.css", "text/css")
        if self.path.startswith("/api/progress?round="):
            if not self._auth():
                return self._json(401, {"error": "未授权"})
            rid = self.path.split("round=", 1)[1]
            try:
                return self._json(200, self.app.progress(rid))
            except Exception as e:
                return self._json(400, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._json(401, {"error": "未授权"})
        try:
            body = self._body()
            if self.path == "/api/local-data":
                return self._json(200, self.app.import_local_data(body))
            if self.path == "/api/join":
                return self._json(200,
                                  self.app.join_round(body["round_id"],
                                                      body["frozen"]))
            if self.path == "/api/advance":
                return self._json(200, self.app.advance(body["round_id"]))
            if self.path == "/api/exit":
                return self._json(200,
                                  self.app.exit_round(body["round_id"],
                                                      body.get("reason", "")))
            self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _serve(self, name: str, ctype: str):
        path = os.path.join(os.path.dirname(__file__), "static", name)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)


def serve(config: dict):
    app = LabApp(config)
    _Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", config["port"]), _Handler)
    threading.Thread(target=app.auto_loop, daemon=True).start()
    print(f"参与端 {app.lab_id} 监听 http://127.0.0.1:{config['port']}")
    httpd.serve_forever()
