"""可恢复、可取消的分块试算引擎。

设计要点：

- **纯函数计算**：给定（快照, 参数版本），逐企业结果与汇总指标完全确定，
  不依赖墙上时钟或随机数，因此任意一次运行都可以被逐字节重放。
- **分块 + 检查点**：企业按固定块大小切分，每块结果独立落盘后才推进检查点；
  进程中断后 :meth:`TrialEngine.resume` 从最后一个完整块之后继续。
- **协作式取消**：运行循环每处理一块检查一次取消标志；被取消的任务
  由服务层清理工作区，临时分块全部删除。
- **绝不触达正式账户**：引擎只接收不可变快照对象，没有任何写回接口。
"""
from __future__ import annotations

import threading

from .models import (
    BatchCheckpoint,
    EnterpriseRecord,
    EnterpriseResult,
    HistoricalSnapshot,
    ParameterVersion,
    ScenarioMetrics,
    ScenarioResult,
)

ENGINE_VERSION = "trial-engine/1"

# 缺口分桶边界（相对当前实缴的比例）
_GAP_BUCKETS = (
    ("surplus", float("-inf")),    # 缺口 < 0（盈余）
    ("mild", 0.05),                # [0, 5%)
    ("moderate", 0.20),            # [5%, 20%)
    ("severe", float("inf")),      # >= 20%
)


def _is_exempt(record: EnterpriseRecord, params: ParameterVersion) -> bool:
    return (
        record.sector in params.exemption.sectors
        or record.enterprise_id in params.exemption.enterprise_ids
    )


def compute_enterprise(record: EnterpriseRecord,
                       params: ParameterVersion) -> EnterpriseResult:
    """计算单家企业的新政策结果（纯函数）。"""
    exempted = _is_exempt(record, params)
    carried_deduction = round(record.carry_over * params.carry_ratio, 6)
    if exempted:
        new_payment = 0.0
        carried_deduction = 0.0
    else:
        raw = record.base_amount * params.coefficient - carried_deduction
        new_payment = round(max(raw, 0.0), 6)
        if raw < 0:
            # 抵扣未用满部分不产生现金价值，按实际使用回算
            carried_deduction = round(record.base_amount * params.coefficient, 6)
    gap = round(new_payment - record.current_payment, 6)
    # 价格压力：净缺口按传导系数进入价格；豁免企业不形成价格压力
    pressure = round(max(gap, 0.0) * params.price_pass_through, 6) if not exempted else 0.0
    return EnterpriseResult(
        enterprise_id=record.enterprise_id,
        sector=record.sector,
        base_amount=record.base_amount,
        new_payment=new_payment,
        current_payment=record.current_payment,
        gap=gap,
        carried_deduction=carried_deduction,
        exempted=exempted,
        price_pressure=pressure,
    )


def _gap_bucket(result: EnterpriseResult) -> str:
    if result.gap < 0:
        return "surplus"
    denominator = result.current_payment or result.base_amount or 1.0
    ratio = result.gap / denominator
    if ratio < 0.05:
        return "mild"
    if ratio < 0.20:
        return "moderate"
    return "severe"


def aggregate(results: list[EnterpriseResult]) -> ScenarioMetrics:
    """把逐企业结果汇总为方案级指标（纯函数，顺序无关）。"""
    sector: dict[str, dict[str, float]] = {}
    pressure_by_sector: dict[str, float] = {}
    distribution = {name: 0.0 for name, _ in _GAP_BUCKETS}
    total_new = total_current = total_gap = total_pressure = 0.0
    exempted = 0

    for r in results:
        total_new += r.new_payment
        total_current += r.current_payment
        total_gap += r.gap
        total_pressure += r.price_pressure
        if r.exempted:
            exempted += 1
        bucket = _gap_bucket(r)
        distribution[bucket] += 1

        row = sector.setdefault(r.sector, {
            "count": 0.0, "new_payment": 0.0, "current_payment": 0.0,
            "gap": 0.0, "exempted": 0.0,
        })
        row["count"] += 1
        row["new_payment"] += r.new_payment
        row["current_payment"] += r.current_payment
        row["gap"] += r.gap
        row["exempted"] += 1 if r.exempted else 0
        pressure_by_sector[r.sector] = pressure_by_sector.get(r.sector, 0.0) + r.price_pressure

    count = len(results)
    # 价格压力指数：单位企业平均正向缺口压力，跨方案可比
    pressure_index = round(total_pressure / count, 6) if count else 0.0
    return ScenarioMetrics(
        enterprise_count=count,
        exempted_count=exempted,
        total_new_payment=round(total_new, 6),
        total_current_payment=round(total_current, 6),
        total_gap=round(total_gap, 6),
        sector_breakdown={k: {kk: round(vv, 6) for kk, vv in v.items()}
                         for k, v in sorted(sector.items())},
        gap_distribution={k: int(distribution[k]) for k, _ in _GAP_BUCKETS},
        price_pressure_index=pressure_index,
        price_pressure_by_sector={k: round(v, 6)
                                  for k, v in sorted(pressure_by_sector.items())},
    )


class CancellationToken:
    """协作式取消标志。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class TrialEngine:
    """分块试算引擎：无状态逻辑 + 通过存储层持久化进度。"""

    def __init__(self, chunk_size: int = 100) -> None:
        if chunk_size <= 0:
            raise ValueError("块大小必须为正")
        self.chunk_size = chunk_size

    def plan_chunks(self, total: int) -> int:
        return (total + self.chunk_size - 1) // self.chunk_size

    def run_chunk(self, snapshot: HistoricalSnapshot, params: ParameterVersion,
                  chunk_index: int) -> list[EnterpriseResult]:
        start = chunk_index * self.chunk_size
        end = min(start + self.chunk_size, len(snapshot.enterprises))
        return [compute_enterprise(rec, params)
                for rec in snapshot.enterprises[start:end]]

    def build_result(self, snapshot: HistoricalSnapshot,
                     params: ParameterVersion,
                     chunk_results: list[list[EnterpriseResult]],
                     input_fingerprint: str) -> ScenarioResult:
        all_results = [r for chunk in chunk_results for r in chunk]
        processed = len(all_results)
        completed = processed == len(snapshot.enterprises)
        # 完成态统一使用 chunk_index=-1，保证结果指纹与块大小无关、
        # 可在不同分块配置的机器上逐字节复现；未完成时才记录实际块位。
        chunk_index = -1 if completed else (len(chunk_results) - 1 if chunk_results else 0)
        return ScenarioResult(
            enterprise_results=tuple(all_results),
            metrics=aggregate(all_results),
            checkpoint=BatchCheckpoint(
                processed=processed,
                chunk_index=chunk_index,
            ),
            completed=completed,
            input_fingerprint=input_fingerprint,
            engine_version=ENGINE_VERSION,
        )
