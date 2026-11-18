"""传感器入库排查页：浏览器上传 SQLite 副本，只读执行 SELECT/WITH 查询。

副本切换与读取的竞态修复（详见 replica_store.py）：
- 上传替换 / 使用演示库 / 清除副本都通过 ReplicaRegistry 操作**版本化的不可变副本**，
  已受理的查询与表结构读取持有受理时副本的 pin，全程读取完整副本，切换不等待慢查询；
- 替换在临时文件上完成并校验，发布只是同目录原子 rename，失败时原副本保留；
- 每个响应携带结果所属的副本版本，前端据此标明来源，并防止旧副本迟到响应覆盖当前展示；
- 后台清理只回收"非当前、无在途读取、超 TTL"的退役副本，当前副本与在途副本受保护。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file, session

from query_manager import QueryManager
from replica_store import ReplicaRegistry

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(REPO_ROOT, "uploads"))
DEMO_DB = os.environ.get("DEMO_DB", os.path.join(REPO_ROOT, "demo", "demo_sensors.db"))

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "300")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

manager = QueryManager(
    max_concurrent=int(os.environ.get("MAX_CONCURRENT", "2")),
    timeout=float(os.environ.get("QUERY_TIMEOUT", "3")),
    max_rows=int(os.environ.get("MAX_ROWS", "1000")),
)
registry = ReplicaRegistry(UPLOAD_DIR)

_UID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------- 会话 ----------------

def _current_uid():
    uid = session.get("uid")
    return uid if uid and _UID_RE.match(uid) else None


def _ensure_uid():
    uid = _current_uid()
    if uid is None:
        uid = uuid.uuid4().hex
        session["uid"] = uid
    return uid


# ---------------- SQL 文本预检（纵深防御的第一层，真正的硬约束在 authorizer） ----------------

def first_keyword(sql):
    """跳过空白与注释，返回第一个 SQL 关键字（大写）。"""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            i = s.find("\n")
            s = "" if i < 0 else s[i + 1 :]
        elif s.startswith("/*"):
            i = s.find("*/")
            if i < 0:
                return ""
            s = s[i + 2 :]
        else:
            break
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).upper() if m else ""


# ---------------- 页面与基础接口 ----------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        max_rows=manager.max_rows,
        timeout=manager.timeout,
        max_concurrent=manager.max_concurrent,
    )


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/current")
def api_current():
    uid = _current_uid()
    if not uid:
        return jsonify({"loaded": False})
    info = registry.current_info(uid)
    if not info:
        return jsonify({"loaded": False})
    return jsonify(
        {
            "loaded": True,
            "size": info["size"],
            "version": info["version"],
            "label": info["label"],
        }
    )


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "请选择要上传的 SQLite 文件"}), 400
    uid = _ensure_uid()
    tmp = registry.temp_path(uid)
    try:
        f.save(tmp)
        # flush 到磁盘，保证 rename 发布的是完整内容
        with open(tmp, "rb+") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        # publish 内部做文件头 + 完整性校验，失败抛异常且当前副本不变
        replica = registry.publish(uid, tmp, label=f"上传：{f.filename}")
    except (ValueError, sqlite3.Error) as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return jsonify({"error": str(exc)}), 400
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return jsonify(
        {
            "ok": True,
            "name": f.filename,
            "size": os.path.getsize(replica.path),
            "version": replica.version,
            "label": replica.label,
        }
    )


@app.post("/use-demo")
def use_demo():
    if not os.path.isfile(DEMO_DB):
        return jsonify({"error": "内置演示库不存在，请先运行 demo/make_demo_db.py 生成"}), 404
    uid = _ensure_uid()
    tmp = registry.temp_path(uid)
    try:
        # 复制到临时文件并落盘，再原子发布 —— 不再原地覆盖当前副本
        shutil.copyfile(DEMO_DB, tmp)
        with open(tmp, "rb+") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        replica = registry.publish(uid, tmp, label="内置演示库 demo_sensors.db")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return jsonify(
        {
            "ok": True,
            "name": "demo_sensors.db",
            "size": os.path.getsize(replica.path),
            "version": replica.version,
            "label": replica.label,
        }
    )


@app.get("/demo/download")
def demo_download():
    if not os.path.isfile(DEMO_DB):
        return jsonify({"error": "演示库不存在"}), 404
    return send_file(DEMO_DB, as_attachment=True, download_name="demo_sensors.db")


@app.post("/reset")
def reset():
    uid = _current_uid()
    if uid:
        # 退役会话全部副本；在途查询继续读完各自持有的副本，结束后自动回收
        registry.reset(uid)
    session.clear()
    return jsonify({"ok": True})


# ---------------- 表结构 ----------------

@app.get("/schema")
def schema():
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    # 受理表结构读取：pin 当前副本，整个读取过程都面对受理时的完整副本
    replica = registry.acquire(uid)
    if replica is None:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    try:
        conn = manager.connect_readonly(replica.path)
        try:
            conn.row_factory = sqlite3.Row
            objects = [
                dict(r)
                for r in conn.execute(
                    "SELECT type, name, tbl_name AS parent, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ]
            tables = []
            for obj in objects:
                if obj["type"] not in ("table", "view"):
                    continue
                name = obj["name"]
                cols = [
                    dict(r)
                    for r in conn.execute(
                        'SELECT cid, name, type, "notnull", dflt_value, pk '
                        "FROM pragma_table_info(?)",
                        (name,),
                    )
                ]
                quoted = '"' + name.replace('"', '""') + '"'
                try:
                    row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                except sqlite3.Error:
                    row_count = None
                tables.append(
                    {
                        "name": name,
                        "type": obj["type"],
                        "sql": obj["sql"],
                        "columns": cols,
                        "row_count": row_count,
                    }
                )
            indexes = [
                {"name": o["name"], "parent": o["parent"], "sql": o["sql"]}
                for o in objects
                if o["type"] == "index"
            ]
            return jsonify(
                {
                    "tables": tables,
                    "indexes": indexes,
                    "replica_version": replica.version,
                    "replica_label": replica.label,
                }
            )
        finally:
            conn.close()
    finally:
        replica.release()


# ---------------- 查询执行 ----------------

@app.post("/query")
def start_query():
    uid = _current_uid()
    if not uid:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    data = request.get_json(silent=True) or {}
    sql = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "SQL 不能为空"}), 400
    if len(sql) > 100_000:
        return jsonify({"error": "SQL 过长（>100KB）"}), 400
    kw = first_keyword(sql)
    if kw not in ("SELECT", "WITH"):
        return jsonify({"error": f"仅允许 SELECT / WITH 只读查询（检测到：{kw or '无法解析'}）"}), 400
    # 受理即 pin：该查询之后读取的始终是受理时的完整副本，切换/清除都不影响它
    replica = registry.acquire(uid)
    if replica is None:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    job = manager.submit(replica, sql)
    if job is None:
        # submit 已负责释放 pin
        return (
            jsonify(
                {
                    "error": f"查询位已满：最多同时运行 {manager.max_concurrent} 条查询，"
                    "请等待其完成或先取消进行中的查询"
                }
            ),
            429,
        )
    return jsonify({"job_id": job.id, "replica_version": replica.version})


@app.get("/query/<job_id>")
def query_status(job_id):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "查询不存在或已过期"}), 404
    return jsonify(job.public())


@app.post("/query/<job_id>/cancel")
def query_cancel(job_id):
    job = manager.cancel_and_wait(job_id)
    if job is None:
        return jsonify({"error": "查询不存在或已过期"}), 404
    return jsonify(job.public())


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "文件过大，超过上传限制"}), 413


def _upload_reaper():
    """定期回收退役副本：当前副本与在途副本受保护，不影响任何读取。"""
    ttl = int(os.environ.get("UPLOAD_TTL_HOURS", "24")) * 3600
    interval = int(os.environ.get("REAPER_INTERVAL_SECONDS", "1800"))
    while True:
        time.sleep(interval)
        try:
            registry.reap(ttl)
        except Exception:
            pass


if os.environ.get("UPLOAD_REAPER", "1") == "1":
    threading.Thread(target=_upload_reaper, daemon=True, name="upload-reaper").start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
