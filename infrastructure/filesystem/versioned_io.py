"""配置文件版本管理 —— 服务台编辑（人设/提示词/技能/模版）的快照与回滚。

设计（2026-10-12，测试期回退诉求）：
- **写前快照**：所有走 write_versioned 的写操作，先把现内容存进中央版本库
  （var/config_versions/<key>/，key=项目根内相对路径），不污染 def 目录树——
  persona/skills 目录会被加载器扫描，旁边不能多出陌生子目录。
- **回滚可撤销**：restore 前把当前内容再快照一版（note 标注），任何回退都能再退回来。
- **保留窗口**：每文件保留最近 30 代，超出淘汰最旧。
- 版本 id 即文件名里的时间戳（微秒），字典序=时间序。

边界：只服务管理面（服务台）主动编辑的配置文件；实例运行时写自己的
runtime 数据不走这里。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("digital_life.filesystem.versioned_io")

_KEEP = 30
_VERSIONS_DIR_NAME = "config_versions"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _store_root() -> Path:
    return _project_root() / "var" / _VERSIONS_DIR_NAME


def _key_for(path: Path) -> str:
    """项目根内相对路径 → 版本库目录名（含内容指纹段，防同名误跨）。"""
    try:
        rel = path.resolve().relative_to(_project_root())
        rel_key = str(rel).replace("/", "__")
    except ValueError:  # 项目根之外（测试 tmp 等）：用绝对路径哈希
        rel_key = "abs__" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    return rel_key


def _store_dir(path: Path) -> Path:
    return _store_root() / _key_for(path)


def _stamp() -> str:
    # %f=真微秒；time_ns()%1e6 是"毫秒内纳秒"，毫秒一绕就非单调（同秒排序会乱）
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def snapshot(path: Path, note: str = "") -> Optional[str]:
    """把 path 现内容存一版。文件不存在时是 no-op。返回版本 id 或 None。"""
    path = Path(path)
    if not path.exists():
        return None
    d = _store_dir(path)
    d.mkdir(parents=True, exist_ok=True)
    vid = f"v{_stamp()}"
    shutil.copy2(path, d / f"{vid}.bak")
    _append_meta(d, vid, path, note)
    _prune(d)
    return vid


def write_versioned(path: Path, content: str, note: str = "") -> None:
    """带快照的写入：先存旧版，再落新内容。"""
    path = Path(path)
    snapshot(path, note=f"改前自动快照{('：' + note) if note else ''}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def list_versions(path: Path) -> list[dict]:
    """按时间倒序返回 [{version, ts, size, note}]。"""
    d = _store_dir(Path(path))
    if not d.exists():
        return []
    meta = _read_meta(d)
    out = []
    for f in sorted(d.glob("v*.bak"), reverse=True):
        vid = f.name[:-4]
        ts = vid[1:]
        # 规整成人类可读：20261012-143015-123456 → 2026-10-12 14:30:15
        pretty = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}" if len(ts) >= 15 else ts
        out.append({
            "version": vid,
            "ts": pretty,
            "size": f.stat().st_size,
            "note": meta.get(vid, ""),
        })
    return out


def read_version(path: Path, version: str) -> Optional[str]:
    f = _store_dir(Path(path)) / f"{version}.bak"
    if not f.exists() or not version.startswith("v"):
        return None
    return f.read_text(encoding="utf-8")


def restore_version(path: Path, version: str, note: str = "") -> bool:
    """回滚到指定版本。当前内容先自动快照——回滚本身可撤销。"""
    content = read_version(path, version)
    if content is None:
        return False
    snapshot(Path(path), note=f"回滚到 {version} 前的自动快照{('：' + note) if note else ''}")
    Path(path).write_text(content, encoding="utf-8")
    logger.info("CONFIG_RESTORED path=%s version=%s", path, version)
    return True


def _meta_file(d: Path) -> Path:
    return d / "meta.jsonl"


def _append_meta(d: Path, vid: str, path: Path, note: str) -> None:
    import json

    with _meta_file(d).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"v": vid, "note": note, "src": str(path.name)},
                           ensure_ascii=False) + "\n")


def _read_meta(d: Path) -> dict:
    import json

    meta: dict = {}
    f = _meta_file(d)
    if not f.exists():
        return meta
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                meta[row.get("v", "")] = str(row.get("note") or "")
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return meta
    return meta


def _prune(d: Path) -> None:
    backs = sorted(d.glob("v*.bak"))
    if len(backs) <= _KEEP:
        return
    meta_path = _meta_file(d)
    for f in backs[: len(backs) - _KEEP]:
        try:
            f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    # meta 里失效行不删（幂等读取，缺文件行自然无效），避免重写竞态
