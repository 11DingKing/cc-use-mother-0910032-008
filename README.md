# 政策调整情景试算

本项目维护政策调整情景试算的领域约定、角色边界与样例数据，并提供**隔离的服务端试算环境**：分析人员可以在不触碰正式账户的前提下，反复调整分配系数、结转比例与企业豁免规则，比较不同政策参数对市场的影响。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scenario_lab/`：隔离试算环境（模型、存储、引擎、服务、HTTP 接口）。
- `tools/check_contract.py`：契约命令行摘要检查。
- `tools/scenario_demo.py`：端到端演示（快照复制 → 试算 → 复核发布 → 重放比较）。
- `tests/`：契约与试算环境的回归测试。

## 试算环境（src/scenario_lab）

- `models.py`：领域模型。方案状态机与契约一致：`草稿 → 待核算 → 执行中 →（完成/取消回到）待核算 → 已确认 → 已封存`。
- `store.py`：文件型存储。快照与参数版本共享、只读、不可覆盖；每个批次只写独立工作区 `workspaces/<run_id>/`，多方案共享输入但互不污染。
- `engine.py`：确定性试算引擎。按企业编号分块处理、逐块写断点，同一输入无论分几块、是否中断恢复，内容指纹完全一致；输出企业分布（豁免/盈余/平衡/轻中重度缺口）、总缺口与价格压力指标（`100 × 缺口率 × (1 − 结转比例)`）。
- `service.py`：API 边界。快照单向复制进环境、参数版本绑定、可恢复批次（启动/中断/恢复/取消）、复核、正式发布、重放与任意两次结果比较。
- `server.py`：基于标准库的 HTTP 接口（`POST /scenarios/<id>/runs`、`POST /runs/<id>/resume|cancel|replay`、`GET /compare?left=&right=` 等）。

合规护栏：

- 取消批次立即清理该批次的全部临时结果，方案回到待核算；
- 正式发布只能引用**已复核（已确认）**方案的结果摘要与内容指纹，`include_balances=True` 或载荷中夹带逐企业余额都会被拒绝；发布后方案封存，禁止改绑与重跑；
- 批次在创建时固化快照与参数版本，任意批次可按原输入重放校验指纹。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

端到端演示：`python3 tools/scenario_demo.py`（演示数据写入 `var/demo`，已被 git 忽略）
