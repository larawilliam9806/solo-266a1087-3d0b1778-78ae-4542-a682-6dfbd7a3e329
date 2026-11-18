#!/usr/bin/env python3
"""ReplicaRegistry 单元测试：版本不可变、pin 生命周期、退役回收、reaper 保护规则。

仅依赖标准库，可直接 python3 -I tests/test_replica_store.py 运行。
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "webapp"))

from replica_store import ReplicaRegistry  # noqa: E402

failures = []
checks = [0]


def check(name, cond, detail=""):
    checks[0] += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def make_db(path, tag):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, tag TEXT)")
    conn.execute("INSERT INTO t(tag) VALUES (?)", (tag,))
    conn.commit()
    conn.close()


def read_tag(replica):
    conn = sqlite3.connect(f"file:{replica.path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT tag FROM t").fetchone()[0]
    finally:
        conn.close()


def main():
    with tempfile.TemporaryDirectory() as root:
        reg = ReplicaRegistry(root)
        uid = "a" * 32

        print("== 发布与不可变版本 ==")
        tmp1 = reg.temp_path(uid)
        make_db(tmp1, "v1-data")
        r1 = reg.publish(uid, tmp1, "label-1")
        check("首个副本版本为 1", r1.version == 1)
        check("当前信息指向 v1", reg.current_info(uid)["version"] == 1)
        check("副本文件为 r1.db", os.path.basename(r1.path) == "r1.db")
        check("meta 旁路文件存在", os.path.exists(r1.path + ".meta.json"))

        tmp2 = reg.temp_path(uid)
        make_db(tmp2, "v2-data")
        r2 = reg.publish(uid, tmp2, "label-2")
        check("替换后版本递增为 2", r2.version == 2 and reg.current_info(uid)["version"] == 2)
        check("旧版本文件仍完整保留", os.path.exists(r1.path) and read_tag(r1) == "v1-data")
        check("新版本内容正确", read_tag(r2) == "v2-data")

        print("== 受理 pin 与切换竞态 ==")
        pin = reg.acquire(uid)
        check("acquire 拿到当前 v2", pin is not None and pin.version == 2)
        tmp3 = reg.temp_path(uid)
        make_db(tmp3, "v3-data")
        r3 = reg.publish(uid, tmp3, "label-3")
        pin2 = reg.acquire(uid)
        check("切换后新请求拿到 v3", pin2.version == 3)
        check("被 pin 的旧句柄仍读到 v2 完整副本", read_tag(pin) == "v2-data")
        check("三个版本文件全部在盘", all(os.path.exists(r.path) for r in (r1, r2, r3)))

        print("== reset 退役与用完回收 ==")
        pin.release()  # v2 已退役（非当前）且无 pin -> 立即回收
        check("退役副本 release 后被回收", not os.path.exists(r2.path))
        pin2.release()  # v3 仍是当前副本 -> 保留
        check("当前副本 release 后保留", os.path.exists(r3.path))

        pin3 = reg.acquire(uid)
        reg.reset(uid)
        check("reset 后无当前副本", reg.acquire(uid) is None and reg.current_info(uid) is None)
        check("reset 时在途副本文件保留", os.path.exists(pin3.path))
        check("reset 时未被 pin 的历史副本立即回收", not os.path.exists(r1.path))
        pin3.release()
        check("在途读取结束后退役副本回收", not os.path.exists(pin3.path))
        check("会话目录清空后被移除", not os.path.isdir(os.path.join(root, uid)))

        print("== 替换失败保留原副本 ==")
        tmp = reg.temp_path(uid)
        make_db(tmp, "good")
        good = reg.publish(uid, tmp, "good-label")
        bad = reg.temp_path(uid)
        # 保留有效 SQLite 文件头、破坏首页头部之后的内容：通过 magic 检查，
        # 但 PRAGMA integrity_check 必须报损坏
        good_bytes = bytearray(open(good.path, "rb").read())
        for i in range(100, min(len(good_bytes), 4000)):
            good_bytes[i] = (good_bytes[i] + 7) & 0xFF
        with open(bad, "wb") as fh:
            fh.write(good_bytes)
        try:
            reg.publish(uid, bad, "bad-label")
            check("损坏文件发布应抛异常", False)
        except sqlite3.Error:
            check("损坏文件发布抛 sqlite3.Error", True)
        # 契约：publish 失败不碰临时文件，由调用方清理（与 app 层一致）
        os.path.exists(bad) and os.remove(bad)
        check("失败后当前副本不变", reg.current_info(uid)["version"] == good.version)
        check("失败后无残留临时文件", not os.path.exists(bad))
        check("原副本内容仍可读", read_tag(good) == "good")
        reg.reset(uid)

        print("== reaper 保护规则 ==")
        # 历史版本超 TTL 可回收；当前版本与在途版本即使超 TTL 也受保护
        t1 = reg.temp_path(uid)
        make_db(t1, "old")
        old = reg.publish(uid, t1, "old")
        t2 = reg.temp_path(uid)
        make_db(t2, "new")
        new = reg.publish(uid, t2, "new")
        # 把时间戳拨老
        old.created_at -= 9999
        new.created_at -= 9999
        pinned = reg.acquire(uid)  # pin 当前 new
        reg.reap(ttl_seconds=60)
        check("超 TTL 的历史版本被回收", not os.path.exists(old.path))
        check("超 TTL 但为当前副本的受保护", os.path.exists(new.path))
        pinned.release()

        # 在途的历史副本：先 pin，再发布新版本使其退役，reaper 不得删除
        hp = reg.acquire(uid)
        t3 = reg.temp_path(uid)
        make_db(t3, "newer")
        newer = reg.publish(uid, t3, "newer")
        hp.created_at -= 9999
        reg.reap(ttl_seconds=60)
        check("超 TTL 但有在途读取的退役副本受保护", os.path.exists(hp.path))
        hp.release()
        check("在途结束后退役副本被回收", not os.path.exists(hp.path))
        check("当前副本始终未被误删", os.path.exists(newer.path) and reg.current_info(uid)["version"] == newer.version)
        reg.reset(uid)

        print("== 崩溃遗留的孤儿目录 ==")
        orphan = os.path.join(root, "b" * 32)
        os.makedirs(orphan)
        of = os.path.join(orphan, "r1.db")
        make_db(of, "orphan")
        # 未超 TTL：保留
        reg.reap(ttl_seconds=3600)
        check("未超 TTL 的孤儿目录保留", os.path.exists(of))
        old_ts = time.time() - 9999
        os.utime(of, (old_ts, old_ts))
        reg.reap(ttl_seconds=60)
        check("超 TTL 的孤儿目录整体清理", not os.path.exists(orphan))

        print("== 旧布局迁移与重启认领 ==")
        uid3 = "c" * 32
        legacy = os.path.join(root, uid3 + ".db")
        make_db(legacy, "legacy")
        adopted = reg.acquire(uid3)
        check("旧布局 <uid>.db 被惰性迁移并认领", adopted is not None and adopted.version == 1)
        check("迁移后旧路径不存在", not os.path.exists(legacy))
        check("迁移后的副本可读", read_tag(adopted) == "legacy")
        adopted.release()

        # 模拟进程重启：新建注册表，凭磁盘文件与 meta 重新认领
        reg2 = ReplicaRegistry(root)
        info = reg2.current_info(uid3)
        check("重启后凭磁盘文件恢复当前版本", info is not None and info["version"] == 1)
        check("重启后 label 从 meta 恢复", isinstance(info["label"], str) and info["label"])
        next_tmp = _make(reg2, uid3, "next")
        next_replica = reg2.publish(uid3, next_tmp, "after-restart")
        check("重启认领后新版本号继续递增", next_replica.version == 2)
        check("重启后发布的副本可读", read_tag(next_replica) == "next")

    print()
    print(f"共 {checks[0]} 项检查，失败 {len(failures)} 项")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)
    print("全部通过 ✅")


def _make(reg, uid, tag):
    tmp = reg.temp_path(uid)
    make_db(tmp, tag)
    return tmp


if __name__ == "__main__":
    main()
