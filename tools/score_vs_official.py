# -*- coding: utf-8 -*-
"""只读：把「我们填进去的答案」与「复习页印着的官方标准答案」逐题比对。

⚠ 只在作业**已提交**后才有意义 —— 提交前页面不会印标准答案。

用法：
  python tools\\score_vs_official.py <我方answers.json或目录> <官方answers.json或目录>
  python tools\\score_vs_official.py            # 自动取 out\\ 下最新的两个产物目录

比对用**选项正文**，不是字母：新版 doWork 页每次加载都会重排屏显字母
（A 常留给页面自己的标准答案），按字母比会得出完全错误的结论。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent


def _answers_file(p: Path) -> Path:
    p = p.expanduser()
    if p.is_dir():
        return p / "answers.json"
    return p


def _recent_runs(n: int = 2):
    out = WS / "out"
    runs = []
    for d in out.iterdir() if out.is_dir() else []:
        f = d / "answers.json"
        if f.is_file():
            runs.append((f.stat().st_mtime, f))
    runs.sort(reverse=True)
    return [f for _, f in runs[:n]]


def load(p: Path):
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for it in data.get("items") or []:
        opts = {}
        for o in it.get("options") or []:
            letter = str(o.get("letter") or "").strip().upper()
            if letter:
                opts[letter] = (o.get("text") or "").strip()
        out[str(it.get("qid"))] = {
            "num": it.get("number"),
            "type": it.get("type"),
            "stem": (it.get("stem") or "")[:40],
            "answer": it.get("answer"),
            "options": opts,
        }
    return out


def as_texts(val, opts) -> set:
    """把答案（字母 / 字母表 / 紧凑 pairs / dict / 正文）翻成正文集合。"""
    out = set()
    if isinstance(val, dict):
        for v in val.values():
            out |= as_texts(v, opts)
        return out
    if isinstance(val, list):
        for v in val:
            out |= as_texts(v, opts)
        return out
    s = str(val or "").strip()
    if not s:
        return out
    if "," in s and ":" in s:                 # "1:C,2:E"
        for part in s.split(","):
            if ":" in part:
                out |= as_texts(part.split(":", 1)[1], opts)
        return out
    if len(s) == 1 and s.upper() in opts:
        out.add(opts[s.upper()])
        return out
    hit = False
    for ch in s:                              # "ABE"
        if ch.upper() in opts:
            out.add(opts[ch.upper()])
            hit = True
    if not hit:
        out.add(s)
    return out


def main(argv) -> int:
    if len(argv) >= 3:
        ours_p, off_p = _answers_file(Path(argv[1])), _answers_file(Path(argv[2]))
    else:
        recent = _recent_runs(2)
        if len(recent) < 2:
            print("out\\ 下不足两个带 answers.json 的产物目录，请显式给两个路径")
            return 2
        # 最新的那个当作官方（复习页），上一个当作我方
        off_p, ours_p = recent[0], recent[1]

    for p in (ours_p, off_p):
        if not p.is_file():
            print(f"找不到：{p}")
            return 2

    ours, off = load(ours_p), load(off_p)
    print(f"我方答案：{ours_p}  共 {len(ours)} 题")
    print(f"官方答案：{off_p}  共 {len(off)} 题")
    common = [k for k in ours if k in off]
    print(f"可比的 qid：{len(common)}\n")
    if not common:
        print("两个文件没有共同的 qid，可能不是同一次作业。")
        return 2

    right, wrong = [], []
    for k in common:
        a = as_texts(ours[k]["answer"], ours[k]["options"])
        b = as_texts(off[k]["answer"], off[k]["options"])
        (right if a == b else wrong).append(k)

    total = len(common)
    pct = 100.0 * len(right) / total
    print(f"一致（按选项正文比）：{len(right)} / {total}  （{pct:.1f}%）")
    print(f"不一致：{len(wrong)} 题")
    if wrong:
        print("\n不一致明细：")
        for k in wrong:
            print(f"  qid={k} #{ours[k]['num']} [{ours[k]['type']}] {ours[k]['stem']}")
            print(f"      我方 = {sorted(as_texts(ours[k]['answer'], ours[k]['options']))}")
            print(f"      官方 = {sorted(as_texts(off[k]['answer'], off[k]['options']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
