"""版本化、不可变的数据库副本注册表（修复副本切换与读取竞态的核心）。

设计要点：
- 每个会话（uid）拥有独立目录，每次"上传替换 / 使用演示库"都会生成一个**新版本**的
  不可变副本文件（r<版本号>.db），原子 rename 发布；切换只改"当前版本"指针，
  绝不覆盖、截断任何已存在的副本文件 —— 因此在途读取永远面对一份完整文件，
  不会再出现"复制到一半被只读连接打开 → database disk image is malformed"。
- 已受理的查询 / 表结构读取在受理时通过 acquire() 获得副本句柄（pin），
  持有期间该副本的引用计数 > 0；替换、清除、过期清理都只会"退役"副本而不删除，
  直到最后一个在途读取释放（退役副本用完即回收）。
- 当前副本永不会被清理线程删除；有在途读取的副本也不会被删除。
- 替换失败（文件头非法、SQLite 校验失败、IO 错误等）发生在发布之前，
  当前指针与原副本完全不受影响。
- 注册表自身由一把锁保护，所有"检查 + 变更"均在锁内完成；锁内不做 IO 重活，
  文件删除在锁外执行，避免阻塞查询受理。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

_UID_RE = re.compile(r"^[0-9a-f]{32}$")
_REPLICA_RE = re.compile(r"^r(\d+)\.db$")
_META_SUFFIX = ".meta.json"
SQLITE_MAGIC = b"SQLite format 3\x00"


def validate_sqlite_file(path):
    """发布前确认临时文件是完整可用的 SQLite 库（文件头 + 只读完整性检查）。

    失败抛 ValueError（文件头不符）或 sqlite3.DatabaseError（完整性失败），
    由调用方丢弃临时文件；当前副本不受影响。
    """
    with open(path, "rb") as fh:
        magic = fh.read(16)
    if magic != SQLITE_MAGIC:
        raise ValueError("文件不是有效的 SQLite 数据库（缺少 SQLite 文件头）")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise sqlite3.DatabaseError(
                f"SQLite 完整性校验失败：{row[0] if row else '无结果'}"
            )
    finally:
        conn.close()


class Replica:
    """一个不可变数据库副本。引用计数记录在途读取数量，仅由 ReplicaRegistry 维护。"""

    def __init__(self, registry, uid, version, path, label, created_at):
        self.registry = registry
        self.uid = uid
        self.version = version
        self.label = label
        self.path = path
        self.created_at = created_at
        self._pins = 0

    def pin(self):
        self._pins += 1

    def release(self):
        """释放一次在途读取引用；引用归零且副本已退役时由注册表回收文件。"""
        self.registry._release(self)

    @property
    def pinned(self):
        return self._pins > 0

    def public(self):
        return {
            "version": self.version,
            "label": self.label,
            "path": self.path,
            "created_at": self.created_at,
        }


class _SessionState:
    def __init__(self, uid):
        self.uid = uid
        self.replicas = {}          # version -> Replica
        self.current_version = None
        self.seq = 0

    def dir(self, upload_dir):
        return os.path.join(upload_dir, self.uid)


class ReplicaRegistry:
    def __init__(self, upload_dir):
        self.upload_dir = os.path.abspath(upload_dir)
        os.makedirs(self.upload_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions = {}        # uid -> _SessionState

    # ---------------- 内部工具 ----------------

    def _meta_path(self, path):
        return path + _META_SUFFIX

    def _write_meta(self, replica):
        """副本元数据落盘，便于服务重启后重新认领（原子写）。"""
        meta = {
            "version": replica.version,
            "label": replica.label,
            "created_at": replica.created_at,
        }
        meta_path = self._meta_path(replica.path)
        tmp = meta_path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, meta_path)

    @staticmethod
    def _remove_files(paths):
        for p in paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _adopt_locked(self, uid, state):
        """惰性认领磁盘上已有但本进程尚未登记的副本（进程重启 / 旧布局迁移）。"""
        sdir = state.dir(self.upload_dir)
        legacy = os.path.join(self.upload_dir, uid + ".db")
        if not os.path.isdir(sdir):
            # 旧版本把副本直接放在 UPLOAD_DIR/<uid>.db：迁移为 r1 版本
            if os.path.isfile(legacy):
                os.makedirs(sdir, exist_ok=True)
                target = os.path.join(sdir, "r1.db")
                try:
                    os.replace(legacy, target)
                except OSError:
                    return
            else:
                return
        entries = []
        try:
            names = os.listdir(sdir)
        except FileNotFoundError:
            return
        for name in names:
            m = _REPLICA_RE.match(name)
            if not m:
                continue
            path = os.path.join(sdir, name)
            if not os.path.isfile(path):
                continue
            version = int(m.group(1))
            label = "重启前保留的副本"
            created_at = None
            try:
                with open(self._meta_path(path), "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                label = meta.get("label") or label
                created_at = meta.get("created_at")
            except (OSError, ValueError):
                pass
            if created_at is None:
                try:
                    created_at = os.path.getmtime(path)
                except OSError:
                    created_at = time.time()
            entries.append((version, path, label, created_at))
        for version, path, label, created_at in sorted(entries):
            state.replicas[version] = Replica(self, uid, version, path, label, created_at)
            state.seq = max(state.seq, version)
        if entries:
            state.current_version = max(v for v, *_ in entries)

    # ---------------- 对外接口 ----------------

    def temp_path(self, uid):
        """为一次替换准备临时文件路径（与最终副本同目录，保证 rename 同文件系统）。"""
        sdir = os.path.join(self.upload_dir, uid)
        os.makedirs(sdir, exist_ok=True)
        return os.path.join(sdir, f"upload-{uuid.uuid4().hex}.part")

    def publish(self, uid, tmp_path, label):
        """把已完整写入并校验过的临时文件发布为新版本，原子切换当前指针。

        成功返回新的 Replica；失败抛异常且当前副本保持不变。
        """
        now = time.time()
        with self._lock:
            state = self._sessions.get(uid)
            if state is None:
                state = _SessionState(uid)
                self._sessions[uid] = state
                self._adopt_locked(uid, state)
            version = state.seq + 1
            sdir = state.dir(self.upload_dir)
            os.makedirs(sdir, exist_ok=True)
            final_path = os.path.join(sdir, f"r{version}.db")
            if os.path.exists(final_path):  # 理论上不可能，防御性处理
                raise FileExistsError(f"副本版本已存在: r{version}.db")
            # 发布前最后校验：损坏 / 半截文件在此被拒，临时文件留给调用方清理，
            # 当前副本与版本号都不受影响（seq 在成功发布后才推进）
            validate_sqlite_file(tmp_path)
            # 同目录原子改名：要么完整发布成功，要么临时文件留在原地，当前副本不受影响
            os.replace(tmp_path, final_path)
            replica = Replica(self, uid, version, final_path, label, now)
            self._write_meta(replica)
            state.replicas[version] = replica
            state.current_version = version
            state.seq = version
            return replica

    def acquire(self, uid):
        """获取会话当前副本并登记一次在途读取（pin）。无副本返回 None。

        调用方必须在读取结束后调用 Replica.release()（推荐 try/finally）。
        """
        with self._lock:
            state = self._sessions.get(uid)
            if state is None:
                state = _SessionState(uid)
                self._sessions[uid] = state
                self._adopt_locked(uid, state)
            # state 已存在（含 reset 留下的空墓碑）：不重新认领磁盘残留
            if state.current_version is None:
                return None
            replica = state.replicas.get(state.current_version)
            if replica is None:
                return None
            replica.pin()
            return replica

    def current_info(self, uid):
        with self._lock:
            state = self._sessions.get(uid)
            if state is None:
                state = _SessionState(uid)
                self._sessions[uid] = state
                self._adopt_locked(uid, state)
            if state.current_version is None:
                return None
            replica = state.replicas[state.current_version]
            try:
                size = os.path.getsize(replica.path)
            except OSError:
                size = None
            info = replica.public()
            info["size"] = size
            return info

    def reset(self, uid):
        """清除会话的全部副本：退役当前版本，在途读取继续使用各自持有的旧副本。"""
        with self._lock:
            state = self._sessions.pop(uid, None)
            # 留下显式的空会话墓碑：避免 acquire/current_info 把同目录残留（在途副本文件）
            # 误当成磁盘孤儿重新认领；墓碑在下次 publish 时自然被覆盖。
            self._sessions[uid] = _SessionState(uid)
            retired = list(state.replicas.values()) if state is not None else []
        # 锁外删除无在途读取的副本；仍被 pin 的退役副本等 release 时回收
        garbage = []
        for replica in retired:
            if not replica.pinned:
                garbage.append(replica.path)
                garbage.append(self._meta_path(replica.path))
        self._remove_files(garbage)
        sdir = os.path.join(self.upload_dir, uid)
        try:
            # 目录可能还留着 .part 或被 pin 副本，忽略"目录非空"错误
            os.rmdir(sdir)
        except OSError:
            pass

    def _release(self, replica):
        garbage = []
        sdir = None
        with self._lock:
            replica._pins -= 1
            if replica._pins < 0:
                replica._pins = 0
                return
            if replica._pins != 0:
                return
            state = self._sessions.get(replica.uid)
            # 仍是会话当前副本：保留（reset 后会话可能被惰性重建，此时 current_version 为 None）
            if state is not None and state.current_version == replica.version:
                return
            # 历史版本或已退役副本（含 reset 后重建的空会话），引用归零即回收
            if state is not None:
                state.replicas.pop(replica.version, None)
            garbage = [replica.path, self._meta_path(replica.path)]
            sdir = state.dir(self.upload_dir) if state is not None else os.path.join(
                self.upload_dir, replica.uid
            )
        self._remove_files(garbage)
        try:
            os.rmdir(sdir)  # 目录里还有其他副本 / 临时文件时会失败，忽略即可
        except OSError:
            pass

    def reap(self, ttl_seconds):
        """过期清理（在后台线程周期调用）。

        规则：
        1. 当前副本永不删除；
        2. 有在途读取（pin > 0）的副本永不删除；
        3. 退役/历史版本副本，超过 TTL 且无在途读取才删除；
        4. 注册表不认识的会话目录（如进程崩溃遗留），整个目录超 TTL 才清理；
        5. 旧布局遗留的散文件超 TTL 才清理。
        """
        now = time.time()
        garbage = []
        orphan_dirs = []
        with self._lock:
            known_uids = set(self._sessions)
            for uid, state in list(self._sessions.items()):
                current = state.current_version
                for version, replica in list(state.replicas.items()):
                    if version == current or replica.pinned:
                        continue  # 当前副本 / 在途副本：受保护
                    if now - replica.created_at <= ttl_seconds:
                        continue
                    state.replicas.pop(version, None)
                    garbage.append(replica.path)
                    garbage.append(self._meta_path(replica.path))
            try:
                names = os.listdir(self.upload_dir)
            except OSError:
                names = []
            for name in names:
                full = os.path.join(self.upload_dir, name)
                if _UID_RE.match(name):
                    if name not in known_uids:
                        orphan_dirs.append(full)
                    continue
                # 旧布局或临时散文件：按 mtime 判断 TTL
                try:
                    if now - os.path.getmtime(full) > ttl_seconds:
                        garbage.append(full)
                except OSError:
                    pass
        self._remove_files(garbage)
        for sdir in orphan_dirs:
            try:
                youngest = max(
                    (os.path.getmtime(os.path.join(sdir, n)) for n in os.listdir(sdir)),
                    default=0,
                )
            except OSError:
                continue
            if now - youngest <= ttl_seconds:
                continue
            shutil.rmtree(sdir, ignore_errors=True)

    def active_pin_count(self):
        """诊断用：当前全部在途读取计数。"""
        with self._lock:
            return sum(r._pins for s in self._sessions.values() for r in s.replicas.values())
