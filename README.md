# 群体伤亡医院应急扩容协调

纯Python标准库实现的群体伤亡医院应急扩容协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、伤员资源需求、医院容量和分流和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/equipment_rules.py`：设备周转台规则——设备状态机、机型匹配（氧气驱动与涡轮不能混用）、消毒有效期与可派筛选。
- `src/equipment_repository.py`：设备周转台资料——设备、申请、事件三张表及派单事务。
- `src/equipment_service.py`：设备周转台用例编排——登记、申请、派单、归还、消毒确认。
- `static/index.html`：最小演示页面。
- `static/equipment.html`：设备周转台页面（登记、清单、待派区、申请）。
- `tests/`：完整流程、规则计算和失败场景测试。

资料（`*_repository.py`）、规则（`*_rules.py`）和页面（`static/`）分开保存。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 设备周转台

针对电话借呼吸机、待消毒设备被误派、两批人抢同一台的问题，服务内置设备周转台：

- 设备登记：编号、机型（`oxygen_driven`氧气驱动 / `turbine`涡轮）、消毒有效期。
- 借用申请：申请方、目的地、机型、预计时长（小时），可指定设备编号。
- 派单：只选消毒有效且机型匹配的设备，氧气驱动与涡轮机型不能混用；两家申请同一台时，后提交的一方留在待派区并显示等待的设备编号。
- 归还与消毒：归还后设备转待消毒，消毒确认（登记新的有效期）后才能再次派单；超期设备自动退出可派清单。

### 设备周转台接口

- `GET /equipment`：设备周转台页面。
- `POST /api/equipment`：登记设备，请求体`{"code":"VENT-001","model_type":"turbine","disinfection_valid_until":"2026-09-30T08:00:00Z"}`。
- `GET /api/equipment`：设备清单，支持`state`过滤，`dispatchable=1`只看可派清单。
- `GET /api/equipment/{code}`：设备详情（含`expired`、`dispatchable`标记）。
- `POST /api/equipment/{code}/actions/return`：归还，设备转待消毒，在借申请同步完结。
- `POST /api/equipment/{code}/actions/confirm_disinfection`：消毒确认，请求体`{"expected_version":1,"data":{"disinfection_valid_until":"..."}}`。
- `GET /api/equipment/{code}/audit`：设备事件时间线。
- `POST /api/equipment-requests`：提交申请，请求体`{"reference":"REQ-1","data":{"requester":"急诊科","destination":"急诊抢救室","model_type":"turbine","estimated_hours":4,"desired_equipment_code":"VENT-001"}}`。
- `GET /api/equipment-requests`：申请列表，`state=pending`即待派区。
- `POST /api/equipment-requests/{id}/actions/dispatch`：派单，`data.equipment_code`可空（自动选择消毒有效且机型匹配的设备）。
- `POST /api/equipment-requests/{id}/actions/cancel`：取消待派申请。
- `GET /api/equipment-requests/{id}/audit`：申请事件时间线。
- `GET /api/equipment-stats`：设备与申请状态统计。

设备周转台角色：`equipment_manager`（登记、派单、归还、消毒确认）、`department_requester`（提交与取消申请），`admin`通用。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
