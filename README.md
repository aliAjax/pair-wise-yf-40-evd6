# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。

## 业务规则

- **批次来源**：创建批次时可带`parent_id`指向上游批次。来源必须是已登记的批次，沿来源链向上不允许成环（含自环），否则创建被拒绝。
- **复检解除隔离**：隔离中的批次必须执行`recheck`并提交`recheck_result`（`passed`/`failed`）；只有`passed`才会回到`inspected`状态，复检结论随批次档案永久保存，`failed`仍留在隔离中。
- **放行联动**：`release`时会沿`parent_id`逐级检查上游，任一上游批次仍处于`quarantined`状态时，下游批次不能放行。
- **传播追溯**：温室/种植点执行`trace`并提交起点批次`consignment_ids`后，档案中的`trace_report`包含：
  - `paths`：从每个起点到末端批次的完整传播路径（含分叉）；
  - `batches`：路径上每批的风险状态（`isolated`隔离中、`eliminated`已销毁、`cleared`已放行、`infected`检出有害生物、`observing`观察中、`pending`待检）；
  - `facilities`：按批次`destination`匹配到的受影响种植点。
  追溯可重复提交；同一批次再次上报时沿用首次追溯时记录的结果，不会因后续状态变化而改写。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
