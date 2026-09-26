# 群体伤亡医院应急扩容协调

纯Python标准库实现的群体伤亡医院应急扩容协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。现已扩展**设备周转台**：呼吸机登记、借用申请、派单、归还与消毒确认的闭环管理。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、伤员资源需求、医院容量和分流和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/equipment_rules.py`：设备周转台规则（机型匹配、消毒有效期、派单选择、待派冲突）。
- `src/equipment_repository.py`：设备、申请、周转事件的SQLite存储，派单在单事务内完成。
- `src/equipment_service.py`：周转台用例编排与总览组装。
- `static/index.html`：最小演示页面。
- `static/equipment.html`：设备周转台页面（`/equipment`）。
- `tests/`：完整流程、规则计算和失败场景测试。

资料（`*_repository.py`/SQLite）、规则（`*_rules.py`）和页面（`static/`）分开保存。

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

## 设备周转台接口

- `GET /equipment`：周转台页面（可派清单、待派区、设备台账）。
- `GET /api/equipment/board`：周转台总览，含可派清单、待派区（显示等待设备编号）、待消毒、已派出、超期分组。
- `POST /api/equipment/devices`：设备登记，请求体为`{"code":"T-201","model":"turbine","disinfection_valid_until":"2026-09-29T08:00:00Z"}`，`model`取`oxygen_driven`（氧气驱动）或`turbine`（涡轮）。
- `POST /api/equipment/requests`：借用申请，请求体为`{"requester":"急诊科","model":"turbine","duration_hours":4,"destination":"抢救室"}`。
- `POST /api/equipment/requests/{id}/dispatch`：派单，自动选择消毒有效且机型匹配的设备；可带`{"device_code":"T-201"}`指定设备。无可派设备时申请留在待派区，响应和总览中显示等待的设备编号。
- `POST /api/equipment/requests/{id}/cancel`：取消待派申请。
- `POST /api/equipment/devices/{code}/return`：归还，设备转入待消毒，确认消毒前不再可派。
- `POST /api/equipment/devices/{code}/disinfect`：消毒确认，请求体为`{"disinfection_valid_until":"..."}`，设备回到可派清单。
- `GET /api/equipment/devices`、`GET /api/equipment/requests`、`GET /api/equipment/events/{device|request}/{ref}`：台账与周转事件查询。

规则要点：氧气驱动与涡轮机型不能混用；派单只选消毒在有效期内的在库设备，超期设备不进入可派清单；两家申请同一台时，后提交的申请留在待派区并显示该设备编号；归还后必须确认消毒才能再次派单。

周转台角色：`equipment_keeper`（登记、派单、消毒确认）、`department_user`（申请、归还、取消），`admin`通用。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
