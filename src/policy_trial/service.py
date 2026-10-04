"""试算服务门面。

把隔离沙箱、分块引擎、正式账户与状态机组装成完整用例：

- 复制历史快照到内容寻址区，参数版本同样内容寻址；多个方案共享输入对象；
- 创建方案即绑定（snapshot_hash, parameter_hash），绑定后不可更改；
- 运行/断点续跑/取消：取消会清理整个方案工作区与临时分块；
- 复核闸门：只有“已完成 + 已复核”的方案能被正式发布引用；
- 发布只保存引用（方案编号 + 结果指纹），不复制任何试算余额；
- 重放：从内容寻址对象重建输入并重新计算，校验指纹一致；
- 比较：对任意两次结果输出指标与逐企业差异。
"""
from __future__ import annotations

import datetime as _dt

from .engine import ENGINE_VERSION, CancellationToken, TrialEngine
from .ledger import FormalLedger
from .models import (
    EnterpriseRecord,
    EnterpriseResult,
    ExemptionRule,
    HistoricalSnapshot,
    ParameterVersion,
    Publication,
    ScenarioResult,
    ScenarioStatus,
    digest,
)
from .storage import SandboxStore


class TrialServiceError(RuntimeError):
    """试算服务规则违反。"""


class CancelledError(TrialServiceError):
    """批次在运行中被协作式取消。"""


# 允许的状态迁移
_TRANSITIONS = {
    ScenarioStatus.DRAFT: {ScenarioStatus.REVIEWED, ScenarioStatus.CANCELLED},
    ScenarioStatus.REVIEWED: {ScenarioStatus.PUBLISHED, ScenarioStatus.DRAFT},
    ScenarioStatus.PUBLISHED: set(),
    ScenarioStatus.CANCELLED: set(),
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# JSON -> 模型重建（重放时从内容寻址对象恢复不可变输入）
# ---------------------------------------------------------------------------

def _snapshot_from_json(data: dict) -> HistoricalSnapshot:
    return HistoricalSnapshot(
        snapshot_id=data["snapshot_id"],
        taken_at=data["taken_at"],
        enterprises=tuple(
            EnterpriseRecord(
                enterprise_id=e["enterprise_id"],
                sector=e["sector"],
                base_amount=e["base_amount"],
                current_payment=e["current_payment"],
                carry_over=e["carry_over"],
                exempt=e.get("exempt", False),
            ) for e in data["enterprises"]
        ),
    )


def _params_from_json(data: dict) -> ParameterVersion:
    ex = data["exemption"]
    return ParameterVersion(
        version=data["version"],
        coefficient=data["coefficient"],
        carry_ratio=data["carry_ratio"],
        exemption=ExemptionRule(
            sectors=tuple(ex["sectors"]),
            enterprise_ids=tuple(ex["enterprise_ids"]),
        ),
        price_pass_through=data.get("price_pass_through", 1.0),
        note=data.get("note", ""),
    )


class TrialService:
    """服务端试算环境门面。"""

    def __init__(self, store: SandboxStore, ledger: FormalLedger | None = None,
                 chunk_size: int = 100) -> None:
        self.store = store
        self.ledger = ledger or FormalLedger()
        self.engine = TrialEngine(chunk_size=chunk_size)
        # scenario_id -> 取消标志（仅运行期保留；服务重启后以状态文件为准）
        self._tokens: dict[str, object] = {}

  # ------------------------------------------------------------------
    # 输入注册：快照复制 + 参数版本
    # ------------------------------------------------------------------

    def register_snapshot(self, snapshot: HistoricalSnapshot) -> str:
        """把历史快照复制进沙箱内容寻址区，返回内容哈希。

        复制的是不可变副本；正式账户与沙箱之间没有任何反向通道。
        """
        return self.store.put_object(snapshot)

    def export_and_register_snapshot(self, snapshot_id: str) -> tuple[str, str]:
        """从正式账户导出快照并复制进沙箱，返回 (snapshot_id, content_hash)。"""
        snapshot = self.ledger.export_snapshot(snapshot_id)
        return snapshot.snapshot_id, self.register_snapshot(snapshot)

    def register_parameters(self, params: ParameterVersion) -> str:
        return self.store.put_object(params)

    def _load_snapshot(self, snapshot_hash: str) -> HistoricalSnapshot:
        return _snapshot_from_json(self.store.get_object(snapshot_hash))

    def _load_params(self, params_hash: str) -> ParameterVersion:
        return _params_from_json(self.store.get_object(params_hash))

    # ------------------------------------------------------------------
    # 方案创建：绑定参数版本与快照
    # ------------------------------------------------------------------

    def create_scenario(self, scenario_id: str,
                        snapshot_hash: str, params_hash: str,
                        created_by: str = "分析人员") -> dict:
        for h in (snapshot_hash, params_hash):
            if not self.store.has_object(h):
                raise TrialServiceError(f"输入对象不存在：{h[:12]}")
        binding = {
            "scenario_id": scenario_id,
            "snapshot_hash": snapshot_hash,
            "params_hash": params_hash,
            "created_by": created_by,
            "created_at": _now(),
        }
        self.store.create_workspace(scenario_id, binding)
        state = {
            "scenario_id": scenario_id,
            "status": ScenarioStatus.DRAFT,
            "completed": False,
            "processed": 0,
            "total": len(self._load_snapshot(snapshot_hash).enterprises),
            "chunks": 0,
            "updated_at": _now(),
        }
        self.store.write_state(scenario_id, state)
        return state

    def get_binding(self, scenario_id: str) -> dict:
        return self.store.read_binding(scenario_id)

    def get_state(self, scenario_id: str) -> dict:
        return self.store.read_state(scenario_id)

    # ------------------------------------------------------------------
    # 运行与可恢复批次
    # ------------------------------------------------------------------

    def _refresh_state(self, scenario_id: str, **changes: object) -> dict:
        state = self.store.read_state(scenario_id)
        state.update(changes)
        state["updated_at"] = _now()
        self.store.write_state(scenario_id, state)
        return state

    def run(self, scenario_id: str, token=None) -> ScenarioResult:
        """运行（或从检查点续跑）一个方案，返回最终结果。

        每次调用只推进未完成的分块；中断后再次调用即“恢复批次”。
        """
        token = token or CancellationToken()
        self._tokens[scenario_id] = token

        state = self.store.read_state(scenario_id)
        if state["status"] == ScenarioStatus.CANCELLED:
            raise TrialServiceError("方案已取消，工作区已清理")
        if state["status"] != ScenarioStatus.DRAFT:
            raise TrialServiceError(f"当前状态 {state['status']} 不允许运行；请先退回草稿")

        binding = self.store.read_binding(scenario_id)
        snapshot = self._load_snapshot(binding["snapshot_hash"])
        params = self._load_params(binding["params_hash"])
        input_fp = self._input_fingerprint(binding)

        # 恢复：已落盘的分块直接跳过
        existing = self.store.read_chunks(scenario_id)
        next_chunk = len(existing)
        total_chunks = self.engine.plan_chunks(len(snapshot.enterprises))

        for index in range(next_chunk, total_chunks):
            if token.cancelled:
                # 协作式取消：不写本块，保留已完成分块由 cancel() 决定清理
                self._tokens.pop(scenario_id, None)
                raise CancelledError(scenario_id)
            rows = self.engine.run_chunk(snapshot, params, index)
            # 先落盘分块结果，再推进检查点状态（崩溃至多重复一块的“可见性”，
            # 原子写保证不会读到半块）
            self.store.write_chunk(scenario_id, index, [r.__dict__ for r in rows])
            self._refresh_state(
                scenario_id,
                processed=min((index + 1) * self.engine.chunk_size,
                              len(snapshot.enterprises)),
                chunks=index + 1,
            )

        # 汇总并固化最终结果
        chunks = [
            [EnterpriseResult(**r) for r in chunk]
            for chunk in self.store.read_chunks(scenario_id)
        ]
        result = self.engine.build_result(snapshot, params, chunks, input_fp)
        self.store.write_result(scenario_id, result)
        self._refresh_state(scenario_id, completed=True,
                            processed=len(snapshot.enterprises),
                            chunks=total_chunks,
                            result_fingerprint=digest(result))
        self._tokens.pop(scenario_id, None)
        return result

    def resume(self, scenario_id: str) -> ScenarioResult:
        """显式断点续跑（与 run 等价，语义入口）。"""
        return self.run(scenario_id)

    def cancel(self, scenario_id: str) -> dict:
        """取消任务并清理全部临时结果（含工作区与分块）。

        已发布方案不可取消；已复核方案需先退回草稿。
        取消后共享输入对象仍然保留（其他方案在用）。
        """
        state = self.store.read_state(scenario_id)
        status = state["status"]
        if status in (ScenarioStatus.PUBLISHED, ScenarioStatus.CANCELLED):
            raise TrialServiceError(f"状态 {status} 的方案不可取消")
        token = self._tokens.get(scenario_id)
        if token is not None:
            token.cancel()
        self.store.purge_workspace(scenario_id)
        self._tokens.pop(scenario_id, None)
        # 工作区已删除，取消事实只通过“不存在”表达；返回一份注销回执
        return {"scenario_id": scenario_id, "status": ScenarioStatus.CANCELLED,
                "purged": True, "cancelled_at": _now()}

    # ------------------------------------------------------------------
    # 复核闸门
    # ------------------------------------------------------------------

    def review(self, scenario_id: str, reviewer: str, comment: str = "") -> dict:
        state = self.store.read_state(scenario_id)
        if not state.get("completed"):
            raise TrialServiceError("试算尚未完成，不能复核")
        if not self.store.has_result(scenario_id):
            raise TrialServiceError("结果文件缺失，不能复核")
        return self._transition(scenario_id, ScenarioStatus.REVIEWED,
                                meta={"reviewer": reviewer,
                                      "review_comment": comment,
                                      "reviewed_at": _now()})

    def unreview(self, scenario_id: str) -> dict:
        """复核退回草稿（用于调参重做）。"""
        return self._transition(scenario_id, ScenarioStatus.DRAFT)

    def _transition(self, scenario_id: str, target: str,
                    meta: dict | None = None) -> dict:
        state = self.store.read_state(scenario_id)
        current = state["status"]
        if target not in _TRANSITIONS.get(current, set()):
            raise TrialServiceError(f"不允许的状态迁移：{current} -> {target}")
        state["status"] = target
        if meta:
            state.update(meta)
        self.store.write_state(scenario_id, state)
        return state

    # ------------------------------------------------------------------
    # 正式发布：只引用、不复制
    # ------------------------------------------------------------------

    def publish(self, policy_id: str, scenario_id: str) -> Publication:
        """正式政策发布：只允许引用“已复核”方案。

        发布记录仅包含引用关系与结果指纹；试算余额不会被复制到任何正式
        账户，正式账户的入账必须走正式核算流程。
        """
        state = self.store.read_state(scenario_id)
        if state["status"] != ScenarioStatus.REVIEWED:
            raise TrialServiceError(
                f"只有已复核方案可发布引用，当前状态：{state['status']}")
        if not state.get("completed") or not self.store.has_result(scenario_id):
            raise TrialServiceError("方案缺少完整结果，不能发布")

        binding = self.store.read_binding(scenario_id)
        snapshot = self._load_snapshot(binding["snapshot_hash"])
        params = self._load_params(binding["params_hash"])
        result = self.store.read_result(scenario_id)
        publication = Publication(
            policy_id=policy_id,
            scenario_id=scenario_id,
            parameter_version=params.version,
            snapshot_id=snapshot.snapshot_id,
            result_fingerprint=state["result_fingerprint"],
            published_at=_now(),
        )
        self.store.append_publication(publication.__dict__)
        self._transition(scenario_id, ScenarioStatus.PUBLISHED)
        return publication

    def list_publications(self) -> list[dict]:
        return self.store.load_publications()

    # ------------------------------------------------------------------
    # 重放：用绑定的输入对象重新计算并核对指纹
    # ------------------------------------------------------------------

    def replay(self, scenario_id: str) -> dict:
        """重放一次方案：重建不可变输入，完整重算，比对结果指纹。

        重放不依赖原工作区里的分块，直接从内容寻址对象出发，
        因此即使结果文件丢失也能验证“同一输入是否给出同一答案”。
        """
        binding = self.store.read_binding(scenario_id)
        snapshot = self._load_snapshot(binding["snapshot_hash"])
        params = self._load_params(binding["params_hash"])
        input_fp = self._input_fingerprint(binding)

        chunks = [
            self.engine.run_chunk(snapshot, params, i)
            for i in range(self.engine.plan_chunks(len(snapshot.enterprises)))
        ]
        recomputed = self.engine.build_result(snapshot, params, chunks, input_fp)
        recomputed_fp = digest(recomputed)

        stored = self.store.read_result(scenario_id)
        matches = recomputed_fp == self.get_state(scenario_id)["result_fingerprint"]
        # 同时逐字段比对规范化结果，防止指纹算法本身出错
        content_matches = digest({
            "enterprise_results": stored["enterprise_results"],
            "metrics": stored["metrics"],
            "input_fingerprint": stored["input_fingerprint"],
            "engine_version": stored["engine_version"],
        }) == digest({
            "enterprise_results": [r.__dict__ for r in recomputed.enterprise_results],
            "metrics": recomputed.metrics.__dict__,
            "input_fingerprint": recomputed.input_fingerprint,
            "engine_version": recomputed.engine_version,
        })
        return {
            "scenario_id": scenario_id,
            "replayed_fingerprint": recomputed_fp,
            "stored_fingerprint": self.get_state(scenario_id)["result_fingerprint"],
            "fingerprint_matches": matches and content_matches,
            "engine_version": ENGINE_VERSION,
            "result": recomputed,
        }

    # ------------------------------------------------------------------
    # 差异比较：任意两次结果
    # ------------------------------------------------------------------

    def compare(self, scenario_id_a: str, scenario_id_b: str) -> dict:
        """比较任意两个方案的结果（通常共享快照、参数版本不同）。"""
        ra = self.store.read_result(scenario_id_a)
        rb = self.store.read_result(scenario_id_b)
        ba = self.store.read_binding(scenario_id_a)
        bb = self.store.read_binding(scenario_id_b)

        if ba["snapshot_hash"] != bb["snapshot_hash"]:
            # 仍可比较，但明确提示输入不同，避免误读参数效应
            shared_snapshot = False
        else:
            shared_snapshot = True

        ma, mb = ra["metrics"], rb["metrics"]
        metric_diff = {
            "shared_snapshot": shared_snapshot,
            "total_new_payment": round(mb["total_new_payment"] - ma["total_new_payment"], 6),
            "total_gap": round(mb["total_gap"] - ma["total_gap"], 6),
            "exempted_count": mb["exempted_count"] - ma["exempted_count"],
            "price_pressure_index": round(
                mb["price_pressure_index"] - ma["price_pressure_index"], 6),
            "gap_distribution": {
                k: mb["gap_distribution"][k] - ma["gap_distribution"][k]
                for k in ma["gap_distribution"]
            },
        }

        by_id_a = {r["enterprise_id"]: r for r in ra["enterprise_results"]}
        by_id_b = {r["enterprise_id"]: r for r in rb["enterprise_results"]}
        per_enterprise = []
        for eid in sorted(set(by_id_a) | set(by_id_b)):
            xa, xb = by_id_a.get(eid), by_id_b.get(eid)
            if xa is None or xb is None:
                per_enterprise.append({"enterprise_id": eid, "present_in":
                                       "b_only" if xa is None else "a_only"})
                continue
            per_enterprise.append({
                "enterprise_id": eid,
                "sector": xb["sector"],
                "new_payment_delta": round(xb["new_payment"] - xa["new_payment"], 6),
                "gap_delta": round(xb["gap"] - xa["gap"], 6),
                "price_pressure_delta": round(
                    xb["price_pressure"] - xa["price_pressure"], 6),
                "exemption_changed": xb["exempted"] != xa["exempted"],
            })
        return {
            "a": scenario_id_a,
            "b": scenario_id_b,
            "metric_diff": metric_diff,
            "per_enterprise": per_enterprise,
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _input_fingerprint(binding: dict) -> str:
        """绑定输入的联合指纹：快照 + 参数版本（发布/重放都锚定它）。"""
        return digest({
            "snapshot_hash": binding["snapshot_hash"],
            "params_hash": binding["params_hash"],
            "engine_version": ENGINE_VERSION,
        })

    def get_result(self, scenario_id: str) -> dict:
        return self.store.read_result(scenario_id)
