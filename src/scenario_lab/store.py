"""文件型存储：共享只读输入 + 按批次隔离的工作区。

数据目录布局：

    <root>/inputs/snapshots/<id>.json        历史快照副本（共享、只读、不可覆盖）
    <root>/inputs/parameters/<id>.json       参数版本（共享、只读、不可覆盖）
    <root>/scenarios/<id>.json               方案元数据
    <root>/batches/<run_id>.json             批次元数据（取消后保留审计痕迹）
    <root>/workspaces/<run_id>/checkpoint.json  断点（临时，取消即清理）
    <root>/workspaces/<run_id>/result.json      试算结果（供重放与比较）
    <root>/releases/<id>.json                正式发布（仅引用，不含试算余额）

隔离约定：所有批次只写自己的 workspaces/<run_id>/，共享输入一旦写入
即不可覆盖，从机制上保证多方案共享输入但互不污染。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .errors import AlreadyExistsError, NotFoundError
from .models import (
    BatchRun,
    ParameterVersion,
    PolicyRelease,
    Scenario,
    ScenarioResult,
    Snapshot,
)


def canonical_digest(payload: Any) -> str:
    """对任意可 JSON 序列化的对象生成稳定内容指纹。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class JsonStore:
    """试算环境的文件存储。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        for relative in (
            "inputs/snapshots",
            "inputs/parameters",
            "scenarios",
            "batches",
            "workspaces",
            "releases",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    # ---- 基础读写 ----

    def _write_json(self, path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
        if path.exists() and not overwrite:
            raise AlreadyExistsError(f"对象已存在且不可覆盖：{path.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 原子落盘，避免半截文件

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise NotFoundError(f"对象不存在：{path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- 共享只读输入 ----

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._write_json(
            self.root / "inputs" / "snapshots" / f"{snapshot.snapshot_id}.json",
            snapshot.to_dict(),
            overwrite=False,
        )

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        return Snapshot.from_dict(self._read_json(self.root / "inputs" / "snapshots" / f"{snapshot_id}.json"))

    def save_parameters(self, params: ParameterVersion) -> None:
        self._write_json(
            self.root / "inputs" / "parameters" / f"{params.version_id}.json",
            params.to_dict(),
            overwrite=False,
        )

    def load_parameters(self, version_id: str) -> ParameterVersion:
        return ParameterVersion.from_dict(self._read_json(self.root / "inputs" / "parameters" / f"{version_id}.json"))

    # ---- 方案 ----

    def save_scenario(self, scenario: Scenario) -> None:
        self._write_json(self.root / "scenarios" / f"{scenario.scenario_id}.json", scenario.to_dict(), overwrite=True)

    def load_scenario(self, scenario_id: str) -> Scenario:
        return Scenario.from_dict(self._read_json(self.root / "scenarios" / f"{scenario_id}.json"))

    def list_scenarios(self) -> list[Scenario]:
        return [Scenario.from_dict(self._read_json(path)) for path in sorted((self.root / "scenarios").glob("*.json"))]

    # ---- 批次与工作区 ----

    def save_run(self, run: BatchRun) -> None:
        self._write_json(self.root / "batches" / f"{run.run_id}.json", run.to_dict(), overwrite=True)

    def load_run(self, run_id: str) -> BatchRun:
        return BatchRun.from_dict(self._read_json(self.root / "batches" / f"{run_id}.json"))

    def delete_run_meta(self, run_id: str) -> None:
        (self.root / "batches" / f"{run_id}.json").unlink(missing_ok=True)

    def workspace(self, run_id: str) -> Path:
        path = self.root / "workspaces" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace_exists(self, run_id: str) -> bool:
        return (self.root / "workspaces" / run_id).exists()

    def save_checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> None:
        self._write_json(self.workspace(run_id) / "checkpoint.json", checkpoint, overwrite=True)

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        path = self.root / "workspaces" / run_id / "checkpoint.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def clear_checkpoint(self, run_id: str) -> None:
        (self.root / "workspaces" / run_id / "checkpoint.json").unlink(missing_ok=True)

    def save_result(self, result: ScenarioResult) -> None:
        self._write_json(self.workspace(result.run_id) / "result.json", result.to_dict(), overwrite=True)

    def load_result(self, run_id: str) -> ScenarioResult:
        return ScenarioResult.from_dict(self._read_json(self.root / "workspaces" / run_id / "result.json"))

    def purge_workspace(self, run_id: str) -> None:
        """清理批次的全部临时结果（取消任务时调用）。"""
        shutil.rmtree(self.root / "workspaces" / run_id, ignore_errors=True)

    # ---- 正式发布 ----

    def save_release(self, release: PolicyRelease) -> None:
        self._write_json(self.root / "releases" / f"{release.release_id}.json", release.to_dict(), overwrite=False)

    def load_release(self, release_id: str) -> PolicyRelease:
        return PolicyRelease.from_dict(self._read_json(self.root / "releases" / f"{release_id}.json"))

    def list_releases(self) -> list[PolicyRelease]:
        return [PolicyRelease.from_dict(self._read_json(path)) for path in sorted((self.root / "releases").glob("*.json"))]
