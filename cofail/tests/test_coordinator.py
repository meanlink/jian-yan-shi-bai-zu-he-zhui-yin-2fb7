"""协调端状态机与隐私边界测试。

覆盖：冻结不可变、少一家不出结果、退出整轮作废、晚包不复活、
重试幂等、同室异包/跨轮包拒收、协调端任何接口都读不到单室计数。
"""
import json
import os
import tempfile
import unittest

from cofail.common.protocol import ALL_LABS
from cofail.coordinator.app import CoordinatorApp
from cofail.demo.make_data import generate
from cofail.demo.run_demo import (
    THRESHOLDS, TOKENS, _build_stack, _drive_until_done, _import_private,
)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = generate(self.tmp)
        self.coord, self.labs = _build_stack(self.tmp)

    def _new_round(self):
        return self.coord.create_round(
            self.data["manifest"], THRESHOLDS)["round_id"]

    def test_freeze_is_immutable(self):
        rid = self._new_round()
        status = self.coord.status(rid)
        self.assertEqual(status["frozen"]["members"], ALL_LABS)
        self.assertEqual(len(status["frozen"]["manifest"]),
                         len(self.data["manifest"]))
        # 阈值字段完整冻结
        self.assertEqual(status["frozen"]["thresholds"], THRESHOLDS)

    def test_missing_one_lab_no_result(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        self.labs["lab-a"].advance(rid)
        self.labs["lab-b"].advance(rid)
        self.assertEqual(self.coord.status(rid)["state"], "frozen")
        with self.assertRaises(ValueError):
            self.coord.briefing_for_round(rid)

    def test_exit_voids_and_late_packages_rejected(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        self.labs["lab-a"].advance(rid)
        self.coord.exit_round("lab-c", rid, "测试退出")
        self.assertEqual(self.coord.status(rid)["state"], "void")
        # 参与端 advance 识别作废；直接向协调端投晚包则抛错、不复活
        self.assertEqual(self.labs["lab-b"].advance(rid)["status"], "void")
        pkg = self.labs["lab-b"].get_package(rid, "frozen")
        if pkg is not None:
            with self.assertRaises(ValueError):
                self.coord.receive_package("lab-b", pkg)

    def test_idempotent_retry_and_conflicting_package(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        self.labs["lab-a"].advance(rid)
        pkg = self.labs["lab-a"].get_package(rid, "frozen")
        # 同内容重试：幂等
        r = self.coord.receive_package("lab-a", pkg)
        self.assertEqual(r["status"], "duplicate_ignored")
        # 同室异包：拒收
        bad = json.loads(json.dumps(pkg))
        bad["dh_pub"] += 1
        with self.assertRaises(ValueError):
            self.coord.receive_package("lab-a", bad)

    def test_cross_round_package_rejected(self):
        r1 = self._new_round()
        r2 = self._new_round()
        _import_private(self.labs, r1, self.data["manifest"],
                        self.data["per_lab"])
        _import_private(self.labs, r2, self.data["manifest"],
                        self.data["per_lab"])
        # lab-b 在 r2 加入，再把 r2 的包伪装成 r1 投递（r1 中 lab-b 尚未占用）
        self.labs["lab-b"].join_round(
            r2, self.coord.status(r2)["frozen"])
        foreign = json.loads(json.dumps(
            self.labs["lab-b"].get_package(r2, "frozen")))
        foreign["round_id"] = r1
        with self.assertRaises(ValueError):
            self.coord.receive_package("lab-b", foreign)

    def test_happy_path_totals_match_plaintext(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        _drive_until_done(self.labs, rid)
        bf = self.coord.briefing_for_round(rid)
        n = len(self.data["manifest"])
        expect_common = sum(
            1 for i in range(n)
            if all(self.data["per_lab"][lab]["labels"][i]
                   for lab in ALL_LABS))
        expect_pass = sum(
            1 for i in range(n)
            if not any(self.data["per_lab"][lab]["labels"][i]
                       for lab in ALL_LABS))
        expect_pooled = sum(
            sum(self.data["per_lab"][lab]["labels"]) for lab in ALL_LABS)
        self.assertEqual(bf["totals"]["common_fail"], expect_common)
        self.assertEqual(bf["totals"]["all_pass"], expect_pass)
        self.assertEqual(bf["totals"]["pooled_fail"], expect_pooled)
        # 分母始终是全部样本（含全通过）
        for rule in bf["rules"]:
            self.assertGreaterEqual(rule["denominator"],
                                    THRESHOLDS["min_support"])

    def test_coordinator_never_stores_per_lab_counts(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        _drive_until_done(self.labs, rid)
        # 协调端 packages 表中：密封步骤只存信封，aggregate 存掩码/份额
        rows = self.coord.store.query(
            "SELECT step, payload_json FROM packages WHERE round_id=?", (rid,))
        for r in rows:
            payload = r["payload_json"]
            # 任何单室的真实标签（0/1 序列）都不得出现
            for lab in ALL_LABS:
                self.assertNotIn(str(self.data["per_lab"][lab]["labels"]),
                                 payload)
        # 状态接口不暴露单室计数字段
        status = self.coord.status(rid)
        self.assertNotIn("per_lab", status)
        # 进度只给 lab_id，不给计数
        for step in status["progress"]:
            self.assertEqual(set(step.keys()),
                             {"step", "received", "complete"})

    def test_briefing_traceability(self):
        rid = self._new_round()
        _import_private(self.labs, rid, self.data["manifest"],
                        self.data["per_lab"])
        _drive_until_done(self.labs, rid)
        bf = self.coord.briefing_for_round(rid)
        self.assertTrue(bf["briefing_id"].startswith("bf_"))
        self.assertEqual(bf["round_id"], rid)
        self.assertEqual(len(bf["manifest_fingerprint"]), 64)
        self.assertEqual(set(bf["member_key_fingerprints"].keys()),
                         set(ALL_LABS))


if __name__ == "__main__":
    unittest.main()
