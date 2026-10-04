"""政策调整情景试算：隔离的服务端试算环境。

- models：领域模型与状态机约定
- store：共享只读输入 + 按批次隔离的工作区
- engine：确定性试算引擎（分块、断点续跑、可中断）
- service：方案生命周期、复核发布、重放比较的 API 边界
- server：基于标准库的 HTTP 接口
"""
from .engine import TrialEngine
from .errors import (
    AlreadyExistsError,
    BatchInterrupted,
    DomainError,
    NotFoundError,
    ReleaseError,
    StateError,
)
from .models import (
    BatchRun,
    EnterpriseRecord,
    ParameterVersion,
    PolicyRelease,
    RunStatus,
    Scenario,
    ScenarioResult,
    ScenarioState,
    Snapshot,
)
from .service import ScenarioLabService
from .store import JsonStore, canonical_digest

__all__ = [
    "AlreadyExistsError",
    "BatchInterrupted",
    "BatchRun",
    "DomainError",
    "EnterpriseRecord",
    "JsonStore",
    "NotFoundError",
    "ParameterVersion",
    "PolicyRelease",
    "ReleaseError",
    "RunStatus",
    "Scenario",
    "ScenarioLabService",
    "ScenarioResult",
    "ScenarioState",
    "Snapshot",
    "StateError",
    "TrialEngine",
    "canonical_digest",
]
