"""多形态答题页的抽取/回填测试。

与 e2e_offline.py 的区别：那个只测「老版 .TiMu 一种形态」，这里把已知的几种
学习通页面形态都塞进一个夹具，检查 extract_js.py 的兜底链路是否真的兜得住。

    python tests/e2e_variants.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession            # noqa: E402
from xxtutor.config import load_config                # noqa: E402
from xxtutor.page import ChaoxingPage                 # noqa: E402
from xxtutor.types import Answer, QType               # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "answer_page_variants.html"
PROFILE = ROOT / ".state" / "variants-profile"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, name: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"[{'OK' if ok else 'FAIL'}] {name}")


def main() -> int:
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.browser.user_data_dir = str(PROFILE)
    cfg.answer.submit = False

    want = {
        # qid: (qtype, 关键断言用的题干片段)
        "qb1": ("single", "心脏的正常起搏点"),
        "qc1": ("single", "海姆立克"),
        "qc2": ("multiple", "生命体征"),
        "qd1": ("judge", "心搏骤停"),        "q9001": ("match", "配对应疾病"),
        "q9002": ("single", "输血的适应证"),
    }

    with BrowserSession(cfg) as sess:
        page = ChaoxingPage(sess, cfg)
        sess.goto(FIXTURE.as_uri(), wait="domcontentloaded")
        sess.page.wait_for_timeout(400)

        qs = page.extract_questions()
        by_id = {q.qid: q for q in qs}
        print("抓到的题：", [q.qid for q in qs])
        for q in qs:
            print(f"  {q.qid:<8} type={q.qtype.value:<8} opts={len(q.options):<2} "
                  f"match_items={len(q.match_items)} stem={q.stem[:34]!r}")

        check(len(qs) == 6, f"抓到 6 道题（实际 {len(qs)}）")
        for qid, (qtype, frag) in want.items():
            q = by_id.get(qid)
            if q is None:
                check(False, f"{qid} 被抓到")
                continue
            check(q.qtype == QType(qtype), f"{qid} 题型={qtype}（实际 {q.qtype.value}）")
            check(frag in q.stem, f"{qid} 题干含「{frag}」（实际 {q.stem[:40]!r}）")

        # 选项文本（含字母前缀剥离、<a> 里的文本、<span> 文本）
        check([o.text for o in by_id["qb1"].options] == ["窦房结", "房室结", "希氏束"],
              "形态B：label 里的选项文本正确")
        check([o.text for o in by_id["qc1"].options] == ["上腹部", "下腹部", "胸骨下段"],
              "形态C1：td/span 里的选项文本正确")
        check([o.text for o in by_id["qd1"].options] == ["正确", "错误"],
              "形态D：判断题选项文本正确")
        check([o.text for o in by_id["q9002"].options]
              == ["急性大失血", "严重贫血", "凝血因子缺乏", "血容量正常的轻度贫血"],
              "形态F：<a> 里的选项文本正确")

        # 配伍题的小题抽取（形态 E：序号在 <i> 里，不在文本里）
        m = by_id.get("q9001")
        if m is not None:
            check(m.match_items == ["入睡困难", "体温升高"],
                  f"形态E：配伍小题抽到 {m.match_items}")
            check([o.text for o in m.options] == ["失眠", "发热", "低血压"],
                  f"形态E：备选去重后为 {[o.text for o in m.options]}")
        check(by_id["q9002"].qtype != QType.MATCH, "形态F：普通单选没有被误判成配伍")

        # 干扰输入框（隐藏域、搜索框）没有被当成题
        check(all(q.qid not in ("search-box", "q12345") for q in qs), "隐藏域/搜索框未被判为题目")

        # ---- 回填：每种形态都真的点进去 ------------------------------- #
        answers = [
            Answer(qid="qb1", value="A"),
            Answer(qid="qc1", value="C"),
            Answer(qid="qc2", value=["A", "C"]),
            Answer(qid="qd1", value="true"),
            Answer(qid="q9002", value="D"),
            Answer(qid="q9001", value="1:B,2:A", pairs={"1": "B", "2": "A"}),
        ]
        rep = page.apply_answers(qs, answers, dry_run=False)
        print("回填报告：", rep)

        # 读真实 DOM 的选中状态（比读 change 事件可靠：程序化点击未必派发 change）
        picks = sess.page.evaluate(
            "() => { const o = {};"
            " document.querySelectorAll('input:checked').forEach(i => {"
            "   if (i.name) o[i.name] = i.value; }); return o; }")
        print("页面 change 记录：", sess.page.evaluate("() => window.__picks"))
        print("页面选中状态：", picks)
        check(picks.get("qb1") == "A", "回填形态B：qb1 选中 A")
        check(picks.get("qc1") == "C", "回填形态C1：qc1 选中 C")
        check(picks.get("qd1") == "true", "回填形态D：qd1 选中 true")
        check(picks.get("q9002") == "D", "回填形态F：q9002 选中 D")
        check(picks.get("q9001_1") == "B" and picks.get("q9001_2") == "A",
              "回填形态E：配伍两组分别选中 B/A")

        cand_count = sess.page.evaluate(
            "() => document.querySelectorAll('input[name=\"qc2\"]:checked').length")
        check(cand_count == 2, f"形态C2 多选真的选中 2 项（实际 {cand_count}）")

        # ---- 保存不提交 -------------------------------------------------- #
        page.save_progress()
        submitted = sess.page.evaluate("() => !!window.__submitted")
        check(submitted is False, "只保存、未提交")

    ok = sum(1 for r, _ in RESULTS if r)
    total = len(RESULTS)
    print(f"\n通过 {ok} / 共 {total}")
    for r, n in RESULTS:
        if not r:
            print(f"  [FAIL] {n}")
    print("全部通过 ✔" if ok == total else "存在失败项 ✘")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
