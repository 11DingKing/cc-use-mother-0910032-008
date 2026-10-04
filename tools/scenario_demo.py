"""端到端演示：隔离试算、断点续跑、取消清理、复核发布与方案比较。

用法：python3 tools/scenario_demo.py [数据目录]
默认把演示数据写到 var/demo（已在 .gitignore 中忽略）。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scenario_lab import EnterpriseRecord, ParameterVersion, ScenarioLabService, Snapshot

NOW = "2026-10-04T00:00:00+00:00"


def build_snapshot() -> Snapshot:
    rows = [
        ("E001", "钢一", "steel", 120000, 10000, 60000),
        ("E002", "钢二", "steel", 80000, 2000, 50000),
        ("E003", "水泥一", "cement", 60000, 5000, 40000),
        ("E004", "水泥二", "cement", 45000, 0, 30000),
        ("E005", "化工一", "chemical", 30000, 12000, 20000),
        ("E006", "造纸一", "paper", 8000, 500, 6000),
        ("E007", "电力一", "power", 200000, 30000, 150000),
        ("E008", "纺织一", "textile", 9000, 100, 7000),
        ("E009", "有色一", "nonferrous", 50000, 40000, 25000),
        ("E010", "玻璃一", "glass", 25000, 1000, 15000),
    ]
    return Snapshot(
        snapshot_id="snap-2024",
        period="2024",
        source="正式账户台账",
        copied_at=NOW,
        records=tuple(
            EnterpriseRecord(eid, name, industry, float(emissions), float(balance), float(output))
            for eid, name, industry, emissions, balance, output in rows
        ),
    )


def build_params(version_id: str, label: str, *, scale: float, carry: float, threshold: float) -> ParameterVersion:
    base = {"steel": 1.05, "cement": 1.0, "chemical": 1.0, "power": 1.1, "nonferrous": 1.0, "glass": 1.0, "default": 1.0}
    return ParameterVersion(
        version_id=version_id,
        label=label,
        coefficients={key: round(value * scale, 6) for key, value in base.items()},
        adjustment_factor=1.0,
        carry_ratio=carry,
        exemption_threshold=threshold,
        exemption_enterprise_ids=(),
        created_at=NOW,
    )


def show_metrics(title: str, metrics: dict) -> None:
    print(f"\n== {title} ==")
    print(f"  企业总数 {metrics['enterprise_count']}，豁免 {metrics['exempt_count']}，"
          f"缺口企业 {metrics['gap_enterprise_count']}（占比 {metrics['gap_enterprise_ratio']:.1%}）")
    print(f"  总排放 {metrics['total_emissions']:,.0f}，总配额 {metrics['total_allocation']:,.0f}，"
          f"总结转 {metrics['total_carried']:,.0f}")
    print(f"  总缺口 {metrics['total_gap']:,.0f}，总盈余 {metrics['total_surplus']:,.0f}，"
          f"缺口率 {metrics['gap_ratio']:.2%}，价格压力指数 {metrics['price_pressure_index']}")
    bands = "，".join(f"{band} {count}" for band, count in metrics["distribution"].items())
    print(f"  企业分布：{bands}")


def main() -> None:
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "var" / "demo"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    service = ScenarioLabService(data_dir)

    # 1. 复制历史快照、注册两个参数版本（基准 vs 强化）
    service.register_snapshot(build_snapshot())
    service.register_parameters(build_params("param-base", "基准方案", scale=1.0, carry=0.5, threshold=10000))
    service.register_parameters(build_params("param-strict", "强化方案", scale=0.9, carry=0.3, threshold=5000))
    print("已复制快照 snap-2024（10 家企业），注册参数版本 param-base / param-strict")

    # 2. 基准方案：中断一次再恢复，验证可恢复批次
    base = service.create_scenario("基准试算", "snap-2024", created_by="核算专员")
    service.bind_parameters(base.scenario_id, "param-base")
    paused = service.start_run(base.scenario_id, chunk_size=4, interrupt_after_chunks=1)
    print(f"\n基准批次在 {paused.processed}/{paused.total} 处中断，断点已落盘，从断点恢复……")
    run_base = service.resume_run(paused.run_id)
    show_metrics("基准方案（断点续跑完成）", service.get_result(run_base.run_id).metrics)

    # 3. 强化方案：共享同一快照，互不污染
    strict = service.create_scenario("强化试算", "snap-2024", created_by="核算专员")
    service.bind_parameters(strict.scenario_id, "param-strict")
    run_strict = service.start_run(strict.scenario_id, chunk_size=4)
    show_metrics("强化方案（系数×0.9、结转 0.3、豁免门槛 5000）", service.get_result(run_strict.run_id).metrics)

    # 4. 取消一个临时方案，临时结果立即清理
    scratch = service.create_scenario("临时方案", "snap-2024", created_by="核算专员")
    service.bind_parameters(scratch.scenario_id, "param-strict")
    paused = service.start_run(scratch.scenario_id, chunk_size=4, interrupt_after_chunks=1)
    service.cancel_run(paused.run_id)
    cleaned = not service.store.workspace_exists(paused.run_id)
    print(f"\n临时方案已取消，临时工作区已清理：{cleaned}")

    # 5. 复核基准方案并正式发布（只引用，不复制试算余额）
    service.review_scenario(base.scenario_id, reviewer="监管审计员", note="口径与复核单一致")
    release = service.publish_release(base.scenario_id, published_by="交易运营员")
    print(f"\n正式发布 {release.release_id}：引用方案 {release.scenario_id}，"
          f"结果指纹 {release.result_digest[:16]}…，仅引用不复制余额：{release.reference_only}")

    # 6. 重放与比较
    replay = service.replay_run(run_base.run_id)
    print(f"\n重放基准批次：指纹一致 = {replay['identical']}")
    comparison = service.compare_runs(run_base.run_id, run_strict.run_id)
    gap_delta = comparison["metric_delta"]["total_gap"]
    pressure_delta = comparison["metric_delta"]["price_pressure_index"]
    print(f"方案比较：总缺口 {gap_delta['left']:,.0f} → {gap_delta['right']:,.0f}"
          f"（Δ {gap_delta['delta']:+,.0f}），价格压力指数 Δ {pressure_delta['delta']:+.2f}")
    print("缺口变化最大的三家企业：")
    for item in comparison["top_movers"][:3]:
        print(f"  {item['enterprise_id']}：{item['left_gap']:>12,.1f} → {item['right_gap']:>12,.1f}"
              f"（{item['left_band']} → {item['right_band']}）")

    print(f"\n演示数据目录：{data_dir}")
    print(json.dumps({"release_id": release.release_id, "identical_replay": replay["identical"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
