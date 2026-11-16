"""MPC 协议测试：三输入 AND 全真值表、多样本汇总、隐私份额均匀性。"""
import unittest

from cofail.common.crypto import FIELD_P
from cofail.common.protocol import ALL_LABS, build_cells, sample_cells
from cofail.lab import mpc


def _fresh_states(n):
    states = {}
    for lab in ALL_LABS:
        st = mpc.init_round_state(
            "t", lab, [x for x in ALL_LABS if x != lab], n)
        for p in st["peers"]:
            st["pair_keys"][p] = ["00" * 32, "11" * 32]
        states[lab] = st
    return states


def _run_protocol(states, labels_by_lab):
    envs = {lab: mpc.triple_step(states[lab]) for lab in ALL_LABS}
    for lab in ALL_LABS:
        mpc.consume_triples(
            states[lab],
            {frm: envs[frm][lab] for frm in ALL_LABS if frm != lab})
    envs = {lab: mpc.input_step(states[lab], labels_by_lab[lab])
            for lab in ALL_LABS}
    for lab in ALL_LABS:
        mpc.consume_inputs(
            states[lab],
            {frm: envs[frm][lab] for frm in ALL_LABS if frm != lab})
    envs = {lab: mpc.mult_step(states[lab]) for lab in ALL_LABS}
    for lab in ALL_LABS:
        mpc.consume_mult(
            states[lab],
            {frm: envs[frm][lab] for frm in ALL_LABS if frm != lab})


class And3GateTests(unittest.TestCase):
    def test_full_truth_table(self):
        cases = [
            ((1, 1, 1), 1, 0), ((1, 1, 0), 0, 0), ((1, 0, 1), 0, 0),
            ((0, 1, 1), 0, 0), ((1, 0, 0), 0, 0), ((0, 1, 0), 0, 0),
            ((0, 0, 1), 0, 0), ((0, 0, 0), 0, 1),
        ]
        for vals, exp_common, exp_pass in cases:
            states = _fresh_states(1)
            labels = {lab: [vals[i]] for i, lab in enumerate(ALL_LABS)}
            _run_protocol(states, labels)
            common = sum(states[lab]["common_share"][0]
                         for lab in ALL_LABS) % FIELD_P
            allpass = sum(states[lab]["allpass_share"][0]
                          for lab in ALL_LABS) % FIELD_P
            self.assertEqual(common, exp_common, f"common fail {vals}")
            self.assertEqual(allpass, exp_pass, f"all pass {vals}")

    def test_multi_sample_totals(self):
        n = 20
        states = _fresh_states(n)
        labels = {lab: [(i + ALL_LABS.index(lab)) % 2 for i in range(n)]
                  for lab in ALL_LABS}
        _run_protocol(states, labels)
        common = sum(sum(states[lab]["common_share"]) for lab in ALL_LABS) \
            % FIELD_P
        allpass = sum(sum(states[lab]["allpass_share"]) for lab in ALL_LABS) \
            % FIELD_P
        expect_common = sum(
            1 for i in range(n)
            if all(labels[lab][i] for lab in ALL_LABS))
        expect_pass = sum(
            1 for i in range(n)
            if not any(labels[lab][i] for lab in ALL_LABS))
        self.assertEqual(common, expect_common)
        self.assertEqual(allpass, expect_pass)

    def test_single_share_reveals_nothing(self):
        # 单份秘密分享在域上应与随机值不可区分：两次独立运行份额不同
        s1 = _fresh_states(1)
        _run_protocol(s1, {"lab-a": [1], "lab-b": [1], "lab-c": [1]})
        s2 = _fresh_states(1)
        _run_protocol(s2, {"lab-a": [1], "lab-b": [1], "lab-c": [1]})
        self.assertNotEqual(
            s1["lab-a"]["common_share"], s2["lab-a"]["common_share"])
        # 单份份额不可能等于明文结果 0/1（压倒性概率）
        self.assertNotIn(s1["lab-a"]["common_share"][0], (0, 1))


class CellTests(unittest.TestCase):
    def test_build_and_sample_cells(self):
        manifest = [
            {"sample_id": "s1", "attrs": {"r": "R1", "t": "T1"}},
            {"sample_id": "s2", "attrs": {"r": "R2", "t": "T1"}},
        ]
        cells = build_cells(manifest, ["r", "t"], 1)
        self.assertEqual(cells, ["r=R1", "r=R2", "t=T1"])
        sc = sample_cells(manifest[0]["attrs"], ["r", "t"], 1)
        self.assertEqual(set(sc), {"r=R1", "t=T1"})

    def test_aggregate_all_cell_counted_once(self):
        n = 6
        states = _fresh_states(n)
        labels = {lab: [1] * n for lab in ALL_LABS}
        _run_protocol(states, labels)
        attrs = [{"r": "R1"}] * n
        pkg = mpc.aggregate_step(
            states["lab-a"], labels["lab-a"], attrs, ["r"], 1, ["r=R1"])
        self.assertIn("__ALL__", pkg["common"])
        # 全集单元与唯一属性单元在此数据下重合
        self.assertEqual(set(pkg["common"].keys()), {"__ALL__", "r=R1"})


if __name__ == "__main__":
    unittest.main()
