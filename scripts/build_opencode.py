#!/usr/bin/env python3
"""Сборка автономного бинарника OpenCode из исходников проекта.

Поддерживаются macOS (arm64/x86_64), Windows (x64) и Linux (x64/arm64).
Требуется Python 3.10+ с PyInstaller, Playwright и mcp.

Использование: python scripts/build_opencode.py emias-mcp
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = {"emias-mcp", "smartmed-mcp"}


def platform_tag() -> str:
    system = sys.platform
    machine = platform.machine().lower()
    if system == "darwin":
        return "darwin-arm64" if machine in {"arm64", "aarch64"} else "darwin-x86_64"
    if system.startswith("win"):
        return "windows-x86_64"
    if system.startswith("linux"):
        return "linux-arm64" if machine in {"arm64", "aarch64"} else "linux-x86_64"
    raise SystemExit(f"Неподдерживаемая платформа: {system}/{machine}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in PROJECTS:
        raise SystemExit("usage: build_opencode.py <emias-mcp|smartmed-mcp>")

    project = sys.argv[1]
    directory = ROOT / project
    entry = directory / "run_server.py"
    manifest = json.loads((directory / "bank_extension" / "manifest.json").read_text("utf-8"))
    name = manifest["name"]
    version = manifest["version"]

    tag = platform_tag()
    if sys.platform == "darwin" and platform.machine().lower() not in {
        "arm64",
        "aarch64",
        "x86_64",
    }:
        raise SystemExit("Нужен нативный Python целевой архитектуры")

    build = directory / "dist" / "opencode" / ".build"
    dist = directory / "dist" / "opencode"
    build.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)

    binary_name = f"{name}-{version}-{tag}"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        binary_name,
        "--paths",
        str(directory),
        "--distpath",
        str(dist),
        "--workpath",
        str(build / "work"),
        "--specpath",
        str(build),
        "--collect-data",
        "playwright",
        "--collect-data",
        "certifi",
        "--exclude-module",
        "mcp.cli",
        str(entry),
    ]
    subprocess.run(args, check=True)

    suffix = ".exe" if sys.platform.startswith("win") else ""
    print(dist / f"{binary_name}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
