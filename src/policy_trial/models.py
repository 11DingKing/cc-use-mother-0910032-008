"""试算领域的不可变模型。

所有模型使用 ``frozen`` 数据类，并提供确定性 JSON 序列化与内容哈希，
以便：

1. 历史快照与参数版本可被内容寻址、去重并校验完整性；
2. 同一（快照, 参数版本）组合在任意机器上重放得到相同结果；
3. 方案之间共享同一份输入对象，但各自持有独立结果，互不污染。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any


# ---------------------------------------------------------------------------
# 输入侧：正式账户的历史快照
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class EnterpriseRecord:
    """一家企业在快照时点的申报状态（来自正式账户，只读）。"""

    enterprise_id: str
    sector: str
    base_amount: float          # 计费基数
    current_payment: float      # 当前政策下实缴
    carry_over: float           # 上年结转余额
    exempt: bool = False        # 是否已经享受豁免（快照事实）


@dataclasses.dataclass(frozen=True)
class HistoricalSnapshot:
    """正式账户在某一时点的只读副本，供任意方案共享。"""

    snapshot_id: str
    taken_at: str
    enterprises: tuple[EnterpriseRecord, ...]

    def content_hash(self) -> str:
        return digest(to_jsonable(self))


# ---------------------------------------------------------------------------
# 参数版本：系数、结转比例、企业豁免规则
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ExemptionRule:
    """企业豁免规则：命中 sector 或显式列入白名单的企业免缴。"""

    sectors: tuple[str, ...] = ()
    enterprise_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ParameterVersion:
    """一组不可变政策参数。

    coefficient：新政策计费系数；
    carry_ratio：上年结转可抵扣比例（0~1）；
    exemption：豁免规则；
    price_pass_through：缺口向交易价格传导的系数（价格压力模型用）。
    """

    version: str
    coefficient: float
    carry_ratio: float
    exemption: ExemptionRule
    price_pass_through: float = 1.0
    note: str = ""

    def __post_init__(self) -> None:
        if self.coefficient < 0:
            raise ValueError("计费系数不能为负")
        if not 0.0 <= self.carry_ratio <= 1.0:
            raise ValueError("结转比例必须落在 [0, 1]")
        if self.price_pass_through < 0:
            raise ValueError("价格传导系数不能为负")

    def content_hash(self) -> str:
        return digest(to_jsonable(self))


# ---------------------------------------------------------------------------
# 输出侧：逐企业结果与方案级指标
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class EnterpriseResult:
    """单家企业的试算结果。只存在于沙箱中，绝不回写正式账户。"""

    enterprise_id: str
    sector: str
    base_amount: float
    new_payment: float          # 新政策应缴
    current_payment: float
    gap: float                  # 缺口 = 应缴 - 实缴（正为补缺口，负为盈余）
    carried_deduction: float    # 本方案实际使用的结转抵扣
    exempted: bool              # 是否被 *本方案参数* 豁免
    price_pressure: float      # 该企业传导到价格的压力值


@dataclasses.dataclass(frozen=True)
class ScenarioMetrics:
    """方案级汇总指标：企业分布、缺口与价格压力。"""

    enterprise_count: int
    exempted_count: int
    total_new_payment: float
    total_current_payment: float
    total_gap: float
    sector_breakdown: dict[str, dict[str, float]]
    gap_distribution: dict[str, float]   # 分桶：surplus / mild / moderate / severe
    price_pressure_index: float
    price_pressure_by_sector: dict[str, float]


@dataclasses.dataclass(frozen=True)
class BatchCheckpoint:
    """批次检查点：记录已处理到的位置，支撑断点续跑。"""

    processed: int
    chunk_index: int


@dataclasses.dataclass(frozen=True)
class ScenarioResult:
    """一次完整（或可继续）的试算运行结果。"""

    enterprise_results: tuple[EnterpriseResult, ...]
    metrics: ScenarioMetrics
    checkpoint: BatchCheckpoint
    completed: bool
    # 结果指纹：绑定输入内容，重放一致性与差异比较均以它为准
    input_fingerprint: str
    engine_version: str


# ---------------------------------------------------------------------------
# 方案生命周期状态
# ---------------------------------------------------------------------------

class ScenarioStatus:
    DRAFT = "草稿"            # 试算可反复调整、可删除
    REVIEWED = "已复核"       # 核算专员复核通过，可被正式发布引用
    PUBLISHED = "已发布"      # 已被正式政策引用（引用关系，不是余额复制）
    CANCELLED = "已取消"      # 任务取消，临时结果已清理


# 正式政策发布记录：只保存“引用了哪个已复核方案”
@dataclasses.dataclass(frozen=True)
class Publication:
    policy_id: str
    scenario_id: str
    parameter_version: str
    snapshot_id: str
    result_fingerprint: str
    published_at: str


# ---------------------------------------------------------------------------
# 序列化与哈希
# ---------------------------------------------------------------------------

def to_jsonable(obj: Any) -> Any:
    """把数据模型递归转成确定性 JSON 原生结构（键排序由序列化阶段负责）。"""
    if dataclasses.is_dataclass(obj):
        return {f.name: to_jsonable(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def dumps_canonical(value: Any) -> str:
    """确定性 JSON：排序键、固定分隔符、不转义非 ASCII。"""
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(value: Any) -> str:
    """计算任意可序列化模型的 SHA-256 内容摘要。"""
    return hashlib.sha256(dumps_canonical(value).encode("utf-8")).hexdigest()
