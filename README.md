# 地下管网施工冲突协同后端

针对“道路改造误伤燃气支线”类事故的完整协同后端：权属单位提交**带生效期与保密等级的管段快照**，
工程方以**施工范围、工法、时间窗、影响面**申请占用；系统按评估当时可见的资料计算
**管段空间/时间冲突**与**相邻工程封路、安全缓冲区冲突**，组织**权属确认与高风险风险会签**，
再由协调员签发许可证。核心红线：**未回复的高风险管段永远不会被默认为安全**。

零外部依赖（仅 Python 3.11+ 标准库），无需浏览器、数据库或其他运行服务。

## 领域规则如何落地

| 需求 | 实现 |
| --- | --- |
| 各单位版本不同的管段资料 | `PipeSnapshot` 版本链：同 `(owner, pipe_code)` 的每次提交形成 `version` + `supersedes`，评估只引用当时最新可见版本 |
| 带生效期与保密等级 | `effective_from/effective_to` + `Classification(1公开/2内部/3敏感)`，未来生效与已失效快照不参与评估 |
| 按当时可见资料评估 | 每次评估固化 `visible_as_of` 与命中快照 id/版本；重新评估生成**新评估**，历史评估永不改写 |
| 相邻工程封路/安全缓冲区冲突 | 施工多边形两两测距，比较时间窗重叠、封路多边形相交、双方影响半径之和 |
| 唯一合法占位 | 持占位状态仅 `in_review/review_closed/permit_issued`；冲突的并发申请为 `occupancy_blocked`，占位释放后按提交顺序自动重评估。API 层全局串行化保证并发原子性 |
| 未回复的高风险不得默认安全 | 高风险要求 `co_sign`；48h（紧急 4h）到期后低风险可留痕为 `no_response_low`，高风险保持 `pending` 并持续阻断签发；任何口头/书面担责都不能替代高风险会签 |
| 设计变更/延期 | `revise_application` 产生新版本并作废旧许可证、重新评估；同一快照版本的历史意见显式记为 `carried` |
| 新版管段资料 | 快照更新自动触发占位中相关申请重评估；新版本必须重新会签；已完成的旧审核仍引用原始快照 |
| 许可证暂停/恢复 | `suspend` 释放占位并作废许可证，`resume` 基于最新资料重新评估 |
| 紧急抢修 | `emergency_repair` 暂停时间窗重叠的已签发许可、抢占占位；抢修仍走完整会签，红线不破 |
| 敏感坐标最小披露 | 按角色密级决定可见性：工程方看不到敏感几何、管段标识与权属被掩码、距离粗化到 5m；权属只见己方；协调员/审计可见全部 |
| 事故可还原 | 所有动作写入**只追加、SHA-256 哈希链**审计日志（JSONL）；通知按接收方视角固化。可用 API 或 CLI 校验链并还原事故前任一方看到的版本、警示、决定、通知 |

## 模块结构

```
src/utility_coordination/
  contracts.py   原始不可变记录与稳定摘要（保留的领域起点）
  models.py      实体/枚举/状态机：快照、申请、评估、确认、许可证、通知
  geometry.py    折线/多边形距离、相交、点包含（纯 Python）
  timeutil.py    UTC 时间、半开区间重叠、可注入时钟
  service.py     CoordinationService：全部业务流程与权限校验
  disclosure.py  按角色最小披露视图
  events.py      哈希链式只追加事件日志（可持久化 JSONL，加载即校验）
  replay.py      事故还原：参与方视图、申请版本时间线、全局报告
  api.py         标准库 HTTP API（X-Actor-Id 鉴权，写操作全局加锁）
  demo.py        端到端事故场景
```

## 运行测试 / 编译检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```

## 命令行

```bash
# 端到端演示（并发占位、逾期红线、延期沿用、新快照重会签、紧急抢修抢占、事故还原）
PYTHONPATH=src python3 run_cli.py demo

# 审计链校验（篡改任一字段都会失败）
PYTHONPATH=src python3 run_cli.py verify

# 事故还原：全局报告 / 单个参与方视角 / 单个申请版本链
PYTHONPATH=src python3 run_cli.py incident --before 2026-09-03T05:00:00Z
PYTHONPATH=src python3 run_cli.py actor owner-gas --before 2026-09-03T05:00:00Z
PYTHONPATH=src python3 run_cli.py application app-000011

# 启动 HTTP API
PYTHONPATH=src python3 run_cli.py serve --port 8080
```

默认审计日志写入 `data/audit.jsonl`，可用 `--audit-file` 更改；事故还原只读该文件，可离线执行。

## HTTP API 摘要

所有写请求带 `X-Actor-Id` 头；服务层再做角色鉴权。

- `POST /actors` 注册参与方（coordinator/owner/contractor/auditor）
- `POST /snapshots` 权属提交管段快照（自动版本递增）
- `POST /applications` 提交占用申请，响应同时给出按调用者披露的评估
- `POST /applications/{id}/respond` 权属确认/会签（approve/reject）
- `POST /system/expire-responses` 回复期收口（低风险留痕、高风险保持阻断）
- `POST /applications/{id}/issue-permit` 签发许可证
- `POST /applications/{id}/revise` 设计变更/延期 → 重评估
- `POST /applications/{id}/suspend` / `resume` 暂停/恢复
- `POST /emergency-repairs` 紧急抢修（抢占重叠许可）
- `POST /applications/{id}/complete` 完工释放占位
- `GET  /applications/{id}/assessments` 评估版本链（旧评估保留旧快照引用）
- `GET  /notifications` 当前参与方收到的通知
- `GET  /audit/verify` 哈希链校验
- `GET  /replay/incident[?before=]`、`/replay/actors/{id}`、`/replay/applications/{id}` 事故还原

调用示例：

```bash
curl -s localhost:8080/audit/verify
curl -s -X POST localhost:8080/snapshots -H 'X-Actor-Id: owner-gas' \
  -H 'Content-Type: application/json' \
  -d '{"pipe_code":"gas-17","utility_type":"gas","classification":3,"risk_level":"high",
       "geometry":[[0,50],[80,50]],"diameter_mm":200,"pressure":"0.4MPa",
       "effective_from":"2026-01-01T00:00:00Z"}'
```

## 说明与边界

- 坐标假设为局部投影平面坐标（米）；接入真实 GIS 时替换 `geometry.py` 即可，领域层不变。
- HTTP 的 `X-Actor-Id` 仅为演示鉴权；生产部署应替换为带签名的身份令牌，鉴权决策本身仍在服务层。
- 状态存储为进程内 + 审计 JSONL；重启后通过重放审计链即可还原事实（事件已包含完整载荷）。
