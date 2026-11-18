#!/usr/bin/env python3
"""关键功能自测：只读约束、并发名额、超时、取消、截断、失败不留半份结果。

用法：
    python3 tests/selftest.py <base_url> <demo_db_path> <server_upload_dir>

需要被测服务已在运行（UPLOAD_DIR 指向 <server_upload_dir>）。
仅用标准库，可用 python3 -I 运行。
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib

SLOW_SQL = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt "
    "WHERE x < 300000000) SELECT COUNT(*), SUM(x) FROM cnt"
)
DUP_SQL = (
    "SELECT sensor_id, ts, COUNT(*) AS n FROM sensor_samples "
    "GROUP BY sensor_id, ts HAVING COUNT(*) > 1 ORDER BY n DESC, sensor_id, ts"
)

failures = []
checks = [0]


def check(name, cond, detail=""):
    checks[0] += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_variant_db(path, marker):
    """基于演示库造一份内容不同的合法 SQLite 变体库（新增标记表）。"""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("ATTACH DATABASE ? AS src", (demo_db_path,))
    conn.execute("CREATE TABLE sensor_samples AS SELECT * FROM src.sensor_samples")
    conn.execute("CREATE TABLE sensors AS SELECT * FROM src.sensors")
    conn.execute("CREATE TABLE variant_marker (note TEXT)")
    conn.execute("INSERT INTO variant_marker VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def make_corrupt_db(path):
    """有效 SQLite 文件头但内容损坏，用于验证替换失败保留原副本。"""
    raw = bytearray(open(demo_db_path, "rb").read())
    for i in range(100, min(len(raw), 20000)):
        raw[i] = (raw[i] + 13) & 0xFF
    with open(path, "wb") as fh:
        fh.write(raw)


def list_replica_files(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".db"):
                out.append(os.path.join(dirpath, name))
    return out


demo_db_path = None


def _b64url_decode(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _uid_of(client):
    """从 Flask 签名会话 cookie 中解出 uid（只解 payload，不依赖签名密钥）。"""
    for handler in client.opener.handlers:
        if isinstance(handler, urllib.request.HTTPCookieProcessor):
            for ck in handler.cookiejar:
                if ck.name != "session":
                    continue
                payload = ck.value.split(".")[0]
                raw = _b64url_decode(payload)
                if raw.startswith(b"."):  # Flask 压缩会话
                    raw = zlib.decompress(raw[1:])
                data = json.loads(raw)
                uid = data.get("uid")
                if uid and len(uid) == 32:
                    return uid
    return ""


class Client:
    """带独立 cookie 会话的 HTTP 客户端。"""

    def __init__(self, base):
        self.base = base
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(self, method, path, payload=None):
        data = headers = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def upload(self, path, filename="demo.db", content=None):
        if content is None:
            with open(path, "rb") as f:
                content = f.read()
        boundary = "----selftest-boundary-9f2c"
        body = b"\r\n".join(
            [
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
                b"Content-Type: application/octet-stream",
                b"",
                content,
                f"--{boundary}--".encode(),
                b"",
            ]
        )
        req = urllib.request.Request(
            self.base + "/upload",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def run_query(self, sql):
        return self.call("POST", "/query", {"sql": sql})

    def wait_final(self, job_id, timeout=20.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st, j = self.call("GET", f"/query/{job_id}")
            assert st == 200, f"status {st}"
            if j["status"] in ("done", "error", "timeout", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("查询未在预期时间内到达终态")


def main():
    base, demo_db, upload_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    global demo_db_path
    demo_db_path = demo_db
    demo_hash = sha256(demo_db)
    c = Client(base)

    print("== 页面与上传 ==")
    with c.opener.open(base + "/", timeout=10) as resp:
        html = resp.read().decode()
    check("首页可访问且为排查页", resp.status == 200 and "只读 SQL" in html)

    st, j = c.upload(None, "evil.db", b"not a sqlite database at all........")
    check("非 SQLite 文件被拒绝", st == 400, f"got {st} {j}")

    st, j = c.upload(demo_db, "demo_sensors.db")
    check("演示库上传成功", st == 200 and j.get("ok"), f"got {st} {j}")

    st, j = c.call("GET", "/schema")
    tables = {t["name"]: t for t in j.get("tables", [])} if st == 200 else {}
    check("表结构包含 sensor_samples", "sensor_samples" in tables)
    if "sensor_samples" in tables:
        cols = {col["name"] for col in tables["sensor_samples"]["columns"]}
        check("列结构完整", {"id", "sensor_id", "ts", "value", "batch_id"} <= cols, str(cols))
        check("行数统计合理", (tables["sensor_samples"]["row_count"] or 0) > 12000)

    print("== 只读约束 ==")
    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    total_before = j["rows"][0][0] if j["status"] == "done" else None
    check("SELECT 正常执行", j["status"] == "done" and total_before, str(j))

    for bad in ["DELETE FROM sensor_samples", "UPDATE sensor_samples SET value=0",
                "PRAGMA user_version=1", "ATTACH DATABASE '/tmp/evil.db' AS e",
                "DROP TABLE sensor_samples", "INSERT INTO sensors VALUES ('X','x','y','z')"]:
        st, j = c.run_query(bad)
        check(f"非只读语句被 400 拒绝: {bad.split()[0]}", st == 400, f"got {st} {j}")

    st, j = c.run_query("WITH d AS (SELECT 1) DELETE FROM sensor_samples")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("WITH 伪装的 DELETE 被 authorizer 拦截", j["status"] == "error" and "not authorized" in j.get("error", ""), str(j))
    else:
        check("WITH 伪装的 DELETE 被 authorizer 拦截", False, f"got {st} {j}")

    st, j = c.run_query("SELECT load_extension('/tmp/evil.so')")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("load_extension 被拦截", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT 1; SELECT 2")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("多语句被拒绝", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    check("写尝试后数据未变", j["status"] == "done" and j["rows"][0][0] == total_before,
          f"before={total_before} after={j.get('rows')}")

    st, j = c.run_query("SELECT name, type FROM pragma_table_info('sensor_samples') ORDER BY cid")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("pragma 函数只读访问可用", j["status"] == "done" and j["row_count"] == 6, str(j)[:200])

    print("== 重复采样排查 SQL ==")
    st, j = c.run_query(DUP_SQL)
    j = c.wait_final(j["job_id"])
    check("重复汇总查询成功", j["status"] == "done", str(j)[:200])
    if j["status"] == "done":
        check("找到 270 组重复", j["row_count"] == 270, f"got {j['row_count']}")
        check("最大重复次数为 3", j["rows"][0][2] == 3, str(j["rows"][0]))
        check("未截断", j["truncated"] is False)

    print("== 行数上限与截断 ==")
    st, j = c.run_query(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5000) SELECT x FROM c"
    )
    j = c.wait_final(j["job_id"])
    check("超过 1000 行被截断", j["status"] == "done" and j["row_count"] == 1000 and j["truncated"] is True,
          f"rows={j.get('row_count')} truncated={j.get('truncated')}")

    print("== 失败不留半份结果 ==")
    st, j = c.run_query("SELECT * FROM no_such_table")
    j = c.wait_final(j["job_id"])
    check("错误查询无结果字段", j["status"] == "error" and "rows" not in j and "columns" not in j, str(j))

    print("== 超时自动终止 ==")
    t0 = time.monotonic()
    st, j = c.run_query(SLOW_SQL)
    check("慢查询被受理", st == 200, f"got {st} {j}")
    j = c.wait_final(j["job_id"])
    wall = time.monotonic() - t0
    check("慢查询 3s 左右自动终止", j["status"] == "timeout" and 2.0 <= wall <= 8.0,
          f"status={j['status']} wall={wall:.2f}s elapsed_ms={j['elapsed_ms']}")

    print("== 并发名额与取消 ==")
    c2, c3 = Client(base), Client(base)
    st, _ = c2.upload(demo_db, "demo_sensors.db")
    assert st == 200
    st, _ = c3.upload(demo_db, "demo_sensors.db")
    assert st == 200

    st1, r1 = c2.run_query(SLOW_SQL)
    st2, r2 = c3.run_query(SLOW_SQL)
    check("两条慢查询均被受理", st1 == 200 and st2 == 200, f"{st1} {st2}")
    time.sleep(0.6)
    st3, r3 = c.run_query("SELECT 1")
    check("第三条查询被 429 明确拒绝", st3 == 429 and "满" in r3.get("error", ""), f"got {st3} {r3}")

    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r1['job_id']}/cancel")
    cancel_wall = time.monotonic() - t0
    check("取消接口返回 cancelled 且响应迅速", j.get("status") == "cancelled" and cancel_wall < 2.5,
          f"status={j.get('status')} wall={cancel_wall:.2f}s")

    st, j = c.run_query("SELECT 1 AS ok")
    check("取消后名额已释放（新查询立即受理）", st == 200, f"got {st} {j}")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("新查询正常完成", j["status"] == "done" and j["rows"][0][0] == 1, str(j))

    j2 = c3.wait_final(r2["job_id"])
    check("另一条查询不受取消影响（自然超时终止）", j2["status"] == "timeout", j2["status"])

    print("== 手动取消进行中的查询 ==")
    st, r = c2.run_query(SLOW_SQL)
    check("慢查询再次被受理", st == 200, f"got {st}")
    time.sleep(0.7)
    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r['job_id']}/cancel")
    wall = time.monotonic() - t0
    check("取消生效且真实停止计算", j.get("status") == "cancelled" and wall < 2.5 and j.get("elapsed_ms", 9999) < 2500,
          f"status={j.get('status')} wall={wall:.2f}s elapsed={j.get('elapsed_ms')}ms")

    print("== 数据库文件未被修改 ==")
    # 此刻本测试创建的会话都只上传过演示库（r1.db）；只校验这些副本，
    # 后续竞态测试产生的变体副本（r2+）与历史轮次残留不纳入本检查
    uploaded = [
        p
        for p in list_replica_files(upload_dir)
        if os.path.basename(p) == "r1.db"
    ]
    check("服务端存在上传副本", len(uploaded) >= 3, str(uploaded))
    for p in uploaded:
        check(f"副本哈希未变 {os.path.relpath(p, upload_dir)}", sha256(p) == demo_hash)

    print("== 内置演示库入口 ==")
    c4 = Client(base)
    st, j = c4.call("POST", "/use-demo")
    check("use-demo 可用", st == 200 and j.get("ok"), f"got {st} {j}")
    st, j = c4.call("GET", "/schema")
    check("演示库表结构可读", st == 200 and any(t["name"] == "sensor_samples" for t in j.get("tables", [])))

    # ============================================================
    # 副本切换与读取竞态专项（本次修复的核心回归测试）
    # ============================================================
    print("== 竞态①：慢查询在途时上传替换，旧查询读旧副本、新查询读新副本 ==")
    rc = Client(base)
    st, j = rc.call("POST", "/use-demo")
    assert st == 200, j
    v0 = j["version"]
    st, r = rc.run_query(SLOW_SQL)
    check("慢查询被受理（旧副本）", st == 200, f"got {st}")
    time.sleep(0.6)  # 确保查询已在旧副本上跑起来

    variant = os.path.join(tempfile.gettempdir(), "_variant_a_selftest.db")
    make_variant_db(variant, "variant-A")
    st, j = rc.upload(variant, "variant_a.db")
    check("在途期间上传替换成功并产生新版本", st == 200 and j.get("version") == v0 + 1, f"got {st} {j}")
    v1 = j.get("version")

    st, cur = rc.call("GET", "/api/current")
    check("当前指针已切到新副本", cur.get("version") == v1 and "variant_a" in cur.get("label", ""), str(cur))

    st, nr = rc.run_query("SELECT note FROM variant_marker")
    check("新请求使用切换后的副本（能查到新表）", st == 200, f"got {st}")
    nj = rc.wait_final(nr["job_id"])
    check("新副本查询成功且带新版本号",
          nj["status"] == "done" and nj["rows"][0][0] == "variant-A" and nj["replica_version"] == v1, str(nj)[:200])

    old_job = rc.wait_final(r["job_id"])
    check("在途慢查询不受切换影响、自然超时且仍归属旧副本",
          old_job["status"] == "timeout" and old_job["replica_version"] == v0, str(old_job)[:200])

    replica_files = list_replica_files(upload_dir)
    check("新旧两个副本文件都在盘（旧副本未被切换破坏/删除）",
          any(f.endswith(f"r{v0}.db") for f in replica_files)
          and any(f.endswith(f"r{v1}.db") for f in replica_files), str(replica_files))

    print("== 竞态②：切换前受理的普通汇总查询绝不报数据库损坏 ==")
    rc2 = Client(base)
    st, j = rc2.call("POST", "/use-demo")
    assert st == 200
    base_v = j["version"]
    st, r = rc2.run_query(DUP_SQL)
    check("汇总查询被受理", st == 200)
    time.sleep(0.2)
    variant2 = os.path.join(tempfile.gettempdir(), "_variant_b_selftest.db")
    make_variant_db(variant2, "variant-B")
    st, _ = rc2.upload(variant2, "variant_b.db")
    assert st == 200
    dj = rc2.wait_final(r["job_id"])
    check("切换未破坏已受理查询（无 malformed/disk image 错误，270 组重复）",
          dj["status"] == "done" and dj["row_count"] == 270
          and "malformed" not in (dj.get("error") or "") and dj["replica_version"] == base_v,
          str(dj)[:200])

    print("== 竞态③：表结构读取在途时切换，返回受理副本的结构且带版本号 ==")
    rc3 = Client(base)
    st, j = rc3.call("POST", "/use-demo")
    assert st == 200
    sv0 = j["version"]
    barrier = threading.Event()

    def slow_schema():
        slow_schema.out = rc3.call("GET", "/schema")
        barrier.set()

    t = threading.Thread(target=slow_schema)
    t.start()
    time.sleep(0.15)
    st, j = rc3.upload(variant, "variant_again.db")
    assert st == 200
    barrier.wait(timeout=10)
    t.join(timeout=10)
    st, sj = slow_schema.out
    check("表结构读取成功且带副本版本/来源",
          st == 200 and sj.get("replica_version") == sv0 and sj.get("replica_label"), str(sj)[:200])
    check("表结构是受理时旧副本的（不含变体新表）",
          all(tb["name"] != "variant_marker" for tb in sj.get("tables", [])), str([tb["name"] for tb in sj.get("tables", [])]))

    print("== 竞态④：损坏/非法文件替换失败，原副本保留可用 ==")
    rc4 = Client(base)
    st, j = rc4.call("POST", "/use-demo")
    assert st == 200
    keep_v = j["version"]
    st, j = rc4.upload(None, "evil.db", b"not a sqlite database at all........")
    check("非 SQLite 文件被拒绝", st == 400, f"got {st} {j}")
    corrupt = os.path.join(tempfile.gettempdir(), "_corrupt_selftest.db")
    make_corrupt_db(corrupt)
    st, j = rc4.upload(corrupt, "corrupt.db")
    check("损坏 SQLite 被拒绝", st == 400 and ("完整性" in j.get("error", "") or "malformed" in j.get("error", "")),
          f"got {st} {j}")
    st, cur = rc4.call("GET", "/api/current")
    check("替换失败后当前副本仍是原版本", cur.get("version") == keep_v, str(cur))
    st, q = rc4.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    qj = rc4.wait_final(q["job_id"])
    check("原副本查询不受失败替换影响", qj["status"] == "done" and qj["rows"][0][0] == 13260, str(qj)[:200])
    leftovers = [f for f in os.listdir(os.path.join(upload_dir, _uid_of(rc4))) if f.endswith(".part")] \
        if os.path.isdir(os.path.join(upload_dir, _uid_of(rc4))) else []
    check("失败替换不残留临时文件", not leftovers, str(leftovers))

    print("== 竞态⑤：清除副本不影响在途查询，结束后退役副本被回收 ==")
    rc5 = Client(base)
    st, j = rc5.call("POST", "/use-demo")
    assert st == 200
    rv = j["version"]
    sdir_before = os.path.join(upload_dir, _uid_of(rc5))
    st, r = rc5.run_query(SLOW_SQL)
    check("慢查询受理后清除副本", st == 200)
    time.sleep(0.5)
    st, j = rc5.call("POST", "/reset")
    check("reset 成功", st == 200 and j.get("ok"), f"got {st} {j}")
    st, _ = rc5.call("GET", "/api/current")
    check("清除后无当前副本", st == 200 and _.get("loaded") is False, str(_))
    files_during = list_replica_files(sdir_before) if os.path.isdir(sdir_before) else []
    check("在途期间退役副本文件仍保留（避开在途读取）",
          any(f.endswith(f"r{rv}.db") for f in files_during), str(files_during))
    rj = rc5.wait_final(r["job_id"])
    check("被清除副本上的在途查询仍正常落定（超时，归属旧版本）",
          rj["status"] == "timeout" and rj["replica_version"] == rv, str(rj)[:200])
    time.sleep(0.5)
    check("在途读取结束后退役副本被回收",
          not any(f.endswith(f"r{rv}.db") for f in list_replica_files(sdir_before)),
          str(list_replica_files(sdir_before)))

    print("== 竞态⑥：use-demo 切换同样走原子发布，不破坏在途查询 ==")
    rc6 = Client(base)
    st, j = rc6.upload(variant, "variant_for_demo.db")
    assert st == 200
    dv0 = j["version"]
    st, r = rc6.run_query(SLOW_SQL)
    check("变体库上的慢查询被受理", st == 200)
    time.sleep(0.5)
    st, j = rc6.call("POST", "/use-demo")
    check("在途期间切换到演示库成功", st == 200 and j.get("version") == dv0 + 1, f"got {st} {j}")
    rj = rc6.wait_final(r["job_id"])
    check("旧副本慢查询未被 use-demo 破坏", rj["status"] == "timeout" and rj["replica_version"] == dv0, str(rj)[:200])
    st, q = rc6.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    qj = rc6.wait_final(q["job_id"])
    check("切换后新查询落在演示库副本", qj["status"] == "done" and qj["replica_version"] == dv0 + 1, str(qj)[:200])

    print("== 竞态⑦：只读保护 / 名额 / 取消 / 超时行为保持不变 ==")
    rc7 = Client(base)
    st, _ = rc7.call("POST", "/use-demo")
    assert st == 200
    st, j = rc7.run_query("DELETE FROM sensor_samples")
    check("切换后写语句仍被 400 拒绝", st == 400, f"got {st}")
    ca, cb = Client(base), Client(base)
    ca.call("POST", "/use-demo")
    cb.call("POST", "/use-demo")
    s1, r1 = ca.run_query(SLOW_SQL)
    s2, r2 = cb.run_query(SLOW_SQL)
    check("名额语义不变：两条受理", s1 == 200 and s2 == 200, f"{s1} {s2}")
    time.sleep(0.5)
    s3, r3 = rc7.run_query("SELECT 1")
    check("名额语义不变：满额 429", s3 == 429 and "满" in r3.get("error", ""), f"got {s3}")
    st, j = ca.call("POST", f"/query/{r1['job_id']}/cancel")
    check("取消语义不变：cancelled 且计算停止", j.get("status") == "cancelled", str(j)[:150])
    cb.wait_final(r2["job_id"])
    managed = [p for p in list_replica_files(upload_dir)
               if os.path.basename(p).startswith("r") and p.endswith(".db")]
    for p in managed:
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            conn.close()
        except sqlite3.Error as exc:
            check(f"所有留存副本均为完好 SQLite：{os.path.relpath(p, upload_dir)}", False, str(exc))
            break
    else:
        check("所有留存副本均为完好 SQLite", bool(managed))

    print()
    print(f"共 {checks[0]} 项检查，失败 {len(failures)} 项")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
