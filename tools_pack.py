"""打一个可以直接发给别人的 zip：源码 + 一键安装脚本，不含任何个人数据。

排除：config.json（里面有真实 API Key）、.state/、out/、__pycache__/、日志。
用法：python -X utf8 tools_pack.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "xuexitong-tutor.zip"
TOP = "xuexitong-tutor"          # 解压后得到的顶层目录名

# 要打进包的文件/目录（相对 ROOT）
INCLUDE = [
    "README.md",
    "LICENSE",
    "requirements.txt",
    "config.example.json",
    "run.bat",
    "setup.bat",
    ".gitignore",
    "selftest.py",
    "xxtutor",
    "tests",
    "tools",
]

# 目录内部的排除规则（按路径片段匹配）
EXCLUDE_PARTS = {".state", "out", "__pycache__", ".git", ".pytest_cache", "dist"}
EXCLUDE_NAMES = {".installed", "config.json", "llm-test.db"}


def collect() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for rel in INCLUDE:
        p = ROOT / rel
        if not p.exists():
            print(f"  [跳过] {rel} 不存在")
            continue
        if p.is_file():
            items.append((p, f"{TOP}/{rel}"))
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            rp = f.relative_to(ROOT)
            if any(part in EXCLUDE_PARTS for part in rp.parts):
                continue
            if f.name in EXCLUDE_NAMES or f.suffix in {".pyc", ".log"} or f.name.endswith(".log"):
                continue
            items.append((f, f"{TOP}/{rp.as_posix()}"))
    return items


def main() -> int:
    items = collect()
    if not items:
        print("没有可打包的文件")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, arc in items:
            z.write(src, arc)
            total += src.stat().st_size

    print(f"\n打好包：{OUT}")
    print(f"文件 {len(items)} 个，原始 {total/1024:.1f} KB，压缩后 {OUT.stat().st_size/1024:.1f} KB\n")
    print("包内容：")
    with zipfile.ZipFile(OUT) as z:
        for n in z.namelist():
            print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
