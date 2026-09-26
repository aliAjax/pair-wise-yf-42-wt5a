# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：血缘台账与配对建议演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/pedigree/founders`：奠基血缘贡献台账，按在存活个体中的占比降序，含携带个体及其贡献。
- `GET /api/pedigree/overlap?sire_id=&dam_id=`：只读试算双亲奠基血缘重合度。

## 奠基血缘台账与配对规则

- 档案里没写`sire_id`（父本）的个体按**奠基个体**处理；贡献沿父母链回算：奠基者对自身为1，每向上一代减半（父、母各一半）。父/母缺失或指向不存在档案时，对应一半血缘不计入任何奠基者。
- 台账占比 = 某奠基者血缘在所有**存活个体**（`active`、`quarantined`）中的平均贡献；去世个体不进分母。隔离或已去世的奠基者及其在后代中的占比**照常显示**。
- 创建`pairing`时必须提供`proposed_by`、`sire_id`、`dam_id`，系统计算双亲**奠基血缘重合度**（逐奠基者取双方占比的较小值后求和，上限`FOUNDER_OVERLAP_LIMIT = 0.25`）。超过上限的建议**无法进入待审**，错误信息会指出重合最多的奠基个体；隔离或已去世个体也不能进入新建议。
- 配对记录保存`founder_overlap`、`founder_overlap_detail`和`top_shared_founder_id`。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
