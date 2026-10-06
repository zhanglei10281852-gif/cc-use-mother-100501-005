# 地下管网施工冲突协同

面向城市更新场景的地下管网施工协同后端：供水、供热、燃气、通信等权属单位提交带**生效期**与**保密等级**的管段快照；工程方以施工范围、工法、时间窗和影响面申请占用；服务按**当时可见资料**计算空间与时间冲突，组织权属确认、风险会签与许可证签发，并支持事故后的完整审计还原。纯 Python 标准库实现，不依赖浏览器、外部数据库或其他运行服务。

## 核心规则

- **版本化快照**：管段快照为不可变版本（revision 递增），冲突评估按 as-of 时刻可见的最新版本计算；已完成的评估永远引用原始快照版本，新快照不影响历史结论。
- **冲突筛查**：施工走廊（折线 + 影响面半宽 + 工法系数）与管段（折线 + 安全缓冲 × 风险系数）做水平净距与垂直净距判定，并要求快照生效期覆盖施工时间窗；同时检测与在册许可证的占位冲突。
- **高风险不默认安全**：高风险管段未获权属单位明确确认前，许可证一律不得签发；紧急抢修可先行临时占位（provisional），但未确认风险以警示形式显式留存并加急通知，绝不标记为安全。
- **变更即重估**：设计变更、工程延期、许可证暂停、紧急抢修、完工核销都会暂停受影响许可并重新评估时空相交的后续占用；确认与会签绑定评估版本，条件变化后自动失效重来。
- **唯一合法占位**：许可证签发在锁内原子检查时空重叠，并发申请只有一个能占位；申请与签发均幂等（request_id / 状态判定）。
- **最小披露**：敏感坐标按角色披露——协调员/审计/权属本单位见全量，public 全员可见，internal 对外模糊到 50m 网格，secret 仅披露存在性与风险等级。
- **审计还原**：全部决定写入哈希链事件日志；审计员可从 API 或 CLI 还原任一参与方在事发前看到的版本、警示、决定与后续通知（视图按该参与方自身角色脱敏）。

## 运行环境

- Python 3.11 或更高版本
- Linux、macOS 或 Windows

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 命令行冒烟

```bash
python3 run_cli.py
```

演示"提交快照 → 申请占用 → 命中冲突 → 权属确认 → 风险会签 → 签发许可 → 审计还原"的最小闭环。

## 命令行操作

```bash
PYTHONPATH=src python3 -m utility_coordination.cli --state uc_state.json \
  --actor gas-ops --role utility_owner --owner gas-corp submit-segment \
  --segment-id gas-01 --utility gas --confidentiality internal \
  --path "0,0;100,0" --depth-top 1.2 --depth-bottom 1.8 \
  --effective-from 2026-11-01T00:00:00+00:00 --risk high

PYTHONPATH=src python3 -m utility_coordination.cli --state uc_state.json \
  --actor builder-1 --role contractor apply \
  --request-id req-1 --project road-7 --path "40,-3;60,3" \
  --impact-radius 2 --method open_cut --depth-top 0.5 --depth-bottom 2 \
  --window-start 2026-11-02T08:00:00+00:00 --window-end 2026-11-06T18:00:00+00:00
```

其余子命令：`confirm` / `cosign` / `issue` / `design-change` / `delay` / `suspend` / `resume` / `complete` / `emergency` / `segments` / `show-application` / `audit-view` / `audit-verify`。状态保存在 `--state` 指定的 JSON 文件中，全部输出为 JSON。

## HTTP API

```python
from utility_coordination import CoordinationService
from utility_coordination.api import serve

serve(CoordinationService(), host="127.0.0.1", port=8080)
```

身份通过请求头声明：`X-Actor-Id` / `X-Actor-Role`（`coordinator|utility_owner|contractor|auditor`）/ `X-Owner-Id`（权属单位必填）。主要路由：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /segments` | 提交管段快照 |
| `GET /segments?at=...` | 按角色查看可见管段（最小披露） |
| `POST /applications` | 申请占用并即时筛查 |
| `POST /applications/{id}/confirmations` | 权属确认 |
| `POST /applications/{id}/cosign` | 风险会签 |
| `POST /applications/{id}/issue` | 签发许可证（原子占位） |
| `POST /applications/{id}/design-change` · `delay` · `cancel` | 变更 / 延期 / 撤销 |
| `POST /permits/{id}/suspend` · `resume` · `complete` | 暂停 / 恢复 / 完工核销 |
| `POST /emergency-repairs` | 紧急抢修临时占位（抢占在册许可） |
| `GET /audit/participants/{id}?at=...` | 还原参与方历史视图 |
| `GET /audit/verify` | 校验审计哈希链 |

## 代码结构

```
src/utility_coordination/
  geometry.py   折线净距、深度/时间窗重叠、坐标模糊化
  models.py     不可变领域模型（快照、申请、评估、确认、会签、许可证、通知、事件）
  store.py      内存仓库 + 哈希链审计日志 + JSON 文件持久化
  service.py    协同服务：筛查、审批链、变更重估、并发占位、最小披露
  audit.py      参与方历史视图还原与哈希链校验
  api.py        标准库 HTTP JSON API
  cli.py        命令行入口
  contracts.py  初始领域契约（保留兼容）
```
