"""基于标准库的 HTTP 接口，把 ScenarioLabService 暴露为服务端 API。

路由一览：
    GET  /health                              健康检查
    POST /snapshots                           复制历史快照进试算环境
    POST /parameters                          注册参数版本
    POST /scenarios                           创建方案 {name, snapshot_id, created_by}
    GET  /scenarios/<id>                      查询方案
    POST /scenarios/<id>/bind                 绑定参数版本 {parameter_version_id}
    POST /scenarios/<id>/runs                 启动批次 {chunk_size?, interrupt_after_chunks?}
    POST /scenarios/<id>/review               复核 {reviewer, note?}
    POST /scenarios/<id>/release              正式发布 {published_by}
    GET  /runs/<id>                           查询批次
    POST /runs/<id>/resume                    恢复批次 {interrupt_after_chunks?}
    POST /runs/<id>/cancel                    取消批次并清理临时结果
    GET  /runs/<id>/result                    查询试算结果
    POST /runs/<id>/replay                    重放批次并校验指纹
    GET  /compare?left=<run>&right=<run>      比较任意两次结果
    GET  /releases/<id>                       查询正式发布
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import AlreadyExistsError, DomainError, NotFoundError
from .models import EnterpriseRecord, ParameterVersion, Snapshot
from .service import ScenarioLabService


def _snapshot_from_payload(data: dict[str, Any]) -> Snapshot:
    return Snapshot(
        snapshot_id=str(data["snapshot_id"]),
        period=str(data["period"]),
        source=str(data.get("source", "正式账户台账")),
        copied_at=str(data["copied_at"]),
        records=tuple(EnterpriseRecord.from_dict(item) for item in data["records"]),
    )


def _parameters_from_payload(data: dict[str, Any]) -> ParameterVersion:
    return ParameterVersion(
        version_id=str(data["version_id"]),
        label=str(data["label"]),
        coefficients={str(k): float(v) for k, v in data["coefficients"].items()},
        adjustment_factor=float(data.get("adjustment_factor", 1.0)),
        carry_ratio=float(data["carry_ratio"]),
        exemption_threshold=float(data.get("exemption_threshold", 0.0)),
        exemption_enterprise_ids=tuple(str(x) for x in data.get("exemption_enterprise_ids", [])),
        created_at=str(data["created_at"]),
    )


def make_handler(service: ScenarioLabService) -> type[BaseHTTPRequestHandler]:
    class ScenarioLabHandler(BaseHTTPRequestHandler):
        server_version = "ScenarioLab/0.1"

        # ---- 基础 ----

        def log_message(self, *args: Any) -> None:  # 保持测试输出干净
            return

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _dispatch(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                segments = [part for part in parsed.path.split("/") if part]
                query = parse_qs(parsed.query)
                payload = self._route(method, segments, query)
                self._send(200, payload)
            except NotFoundError as exc:
                self._send(404, {"error": str(exc)})
            except (AlreadyExistsError, DomainError) as exc:
                self._send(409, {"error": str(exc)})
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": f"请求不合法：{exc}"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        # ---- 路由 ----

        def _route(self, method: str, segments: list[str], query: dict[str, list[str]]) -> Any:
            body = self._body() if method == "POST" else {}

            if method == "GET" and segments == ["health"]:
                return {"status": "ok"}

            if method == "POST" and segments == ["snapshots"]:
                snapshot = service.register_snapshot(_snapshot_from_payload(body))
                return snapshot.to_dict()

            if method == "POST" and segments == ["parameters"]:
                params = service.register_parameters(_parameters_from_payload(body))
                return params.to_dict()

            if method == "POST" and segments == ["scenarios"]:
                scenario = service.create_scenario(
                    name=str(body["name"]),
                    snapshot_id=str(body["snapshot_id"]),
                    created_by=str(body["created_by"]),
                )
                return scenario.to_dict()

            if len(segments) == 2 and segments[0] == "scenarios" and method == "GET":
                return service.get_scenario(segments[1]).to_dict()

            if len(segments) == 3 and segments[0] == "scenarios" and method == "POST":
                scenario_id = segments[1]
                action = segments[2]
                if action == "bind":
                    return service.bind_parameters(scenario_id, str(body["parameter_version_id"])).to_dict()
                if action == "runs":
                    return service.start_run(
                        scenario_id,
                        chunk_size=int(body.get("chunk_size", 500)),
                        interrupt_after_chunks=_optional_int(body.get("interrupt_after_chunks")),
                    ).to_dict()
                if action == "review":
                    return service.review_scenario(
                        scenario_id,
                        reviewer=str(body["reviewer"]),
                        note=str(body.get("note", "")),
                    ).to_dict()
                if action == "release":
                    return service.publish_release(scenario_id, published_by=str(body["published_by"])).to_dict()

            if len(segments) == 2 and segments[0] == "runs" and method == "GET":
                return service.get_run(segments[1]).to_dict()

            if len(segments) == 3 and segments[0] == "runs" and method == "POST":
                run_id = segments[1]
                action = segments[2]
                if action == "resume":
                    return service.resume_run(
                        run_id,
                        interrupt_after_chunks=_optional_int(body.get("interrupt_after_chunks")),
                    ).to_dict()
                if action == "cancel":
                    return service.cancel_run(run_id).to_dict()
                if action == "replay":
                    return service.replay_run(run_id)

            if len(segments) == 3 and segments[0] == "runs" and segments[2] == "result" and method == "GET":
                return service.get_result(segments[1]).to_dict()

            if method == "GET" and segments == ["compare"]:
                left = query["left"][0]
                right = query["right"][0]
                return service.compare_runs(left, right)

            if len(segments) == 2 and segments[0] == "releases" and method == "GET":
                return service.store.load_release(segments[1]).to_dict()

            raise NotFoundError(f"未知路由：{method} /{'/'.join(segments)}")

    return ScenarioLabHandler


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def create_server(
    service: ScenarioLabService,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """创建试算服务 HTTP 服务器（port=0 时自动分配端口）。"""
    return ThreadingHTTPServer((host, port), make_handler(service))
