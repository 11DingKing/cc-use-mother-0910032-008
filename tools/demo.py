"""端到端演示：隔离试算 -> 可恢复批次 -> 比较 -> 复核 -> 发布引用。

运行：

    python3 tools/demo.py

演示使用临时沙箱目录，全过程不触碰任何“正式余额”。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from policy_trial.engine import CancellationToken
from policy_trial.ledger import FormalLedger
from policy_trial.models import (
    EnterpriseRecord,
    ExemptionRule,
    ParameterVersion,
)
from policy_trial.service import CancelledError, TrialService
from policy_trial.storage import SandboxStore


def build_ledger() -> FormalLedger:
    sectors = ["制造", "服务", "农业", "能源"]
    records = [
        EnterpriseRecord(
            enterprise_id=f"ENT-{i:04d}",
            sector=sectors[i % 4],
            base_amount=5000.0 + i * 137.0,
            current_payment=1200.0 + i * 31.0,
            carry_over=200.0 + (i % 11) * 15.0,
            exempt=(i % 53 == 0),
        )
        for i in range(500)
    ]
    return FormalLedger().ingested(records)


def params(version: str, coefficient: float, carry_ratio: float,
          exempt_sectors: tuple[str, ...] = ()) -> ParameterVersion:
    return ParameterVersion(
        version=version,
        coefficient=coefficient,
        carry_ratio=carry_ratio,
        exemption=ExemptionRule(sectors=exempt_sectors),
        price_pass_through=0.8,
        note=f"{version} 方案",
    )


def show_metrics(title: str, metrics) -> None:
    print(f"\n== {title} ==")
    payload = {
        "企业数": metrics.enterprise_count,
        "豁免企业": metrics.exempted_count,
        "新政策应缴总额": round(metrics.total_new_payment, 2),
        "当前实缴总额": round(metrics.total_current_payment, 2),
        "总缺口": round(metrics.total_gap, 2),
        "价格压力指数": metrics.price_pressure_index,
        "缺口分布": metrics.gap_distribution,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    sandbox = Path(tmp.name) / "trial-sandbox"
    ledger = build_ledger()
    svc = TrialService(SandboxStore(sandbox), ledger=ledger, chunk_size=128)

    # 1) 复制指定历史快照到沙箱（共享输入）
    snap_id, snap_hash = svc.export_and_register_snapshot("snapshot-2026-09")
    print(f"历史快照 {snap_id} 已复制入沙箱：{snap_hash[:16]}…")

    # 2) 绑定不同参数版本，建立多个互不污染的方案
    plans = {
        "PLAN-A 基准": ("PLAN-A", params("p-baseline", 0.30, 0.50)),
        "PLAN-B 提系数+结转放宽": ("PLAN-B", params("p-tight", 0.36, 0.70)),
        "PLAN-C 豁免农业": ("PLAN-C", params("p-exempt", 0.36, 0.70, ("农业",))),
    }
    for _, (sid, p) in plans.items():
        ph = svc.register_parameters(p)
        svc.create_scenario(sid, snap_hash, ph, created_by="分析人员")

    # 3) 可恢复批次：PLAN-B 先在首块前被取消（0 块），再续跑完成
    token = CancellationToken()
    token.cancel()
    try:
        svc.run("PLAN-B", token=token)
    except CancelledError:
        print("\nPLAN-B 首批即收到取消信号，状态：",
              svc.get_state("PLAN-B")["status"], "已处理：",
              svc.get_state("PLAN-B")["processed"])
    result_b = svc.resume("PLAN-B")
    print("PLAN-B 断点续跑完成，已处理：",
          svc.get_state("PLAN-B")["processed"], "/",
          svc.get_state("PLAN-B")["total"])

    result_a = svc.run("PLAN-A")
    result_c = svc.run("PLAN-C")
    show_metrics("PLAN-A 指标", result_a.metrics)
    show_metrics("PLAN-B 指标", result_b.metrics)
    show_metrics("PLAN-C 指标", result_c.metrics)

    # 4) 方案互不污染：取消一个草稿方案，临时结果被清理，其余照常
    extra_ph = svc.register_parameters(params("p-throwaway", 0.40, 0.50))
    svc.create_scenario("PLAN-X", snap_hash, extra_ph)
    svc.run("PLAN-X")
    receipt = svc.cancel("PLAN-X")
    print("\n取消 PLAN-X，工作区已清理：", receipt["purged"],
          "；共享快照对象仍在：", svc.store.has_object(snap_hash))

    # 5) 任意两次结果比较（API 等价：GET /compare?a=PLAN-A&b=PLAN-C）
    diff = svc.compare("PLAN-A", "PLAN-C")
    print("\n== PLAN-A -> PLAN-C 差异 ==")
    print(json.dumps(diff["metric_diff"], ensure_ascii=False, indent=2))

    # 6) 重放校验
    replay = svc.replay("PLAN-C")
    print("\nPLAN-C 重放指纹一致：", replay["fingerprint_matches"])

    # 7) 复核闸门 + 正式发布只引用
    try:
        svc.publish("POL-2026-001", "PLAN-C")
    except Exception as exc:  # 未复核不能发布
        print("未复核发布被拦截：", exc)
    svc.review("PLAN-C", reviewer="核算专员-王", comment="指标与抽样核对一致")
    publication = svc.publish("POL-2026-001", "PLAN-C")
    print("\n正式政策发布（仅引用，无余额复制）：")
    print(json.dumps(publication.__dict__, ensure_ascii=False, indent=2))
    print("发布后台账：", json.dumps(svc.list_publications(),
                                    ensure_ascii=False))

    tmp.cleanup()


if __name__ == "__main__":
    main()
