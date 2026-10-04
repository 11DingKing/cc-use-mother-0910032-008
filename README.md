# 政策调整情景试算

本项目维护政策调整情景试算的领域约定、角色边界与样例数据，并提供**服务端隔离试算环境**的参考实现，供后端服务、接口和自动化验证统一使用。当前契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并明确试算环境隔离、参数版本绑定、可恢复批处理、方案差异比较等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/policy_trial/`：隔离试算服务实现（仅依赖 Python 标准库，Python ≥ 3.11）。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/demo.py`：试算全流程端到端演示。
- `tests/`：契约完整性与试算服务回归测试。

## 试算服务能力

| 需求 | 实现 |
| --- | --- |
| 不污染正式账户 | `ledger.FormalLedger` 对试算侧只读；快照为 frozen 副本，引擎无任何写回接口 |
| 复制历史快照、多方案共享输入 | 快照与参数版本按 SHA-256 内容寻址存放，相同输入物理去重 |
| 绑定参数版本 | 方案创建即绑定 `(snapshot_hash, params_hash)` 并落盘 `binding.json`，不可更换 |
| 可恢复批次 | 企业按块切分，每块原子落盘后推进检查点；中断/重启后 `resume` 跳过已完成块 |
| 取消任务清理临时结果 | 协作式取消标志 + 删除整个方案工作区（分块、结果），共享输入对象保留 |
| 企业分布 / 缺口 / 价格压力 | `engine.aggregate` 输出分行业分布、缺口分桶与价格压力指数 |
| 正式发布闸门 | 仅「已复核」方案可发布；发布台账只记录方案引用与结果指纹，**不复制任何余额** |
| 重放与比较 | `replay` 从内容对象重算并核对指纹；`compare` 比较任意两次结果的指标与逐企业差异 |

所有计算均为纯函数且结果指纹与分块大小无关：同一（快照, 参数版本）在任意机器上重放逐字节一致。

### 沙箱布局

```
<sandbox>/
  objects/<hash[:2]>/<hash>     # 不可变快照与参数版本（跨方案共享）
  scenarios/<scenario_id>/
    binding.json                # 输入绑定
    state.json                  # 状态机 + 检查点
    chunks/000000.json …        # 分块中间结果
    result.json                 # 最终结果（完成后）
  publications.json             # 正式发布引用台账（无余额字段）
```

## HTTP 接口

启动：

```bash
python3 -m policy_trial.api --sandbox ./sandbox-data --port 8080
```

（或将 `src/` 加入 `PYTHONPATH`：`PYTHONPATH=src python3 -m policy_trial.api ...`）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /snapshots` | 复制历史快照到沙箱，返回内容哈希 |
| `POST /parameters` | 注册参数版本（系数、结转比例、豁免规则） |
| `POST /scenarios` | 创建方案并绑定快照 + 参数版本 |
| `GET  /scenarios/{id}` | 查询状态与检查点进度 |
| `POST /scenarios/{id}/run` / `/resume` | 运行 / 断点续跑批次 |
| `POST /scenarios/{id}/cancel` | 取消并清理临时结果 |
| `GET  /scenarios/{id}/result` | 企业明细与汇总指标 |
| `POST /scenarios/{id}/review` | 核算专员复核 |
| `POST /scenarios/{id}/replay` | 重放并校验结果指纹 |
| `GET  /compare?a=&b=` | 比较任意两次结果 |
| `POST /policies` / `GET /policies` | 正式发布（仅引用已复核方案）/ 发布台账 |

## 验证

```bash
# 全部回归测试（契约 + 试算服务）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 端到端演示（隔离运行、中断续跑、比较、复核、发布引用）
PYTHONPATH=src python3 tools/demo.py
```
