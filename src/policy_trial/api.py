"""仅依赖标准库的试算 HTTP 接口。

路由：

    POST   /snapshots                 复制历史快照（企业列表）到沙箱
    POST   /parameters                注册参数版本
    POST   /scenarios                 创建方案并绑定快照 + 参数版本
    GET    /scenarios/{id}            查询状态（含检查点进度）
    POST   /scenarios/{id}/run        运行 / 断点续跑批次
    POST   /scenarios/{id}/cancel     取消并清理临时结果
    GET    /scenarios/{id}/result     读取完整结果与指标
    POST   /scenarios/{id}/review     核算专员复核
    POST   /scenarios/{id}/replay     重放并校验结果指纹
    GET    /compare?a=&b=             比较任意两次结果
    POST   /policies                  正式政策发布（只允许引用已复核方案）
    GET    /policies                  发布引用台账

服务本身无状态：所有方案状态都在沙箱目录里，重启进程后批次仍可恢复、
结果仍可重放。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .ledger import FormalLedger
from .models import (
    EnterpriseRecord,
    ExemptionRule,
    HistoricalSnapshot,
    ParameterVersion,
)
from .service import CancelledError, TrialService, TrialServiceError
from .storage import SandboxError, SandboxStore


def _build_snapshot(payload: dict) -> HistoricalSnapshot:
    enterprises = tuple(
        EnterpriseRecord(
            enterprise_id=e["enterprise_id"],
            sector=e["sector"],
            base_amount=float(e["base_amount"]),
            current_payment=float(e["current_payment"]),
            carry_over=float(e.get("carry_over", 0.0)),
            exempt=bool(e.get("exempt", False)),
        ) for e in payload["enterprises"]
    )
    return HistoricalSnapshot(
        snapshot_id=payload["snapshot_id"],
        taken_at=payload.get("taken_at", "api-import"),
        enterprises=enterprises,
    )


def _build_params(payload: dict) -> ParameterVersion:
    ex = payload.get("exemption", {})
    return ParameterVersion(
        version=payload["version"],
        coefficient=float(payload["coefficient"]),
        carry_ratio=float(payload["carry_ratio"]),
        exemption=ExemptionRule(
            sectors=tuple(ex.get("sectors", [])),
            enterprise_ids=tuple(ex.get("enterprise_ids", [])),
        ),
        price_pass_through=float(payload.get("price_pass_through", 1.0)),
        note=payload.get("note", ""),
    )


def create_handler(service: TrialService) -> type[BaseHTTPRequestHandler]:
    class TrialHandler(BaseHTTPRequestHandler):
        server_version = "PolicyTrial/0.1"

        def log_message(self, fmt: str, *args: object) -> None:  # 安静日志
            return

        # -- 基础读写 -----------------------------------------------------

        def _send(self, status: int, body: object) -> None:
            from .models import to_jsonable
            data = json.dumps(to_jsonable(body), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _guard(self, fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (TrialServiceError, SandboxError, KeyError, ValueError) as exc:
                status = 409 if isinstance(exc, (TrialServiceError, SandboxError)) else 400
                self._send(status, {"error": type(exc).__name__,
                                    "message": str(exc)})
            except CancelledError as exc:
                self._send(409, {"error": "Cancelled",
                                 "message": f"批次已取消：{exc.args[0]}"})

        # -- 路由 ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            route = parts.path.rstrip("/") or "/"

            if route == "/policies":
                self._send(200, {"publications": service.list_publications()})
                return
            match = re.fullmatch(r"/scenarios/([^/]+)", route)
            if match:
                self._guard(lambda: self._send(
                    200, service.get_state(match.group(1))))
                return
            match = re.fullmatch(r"/scenarios/([^/]+)/result", route)
            if match:
                self._guard(lambda: self._send(
                    200, service.get_result(match.group(1))))
                return
            if route == "/compare":
                query = parse_qs(parts.query)
                if "a" not in query or "b" not in query:
                    self._send(400, {"message": "需要 a、b 两个方案编号"})
                    return
                self._guard(lambda: self._send(
                    200, service.compare(query["a"][0], query["b"][0])))
                return
            self._send(404, {"message": f"未知路由：{route}"})

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            route = parts.path.rstrip("/") or "/"
            body = self._body()

            if route == "/snapshots":
                def handle():
                    snapshot = _build_snapshot(body)
                    digest = service.register_snapshot(snapshot)
                    self._send(201, {"snapshot_id": snapshot.snapshot_id,
                                     "snapshot_hash": digest,
                                     "enterprise_count": len(snapshot.enterprises)})
                self._guard(handle)
                return

            if route == "/parameters":
                def handle():
                    params = _build_params(body)
                    digest = service.register_parameters(params)
                    self._send(201, {"version": params.version,
                                     "params_hash": digest})
                self._guard(handle)
                return

            if route == "/scenarios":
                def handle():
                    state = service.create_scenario(
                        scenario_id=body["scenario_id"],
                        snapshot_hash=body["snapshot_hash"],
                        params_hash=body["params_hash"],
                        created_by=body.get("created_by", "分析人员"),
                    )
                    self._send(201, state)
                self._guard(handle)
                return

            if route == "/policies":
                def handle():
                    publication = service.publish(
                        policy_id=body["policy_id"],
                        scenario_id=body["scenario_id"],
                    )
                    self._send(201, publication)
                self._guard(handle)
                return

            match = re.fullmatch(r"/scenarios/([^/]+)/(run|resume|cancel|review|replay)", route)
            if match:
                scenario_id, action = match.group(1), match.group(2)
                self._guard(self._do_action, scenario_id, action, body)
                return

            self._send(404, {"message": f"未知路由：{route}"})

        def _do_action(self, scenario_id: str, action: str, body: dict) -> None:
            if action in ("run", "resume"):
                result = service.run(scenario_id)
                self._send(200, {"state": service.get_state(scenario_id),
                                 "metrics": result.metrics})
            elif action == "cancel":
                self._send(200, service.cancel(scenario_id))
            elif action == "review":
                state = service.review(
                    scenario_id,
                    reviewer=body.get("reviewer", "核算专员"),
                    comment=body.get("comment", ""),
                )
                self._send(200, state)
            elif action == "replay":
                self._send(200, service.replay(scenario_id))

    return TrialHandler


def build_service(sandbox_dir: str, ledger: FormalLedger | None = None,
                  chunk_size: int = 100) -> TrialService:
    """组装服务（沙箱目录可指向持久化磁盘，重启后状态仍在）。"""
    return TrialService(SandboxStore(sandbox_dir), ledger=ledger,
                        chunk_size=chunk_size)


def serve(sandbox_dir: str, host: str = "127.0.0.1", port: int = 8080,
          chunk_size: int = 100) -> None:
    service = build_service(sandbox_dir, chunk_size=chunk_size)
    httpd = ThreadingHTTPServer((host, port), create_handler(service))
    print(f"政策试算服务已启动：http://{host}:{port}  沙箱：{sandbox_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="政策调整情景试算服务")
    parser.add_argument("--sandbox", required=True, help="沙箱持久化目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--chunk-size", type=int, default=100)
    args = parser.parse_args()
    serve(args.sandbox, host=args.host, port=args.port,
          chunk_size=args.chunk_size)
