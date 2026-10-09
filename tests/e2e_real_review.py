"""真实复习页（mooc-ans `mooc2/work/view`）离线回归。

夹具 `tests/fixtures/real_work_review.html` 的结构（标签、属性、id、class、答案字母、
界面文案、内联 JS/CSS）取自真实账号的一份**已提交答卷复习页**，含 14 道 A1 型单选 +
1 道 B 型配伍题；但**题干、选项、配伍小题、备选答案等题面文本已全部替换为合成的占位
字符串**（例如 `合成占位085本夹`），以剔除真实试题与标准答案。

> 替换只动文本、不动结构，所以下面这些「结构类」断言仍然是真机回归；
> 而依赖题面文字的断言（`EXPECTED_MATCH_ITEMS`）比对的是合成后的文本。
> 合成文本**每条原文各不相同**——早期的等长填充把两道配伍小题填成同一串，
> 抽取器去重后只剩 2 小题，正是这条断言抓出来的。

它锁住三件事，都在真实页面上踩过坑：

1. 复习页的题目容器是 `div.questionLi`，外面还套着题型分组壳 `div.mark_item`
   （曾经把 `.mark_item` 当题目 → 14 小题被压成 1 道怪题）。
2. B 型配伍题（`#question214133358`）是「外层 questionLi 里再嵌一个内层 questionLi」，
   曾经被 `isNested` 整组丢掉（15 题只剩 14 题）。
3. 页面把标准答案印在 `.rightAnswerContent`（B 型是 `.B_daan.rightAnswerContent`）上，
   `parse_review_answer` 要能 15/15 全部解析出来 —— 这是**零 token** 的答案来源。

不需要网络、不需要登录。用法：python -X utf8 tests\e2e_real_review.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession  # noqa: E402
from xxtutor.config import load_config  # noqa: E402
from xxtutor.page import ChaoxingPage  # noqa: E402
from xxtutor.types import parse_review_answer  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "real_work_review.html"

# 合成夹具里 B 型配伍题的三个小题文本（见 docstring：题面已脱敏，结构未动）。
# 断言写死精确值，是因为曾经出现过「子项被累积/合并」的 bug —— 只查个数会漏。
EXPECTED_MATCH_ITEMS = [
    "合成占位085本夹",
    "合成占位086本夹具为回归测",
    "合成占位087本夹具为回归测",
]

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [OK ] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    if not FIXTURE.exists():
        print(f"缺少夹具：{FIXTURE}")
        return 1
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True

    with BrowserSession(cfg) as sess:
        sess.page.set_content(FIXTURE.read_text(encoding="utf-8"), wait_until="domcontentloaded")
        sess.page.wait_for_timeout(500)
        page = ChaoxingPage(sess, cfg)
        print("\n=== 抓题 ===")
        qs = page.extract_questions()
        counts = sess.page.evaluate(
            "() => ({qui: document.querySelectorAll('div.questionLi').length,"
            " mark_item: document.querySelectorAll('div.mark_item').length,"
            " right: document.querySelectorAll('.rightAnswerContent').length})"
        )

    print(f"  DOM: {counts}")
    check("抓到 15 道题", len(qs) == 15, len(qs))
    check("题目容器数是 15（不是 3 个题型壳）", counts["qui"] == 15, counts["qui"])
    check("页面印了 15 个标准答案", counts["right"] == 15, counts["right"])

    singles = [q for q in qs if q.qtype.value == "single"]
    matches = [q for q in qs if q.qtype.value == "match"]
    check("14 道单选", len(singles) == 14, len(singles))
    check("1 道 B 型配伍题", len(matches) == 1, [q.qid for q in matches])

    if matches:
        m = matches[0]
        check("配伍题 qid = question214133358", m.qid == "question214133358", m.qid)
        check("配伍题 3 个小题", len(m.match_items) == 3, m.match_items)
        check(
            "配伍题小题文字正确（3 个各不相同，未被合并/累积）",
            m.match_items == EXPECTED_MATCH_ITEMS,
            m.match_items,
        )
        check(
            "配伍题小题互不相同",
            len(set(m.match_items)) == len(m.match_items),
            m.match_items,
        )
        check("配伍题 5 个共享备选", len(m.options) == 5, len(m.options))
        check("配伍题页面答案 = (1)D (2)A (3)C", m.review_answer == "(1)D (2)A (3)C", m.review_answer)

    print("\n=== 页面标准答案解析（零 token 通路）===")
    ok = 0
    failed: list[tuple[str, str]] = []
    for q in qs:
        ans = parse_review_answer(q.review_answer, q.qtype, q.options, q.match_items)
        if ans.is_empty:
            failed.append((q.qid, q.review_answer))
        else:
            ok += 1
    check("15 题标准答案全部解析成功", ok == 15, f"{ok}/15 失败={failed}")

    by_qid = {q.qid: q for q in qs}
    first = by_qid.get("question214133344")
    if first:
        ans = parse_review_answer(first.review_answer, first.qtype, first.options, first.match_items)
        check("第 1 题答案 = D", ans.value == "D", (ans.value, first.review_answer))
    if matches:
        m = matches[0]
        ans = parse_review_answer(m.review_answer, m.qtype, m.options, m.match_items)
        check("配伍题答案是 pairs 字典", isinstance(ans.pairs, dict) and ans.pairs == {"1": "D", "2": "A", "3": "C"}, ans.pairs)
        check("配伍题 source = page", ans.source == "page", ans.source)
        check("配伍题 confidence = 1.0", ans.confidence == 1.0, ans.confidence)

    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} / 共 {total}")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print(f"  - {name}")
        print("未全部通过 ✘")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
