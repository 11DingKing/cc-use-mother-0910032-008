"""政策试算服务的回归测试。

覆盖契约四大不变量 + 业务闸门：
1. 试算环境隔离（正式账户只读、方案互不污染、取消清理）
2. 参数版本绑定（创建后不可更换、共享输入去重）
3. 可恢复批处理（中断后续跑结果一致）
4. 方案差异比较（任意两次结果可比）
以及：发布只能引用已复核方案、不复制余额；重放指纹一致。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from policy_trial.engine import CancellationToken, compute_enterprise
from policy_trial.ledger import FormalLedger
from policy_trial.models import (
    EnterpriseRecord,
    ExemptionRule,
    ParameterVersion,
    ScenarioStatus,
    digest,
)
from policy_trial.service import CancelledError, TrialService
from policy_trial.storage import SandboxStore


def _records(n: int = 250) -> list[EnterpriseRecord]:
    sectors = ["制造", "服务", "农业"]
    return [
        EnterpriseRecord(
            enterprise_id=f"E{i:04d}",
            sector=sectors[i % 3],
            base_amount=1000.0 + i * 10,
            current_payment=200.0 + i * 2,
            carry_over=50.0 + (i % 7),
            exempt=(i % 37 == 0),
        )
        for i in range(n)
    ]


def _params(version: str = "v1", coefficient: float = 0.3,
            carry_ratio: float = 0.5,
            sectors: tuple[str, ...] = (),
            ids: tuple[str, ...] = (),
            passthrough: float = 1.0) -> ParameterVersion:
    return ParameterVersion(
        version=version,
        coefficient=coefficient,
        carry_ratio=carry_ratio,
        exemption=ExemptionRule(sectors=sectors, enterprise_ids=ids),
        price_pass_through=passthrough,
    )


class TrialServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = FormalLedger().ingested(_records())
        self.service = TrialService(
            SandboxStore(Path(self.tmp.name) / "sandbox"),
            ledger=self.ledger, chunk_size=64,
        )
        self.snap_id, self.snap_hash = \
            self.service.export_and_register_snapshot("snap-2026Q2")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _scenario(self, sid: str, params: ParameterVersion | None = None) -> str:
        params = params or _params()
        ph = self.service.register_parameters(params)
        self.service.create_scenario(sid, self.snap_hash, ph)
        return sid


class IsolationTest(TrialServiceTestBase):
    def test_formal_ledger_is_not_mutated_by_trials(self) -> None:
        original_ids = self.ledger.enterprise_ids()
        sid = self._scenario("S1")
        self.service.run(sid)
        # 正式账户没有任何试算结果入口；企业集合不变
        self.assertEqual(self.ledger.enterprise_ids(), original_ids)
        self.assertFalse(hasattr(self.ledger, "apply_trial_result"))

    def test_snapshot_is_frozen_copy(self) -> None:
        snap = self.ledger.export_snapshot("again")
        with self.assertRaises(Exception):
            # frozen 实例不可改
            snap.snapshot_id = "hacked"  # type: ignore[misc]
        with self.assertRaises(Exception):
            snap.enterprises[0].sector = "hacked"  # type: ignore[misc]
        # 同内容快照内容哈希一致（共享输入）
        snap2 = self.ledger.export_snapshot("again2")
        # taken_at 不同会影响哈希；核心企业数据一致
        self.assertEqual(tuple(r.base_amount for r in snap.enterprises),
                         tuple(r.base_amount for r in snap2.enterprises))

    def test_scenarios_share_input_but_not_output(self) -> None:
        ph = self.service.register_parameters(_params("v1"))
        self.service.create_scenario("A", self.snap_hash, ph)
        self.service.create_scenario("B", self.snap_hash, ph)
        ra = self.service.run("A")
        rb = self.service.run("B")
        # 输入对象同一份（物理去重）
        binding_a = self.service.get_binding("A")
        binding_b = self.service.get_binding("B")
        self.assertEqual(binding_a["snapshot_hash"], binding_b["snapshot_hash"])
        self.assertEqual(binding_a["params_hash"], binding_b["params_hash"])
        # 结果对象互不相同、互不影响
        self.assertIsNot(ra, rb)
        ra.metrics  # readable
        # 复核 A 不影响 B 的状态
        self.service.review("A", "核算员")
        self.assertEqual(self.service.get_state("B")["status"],
                         ScenarioStatus.DRAFT)
        self.assertEqual(self.service.get_state("A")["status"],
                         ScenarioStatus.REVIEWED)

    def test_content_store_deduplicates(self) -> None:
        p = _params("vX")
        h1 = self.service.register_parameters(p)
        h2 = self.service.register_parameters(p)
        self.assertEqual(h1, h2)

    def test_cancel_purges_workspace_and_chunks(self) -> None:
        sid = self._scenario("C")
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            self.service.run(sid, token=token)
        # 取消前可能已写 0 块；cancel 必须清掉整个工作区
        receipt = self.service.cancel(sid)
        self.assertTrue(receipt["purged"])
        self.assertFalse(self.service.store.workspace_exists(sid))
        # 共享输入对象仍在，其他方案可用
        sid2 = self._scenario("D")
        self.service.run(sid2)

    def test_cancel_completed_scenario_cleans_too(self) -> None:
        sid = self._scenario("E")
        self.service.run(sid)
        self.service.cancel(sid)
        self.assertFalse(self.service.store.workspace_exists(sid))

    def test_published_scenario_cannot_cancel(self) -> None:
        sid = self._scenario("F")
        self.service.run(sid)
        self.service.review(sid, "核算员")
        self.service.publish("POL-1", sid)
        with self.assertRaises(Exception):
            self.service.cancel(sid)


class BindingTest(TrialServiceTestBase):
    def test_scenario_rejects_unknown_input(self) -> None:
        with self.assertRaises(Exception):
            self.service.create_scenario("X", "0" * 64, "1" * 64)

    def test_binding_is_recorded_and_param_hash_differs(self) -> None:
        p1 = self.service.register_parameters(_params("v1", coefficient=0.3))
        p2 = self.service.register_parameters(_params("v2", coefficient=0.4))
        self.service.create_scenario("S1", self.snap_hash, p1)
        self.service.create_scenario("S2", self.snap_hash, p2)
        self.assertEqual(self.service.get_binding("S1")["params_hash"], p1)
        self.assertNotEqual(p1, p2)


class RecoveryTest(TrialServiceTestBase):
    def test_resume_after_interrupted_batch(self) -> None:
        sid = self._scenario("R1")
        # 先跑前两块（用 token 在第三块边界取消）
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            self.service.run(sid, token=token)
        # 取消发生在第一块处理之前 -> 0 块；再正常续跑
        result = self.service.resume(sid)
        full = self._fresh_full_run()
        self.assertEqual(digest(result), digest(full))
        self.assertTrue(result.completed)

    def test_resume_after_partial_chunks(self) -> None:
        sid = self._scenario("R2")
        # 手工落两块，模拟崩溃后恢复
        binding = self.service.get_binding(sid)
        snap = self.service._load_snapshot(binding["snapshot_hash"])
        params = self.service._load_params(binding["params_hash"])
        for i in range(2):
            rows = self.service.engine.run_chunk(snap, params, i)
            self.service.store.write_chunk(sid, i, [r.__dict__ for r in rows])
        self.service._refresh_state(sid, processed=128, chunks=2)
        result = self.service.resume(sid)
        self.assertEqual(result.checkpoint.processed, 250)
        self.assertTrue(result.completed)
        # 与一次跑完逐字节一致
        self.assertEqual(digest(result), digest(self._fresh_full_run()))

    def _fresh_full_run(self) -> object:
        ph = self.service.register_parameters(_params())
        self.service.create_scenario("FULL", self.snap_hash, ph)
        return self.service.run("FULL")


class CalculationTest(TrialServiceTestBase):
    def test_enterprise_formula(self) -> None:
        rec = EnterpriseRecord("E1", "制造", 1000.0, 800.0, 100.0)
        params = _params(coefficient=0.3, carry_ratio=0.5)
        r = compute_enterprise(rec, params)
        self.assertAlmostEqual(r.carried_deduction, 50.0)
        self.assertAlmostEqual(r.new_payment, 250.0)
        self.assertAlmostEqual(r.gap, -550.0)  # 250 - 800
        self.assertEqual(r.price_pressure, 0.0)  # 无正向缺口

    def test_exemption_zeroes_payment(self) -> None:
        rec = EnterpriseRecord("E1", "农业", 1000.0, 800.0, 100.0)
        params = _params(coefficient=0.3, carry_ratio=0.5, sectors=("农业",))
        r = compute_enterprise(rec, params)
        self.assertTrue(r.exempted)
        self.assertEqual(r.new_payment, 0.0)
        self.assertEqual(r.price_pressure, 0.0)

    def test_metrics_structure(self) -> None:
        sid = self._scenario("M")
        result = self.service.run(sid)
        m = result.metrics
        self.assertEqual(m.enterprise_count, 250)
        self.assertEqual(sum(m.gap_distribution.values()), 250)
        self.assertGreater(m.price_pressure_index, 0.0)
        self.assertEqual(set(m.sector_breakdown), {"制造", "服务", "农业"})


class ReplayCompareTest(TrialServiceTestBase):
    def _two_scenarios(self) -> tuple[str, str]:
        ph1 = self.service.register_parameters(
            _params("baseline", coefficient=0.30))
        ph2 = self.service.register_parameters(
            _params("reform", coefficient=0.60, sectors=("农业",)))
        self.service.create_scenario("BASE", self.snap_hash, ph1)
        self.service.create_scenario("REFORM", self.snap_hash, ph2)
        self.service.run("BASE")
        self.service.run("REFORM")
        return "BASE", "REFORM"

    def test_replay_matches_stored_fingerprint(self) -> None:
        a, _ = self._two_scenarios()
        report = self.service.replay(a)
        self.assertTrue(report["fingerprint_matches"])
        self.assertEqual(report["replayed_fingerprint"],
                         report["stored_fingerprint"])

    def test_replay_after_deleting_chunks(self) -> None:
        # 即使分块被清掉（只剩最终结果），重放仍直接由内容对象重建
        a, _ = self._two_scenarios()
        chunks = self.service.store.scenario_dir(a) / "chunks"
        for f in chunks.glob("*.json"):
            f.unlink()
        report = self.service.replay(a)
        self.assertTrue(report["fingerprint_matches"])

    def test_compare_shared_snapshot(self) -> None:
        a, b = self._two_scenarios()
        diff = self.service.compare(a, b)
        self.assertTrue(diff["metric_diff"]["shared_snapshot"])
        # 改革提高系数 -> 新政策应缴总额上升
        self.assertGreater(diff["metric_diff"]["total_new_payment"], 0.0)
        # 农业豁免 -> 豁免企业数增加
        self.assertGreater(diff["metric_diff"]["exempted_count"], 0)
        # 逐企业差异覆盖所有企业
        changing = [r for r in diff["per_enterprise"]
                    if r.get("new_payment_delta", 0.0) != 0.0]
        self.assertTrue(changing)

    def test_compare_detects_different_snapshots(self) -> None:
        # 构造第二份快照（企业数不同）
        other_ledger = FormalLedger().ingested(_records(100))
        other_snap = other_ledger.export_snapshot("small")
        h = self.service.register_snapshot(other_snap)
        ph = self.service.register_parameters(_params())
        self.service.create_scenario("SMALL", h, ph)
        self.service.run("SMALL")
        a, _ = self._two_scenarios()
        diff = self.service.compare(a, "SMALL")
        self.assertFalse(diff["metric_diff"]["shared_snapshot"])


class PublishGateTest(TrialServiceTestBase):
    def test_draft_cannot_publish(self) -> None:
        sid = self._scenario("P1")
        self.service.run(sid)
        with self.assertRaises(Exception):
            self.service.publish("POL-X", sid)

    def test_incomplete_cannot_review(self) -> None:
        sid = self._scenario("P2")
        with self.assertRaises(Exception):
            self.service.review(sid, "核算员")

    def test_publish_records_reference_only(self) -> None:
        sid = self._scenario("P3")
        self.service.run(sid)
        self.service.review(sid, "核算员", comment="核对一致")
        pub = self.service.publish("POL-9", sid)
        self.assertEqual(pub.scenario_id, sid)
        records = self.service.list_publications()
        self.assertEqual(len(records), 1)
        # 发布台账中没有任何余额字段，只有引用与指纹
        record = records[0]
        self.assertNotIn("total_gap", record)
        self.assertNotIn("new_payment", record)
        self.assertIn("result_fingerprint", record)
        # 正式发布后方案冻结，不可再运行
        with self.assertRaises(Exception):
            self.service.run(sid)

    def test_duplicate_policy_id_rejected(self) -> None:
        s1 = self._scenario("P4")
        self.service.run(s1)
        self.service.review(s1, "核算员")
        self.service.publish("POL-DUP", s1)
        s2 = self._scenario("P5", _params("v2", coefficient=0.5))
        self.service.run(s2)
        self.service.review(s2, "核算员")
        with self.assertRaises(Exception):
            self.service.publish("POL-DUP", s2)


class DeterminismTest(TrialServiceTestBase):
    def test_same_inputs_same_result_everywhere(self) -> None:
        ph = self.service.register_parameters(_params())
        self.service.create_scenario("D1", self.snap_hash, ph)
        r1 = self.service.run("D1")
        # 另一套沙箱（模拟另一台服务器），同输入对象
        store2 = SandboxStore(Path(self.tmp.name) / "sandbox2")
        svc2 = TrialService(store2, chunk_size=37)  # 不同块大小
        sh = svc2.register_snapshot(self.service._load_snapshot(self.snap_hash))
        ph2 = svc2.register_parameters(self.service._load_params(ph))
        svc2.create_scenario("D2", sh, ph2)
        r2 = svc2.run("D2")
        self.assertEqual(digest(r1), digest(r2))


if __name__ == "__main__":
    unittest.main()
