"""确定性试算引擎：分块处理、断点续跑、可中断。

计算口径（对快照中的每家企业）：
- 豁免企业（排放量低于豁免阈值，或在显式豁免名单内）：配额全额覆盖，缺口为 0；
- 其余企业：应发配额 = 产出量 × 行业基准系数 × 全局调整因子；
  可结转量 = 配额结余 × 结转比例；缺口 = 排放量 − 应发配额 − 可结转量。

价格压力指标 = 100 × 总缺口率 × (1 − 结转比例)，缺口率越高、
结转比例越低，市场补缺口需求越大，价格压力越高。

引擎按企业编号排序后分块处理，每块结束写入断点；同样的输入
无论何时重放、分几块跑，内容指纹完全一致。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import BatchInterrupted
from .models import (
    GAP_BANDS,
    BatchRun,
    EnterpriseRecord,
    ParameterVersion,
    RunStatus,
    ScenarioResult,
)
from .store import JsonStore, canonical_digest

MILD_GAP_LIMIT = 0.05  # 缺口率 ≤ 5% 记为轻度缺口
SEVERE_GAP_LIMIT = 0.15  # 缺口率 ≤ 15% 记为中度缺口，超过记为重度缺口

PRECISION = 6


def _round(value: float) -> float:
    return round(value, PRECISION)


def classify_band(exempt: bool, gap: float, emissions: float) -> str:
    """按缺口率把企业分带。"""
    if exempt:
        return "豁免"
    if gap < 0:
        return "盈余"
    if gap == 0:
        return "平衡"
    ratio = gap / emissions if emissions > 0 else float("inf")
    if ratio <= MILD_GAP_LIMIT:
        return "轻度缺口"
    if ratio <= SEVERE_GAP_LIMIT:
        return "中度缺口"
    return "重度缺口"


def evaluate_enterprise(record: EnterpriseRecord, params: ParameterVersion) -> dict[str, Any]:
    """计算单家企业的试算结果。"""
    exempt = (
        record.verified_emissions < params.exemption_threshold
        or record.enterprise_id in params.exemption_enterprise_ids
    )
    if exempt:
        allocation = record.verified_emissions
        carried = 0.0
        gap = 0.0
    else:
        coefficient = params.coefficients.get(record.industry, params.coefficients.get("default", 1.0))
        allocation = record.output * coefficient * params.adjustment_factor
        carried = record.allowance_balance * params.carry_ratio
        gap = record.verified_emissions - allocation - carried
    return {
        "enterprise_id": record.enterprise_id,
        "industry": record.industry,
        "exempt": exempt,
        "emissions": _round(record.verified_emissions),
        "allocation": _round(allocation),
        "carried": _round(carried),
        "gap": _round(gap),
        "band": classify_band(exempt, _round(gap), record.verified_emissions),
    }


def build_metrics(outcomes: list[dict[str, Any]], params: ParameterVersion) -> dict[str, Any]:
    """汇总企业分布、缺口与价格压力指标。"""
    count = len(outcomes)
    total_emissions = _round(sum(item["emissions"] for item in outcomes))
    total_allocation = _round(sum(item["allocation"] for item in outcomes))
    total_carried = _round(sum(item["carried"] for item in outcomes))
    total_gap = _round(sum(item["gap"] for item in outcomes if item["gap"] > 0))
    total_surplus = _round(sum(-item["gap"] for item in outcomes if item["gap"] < 0))
    gap_enterprises = sum(1 for item in outcomes if item["gap"] > 0)
    gap_ratio = _round(total_gap / total_emissions) if total_emissions > 0 else 0.0
    distribution = {band: 0 for band in GAP_BANDS}
    for item in outcomes:
        distribution[item["band"]] += 1
    return {
        "enterprise_count": count,
        "exempt_count": sum(1 for item in outcomes if item["exempt"]),
        "total_emissions": total_emissions,
        "total_allocation": total_allocation,
        "total_carried": total_carried,
        "total_gap": total_gap,
        "total_surplus": total_surplus,
        "gap_enterprise_count": gap_enterprises,
        "gap_enterprise_ratio": _round(gap_enterprises / count) if count else 0.0,
        "gap_ratio": gap_ratio,
        "price_pressure_index": _round(100 * gap_ratio * (1 - params.carry_ratio)),
        "distribution": distribution,
    }


class TrialEngine:
    """在隔离工作区内执行试算批次。"""

    def __init__(self, store: JsonStore):
        self._store = store

    def execute(self, run_id: str, *, interrupt_after_chunks: int | None = None) -> ScenarioResult:
        """执行（或恢复）一个批次。

        interrupt_after_chunks：本次调用最多处理的块数，达到后抛出
        BatchInterrupted 并保留断点，用于模拟中断与恢复。
        """
        store = self._store
        run = store.load_run(run_id)
        snapshot = store.load_snapshot(run.snapshot_id)
        params = store.load_parameters(run.parameter_version_id)
        records = sorted(snapshot.records, key=lambda item: item.enterprise_id)

        checkpoint = store.load_checkpoint(run_id)
        outcomes: list[dict[str, Any]] = list(checkpoint["outcomes"]) if checkpoint else []
        processed = len(outcomes)

        chunks_done = 0
        while processed < len(records):
            chunk = records[processed : processed + run.chunk_size]
            outcomes.extend(evaluate_enterprise(record, params) for record in chunk)
            processed += len(chunk)
            chunks_done += 1
            store.save_checkpoint(run_id, {"run_id": run_id, "processed": processed, "outcomes": outcomes})
            run.processed = processed
            store.save_run(run)
            if (
                interrupt_after_chunks is not None
                and chunks_done >= interrupt_after_chunks
                and processed < len(records)
            ):
                raise BatchInterrupted(f"批次 {run_id} 在 {processed}/{len(records)} 处中断")

        metrics = build_metrics(outcomes, params)
        snapshot_digest = canonical_digest(snapshot.to_dict())
        parameter_digest = canonical_digest(params.to_dict())
        digest = canonical_digest(
            {
                "snapshot_digest": snapshot_digest,
                "parameter_digest": parameter_digest,
                "metrics": metrics,
                "outcomes": outcomes,
            }
        )
        result = ScenarioResult(
            run_id=run.run_id,
            scenario_id=run.scenario_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=snapshot_digest,
            parameter_version_id=params.version_id,
            parameter_digest=parameter_digest,
            metrics=metrics,
            outcomes=tuple(outcomes),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            digest=digest,
        )
        store.save_result(result)
        store.clear_checkpoint(run_id)
        run.status = RunStatus.COMPLETED
        run.finished_at = result.finished_at
        store.save_run(run)
        return result
