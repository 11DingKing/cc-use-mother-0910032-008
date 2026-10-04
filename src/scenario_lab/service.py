"""情景试算服务：隔离运行、复核发布、重放比较的 API 边界。

隔离与合规约定：
- 历史快照从正式账户单向复制进试算环境，试算余额绝不回流正式账户；
- 快照与参数版本共享、只读、不可覆盖，每个批次只写自己的独立工作区；
- 取消批次会清理该批次的全部临时结果；
- 正式发布只能引用已复核（已确认）方案的结果摘要与指纹，不能复制试算余额；
- 任意批次可按固化的输入重放，任意两次结果可比较。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import TrialEngine
from .errors import BatchInterrupted, ReleaseError, StateError
from .models import (
    BatchRun,
    ParameterVersion,
    PolicyRelease,
    RunStatus,
    Scenario,
    ScenarioResult,
    ScenarioState,
    Snapshot,
)
from .store import JsonStore

#: 正式发布载荷中禁止出现的字段（试算余额与逐企业明细）。
FORBIDDEN_RELEASE_KEYS = {"outcomes", "balances", "per_enterprise", "records", "trial_balances"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _assert_reference_only(payload: Any) -> None:
    """递归检查发布载荷，确保没有夹带试算余额。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_RELEASE_KEYS:
                raise ReleaseError(f"正式发布不能复制试算余额，载荷中禁止出现字段：{key}")
            _assert_reference_only(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_reference_only(item)


class ScenarioLabService:
    """政策调整情景试算的服务端入口。"""

    def __init__(self, root: str | Path):
        self.store = JsonStore(root)
        self.engine = TrialEngine(self.store)

    # ---- 共享输入：快照复制与参数版本 ----

    def register_snapshot(self, snapshot: Snapshot) -> Snapshot:
        """把指定历史快照复制进试算环境（单向，只读）。"""
        if not snapshot.records:
            raise ValueError("快照必须至少包含一家企业")
        self.store.save_snapshot(snapshot)
        return snapshot

    def register_parameters(self, params: ParameterVersion) -> ParameterVersion:
        """注册参数版本；版本号一经注册即冻结，调整参数只能注册新版本。"""
        self.store.save_parameters(params)
        return params

    # ---- 方案生命周期 ----

    def create_scenario(self, name: str, snapshot_id: str, created_by: str) -> Scenario:
        self.store.load_snapshot(snapshot_id)  # 校验快照存在
        scenario = Scenario(
            scenario_id=_new_id("scn"),
            name=name,
            snapshot_id=snapshot_id,
            created_by=created_by,
            created_at=_now(),
        )
        self.store.save_scenario(scenario)
        return scenario

    def bind_parameters(self, scenario_id: str, parameter_version_id: str) -> Scenario:
        """绑定参数版本；只有草稿 / 待核算状态允许绑定或改绑。"""
        scenario = self.store.load_scenario(scenario_id)
        if scenario.state not in (ScenarioState.DRAFT, ScenarioState.PENDING):
            raise StateError(f"方案当前状态为「{scenario.state}」，不允许改绑参数版本")
        self.store.load_parameters(parameter_version_id)  # 校验版本存在
        scenario.parameter_version_id = parameter_version_id
        scenario.state = ScenarioState.PENDING
        self.store.save_scenario(scenario)
        return scenario

    def start_run(
        self,
        scenario_id: str,
        *,
        chunk_size: int = 500,
        interrupt_after_chunks: int | None = None,
    ) -> BatchRun:
        """启动一个可恢复批次；批次固化当前绑定的快照与参数版本。"""
        scenario = self.store.load_scenario(scenario_id)
        if scenario.state != ScenarioState.PENDING:
            raise StateError(f"方案当前状态为「{scenario.state}」，只有待核算方案可以启动试算")
        if not scenario.parameter_version_id:
            raise StateError("方案尚未绑定参数版本")
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正数")
        snapshot = self.store.load_snapshot(scenario.snapshot_id)
        run = BatchRun(
            run_id=_new_id("run"),
            scenario_id=scenario.scenario_id,
            snapshot_id=scenario.snapshot_id,
            parameter_version_id=scenario.parameter_version_id,
            chunk_size=chunk_size,
            total=len(snapshot.records),
            started_at=_now(),
        )
        self.store.save_run(run)
        self.store.workspace(run.run_id)
        scenario.state = ScenarioState.RUNNING
        self.store.save_scenario(scenario)
        return self._execute(run, interrupt_after_chunks)

    def resume_run(self, run_id: str, *, interrupt_after_chunks: int | None = None) -> BatchRun:
        """从断点恢复批次。"""
        run = self.store.load_run(run_id)
        if run.status != RunStatus.PAUSED:
            raise StateError(f"批次当前状态为「{run.status}」，只有已暂停的批次可以恢复")
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        return self._execute(run, interrupt_after_chunks)

    def cancel_run(self, run_id: str) -> BatchRun:
        """取消批次：清理该批次的全部临时结果，方案回到待核算。"""
        run = self.store.load_run(run_id)
        if run.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            raise StateError(f"批次当前状态为「{run.status}」，无法取消")
        run.status = RunStatus.CANCELLED
        run.finished_at = _now()
        self.store.save_run(run)
        self.store.purge_workspace(run_id)
        scenario = self.store.load_scenario(run.scenario_id)
        if scenario.state == ScenarioState.RUNNING:
            scenario.state = ScenarioState.PENDING
            self.store.save_scenario(scenario)
        return run

    def review_scenario(self, scenario_id: str, reviewer: str, note: str = "") -> Scenario:
        """复核试算结果，方案进入已确认。"""
        scenario = self.store.load_scenario(scenario_id)
        if scenario.state != ScenarioState.PENDING or not scenario.result_run_id:
            raise StateError("只有完成试算且尚未复核的方案可以复核")
        scenario.state = ScenarioState.CONFIRMED
        scenario.reviewed_by = reviewer
        scenario.review_note = note
        self.store.save_scenario(scenario)
        return scenario

    def publish_release(
        self,
        scenario_id: str,
        published_by: str,
        *,
        include_balances: bool = False,
    ) -> PolicyRelease:
        """正式发布：只引用已复核方案的结果摘要与指纹。

        include_balances 是显式护栏：任何试图把试算余额带进正式发布的
        调用都会被拒绝。
        """
        if include_balances:
            raise ReleaseError("正式发布只能引用已复核方案，禁止复制试算余额")
        scenario = self.store.load_scenario(scenario_id)
        if scenario.state != ScenarioState.CONFIRMED:
            raise ReleaseError(f"方案当前状态为「{scenario.state}」，只有已确认（已复核）方案才能用于正式发布")
        result = self.store.load_result(scenario.result_run_id)
        release = PolicyRelease(
            release_id=_new_id("rel"),
            scenario_id=scenario.scenario_id,
            run_id=result.run_id,
            snapshot_id=result.snapshot_id,
            parameter_version_id=result.parameter_version_id,
            result_digest=result.digest,
            metric_summary=result.metrics,
            published_by=published_by,
            published_at=_now(),
        )
        _assert_reference_only(release.to_dict())
        self.store.save_release(release)
        scenario.state = ScenarioState.SEALED
        self.store.save_scenario(scenario)
        return release

    # ---- 结果查询、重放与比较 ----

    def get_scenario(self, scenario_id: str) -> Scenario:
        return self.store.load_scenario(scenario_id)

    def get_run(self, run_id: str) -> BatchRun:
        return self.store.load_run(run_id)

    def get_result(self, run_id: str) -> ScenarioResult:
        return self.store.load_result(run_id)

    def replay_run(self, run_id: str) -> dict[str, Any]:
        """按批次固化的输入重放一次，校验内容指纹是否一致。

        重放在一次性影子工作区内进行，结束后立即清理，不污染原结果。
        """
        original_run = self.store.load_run(run_id)
        original = self.store.load_result(run_id)
        shadow = BatchRun(
            run_id=f"{run_id}-replay",
            scenario_id=original_run.scenario_id,
            snapshot_id=original_run.snapshot_id,
            parameter_version_id=original_run.parameter_version_id,
            chunk_size=original_run.chunk_size,
            total=original_run.total,
            started_at=_now(),
        )
        self.store.save_run(shadow)
        self.store.workspace(shadow.run_id)
        try:
            replayed = self.engine.execute(shadow.run_id)
        finally:
            self.store.purge_workspace(shadow.run_id)
            self.store.delete_run_meta(shadow.run_id)
        return {
            "run_id": run_id,
            "original_digest": original.digest,
            "replayed_digest": replayed.digest,
            "identical": replayed.digest == original.digest,
        }

    def compare_runs(self, left_run_id: str, right_run_id: str, *, top: int = 10) -> dict[str, Any]:
        """比较任意两次试算结果：指标差异、分布迁移与缺口变化最大的企业。"""
        left = self.store.load_result(left_run_id)
        right = self.store.load_result(right_run_id)

        metric_delta: dict[str, Any] = {}
        for key, left_value in left.metrics.items():
            if key == "distribution":
                continue
            right_value = right.metrics.get(key)
            metric_delta[key] = {
                "left": left_value,
                "right": right_value,
                "delta": _delta(left_value, right_value),
            }

        distribution_shift: dict[str, Any] = {}
        for band, left_count in left.metrics["distribution"].items():
            right_count = right.metrics["distribution"].get(band, 0)
            distribution_shift[band] = {
                "left": left_count,
                "right": right_count,
                "delta": right_count - left_count,
            }

        left_outcomes = {item["enterprise_id"]: item for item in left.outcomes}
        right_outcomes = {item["enterprise_id"]: item for item in right.outcomes}
        movers = []
        for enterprise_id in sorted(set(left_outcomes) | set(right_outcomes)):
            left_item = left_outcomes.get(enterprise_id)
            right_item = right_outcomes.get(enterprise_id)
            left_gap = left_item["gap"] if left_item else None
            right_gap = right_item["gap"] if right_item else None
            movers.append(
                {
                    "enterprise_id": enterprise_id,
                    "left_gap": left_gap,
                    "right_gap": right_gap,
                    "gap_delta": _delta(left_gap, right_gap),
                    "left_band": left_item["band"] if left_item else None,
                    "right_band": right_item["band"] if right_item else None,
                }
            )
        movers.sort(key=lambda item: abs(item["gap_delta"] or 0.0), reverse=True)

        return {
            "left_run_id": left_run_id,
            "right_run_id": right_run_id,
            "left_digest": left.digest,
            "right_digest": right.digest,
            "identical": left.digest == right.digest,
            "metric_delta": metric_delta,
            "distribution_shift": distribution_shift,
            "top_movers": movers[:top],
        }

    # ---- 内部 ----

    def _execute(self, run: BatchRun, interrupt_after_chunks: int | None) -> BatchRun:
        try:
            self.engine.execute(run.run_id, interrupt_after_chunks=interrupt_after_chunks)
        except BatchInterrupted:
            paused = self.store.load_run(run.run_id)
            paused.status = RunStatus.PAUSED
            self.store.save_run(paused)
            return paused
        scenario = self.store.load_scenario(run.scenario_id)
        scenario.result_run_id = run.run_id
        scenario.state = ScenarioState.PENDING
        self.store.save_scenario(scenario)
        return self.store.load_run(run.run_id)


def _delta(left: Any, right: Any) -> float | None:
    if left is None or right is None:
        return None
    return round(right - left, 6)
