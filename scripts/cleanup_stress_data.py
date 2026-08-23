#!/usr/bin/env python3
"""Clean st-imagen stress-test logs, users, invites, and generated files.

Default rules match the project's bundled stress tools:

- `scripts/stress_concurrent.py` writes prompts containing `(test-xxxxxxxx)`
- `scripts/stress_real_users.py` writes prompts containing `[stress cXXX ...]`
- `scripts/stress_real_users.py` creates users prefixed with `stressu`
- `scripts/stress_real_users.py` creates invite notes prefixed with `stress:`

The script supports a dry run by default. Pass `--apply` to persist changes.
Before applying, it creates a SQLite backup under `data/backups/`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "image_gen.db"
DEFAULT_GENERATED_DIR = PROJECT_ROOT / "data" / "uploads" / "generated"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


@dataclass(frozen=True)
class CleanupConfig:
    db_path: Path
    generated_dir: Path
    backup_dir: Path
    apply: bool
    user_prefix: str
    invite_note_prefix: str


def utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> CleanupConfig:
    parser = argparse.ArgumentParser(description="Remove st-imagen stress-test artifacts.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--generated-dir", default=str(DEFAULT_GENERATED_DIR))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--user-prefix", default="stressu")
    parser.add_argument("--invite-note-prefix", default="stress:")
    parser.add_argument("--apply", action="store_true", help="persist deletion instead of dry-run")
    args = parser.parse_args()
    return CleanupConfig(
        db_path=Path(args.db_path).expanduser().resolve(),
        generated_dir=Path(args.generated_dir).expanduser().resolve(),
        backup_dir=Path(args.backup_dir).expanduser().resolve(),
        apply=bool(args.apply),
        user_prefix=str(args.user_prefix or "").strip(),
        invite_note_prefix=str(args.invite_note_prefix or "").strip(),
    )


def basename_from_url(url: str) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    path = urlparse(raw).path
    name = Path(path).name.strip()
    return name or None


def iter_output_files(output_preview: str | None, output_images: str | None) -> Iterable[str]:
    preview_name = basename_from_url(output_preview or "")
    if preview_name:
        yield preview_name

    raw_images = str(output_images or "").strip()
    if not raw_images:
        return

    try:
        decoded = json.loads(raw_images)
    except json.JSONDecodeError:
        decoded = [raw_images]

    if isinstance(decoded, list):
        for item in decoded:
            name = basename_from_url(str(item or ""))
            if name:
                yield name


def create_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{db_path.stem}_pre_stress_cleanup_{utc_compact()}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def main() -> int:
    cfg = parse_args()
    if not cfg.db_path.exists():
        raise SystemExit(f"database not found: {cfg.db_path}")

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    stress_where = """
        (
            prompt_preview LIKE '%(test-%'
            OR prompt_preview LIKE '%[stress %'
            OR user_id IN (
                SELECT id FROM users WHERE username LIKE ?
            )
        )
    """
    stress_user_like = f"{cfg.user_prefix}%" if cfg.user_prefix else ""
    stress_invite_like = f"{cfg.invite_note_prefix}%" if cfg.invite_note_prefix else ""

    stress_rows = cur.execute(
        f"""
        SELECT id, user_id, output_preview, output_images
        FROM generation_logs
        WHERE {stress_where}
        """,
        (stress_user_like,),
    ).fetchall()
    keep_rows = cur.execute(
        f"""
        SELECT id, output_preview, output_images
        FROM generation_logs
        WHERE NOT {stress_where}
        """,
        (stress_user_like,),
    ).fetchall()

    stress_log_ids = [str(row["id"]) for row in stress_rows]
    keep_files = {
        name
        for row in keep_rows
        for name in iter_output_files(row["output_preview"], row["output_images"])
    }
    stress_files = {
        name
        for row in stress_rows
        for name in iter_output_files(row["output_preview"], row["output_images"])
    }
    if cfg.generated_dir.exists():
        disk_files = {path.name for path in cfg.generated_dir.iterdir() if path.is_file()}
    else:
        disk_files = set()
    files_to_delete = sorted((stress_files - keep_files) & disk_files)

    stress_users = cur.execute(
        "SELECT id, username FROM users WHERE username LIKE ? ORDER BY created_at ASC",
        (stress_user_like,),
    ).fetchall() if stress_user_like else []
    stress_user_ids = [str(row["id"]) for row in stress_users]

    stress_invites = cur.execute(
        "SELECT id, note FROM invite_codes WHERE note LIKE ? ORDER BY created_at ASC",
        (stress_invite_like,),
    ).fetchall() if stress_invite_like else []
    stress_invite_ids = [str(row["id"]) for row in stress_invites]

    session_count = 0
    if stress_user_ids:
        placeholders = ",".join("?" for _ in stress_user_ids)
        session_count = int(
            cur.execute(
                f"SELECT count(*) FROM user_sessions WHERE user_id IN ({placeholders})",
                stress_user_ids,
            ).fetchone()[0]
        )

    summary = {
        "mode": "apply" if cfg.apply else "dry-run",
        "db_path": str(cfg.db_path),
        "generated_dir": str(cfg.generated_dir),
        "stress_log_count": len(stress_log_ids),
        "kept_log_count": len(keep_rows),
        "stress_user_count": len(stress_user_ids),
        "stress_invite_count": len(stress_invite_ids),
        "stress_session_count": session_count,
        "stress_referenced_files": len(stress_files),
        "kept_referenced_files": len(keep_files),
        "disk_generated_files": len(disk_files),
        "files_to_delete": len(files_to_delete),
        "files_to_keep_on_disk": len(disk_files) - len(files_to_delete),
        "backup_path": None,
    }

    if not cfg.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if files_to_delete:
            print("\nFILES_TO_DELETE:")
            for name in files_to_delete[:200]:
                print(name)
        conn.close()
        return 0

    backup_path = create_backup(cfg.db_path, cfg.backup_dir)
    summary["backup_path"] = str(backup_path)

    try:
        conn.execute("BEGIN")
        cur.execute(
            f"DELETE FROM generation_logs WHERE {stress_where}",
            (stress_user_like,),
        )
        if stress_user_ids:
            placeholders = ",".join("?" for _ in stress_user_ids)
            cur.execute(f"DELETE FROM user_sessions WHERE user_id IN ({placeholders})", stress_user_ids)
            cur.execute(f"DELETE FROM users WHERE id IN ({placeholders})", stress_user_ids)
        if stress_invite_ids:
            placeholders = ",".join("?" for _ in stress_invite_ids)
            cur.execute(f"DELETE FROM invite_codes WHERE id IN ({placeholders})", stress_invite_ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    deleted_files = 0
    missing_files = 0
    for name in files_to_delete:
        path = cfg.generated_dir / name
        if not path.exists():
            missing_files += 1
            continue
        path.unlink()
        deleted_files += 1

    summary["deleted_files"] = deleted_files
    summary["missing_files"] = missing_files
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
