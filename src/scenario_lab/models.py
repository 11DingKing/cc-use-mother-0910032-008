"""政策调整情景试算的领域模型。

方案状态机与 domain/contract.json 的 states 约定保持一致：

    草稿 -> 待核算 -> 执行中 -> 待核算（批次完成或取消）-> 已确认 -> 已封存

- 草稿：方案已创建、选定历史快照，尚未绑定参数版本。
- 待核算：参数版本已绑定，可以启动试算；批次完成或取消后也回到此状态。
- 执行中：批次正在运行或暂停待恢复。
- 已确认：试算结果经核算专员 / 监管审计员复核确认。
- 已封存：方案被正式发布引用，永久只读。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ScenarioState:
    """方案状态，取值与领域契约一致。"""

    DRAFT = "草稿"
    PENDING = "待核算"
    RUNNING = "执行中"
    CONFIRMED = "已确认"
    SEALED = "已封存"

    ALL = (DRAFT, PENDING, RUNNING, CONFIRMED, SEALED)


class RunStatus:
    """批次状态。"""

    RUNNING = "运行中"
    PAUSED = "已暂停"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


#: 缺口分带的固定顺序，分布指标按此输出。
GAP_BANDS = ("豁免", "盈余", "平衡", "轻度缺口", "中度缺口", "重度缺口")


@dataclass(frozen=True)
class EnterpriseRecord:
    """历史快照中的企业账户记录（正式账户的只读副本）。"""

    enterprise_id: str
    name: str
    industry: str
    verified_emissions: float  # 经核查排放量
    allowance_balance: float  # 配额结余
    output: float  # 产出量（用于基准法分配）

    def to_dict(self) -> dict[str, Any]:
        return {
            "enterprise_id": self.enterprise_id,
            "name": self.name,
            "industry": self.industry,
            "verified_emissions": self.verified_emissions,
            "allowance_balance": self.allowance_balance,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnterpriseRecord":
        return cls(
            enterprise_id=str(data["enterprise_id"]),
            name=str(data["name"]),
            industry=str(data["industry"]),
            verified_emissions=float(data["verified_emissions"]),
            allowance_balance=float(data["allowance_balance"]),
            output=float(data["output"]),
        )


@dataclass(frozen=True)
class Snapshot:
    """指定历史时点的账户快照副本，是各方案共享的只读输入。"""

    snapshot_id: str
    period: str
    source: str
    copied_at: str
    records: tuple[EnterpriseRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "period": self.period,
            "source": self.source,
            "copied_at": self.copied_at,
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            period=str(data["period"]),
            source=str(data["source"]),
            copied_at=str(data["copied_at"]),
            records=tuple(EnterpriseRecord.from_dict(item) for item in data["records"]),
        )


@dataclass(frozen=True)
class ParameterVersion:
    """试算参数版本：分配系数、结转比例与企业豁免规则。

    版本一经注册即冻结，方案绑定的是不可变的版本号，
    调整参数只能注册新版本，从而保证任意批次可重放。
    """

    version_id: str
    label: str
    coefficients: dict[str, float]  # 行业基准系数，"default" 为兜底
    adjustment_factor: float  # 全局调整因子
    carry_ratio: float  # 结余结转比例，[0, 1]
    exemption_threshold: float  # 豁免排放阈值（低于该值的企业豁免）
    exemption_enterprise_ids: tuple[str, ...]  # 显式豁免名单
    created_at: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.carry_ratio <= 1.0:
            raise ValueError("结转比例必须落在 [0, 1]")
        if self.adjustment_factor <= 0:
            raise ValueError("调整因子必须为正数")
        if self.exemption_threshold < 0:
            raise ValueError("豁免阈值不能为负")
        for industry, coefficient in self.coefficients.items():
            if coefficient < 0:
                raise ValueError(f"行业 {industry} 的系数不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "label": self.label,
            "coefficients": dict(self.coefficients),
            "adjustment_factor": self.adjustment_factor,
            "carry_ratio": self.carry_ratio,
            "exemption_threshold": self.exemption_threshold,
            "exemption_enterprise_ids": list(self.exemption_enterprise_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParameterVersion":
        return cls(
            version_id=str(data["version_id"]),
            label=str(data["label"]),
            coefficients={str(k): float(v) for k, v in data["coefficients"].items()},
            adjustment_factor=float(data["adjustment_factor"]),
            carry_ratio=float(data["carry_ratio"]),
            exemption_threshold=float(data["exemption_threshold"]),
            exemption_enterprise_ids=tuple(str(x) for x in data["exemption_enterprise_ids"]),
            created_at=str(data["created_at"]),
        )


@dataclass
class Scenario:
    """试算方案：一份快照副本 + 一个参数版本的绑定。"""

    scenario_id: str
    name: str
    snapshot_id: str
    created_by: str
    created_at: str
    state: str = ScenarioState.DRAFT
    parameter_version_id: str | None = None
    result_run_id: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "snapshot_id": self.snapshot_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "state": self.state,
            "parameter_version_id": self.parameter_version_id,
            "result_run_id": self.result_run_id,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(
            scenario_id=str(data["scenario_id"]),
            name=str(data["name"]),
            snapshot_id=str(data["snapshot_id"]),
            created_by=str(data["created_by"]),
            created_at=str(data["created_at"]),
            state=str(data["state"]),
            parameter_version_id=data.get("parameter_version_id"),
            result_run_id=data.get("result_run_id"),
            reviewed_by=data.get("reviewed_by"),
            review_note=data.get("review_note"),
        )


@dataclass
class BatchRun:
    """一次可恢复批次。

    批次在创建时固化快照与参数版本，此后方案重新绑定参数
    也不影响已发生的批次，任意两次结果都可比较、可重放。
    """

    run_id: str
    scenario_id: str
    snapshot_id: str
    parameter_version_id: str
    chunk_size: int
    total: int
    started_at: str
    status: str = RunStatus.RUNNING
    processed: int = 0
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "snapshot_id": self.snapshot_id,
            "parameter_version_id": self.parameter_version_id,
            "chunk_size": self.chunk_size,
            "total": self.total,
            "started_at": self.started_at,
            "status": self.status,
            "processed": self.processed,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchRun":
        return cls(
            run_id=str(data["run_id"]),
            scenario_id=str(data["scenario_id"]),
            snapshot_id=str(data["snapshot_id"]),
            parameter_version_id=str(data["parameter_version_id"]),
            chunk_size=int(data["chunk_size"]),
            total=int(data["total"]),
            started_at=str(data["started_at"]),
            status=str(data["status"]),
            processed=int(data["processed"]),
            finished_at=data.get("finished_at"),
        )


@dataclass(frozen=True)
class ScenarioResult:
    """批次完成后的试算结果，含企业分布、缺口与价格压力指标。"""

    run_id: str
    scenario_id: str
    snapshot_id: str
    snapshot_digest: str
    parameter_version_id: str
    parameter_digest: str
    metrics: dict[str, Any]
    outcomes: tuple[dict[str, Any], ...]  # 逐企业试算结果
    finished_at: str
    digest: str  # 内容指纹：仅覆盖输入指纹 + 指标 + 逐企业结果

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "parameter_version_id": self.parameter_version_id,
            "parameter_digest": self.parameter_digest,
            "metrics": self.metrics,
            "outcomes": list(self.outcomes),
            "finished_at": self.finished_at,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScenarioResult":
        return cls(
            run_id=str(data["run_id"]),
            scenario_id=str(data["scenario_id"]),
            snapshot_id=str(data["snapshot_id"]),
            snapshot_digest=str(data["snapshot_digest"]),
            parameter_version_id=str(data["parameter_version_id"]),
            parameter_digest=str(data["parameter_digest"]),
            metrics=dict(data["metrics"]),
            outcomes=tuple(dict(item) for item in data["outcomes"]),
            finished_at=str(data["finished_at"]),
            digest=str(data["digest"]),
        )


@dataclass(frozen=True)
class PolicyRelease:
    """正式发布：只引用已复核方案的结果摘要与指纹，不复制试算余额。"""

    release_id: str
    scenario_id: str
    run_id: str
    snapshot_id: str
    parameter_version_id: str
    result_digest: str
    metric_summary: dict[str, Any]
    published_by: str
    published_at: str
    reference_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "parameter_version_id": self.parameter_version_id,
            "result_digest": self.result_digest,
            "metric_summary": self.metric_summary,
            "published_by": self.published_by,
            "published_at": self.published_at,
            "reference_only": self.reference_only,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyRelease":
        return cls(
            release_id=str(data["release_id"]),
            scenario_id=str(data["scenario_id"]),
            run_id=str(data["run_id"]),
            snapshot_id=str(data["snapshot_id"]),
            parameter_version_id=str(data["parameter_version_id"]),
            result_digest=str(data["result_digest"]),
            metric_summary=dict(data["metric_summary"]),
            published_by=str(data["published_by"]),
            published_at=str(data["published_at"]),
            reference_only=bool(data["reference_only"]),
        )
