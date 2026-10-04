"""隔离试算环境的回归测试，覆盖契约的四条不变量与发布合规。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scenario_lab import (  # noqa: E402
    AlreadyExistsError,
    EnterpriseRecord,
    ParameterVersion,
    ReleaseError,
    RunStatus,
    ScenarioLabService,
    ScenarioState,
    Snapshot,
    StateError,
)
from scenario_lab.store import canonical_digest  # noqa: E402

NOW = "2026-10-04T00:00:00+00:00"


def make_snapshot() -> Snapshot:
    records = [
        EnterpriseRecord("E001", "钢一", "steel", 120000, 10000, 60000),
        EnterpriseRecord("E002", "钢二", "steel", 80000, 2000, 50000),
        EnterpriseRecord("E003", "水泥一", "cement", 60000, 5000, 40000),
        EnterpriseRecord("E004", "水泥二", "cement", 45000, 0, 30000),
        EnterpriseRecord("E005", "化工一", "chemical", 30000, 12000, 20000),
        EnterpriseRecord("E006", "造纸一", "paper", 8000, 500, 6000),
        EnterpriseRecord("E007", "电力一", "power", 200000, 30000, 150000),
        EnterpriseRecord("E008", "纺织一", "textile", 9000, 100, 7000),
        EnterpriseRecord("E009", "有色一", "nonferrous", 50000, 40000, 25000),
        EnterpriseRecord("E010", "玻璃一", "glass", 25000, 1000, 15000),
    ]
    return Snapshot(
        snapshot_id="snap-2024",
        period="2024",
        source="正式账户台账",
        copied_at=NOW,
        records=tuple(records),
    )


def make_params(version_id: str = "param-v1", *, carry_ratio: float = 0.5, strict: bool = False) -> ParameterVersion:
    coefficients = {
        "steel": 1.05,
        "cement": 1.0,
        "chemical": 1.0,
        "power": 1.1,
        "nonferrous": 1.0,
        "glass": 1.0,
        "default": 1.0,
    }
    if strict:  # 收紧方案：系数下调、结转比例降低、豁免门槛降低
        coefficients = {key: value * 0.9 for key, value in coefficients.items()}
    return ParameterVersion(
        version_id=version_id,
        label="基准方案" if not strict else "强化方案",
        coefficients=coefficients,
        adjustment_factor=1.0,
        carry_ratio=carry_ratio,
        exemption_threshold=10000 if not strict else 5000,
        exemption_enterprise_ids=(),
        created_at=NOW,
    )


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ScenarioLabService(self._tmp.name)
        self.service.register_snapshot(make_snapshot())
        self.service.register_parameters(make_params())

    def make_ready_scenario(self, name: str = "方案A", params: str = "param-v1") -> str:
        scenario = self.service.create_scenario(name, "snap-2024", created_by="核算专员")
        self.service.bind_parameters(scenario.scenario_id, params)
        return scenario.scenario_id


class LifecycleTest(ServiceTestBase):
    def test_full_lifecycle_follows_contract_states(self) -> None:
        scenario = self.service.create_scenario("方案A", "snap-2024", created_by="核算专员")
        self.assertEqual(scenario.state, ScenarioState.DRAFT)

        bound = self.service.bind_parameters(scenario.scenario_id, "param-v1")
        self.assertEqual(bound.state, ScenarioState.PENDING)

        run = self.service.start_run(scenario.scenario_id, chunk_size=4)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        after_run = self.service.get_scenario(scenario.scenario_id)
        self.assertEqual(after_run.state, ScenarioState.PENDING)
        self.assertEqual(after_run.result_run_id, run.run_id)

        reviewed = self.service.review_scenario(scenario.scenario_id, reviewer="监管审计员", note="口径无误")
        self.assertEqual(reviewed.state, ScenarioState.CONFIRMED)

        release = self.service.publish_release(scenario.scenario_id, published_by="交易运营员")
        self.assertEqual(release.run_id, run.run_id)
        self.assertTrue(release.reference_only)
        self.assertEqual(self.service.get_scenario(scenario.scenario_id).state, ScenarioState.SEALED)

    def test_run_requires_bound_parameters(self) -> None:
        scenario = self.service.create_scenario("方案A", "snap-2024", created_by="核算专员")
        with self.assertRaises(StateError):
            self.service.start_run(scenario.scenario_id)

    def test_review_requires_completed_result(self) -> None:
        scenario_id = self.make_ready_scenario()
        with self.assertRaises(StateError):
            self.service.review_scenario(scenario_id, reviewer="监管审计员")

    def test_sealed_scenario_rejects_rebind_and_rerun(self) -> None:
        scenario_id = self.make_ready_scenario()
        self.service.start_run(scenario_id)
        self.service.review_scenario(scenario_id, reviewer="监管审计员")
        self.service.publish_release(scenario_id, published_by="交易运营员")
        with self.assertRaises(StateError):
            self.service.bind_parameters(scenario_id, "param-v1")
        with self.assertRaises(StateError):
            self.service.start_run(scenario_id)


class IsolationTest(ServiceTestBase):
    def test_scenarios_share_inputs_without_pollution(self) -> None:
        self.service.register_parameters(make_params("param-v2", strict=True))
        snapshot_digest_before = canonical_digest(self.service.store.load_snapshot("snap-2024").to_dict())

        scenario_a = self.make_ready_scenario("基准", "param-v1")
        scenario_b = self.make_ready_scenario("强化", "param-v2")
        run_a = self.service.start_run(scenario_a, chunk_size=3)
        run_b = self.service.start_run(scenario_b, chunk_size=3)

        result_a = self.service.get_result(run_a.run_id)
        result_b = self.service.get_result(run_b.run_id)
        # 共享同一快照，但参数不同 → 结果不同
        self.assertEqual(result_a.snapshot_digest, result_b.snapshot_digest)
        self.assertNotEqual(result_a.digest, result_b.digest)
        self.assertGreater(result_b.metrics["total_gap"], result_a.metrics["total_gap"])

        # 方案 B 运行后，方案 A 的结果与共享快照都未被污染
        self.assertEqual(self.service.get_result(run_a.run_id).to_dict(), result_a.to_dict())
        snapshot_digest_after = canonical_digest(self.service.store.load_snapshot("snap-2024").to_dict())
        self.assertEqual(snapshot_digest_before, snapshot_digest_after)

    def test_inputs_are_immutable(self) -> None:
        with self.assertRaises(AlreadyExistsError):
            self.service.register_snapshot(make_snapshot())
        with self.assertRaises(AlreadyExistsError):
            self.service.register_parameters(make_params())

    def test_run_pins_parameter_version(self) -> None:
        """批次固化参数版本：事后改绑不影响已完成的批次。"""
        self.service.register_parameters(make_params("param-v2", strict=True))
        scenario_id = self.make_ready_scenario("方案A", "param-v1")
        run = self.service.start_run(scenario_id)
        self.service.bind_parameters(scenario_id, "param-v2")
        replay = self.service.replay_run(run.run_id)
        self.assertTrue(replay["identical"])


class ResumableBatchTest(ServiceTestBase):
    def test_resumed_run_matches_uninterrupted_run(self) -> None:
        scenario_a = self.make_ready_scenario("一口气跑完")
        run_a = self.service.start_run(scenario_a, chunk_size=3)

        scenario_b = self.make_ready_scenario("中断恢复")
        paused = self.service.start_run(scenario_b, chunk_size=3, interrupt_after_chunks=1)
        self.assertEqual(paused.status, RunStatus.PAUSED)
        self.assertEqual(paused.processed, 3)
        self.assertEqual(self.service.get_scenario(scenario_b).state, ScenarioState.RUNNING)

        resumed = self.service.resume_run(paused.run_id)
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.processed, resumed.total)

        # 同一快照、同一参数版本：断点续跑与一次跑完的内容指纹一致
        self.assertEqual(
            self.service.get_result(run_a.run_id).digest,
            self.service.get_result(resumed.run_id).digest,
        )

    def test_cancel_purges_temporary_results(self) -> None:
        scenario_id = self.make_ready_scenario()
        paused = self.service.start_run(scenario_id, chunk_size=3, interrupt_after_chunks=1)
        workspace = self.service.store.workspace(paused.run_id)
        self.assertTrue((workspace / "checkpoint.json").exists())

        cancelled = self.service.cancel_run(paused.run_id)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertFalse(self.service.store.workspace_exists(paused.run_id))
        with self.assertRaises(Exception):
            self.service.get_result(paused.run_id)

        # 方案回到待核算，可以重新开跑
        scenario = self.service.get_scenario(scenario_id)
        self.assertEqual(scenario.state, ScenarioState.PENDING)
        rerun = self.service.start_run(scenario_id, chunk_size=3)
        self.assertEqual(rerun.status, RunStatus.COMPLETED)

    def test_resume_requires_paused_run(self) -> None:
        scenario_id = self.make_ready_scenario()
        run = self.service.start_run(scenario_id)
        with self.assertRaises(StateError):
            self.service.resume_run(run.run_id)


class ReleaseComplianceTest(ServiceTestBase):
    def test_release_requires_reviewed_scenario(self) -> None:
        scenario_id = self.make_ready_scenario()
        self.service.start_run(scenario_id)
        with self.assertRaises(ReleaseError):
            self.service.publish_release(scenario_id, published_by="交易运营员")

    def test_release_never_copies_trial_balances(self) -> None:
        scenario_id = self.make_ready_scenario()
        self.service.start_run(scenario_id)
        self.service.review_scenario(scenario_id, reviewer="监管审计员")

        with self.assertRaises(ReleaseError):
            self.service.publish_release(scenario_id, published_by="交易运营员", include_balances=True)

        release = self.service.publish_release(scenario_id, published_by="交易运营员")
        payload = release.to_dict()
        for forbidden in ("outcomes", "balances", "records", "trial_balances"):
            self.assertNotIn(forbidden, payload)
        # 发布只携带指标摘要与内容指纹，不携带逐企业余额
        self.assertIn("total_gap", payload["metric_summary"])
        self.assertEqual(len(payload["result_digest"]), 64)


class ReplayAndCompareTest(ServiceTestBase):
    def test_replay_is_deterministic(self) -> None:
        scenario_id = self.make_ready_scenario()
        run = self.service.start_run(scenario_id, chunk_size=4)
        report = self.service.replay_run(run.run_id)
        self.assertTrue(report["identical"])
        self.assertEqual(report["original_digest"], report["replayed_digest"])
        # 重放的影子工作区已清理
        self.assertFalse(self.service.store.workspace_exists(f"{run.run_id}-replay"))

    def test_compare_reports_metric_and_distribution_deltas(self) -> None:
        self.service.register_parameters(make_params("param-v2", strict=True))
        run_a = self.service.start_run(self.make_ready_scenario("基准", "param-v1"))
        run_b = self.service.start_run(self.make_ready_scenario("强化", "param-v2"))

        report = self.service.compare_runs(run_a.run_id, run_b.run_id)
        self.assertFalse(report["identical"])
        self.assertGreater(report["metric_delta"]["total_gap"]["delta"], 0)
        self.assertGreater(report["metric_delta"]["price_pressure_index"]["delta"], 0)
        self.assertEqual(
            report["metric_delta"]["total_gap"]["delta"],
            report["metric_delta"]["total_gap"]["right"] - report["metric_delta"]["total_gap"]["left"],
        )
        self.assertEqual(len(report["distribution_shift"]), 6)
        self.assertEqual(len(report["top_movers"]), 10)
        # 缺口变化最大的企业排在最前
        deltas = [abs(item["gap_delta"]) for item in report["top_movers"]]
        self.assertEqual(deltas, sorted(deltas, reverse=True))


class EngineMathTest(ServiceTestBase):
    def test_metrics_match_hand_computed_values(self) -> None:
        snapshot = Snapshot(
            snapshot_id="snap-tiny",
            period="2024",
            source="正式账户台账",
            copied_at=NOW,
            records=(
                EnterpriseRecord("E1", "甲", "steel", 100.0, 10.0, 90.0),
                EnterpriseRecord("E2", "乙", "paper", 5.0, 0.0, 4.0),
            ),
        )
        params = ParameterVersion(
            version_id="param-tiny",
            label="手算校验",
            coefficients={"steel": 1.0, "default": 1.0},
            adjustment_factor=1.0,
            carry_ratio=0.5,
            exemption_threshold=10.0,
            exemption_enterprise_ids=(),
            created_at=NOW,
        )
        self.service.register_snapshot(snapshot)
        self.service.register_parameters(params)
        scenario = self.service.create_scenario("手算", "snap-tiny", created_by="核算专员")
        self.service.bind_parameters(scenario.scenario_id, "param-tiny")
        run = self.service.start_run(scenario.scenario_id, chunk_size=1)
        result = self.service.get_result(run.run_id)

        outcomes = {item["enterprise_id"]: item for item in result.outcomes}
        # E1：应发 90×1.0=90，结转 10×0.5=5，缺口 100-90-5=5，缺口率 5% → 轻度缺口
        self.assertEqual(outcomes["E1"]["allocation"], 90.0)
        self.assertEqual(outcomes["E1"]["carried"], 5.0)
        self.assertEqual(outcomes["E1"]["gap"], 5.0)
        self.assertEqual(outcomes["E1"]["band"], "轻度缺口")
        # E2：排放量 5 低于阈值 10 → 豁免，全额覆盖
        self.assertTrue(outcomes["E2"]["exempt"])
        self.assertEqual(outcomes["E2"]["gap"], 0.0)
        self.assertEqual(outcomes["E2"]["band"], "豁免")

        metrics = result.metrics
        self.assertEqual(metrics["enterprise_count"], 2)
        self.assertEqual(metrics["exempt_count"], 1)
        self.assertEqual(metrics["total_emissions"], 105.0)
        self.assertEqual(metrics["total_gap"], 5.0)
        self.assertEqual(metrics["gap_ratio"], round(5 / 105, 6))
        # 价格压力 = 100 × 缺口率 × (1 − 结转比例)
        self.assertEqual(metrics["price_pressure_index"], round(100 * round(5 / 105, 6) * 0.5, 6))
        self.assertEqual(metrics["distribution"]["豁免"], 1)
        self.assertEqual(metrics["distribution"]["轻度缺口"], 1)


if __name__ == "__main__":
    unittest.main()
