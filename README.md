# 传感器入库排查 · 只读 SQL 工作台

数据工程师排查传感器入库任务重复采样问题的内部工具：在浏览器中上传 SQLite
落地库副本，查看表结构，执行 **只读** `SELECT` / `WITH` 查询，依据结果修正入库逻辑。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

页面上可直接点击 **「使用内置演示库」** 加载传感器演示数据（含故意制造的重复采样），
或 **「下载演示库」** 后走一遍真实上传流程。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
python3 demo/make_demo_db.py            # 生成 demo/demo_sensors.db
cd webapp && python3 app.py             # http://localhost:8000
```

生产模式（与镜像内一致）：

```bash
gunicorn --pythonpath webapp --bind 0.0.0.0:8000 --workers 1 --threads 16 app:app
```

> 查询名额是进程内信号量，**必须单 worker**（多线程没问题）。

## 功能与约束的对应关系

| 需求 | 实现 |
| --- | --- |
| 上传 SQLite 副本 | `POST /upload`，校验 SQLite 文件头与完整性，按会话隔离存储，可随时清除 |
| 查看表结构 | `GET /schema`：表/视图/索引、列定义、行数；读取期间 pin 住受理时副本 |
| 仅 SELECT / WITH | 文本预检（跳过注释取首关键字）+ SQLite authorizer 双层约束 |
| 不得修改原库 | 连接以 `file:...?mode=ro` 只读打开；authorizer 拒绝 INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA 写；自测含写尝试前后行数与文件哈希校验 |
| 不得访问其他文件 | authorizer 拒绝 `ATTACH`；无法通过 SQL 触达库外文件 |
| 不得加载扩展 | `enable_load_extension(False)` + authorizer 拦截 `load_extension()` 函数 |
| 最多 2 条并发，满额明确拒绝 | 信号量名额（默认 2，不排队），满额返回 **429** 与中文提示 |
| 用户可取消 | `POST /query/<id>/cancel`：`conn.interrupt()` 真正中断计算，join 线程后才响应 |
| 超 3 秒自动终止 | 每条查询启动定时器，超时走同一 interrupt 路径 |
| 终止前停止计算并释放名额 | worker 线程**先关闭连接、释放名额，再对外落定终态**；看到 `timeout`/`cancelled` 时计算必然已停、名额必然已还 |
| 其他查询和原库不受影响 | 每条查询独立连接，`interrupt()` 只作用于本连接；库文件只读 |
| 失败不留半份结果 | 结果完整取回后才挂载；任何失败只返回错误信息 |
| 最多 1000 行并提示截断 | `fetchmany(1001)` 判断截断，页面显示截断横幅 |

## 副本切换与读取的竞态修复

排查中曾出现：慢查询运行时点击「使用内置演示库 / 上传替换 / 清除副本」，
原本正常的汇总查询随后报 `database disk image is malformed`。根因是旧实现原地覆盖
同一个 `<uid>.db` 文件，只读连接在复制 / 删除过程中读到了半截文件。修复方案集中在
`webapp/replica_store.py` 的 **版本化不可变副本注册表**：

| 竞态需求 | 实现方式 |
| --- | --- |
| 上传 / 演示库 / 清除均不得破坏已受理查询与表结构读取 | 每次替换生成新版本文件 `uploads/<uid>/r<N>.db`，**只增不盖**；查询与表结构在受理时 `acquire()` 取得副本 pin，全程读取受理时的完整副本 |
| 切换不等待慢查询 | 切换只改"当前版本"指针（锁内 O(1)），不 join、不中断任何在途查询 |
| 新请求使用切换后的副本 | `acquire()` 始终返回当前版本；响应与 `/api/current` 均带 `version` / `label` |
| 替换失败保留原副本 | 写入与完整性校验（文件头 + `PRAGMA integrity_check`）都在 `.part` 临时文件上完成，发布只是同目录原子 `rename`；失败则丢弃临时文件，原副本不动 |
| 页面标明结果来源 | 表结构、查询结果都显示「来源：副本 vN · 标签」；状态栏显示当前副本版本 |
| 旧副本迟到响应不覆盖当前展示 | 前端按 `replica_version` 与当前版本比对：不一致的结果进入「④ 旧副本的迟到响应」存档区，主结果区只展示当前副本内容 |
| 过期清理避开在途读取 | reaper 只回收**非当前、pin=0、超 TTL** 的退役副本；当前副本与在途副本硬保护（TTL=0 也不删） |
| 退役副本用完回收 | 最后一个 pin 释放时（`Replica.release`）立即回收已退役副本及 meta 文件、清空目录 |
| 不能误删当前副本 | 当前版本指针在注册表中受保护，reaper 与 reset 都不会删除它 |
| 重启后会话仍可用 | 每个副本旁写 `<rN>.db.meta.json`；新进程惰性认领磁盘副本（含旧布局 `<uid>.db` 自动迁移为 r1） |

只读保护、查询名额、取消与超时的原有语义与代码路径保持不变（`query_manager.py`
仅把入参从路径换成副本句柄，并在 worker 退出时释放 pin）。

关键参数均可通过环境变量调整：`QUERY_TIMEOUT`（默认 3 秒）、`MAX_CONCURRENT`（默认 2）、
`MAX_ROWS`（默认 1000）、`MAX_UPLOAD_MB`（默认 300）、`UPLOAD_TTL_HOURS`（默认 24）、
`REAPER_INTERVAL_SECONDS`（默认 1800）、`SECRET_KEY`（生产必须覆盖）。

## 演示库与排查 SQL

- `demo/make_demo_db.py`：生成 `demo_sensors.db`（6 个传感器 × 6 小时 × 10 秒间隔，
  13260 行；重试批次 `B-20261110-RETRY` 造成 270 组重复采样，共 300 行多余）。
- `demo/find_duplicates.sql`：重复采样排查 SQL 五连——汇总、明细、按批次定位、
  待清理行预览（`ROW_NUMBER()` 去重）、修复后验证。页面内置了其中三条作为一键示例。
- 页面「慢查询」示例（递归 CTE）用于演示 3 秒自动终止与手动取消。

## 目录结构

```
webapp/
  app.py               # Flask 路由：上传/表结构/查询/取消/演示库/清除
  replica_store.py     # 版本化不可变副本注册表：pin、原子发布、退役回收、TTL 清理
  query_manager.py     # 查询执行器：名额、超时、取消、只读 authorizer、行数上限
  templates/index.html # 单页面前端（无外部依赖，含来源标注与迟到响应存档区）
demo/
  make_demo_db.py      # 演示库生成脚本
  find_duplicates.sql  # 重复采样排查 SQL
tests/
  selftest.py          # 端到端自测（含 7 组副本切换竞态专项）
  test_replica_store.py# ReplicaRegistry 单元测试（36 项）
Dockerfile  docker-compose.yml  requirements.txt
```

## 自测

```bash
# 1. 启动服务（任选一种方式），假设监听 8123
UPLOAD_DIR=/tmp/uploads DEMO_DB=/tmp/demo_sensors.db PORT=8123 python3 webapp/app.py
# 2. 另开终端运行
python3 tests/selftest.py http://127.0.0.1:8123 /tmp/demo_sensors.db /tmp/uploads
# 副本注册表单元测试（无需起服务，仅标准库）
python3 tests/test_replica_store.py
```

端到端覆盖：上传校验、表结构、只读约束（含 `WITH` 伪装写、`ATTACH`、`load_extension`、
多语句）、重复采样 SQL、1000 行截断、失败无半份结果、3 秒自动终止、并发满额 429、
取消真实停止计算并释放名额、其他查询不受影响、库文件哈希不变；以及竞态专项——
在途慢查询时上传替换 / 切演示库 / 清除副本，验证旧查询读旧副本、新查询读新副本、
汇总查询绝不报损坏、替换失败保留原副本、退役副本用完回收、reaper 不误删当前 / 在途副本。

## 已知限制

- 会话与上传副本通过 cookie 关联，重启服务（默认 SECRET_KEY 变化）后需重新加载；
  SECRET_KEY 不变时，磁盘上的版本化副本与 meta 会被自动重新认领。
- 退役历史副本默认 24 小时后由后台线程清理（`UPLOAD_TTL_HOURS`）；当前副本与
  在途副本不受 TTL 影响，期间占用卷空间。
- 查询名额与副本注册表均为进程内实现，不能多 worker / 多副本水平扩展
  （如需扩展应改为外部锁，如 Redis）。
- 结果集在内存中物化（上限 1001 行），不会流式输出；超大单行（如大 BLOB）以 hex 显示。
