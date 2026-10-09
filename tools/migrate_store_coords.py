# -*- coding: utf-8 -*-
"""一次性迁移：把库里「旧屏显字母坐标」的答案升级成「选项正文」坐标。

背景见 `xxtutor/answer_codec.py`：真实 doWork 页每次加载都会打乱屏显字母，
旧库存的是字母，于是从第二轮起「缓存命中 100%」却把 51/73 题填到了别的选项上。

迁移是**无损可逆**的：库内每行都带 `options`（旧字母 -> 正文），所以
「旧字母 --(该行自己的 options)--> 正文」不需要任何外部信息，也不需要猜。

用法：
    python .state\_migrate_store_coords.py            # dry-run，只打印
    python .state\_migrate_store_coords.py --apply    # 先备份 answers.db 再写
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / ".state" / "answers.db"


def table_of(stored) -> dict:
    """把库里的 options 归一成 {字母: 正文}。"""
    out: dict[str, str] = {}
    for o in stored or []:
        if isinstance(o, dict):
            L = str(o.get("letter") or "").strip().upper()
            if L:
                out[L] = str(o.get("text") or "")
        elif isinstance(o, (list, tuple)) and len(o) == 2:
            out[str(o[0]).strip().upper()] = str(o[1])
    return out


def convert(value, table: dict):
    """老字母值 -> 正文值。翻译不了返回 None（调用方删行）。"""
    def one(v):
        L = str(v or "").strip().upper().rstrip(".．、。")
        return table.get(L)

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            t = one(v)
            if t is None:
                return None
            out[str(k).strip()] = t
        return out
    if isinstance(value, list):
        out_l = []
        for v in value:
            t = one(v)
            if t is None:
                return None
            out_l.append(t)
        return out_l
    if isinstance(value, str):
        s = value.strip()
        # 已经是正文（不是单字母）就原样保留 —— 幂等
        if not (len(s) == 1 and s.isalpha()):
            return value
        t = table.get(s.upper())
        return t
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid, fp, qtype, answer, options, stem FROM questions"
    ).fetchall()
    print(f"库里 questions 表共 {len(rows)} 行")

    updates, deletes, untouched, already = [], [], 0, 0
    for row in rows:
        if not (row["stem"] or "").strip():
            # 纯「记忆」条目（没有题面/选项），无从定位，保持原样
            untouched += 1
            continue
        try:
            stored = json.loads(row["options"] or "[]")
        except Exception:
            stored = []
        table = table_of(stored)
        if not table:
            untouched += 1
            continue
        try:
            value = json.loads(row["answer"])
        except (json.JSONDecodeError, TypeError):
            value = row["answer"]

        new_value = convert(value, table)
        if new_value is None:
            deletes.append(row["rowid"])
            print(f"  [删] fp={row['fp'][:12]} 无法翻译 {row['answer']!r}（{row['qtype']}）")
            continue
        if new_value == value:
            already += 1
            continue
        updates.append((json.dumps(new_value, ensure_ascii=False), row["rowid"]))
        print(f"  [改] fp={row['fp'][:12]} {row['answer']!r} -> "
              f"{json.dumps(new_value, ensure_ascii=False)!r}")

    print(f"\n汇总：改 {len(updates)} 行 / 删 {len(deletes)} 行 / "
          f"已是正文 {already} 行 / 无从定位保持原样 {untouched} 行")
    if args.apply:
        bak = DB.with_name(f"answers.db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(DB, bak)
        print(f"已备份 -> {bak}")
        conn.executemany("UPDATE questions SET answer=? WHERE rowid=?", updates)
        if deletes:
            conn.executemany("DELETE FROM questions WHERE rowid=?",
                             [(r,) for r in deletes])
        conn.commit()
        print("已写库")
    else:
        print("（dry-run，未写库；加 --apply 才落盘）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
