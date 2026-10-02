# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/maintenance/backfill-magnitudes`：按报文回填旧事件缺失的震级（仅admin）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 事件合并与拆分

分析员可能对同一次地震各建一条候选事件（需要合并），也可能把两场地震并成了一条（需要拆分）。两个动作都通过`POST /api/entities/<id>/actions`提交，可执行角色为`admin`和`analyst`。

- **合并**：`{"action":"merge","data":{"source_id":"被并事件id","reason":"..."}}`，提交到幸存事件上。被并事件状态变为`merged`，其`merged_into`指向幸存事件。
- **拆分**：`{"action":"split","data":{"stations":["要转出的台站",...],"target_id":"既有事件id"}}`转到既有事件；或把`target_id`换成`"new_event":{"title":"...","id":"可选"}`新建候选事件。

语义规则：

- 一份报文只归一个事件：合并后被并事件不再持有报文；拆分选中的报文从源事件转到目标事件。
- 合并（及拆到既有事件）时同台站重复报文只留一份，保留幸存事件原有报文。
- 报文集合一变动就按现有报文中的震级取中位数重算事件震级；报文没有震级时保留原值。
- 任一边发布过（`published`/`revised`），幸存事件退回待复核（`associated`），需重新复核发布；先前外发内容以旧版形式留在`previous_publications`里。
- 合并和拆分在单个事务里完成并按乐观锁校验版本：两人同时提交同一对事件的合并，先提交的生效，后提交的收到409并能看到并到了哪条。
- 拆分要求源事件至少保留两条报文；新建事件至少转入两条报文。

## 旧数据震级回填

`POST /api/maintenance/backfill-magnitudes`（仅admin）扫描所有事件：事件缺震级且报文带震级时，按报文中位数回填并写审计；已有震级或报文无震级的事件不动。启动时加`--backfill-magnitudes`可在服务起来前先跑一遍回填。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
