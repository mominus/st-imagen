#!/usr/bin/env python3
"""Create a consistent SQLite backup plus an optional uploads archive."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/image_gen.db")
    parser.add_argument("--output-dir", default="data/backups")
    parser.add_argument("--include-uploads", action="store_true")
    args = parser.parse_args()
    source = Path(args.database).resolve()
    if not source.is_file():
        parser.error(f"database does not exist: {source}")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"image_gen_{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(target)
    if args.include_uploads:
        archive = shutil.make_archive(str(output_dir / f"uploads_{stamp}"), "gztar", "data/uploads")
        print(archive)


if __name__ == "__main__":
    main()
