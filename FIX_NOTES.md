# 副本切换竞态修复 — 完成说明

## 一、问题根因

旧实现中，会话副本固定为 `uploads/<uid>.db` 一个文件：

- `POST /use-demo` 用 `shutil.copyfile` **原地覆盖**该文件，复制进行中若有只读连接
  打开（慢查询 / 表结构读取），SQLite 会读到只写了一半的页，报
  `database disk image is malformed`，即"运行中切换演示库导致汇总查询报数据库损坏"；
- `POST /upload` 虽用 `os.replace` 原子改名，但同样复用同一路径，已受理但尚未打开
  连接的查询可能读到新文件，不满足"读取受理时的完整副本"；
- `POST /reset` 直接删文件、后台 reaper 按 mtime 删文件，都可能删掉在途读取的副本；
- 响应中没有副本版本概念，前端无法区分结果来自切换前还是切换后的副本。

## 二、实现情况

### 1. 新增 `webapp/replica_store.py`：版本化不可变副本注册表

- 每个会话独立目录 `uploads/<uid>/`，每次替换发布不可变新版本 `r<N>.db`
  （只增不盖），旁路 `r<N>.db.meta.json` 记录来源标签与时间戳；
- 替换流程：写同目录 `.part` 临时文件 → fsync → 文件头 + `PRAGMA integrity_check`
  校验 → 同目录原子 `os.replace` 发布 → 更新"当前版本"指针。任何失败都发生在发布前，
  临时文件由调用方清理，**原副本与版本号均不变**；
- `acquire()` 受理读取时 pin 当前副本（引用计数 +1），`Replica.release()` 释放；
  查询 / 表结构全程持有 pin，切换不等待、不中断在途读取；
- `reset()` 退役会话全部副本：无 pin 的立即删除，有在途读取的保留到最后一个
  release 自动回收（含空目录清理）；reset 留空会话墓碑，防止磁盘残留被误认领；
- `reap(ttl)` 硬保护规则：当前副本不删、pin>0 的副本不删（TTL=0 也不删），
  仅回收非当前、无在途读取、超 TTL 的历史副本；注册表外的崩溃遗留目录整体超 TTL
  才清理；旧布局 `<uid>.db` 在首次访问时惰性迁移为 r1；
- 所有状态变更在一把锁内完成，文件删除在锁外执行，锁内不做重 IO。

### 2. `webapp/query_manager.py`（保持原有并发/取消/超时语义）

- `Job` 入参由路径改为副本句柄，`public()` 增加 `replica_version` / `replica_label`；
- worker 在 finally 中按原顺序停计时器 → 关连接 → 释放名额 → **释放副本 pin** →
  落定终态；名额满或建线程失败时 `submit()` 负责释放 pin，无泄漏；
- 只读 `mode=ro` 连接、authorizer、信号量名额、interrupt 取消、3 秒超时定时器、
  1001 行截断判断等原有代码路径均未改动。

### 3. `webapp/app.py`

- 全局 `ReplicaRegistry`；`/upload`、`/use-demo` 走"临时文件 + 原子发布"，
  损坏 / 非法文件返回 400 且保留原副本；
- `/query` 与 `/schema` 受理即 pin，try/finally 中释放，响应携带副本版本与来源；
- `/api/current` 返回 `version` / `label` / `size`；
- reaper 线程改为调用 `registry.reap()`，间隔新增 `REAPER_INTERVAL_SECONDS` 可调。

### 4. 前端 `webapp/templates/index.html`

- 状态栏、表结构区、查询结果区均显示「来源：副本 vN · 标签」；
- 切换 / 清除后更新当前版本；旧副本的迟到响应（轮询落定的查询、迟到的表结构）
  进入独立的「④ 旧副本的迟到响应」存档区，**绝不覆盖**当前副本的主展示区；
- 切换时给出横幅提示新查询使用新副本。

### 5. Docker

- `Dockerfile` 无需改动（`COPY webapp/` 自动包含新模块，仍单 worker 多线程）；
- `docker-compose.yml` 补充 `UPLOAD_TTL_HOURS` 注释与变量；
  `docker compose up --build` 即可启动，浏览器访问 http://localhost:8000 演示：
  加载演示库 → 跑慢查询 → 中途上传 / 切演示库 / 清除 → 观察旧查询超时落定且归属
  旧版本、新查询走新版本、迟到结果进入存档区。

## 三、验证结果

环境无 Docker / 系统 pip，采用 get-pip 自定义目录安装 Flask 3.1.3 + gunicorn 27.0.1
（`/tmp/pylibs`，仅用于本地验证，不写入仓库），Python 3.11.2。

1. **单元测试** `python3 -I tests/test_replica_store.py`：36 项全部通过，
   覆盖版本不可变、pin 生命周期、reset 退役与用完回收、替换失败保留、
   reaper 三条保护规则、孤儿目录、旧布局迁移与重启认领。
2. **端到端自测** `python3 -I tests/selftest.py`：101 项全部通过（连跑两轮、
   不清理目录均通过），在原 40 项功能回归之外新增 7 组竞态专项：
   - ① 慢查询在途时上传替换：旧查询超时且归属 v1，新查询命中 v2 新表，两份副本均完好；
   - ② 受理后的重复汇总查询在切换后正常返回 270 组，无 malformed；
   - ③ 表结构读取在途切换，返回受理副本结构并带版本号；
   - ④ 非 SQLite / 损坏 SQLite 替换被 400 拒绝，原副本版本与数据不变、无临时文件残留；
   - ⑤ reset 时在途副本保留、查询正常落定后文件被回收；
   - ⑥ use-demo 在途切换同样原子发布、不破坏旧查询；
   - ⑦ 写语句 400、满额 429、取消 cancelled、所有留存副本完好。
3. **gunicorn 验证**：以与镜像相同的 `--workers 1 --threads 8` 启动，完整自测通过。
4. **reaper 专项**：TTL=0、2 秒间隔的实例中，在途副本始终受保护；查询结束并切换后，
   退役 r1 被回收、当前 r2 保留。
5. **重启认领**：同 SECRET_KEY 重启进程后，原 cookie 仍能解析到磁盘副本并正常查询。
6. **压力测试**：6 线程 × 45 秒混合快/慢查询、上传替换、reset，服务端零 500、
   零 malformed、零 pin/名额泄漏（20 条连续快查询全部受理）。

## 四、未完成 / 限制说明

- 本环境无 Docker 守护进程，**未能实际执行 `docker compose build/up`**；
  已用与镜像一致的 gunicorn 单 worker 多线程模式完成等价验证，Dockerfile / compose
  配置本身未做破坏性改动，需在有 Docker 的环境执行一次构建确认。
- 注册表与查询名额仍是进程内结构，维持单 worker 约束（多 worker 需外置协调，超出本次范围）。
- 发布前的 `PRAGMA integrity_check` 对超大库有额外耗时（与文件大小相关），属完整性
  保证的必要成本；当前 300MB 上传上限下可接受，未引入异步校验队列。
