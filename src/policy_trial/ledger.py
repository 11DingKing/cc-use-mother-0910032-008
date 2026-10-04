"""正式账户区。

正式账户是试算的唯一数据来源，对试算侧 **严格只读**：

- 试算引擎只能通过 :meth:`FormalLedger.export_snapshot` 拿到不可变快照副本；
- 快照一旦导出即冻结（frozen 模型 + 内容哈希），任何方案都无法回写；
- 试算结果永远不会进入这里；正式政策发布只在沙箱侧记录“引用关系”，
  正式账户余额只能由正式核算流程产生，绝不从试算结果复制。
"""
from __future__ import annotations

import datetime as _dt

from .models import EnterpriseRecord, HistoricalSnapshot


class FormalLedger:
    """正式账户：保存企业申报数据，只暴露读接口给试算域。"""

    def __init__(self) -> None:
        # 正式账户内部可变存储；对外不直接暴露
        self._enterprises: dict[str, EnterpriseRecord] = {}

    def ingested(self, records: list[EnterpriseRecord]) -> "FormalLedger":
        """正式数据入库（申报/核算流程使用，不属于试算链路）。"""
        for record in records:
            self._enterprises[record.enterprise_id] = record
        return self

    def export_snapshot(self, snapshot_id: str) -> HistoricalSnapshot:
        """导出一份只读历史快照副本，供试算方案共享。

        多个方案引用同一个 snapshot_id / content_hash 时共享同一份输入，
        但副本对象不可变，任何一方都无法影响另一方。
        """
        if not self._enterprises:
            raise ValueError("正式账户为空，无法导出快照")
        taken_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        snapshot = HistoricalSnapshot(
            snapshot_id=snapshot_id,
            taken_at=taken_at,
            enterprises=tuple(self._enterprises.values()),
        )
        # 立即计算并固定内容哈希，调用方可据此校验完整性
        snapshot.content_hash()
        return snapshot

    def enterprise_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._enterprises))
