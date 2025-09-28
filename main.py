#!/usr/bin/env python3
"""Compat CLI entry that proxies to :mod:`radio_downloader.cli`."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from radio_downloader.cli import main


if __name__ == "__main__":
    main()
