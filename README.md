# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/extensions`，工程人员提交延期申请（`requested_deadline`、`reason`）
- `GET /api/items/{id}/extensions`
- `POST /api/items/{id}/extensions/{request_id}/decision`，负责人审批（`decision`为`approved`或`rejected`，可附`note`）
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 延期审批

- 仅`dam_engineer`可提交延期申请，需填写新的完成时间和原因；新时间必须晚于当前时间和当前期限。
- 同一缺陷同一时刻只允许一笔待审申请，重复提交会提示已有申请。
- 申请待审期间缺陷仍按原期限催办；`emergency_manager`批准后新期限才生效，驳回则保留原期限。
- 申请人不能审批自己的申请；两人同时审批同一申请时只有一笔生效。
- 已关闭的缺陷不再受理延期申请和审批。
- 申请、审批结果和期限变化全部写入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
