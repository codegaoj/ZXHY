from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_DIR = PROJECT_ROOT / ".versions"
SNAPSHOT_DIR = VERSION_DIR / "snapshots"
BACKUP_DIR = VERSION_DIR / "restore_backups"
INDEX_PATH = VERSION_DIR / "index.json"
IGNORE_PATH = PROJECT_ROOT / ".versionignore"


DEFAULT_IGNORE = [
    "__pycache__/",
    "*.pyc",
    ".versions/",
    "outputs/",
    ".git/",
    ".pytest_cache/",
    "node_modules/",
    "*.log",
]


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_ignore() -> list[str]:
    if not IGNORE_PATH.exists():
        return DEFAULT_IGNORE
    lines = []
    for raw in IGNORE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines or DEFAULT_IGNORE


def normalize_rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def ignored(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for pattern in patterns:
        p = pattern.replace("\\", "/")
        if p.endswith("/"):
            if rel == p.rstrip("/") or rel.startswith(p):
                return True
        elif fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p):
            return True
    return False


def iter_tracked_files() -> list[Path]:
    patterns = load_ignore()
    files = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = normalize_rel(path)
        if ignored(rel, patterns):
            continue
        files.append(path)
    return sorted(files, key=lambda p: normalize_rel(p))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(files: list[Path]) -> dict[str, dict]:
    manifest = {}
    for path in files:
        rel = normalize_rel(path)
        manifest[rel] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    return manifest


def load_index() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def save_index(items: list[dict]) -> None:
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def create_snapshot(message: str) -> dict:
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sid = now_id()
    files = iter_tracked_files()
    manifest = build_manifest(files)
    zip_path = SNAPSHOT_DIR / f"{sid}.zip"
    manifest_path = SNAPSHOT_DIR / f"{sid}.manifest.json"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, normalize_rel(path))

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    item = {
        "id": sid,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
        "files": len(files),
        "zip": normalize_rel(zip_path),
        "manifest": normalize_rel(manifest_path),
    }
    index = load_index()
    index.append(item)
    save_index(index)
    return item


def list_snapshots() -> list[dict]:
    return load_index()


def latest_snapshot() -> dict | None:
    items = load_index()
    return items[-1] if items else None


def find_snapshot(sid: str) -> dict:
    for item in load_index():
        if item["id"].startswith(sid):
            return item
    raise SystemExit(f"找不到版本：{sid}")


def status() -> dict:
    item = latest_snapshot()
    if not item:
        return {"has_snapshot": False}
    manifest_path = PROJECT_ROOT / item["manifest"]
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_manifest = build_manifest(iter_tracked_files())
    added = sorted(set(current_manifest) - set(old_manifest))
    deleted = sorted(set(old_manifest) - set(current_manifest))
    changed = sorted(
        rel for rel in set(current_manifest) & set(old_manifest)
        if current_manifest[rel]["sha256"] != old_manifest[rel]["sha256"]
    )
    return {
        "has_snapshot": True,
        "base": item["id"],
        "added": added,
        "deleted": deleted,
        "changed": changed,
    }


def restore_snapshot(sid: str, yes: bool = False) -> dict:
    item = find_snapshot(sid)
    zip_path = PROJECT_ROOT / item["zip"]
    if not zip_path.exists():
        raise SystemExit(f"快照文件不存在：{zip_path}")
    if not yes:
        raise SystemExit("恢复会覆盖同名文件。请追加 --yes 确认。")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = create_snapshot(f"恢复 {item['id']} 前自动备份")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(PROJECT_ROOT)
    return {"restored": item, "backup": backup}


def main() -> None:
    parser = argparse.ArgumentParser(description="交易系统轻量版本管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snapshot = sub.add_parser("snapshot", help="创建版本快照")
    p_snapshot.add_argument("-m", "--message", default="手动快照")

    sub.add_parser("list", help="查看版本历史")
    sub.add_parser("status", help="查看相对最新版本的改动")

    p_restore = sub.add_parser("restore", help="恢复指定版本")
    p_restore.add_argument("id")
    p_restore.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    if args.cmd == "snapshot":
        item = create_snapshot(args.message)
        print(f"已创建版本 {item['id']}：{item['message']}，文件数 {item['files']}")
    elif args.cmd == "list":
        items = list_snapshots()
        if not items:
            print("暂无版本快照")
        for item in items:
            print(f"{item['id']}  {item['time']}  {item['message']}  文件数:{item['files']}")
    elif args.cmd == "status":
        data = status()
        if not data["has_snapshot"]:
            print("暂无版本快照")
            return
        print(f"对比基线版本：{data['base']}")
        for label, key in [("新增", "added"), ("修改", "changed"), ("删除", "deleted")]:
            values = data[key]
            print(f"{label}: {len(values)}")
            for rel in values[:50]:
                print(f"  {rel}")
            if len(values) > 50:
                print(f"  ... 还有 {len(values) - 50} 项")
    elif args.cmd == "restore":
        result = restore_snapshot(args.id, args.yes)
        print(f"已恢复版本：{result['restored']['id']}")
        print(f"恢复前自动备份：{result['backup']['id']}")


if __name__ == "__main__":
    main()
