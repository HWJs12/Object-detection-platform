"""文件生命周期治理: 引用计数 / 安全回收 / 回收站 / 存储审计。

设计背景
--------
本系统启用内容去重(P1-4): 上传文件按内容 MD5 命名, **同一文件可能被多条记录引用**
(重复上传同一张图 → 两条记录共享一个 uploads 文件)。因此删除记录时**不能无条件删文件**,
必须先确认该文件已无任何记录引用, 否则会误删其他记录仍在使用的数据。

三层保证
--------
1. **引用计数**: 删任何文件前, 重新统计全库引用; 计数 > 0 则保留。
2. **回收站(软删除)**: 被删除的文件先移入 `data/.trash/`, 保留可恢复窗口,
   超期后由 purge 彻底清除 —— 误删可救, 磁盘也终会释放。
3. **审计日志**: 每次回收记录到 `data/cleanup.log`, 便于追溯"哪个文件何时被谁删掉"。

安全边界
--------
只处理 uploads / results / .trash 三个受管目录, 一律 resolve() 校验,
不做任何目录级递归删除。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.db.database import checkpoint, get_connection
from app.utils.file_utils import safe_filename
from config import settings

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024

# 数据库列名 -> 受管目录键
_FILE_COLUMNS: Dict[str, str] = {
    "saved_name": "uploads",
    "result_image": "results",
    "result_video": "results",
}


# ---------------------------------------------------------------- 目录

def trash_dir() -> Path:
    """回收站目录(惰性创建)。"""
    d = settings.data_dir / ".trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dir_of(key: str) -> Path:
    return settings.upload_dir if key == "uploads" else settings.result_dir


def _resolve(column: str, name: str) -> Optional[Path]:
    """把数据库文件名解析为受管目录下的绝对路径(含路径穿越防护)。"""
    if not name:
        return None
    key = _FILE_COLUMNS.get(column)
    if not key:
        return None
    base = _dir_of(key).resolve()
    try:
        target = (base / Path(name).name).resolve()
    except OSError:
        return None
    if base not in target.parents:
        logger.warning("检测到非法文件引用(已跳过): %s", name)
        return None
    return target


# ---------------------------------------------------------------- 引用计数

def referenced_names() -> Set[str]:
    """当前全库引用的全部文件名(一次查询, 供多处复用)。"""
    names: Set[str] = set()
    rows = get_connection().execute(
        "SELECT saved_name, result_image, result_video FROM detections"
    ).fetchall()
    for row in rows:
        for column in _FILE_COLUMNS:
            if row[column]:
                names.add(Path(row[column]).name)
    return names


def record_files(record: Dict[str, Any]) -> List[Tuple[str, Path]]:
    """某条记录涉及的全部受管文件 -> [(列名, 路径)]。"""
    out: List[Tuple[str, Path]] = []
    for column in _FILE_COLUMNS:
        target = _resolve(column, record.get(column) or "")
        if target is not None:
            out.append((column, target))
    return out


def file_usage(name: str) -> int:
    """统计某文件名被多少条记录引用(精确计数, 供删除前复核)。"""
    return int(get_connection().execute(
        "SELECT COUNT(*) AS c FROM detections "
        "WHERE saved_name = ? OR result_image = ? OR result_video = ?",
        (name, name, name),
    ).fetchone()["c"])


# ---------------------------------------------------------------- 审计

def _audit(action: str, entries: Iterable[Dict[str, Any]]) -> None:
    """追加一条 JSONL 审计记录, 便于追溯文件去向。"""
    items = list(entries)
    if not items:
        return
    line = json.dumps({
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "count": len(items),
        "files": items,
    }, ensure_ascii=False)
    try:
        log = settings.data_dir / "cleanup.log"
        with open(log, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError as exc:
        logger.debug("审计日志写入失败: %s", exc)


# ---------------------------------------------------------------- 回收

def original_name_of(trash_name: str) -> str:
    """从回收站文件名反解出原始文件名。

    回收站命名规则: `{YYYYMMDD}_{HHMMSS}_{rand}_{原名}`。
    时间戳自身含下划线, 因此不能用 `split("_", 2)`(会把时间戳后半段混进原名),
    必须按固定宽度逐段剥掉: 8位日期 + 6位时间 + 序号, 剩余部分才是原名。
    """
    name = Path(trash_name).name
    parts = name.split("_")
    # parts[0] = YYYYMMDD, parts[1] = HHMMSS, parts[2] = 序号, 之后为原名
    if len(parts) >= 4 and len(parts[0]) == 8 and parts[0].isdigit() \
            and len(parts[1]) == 6 and parts[1].isdigit():
        return "_".join(parts[3:])
    return name


def _move_to_trash(target: Path) -> Optional[Dict[str, Any]]:
    """把文件移入回收站(不直接删除), 返回审计条目。"""
    if not target.exists():
        return None
    try:
        size = target.stat().st_size
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = trash_dir() / f"{stamp}_{int(time.time()*1000)%100000}_{target.name}"
        shutil.move(str(target), str(dest))
        return {"name": target.name, "bytes": size, "trash": dest.name}
    except OSError as exc:
        logger.debug("移入回收站失败 %s: %s", target.name, exc)
        return None


def release_record_files(record: Dict[str, Any], to_trash: bool = True) -> Dict[str, Any]:
    """释放一条记录占用的文件 —— **仅当该文件已无其他记录引用时**。

    这是删除记录的收尾动作: 先删数据库行(由调用方完成), 再调用本函数。
    由于此时该行已不存在, 引用计数天然反映"剩余引用方", 无需额外排除自己。

    Args:
        record: 已删除的原始记录(含 saved_name / result_image / result_video)
        to_trash: True 移入回收站(可恢复); False 直接物理删除

    Returns:
        {"freed_bytes": n, "moved": [...], "kept": [文件名...]}
    """
    freed = 0
    moved: List[Dict[str, Any]] = []
    kept: List[str] = []

    for _column, path in record_files(record):
        if not path.exists():
            continue
        name = path.name
        if file_usage(name) > 0:
            kept.append(name)          # 仍被其他记录引用(去重共享): 必须保留
            continue
        if to_trash:
            entry = _move_to_trash(path)
            if entry:
                moved.append(entry)
                freed += int(entry["bytes"])
        else:
            try:
                size = path.stat().st_size
                path.unlink()
                moved.append({"name": name, "bytes": size})
                freed += size
            except OSError:
                pass

    if moved:
        _audit("record_delete", moved)
    return {"freed_bytes": freed, "moved": moved, "kept": kept}


# ---------------------------------------------------------------- 孤儿扫描

def scan_orphans(min_age_sec: int = 0) -> List[Tuple[Path, int]]:
    """扫描受管目录中"无任何记录引用"的文件。

    **判断依据只有引用关系** —— 不再依赖 mtime。这是与旧实现的关键差异:
    旧实现用 mtime + 保留期判断, 导致"刚删记录留下的文件要等一年才清"。

    Args:
        min_age_sec: 仅返回"最后修改时间早于该秒数"的文件, 用于避开正在上传的
                     文件(默认 0 = 全部返回, 供手动清理使用)。
    """
    referenced = referenced_names()
    cutoff = time.time() - min_age_sec if min_age_sec > 0 else None
    out: List[Tuple[Path, int]] = []

    for directory in (settings.upload_dir, settings.result_dir):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.name.startswith(".tmp_"):
                continue
            if path.name in referenced:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if cutoff is not None and st.st_mtime > cutoff:
                continue
            out.append((path, st.st_size))
    return out


def cleanup_orphans(dry_run: bool = False, to_trash: bool = True,
                    min_age_sec: int = 60) -> Dict[str, Any]:
    """回收无人引用的孤儿文件(回收站模式)。

    与保留期解耦: 只要没记录引用, 即可回收, 不受 file_retention_days 约束。
    """
    orphans = scan_orphans(min_age_sec=min_age_sec)
    freed, moved = 0, []

    for path, size in orphans:
        if dry_run:
            freed += size
            moved.append({"name": path.name, "bytes": size})
            continue
        if to_trash:
            entry = _move_to_trash(path)
            if entry:
                moved.append(entry)
                freed += int(entry["bytes"])
        else:
            try:
                path.unlink()
                moved.append({"name": path.name, "bytes": size})
                freed += size
            except OSError:
                pass

    if moved and not dry_run:
        _audit("orphan_cleanup", moved)
        logger.info("孤儿清理: 回收 %d 个文件, %.1f MB", len(moved), freed / 1024 / 1024)

    return {"count": len(moved), "freed_bytes": freed, "files": moved}


# ---------------------------------------------------------------- 回收站管理

def trash_stats() -> Dict[str, Any]:
    """回收站概况。"""
    d = trash_dir()
    total, files = 0, 0
    oldest_days = 0.0
    now = time.time()
    for p in d.iterdir():
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        files += 1
        oldest_days = max(oldest_days, (now - st.st_mtime) / 86400)
    return {"bytes": total, "files": files, "oldest_days": round(oldest_days, 2)}


def purge_trash(older_than_days: int = 7) -> Dict[str, Any]:
    """彻底清除回收站中超过保留窗口的文件。"""
    d = trash_dir()
    cutoff = time.time() - older_than_days * 86400
    freed, removed = 0, []

    for p in d.iterdir():
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue
        try:
            p.unlink()
            freed += st.st_size
            removed.append({"name": p.name, "bytes": st.st_size})
        except OSError:
            pass

    if removed:
        _audit("trash_purge", removed)
        logger.info("回收站清理: 彻底删除 %d 个文件, %.1f MB",
                    len(removed), freed / 1024 / 1024)
    return {"count": len(removed), "freed_bytes": freed, "files": removed}


def restore_from_trash(trash_name: str) -> Optional[Dict[str, Any]]:
    """把回收站文件恢复到原目录(按文件名中的目标目录前缀还原)。

    回收站命名规则: {stamp}_{rand}_{原名}, 原名不带目录信息, 因此恢复到 uploads。
    results 下的文件因命名(带 result_ 前缀)可识别, 做启发式还原。
    """
    src = trash_dir() / Path(trash_name).name
    if not src.exists():
        return None
    original = original_name_of(src.name)
    # 安全: 反解出的原名来自回收站文件名, 不可信, 先净化掉路径分隔符与 ".."。
    original = safe_filename(original)
    dest_dir = settings.result_dir if original.startswith("result_") else settings.upload_dir
    dest = dest_dir / original
    # 安全: 二次防线 —— 确认最终落点位于受管目录之内, 否则拒绝(防 "..\\" 穿越)。
    try:
        if dest_dir.resolve() not in dest.resolve().parents:
            logger.warning("回收站恢复被拒绝(目标越界): %s -> %s", trash_name, dest)
            return None
    except OSError as exc:
        logger.warning("回收站恢复被拒绝(路径解析失败): %s: %s", trash_name, exc)
        return None
    try:
        shutil.move(str(src), str(dest))
        _audit("trash_restore", [{"name": original}])
        return {"restored": original, "path": str(dest)}
    except OSError as exc:
        logger.debug("恢复失败 %s: %s", trash_name, exc)
        return None


# ---------------------------------------------------------------- 存储审计

def storage_audit() -> Dict[str, Any]:
    """完整存储审计: 有效文件 / 孤儿 / 回收站 / 去重收益。"""
    referenced = referenced_names()

    def _scan(directory: Path) -> Dict[str, Any]:
        total = valid_bytes = orphan_bytes = 0
        total_files = valid_files = orphan_files = 0
        if not directory.exists():
            return {"bytes": 0, "files": 0, "valid_bytes": 0, "valid_files": 0,
                    "orphan_bytes": 0, "orphan_files": 0}
        for p in directory.iterdir():
            if not p.is_file() or p.name.startswith(".tmp_"):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            total += size
            total_files += 1
            if p.name in referenced:
                valid_bytes += size
                valid_files += 1
            else:
                orphan_bytes += size
                orphan_files += 1
        return {
            "bytes": total, "files": total_files,
            "valid_bytes": valid_bytes, "valid_files": valid_files,
            "orphan_bytes": orphan_bytes, "orphan_files": orphan_files,
        }

    up = _scan(settings.upload_dir)
    res = _scan(settings.result_dir)
    tr = trash_stats()

    return {
        "uploads": up,
        "results": res,
        "trash": tr,
        "total_bytes": up["bytes"] + res["bytes"] + tr["bytes"],
        "orphan_bytes": up["orphan_bytes"] + res["orphan_bytes"],
        "orphan_files": up["orphan_files"] + res["orphan_files"],
    }


def append_audit(action: str, entries: Iterable[Dict[str, Any]]) -> None:
    """对外暴露的审计写入接口(供删除接口调用)。"""
    _audit(action, entries)


def checkpoint_db() -> None:
    """统一的事务落盘入口。"""
    checkpoint()
