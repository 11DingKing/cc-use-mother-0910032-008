"""隔离沙箱存储。

两层结构：

1. **内容寻址对象区**（``objects/``）：按 SHA-256 存放快照与参数版本。
   多个方案引用同一哈希时共享同一份物理输入，天然去重，且内容不可变。

2. **方案工作区**（``scenarios/<scenario_id>/``）：每个方案一个独立目录，
   存放运行状态、分块中间结果与最终指标。方案之间目录互不可见，
   取消任务时整个工作区连同临时分块一起删除，不留下任何余额痕迹。

所有写入均为“临时文件 + 原子替换”，崩溃不会产生半写状态；
工作区路径做越界校验，scenario_id 不允许携带路径分隔符。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .models import dumps_canonical, to_jsonable

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SandboxError(RuntimeError):
    """沙箱访问异常（越界、工作区不存在等）。"""


def _safe_id(scenario_id: str) -> str:
    if not _SAFE_ID.match(scenario_id):
        raise SandboxError(f"非法方案编号：{scenario_id!r}")
    return scenario_id


class SandboxStore:
    """文件系统支撑的隔离沙箱。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.scenarios_dir = self.root / "scenarios"
        self.publications_file = self.root / "publications.json"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 内容寻址对象：共享输入
    # ------------------------------------------------------------------

    def put_object(self, value: object) -> str:
        """写入不可变对象，返回内容哈希；相同内容只存一份。"""
        payload = dumps_canonical(value)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        path = self.objects_dir / digest[:2] / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(path, payload)
        return digest

    def get_object(self, digest: str) -> dict:
        path = self._object_path(digest)
        if not path.exists():
            raise SandboxError(f"对象不存在或已被清理：{digest}")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_object(self, digest: str) -> bool:
        return self._object_path(digest).exists()

    def _object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SandboxError("非法对象哈希")
        path = (self.objects_dir / digest[:2] / digest).resolve()
        if self.objects_dir.resolve() not in path.parents:
            raise SandboxError("对象路径越界")
        return path

    # ------------------------------------------------------------------
    # 方案工作区：互不污染
    # ------------------------------------------------------------------

    def scenario_dir(self, scenario_id: str) -> Path:
        safe = _safe_id(scenario_id)
        path = (self.scenarios_dir / safe).resolve()
        if self.scenarios_dir.resolve() not in path.parents:
            raise SandboxError("工作区路径越界")
        return path

    def create_workspace(self, scenario_id: str, binding: dict) -> Path:
        """创建全新的方案工作区；已存在则报错，防止覆盖他人状态。"""
        path = self.scenario_dir(scenario_id)
        if path.exists():
            raise SandboxError(f"方案工作区已存在：{scenario_id}")
        path.mkdir(parents=True)
        (path / "chunks").mkdir()
        self._atomic_write_text(path / "binding.json",
                                dumps_canonical(binding))
        return path

    def workspace_exists(self, scenario_id: str) -> bool:
        return self.scenario_dir(scenario_id).exists()

    def read_binding(self, scenario_id: str) -> dict:
        return self._read_json(scenario_id, "binding.json")

    # -- 运行状态（状态机 + 检查点） -------------------------------------

    def write_state(self, scenario_id: str, state: dict) -> None:
        self._atomic_write_text(
            self.scenario_dir(scenario_id) / "state.json",
            dumps_canonical(state),
        )

    def read_state(self, scenario_id: str) -> dict:
        return self._read_json(scenario_id, "state.json")

    # -- 分块结果：每块一个不可变分片，支撑断点续跑 -----------------------

    def write_chunk(self, scenario_id: str, chunk_index: int, value: object) -> None:
        path = self.scenario_dir(scenario_id) / "chunks" / f"{chunk_index:06d}.json"
        self._atomic_write_text(path, dumps_canonical(value))

    def read_chunks(self, scenario_id: str) -> list[dict]:
        chunks_dir = self.scenario_dir(scenario_id) / "chunks"
        if not chunks_dir.exists():
            return []
        out = []
        for path in sorted(chunks_dir.glob("*.json")):
            out.append(json.loads(path.read_text(encoding="utf-8")))
        return out

    def chunk_count(self, scenario_id: str) -> int:
        chunks_dir = self.scenario_dir(scenario_id) / "chunks"
        return len(list(chunks_dir.glob("*.json"))) if chunks_dir.exists() else 0

    def write_result(self, scenario_id: str, result: object) -> None:
        self._atomic_write_text(
            self.scenario_dir(scenario_id) / "result.json",
            dumps_canonical(to_jsonable(result)),
        )

    def read_result(self, scenario_id: str) -> dict:
        return self._read_json(scenario_id, "result.json")

    def has_result(self, scenario_id: str) -> bool:
        return (self.scenario_dir(scenario_id) / "result.json").exists()

    # -- 取消：彻底清理临时结果 -------------------------------------------

    def purge_workspace(self, scenario_id: str) -> None:
        """删除整个方案工作区（含临时分块与最终结果）。

        共享的输入对象位于内容寻址区，不会被删除——其他方案仍在使用。
        """
        path = self.scenario_dir(scenario_id)
        if path.exists():
            shutil.rmtree(path)

    # ------------------------------------------------------------------
    # 发布引用台账（只记录引用，不复制任何余额）
    # ------------------------------------------------------------------

    def load_publications(self) -> list[dict]:
        if not self.publications_file.exists():
            return []
        return json.loads(self.publications_file.read_text(encoding="utf-8"))

    def append_publication(self, publication: dict) -> None:
        records = self.load_publications()
        if any(r["policy_id"] == publication["policy_id"] for r in records):
            raise SandboxError(f"政策已发布过：{publication['policy_id']}")
        records.append(publication)
        self._atomic_write_text(self.publications_file,
                                dumps_canonical(records))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _read_json(self, scenario_id: str, name: str) -> dict:
        path = self.scenario_dir(scenario_id) / name
        if not path.exists():
            raise SandboxError(f"{name} 不存在：{scenario_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
