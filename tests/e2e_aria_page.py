"""浏览器端到端回归：**新版 ARIA 答题页**（`.answerBg[role=radio]`，无原生 input）。

为什么单独有这个文件
====================
真机上真实翻过两次车，都是在这个页型上，而且**日志全绿、退出码 0**：

1. **一道题都抓不到** —— 选项不是原生 `input`，而是
   `<div class="answerBg" role="radio" onclick="addChoice(this)">`，`input[type=radio]` 数为 0。
   旧 EXTRACT_JS 的三条出口全认原生控件，73 道题整体被丢，报「没抓到任何题目」。
2. **回填自己批改自己的卷子** —— 框架直接往 `#answer<qid>` 写字符串，而页面自己的
   `addChoice`/`addMultipleChoice` 才是权威。复核发现**多选题只剩最后一个字母、
   B 型题隐藏域全空**，而 `fill-report.json` 写着 `filled: 73, failed: 0`。

所以这里用 `tests/fixtures/answer_page_aria.html`（结构照抄真实页面，题面全是合成占位串）
把这两条钉死。夹具里选项字母**故意**和屏显文字不一致：

    屏显 A -> data E,  屏显 B -> data B,  屏显 C -> data D,  屏显 D -> data A

于是「按屏显文字点」的点法必然失败，只有**认 `data` 属性**才可能全对；
而隐藏域只由**页面自己的 handler** 写，框架若自己塞字符串就会被断言抓住。

运行：python tests/e2e_aria_page.py
退出码 0 = 全通过；非 0 = 有失败项。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession          # noqa: E402
from xxtutor.config import Config, load_config      # noqa: E402
from xxtutor.page import ChaoxingPage               # noqa: E402
from xxtutor.types import Answer                    # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "answer_page_aria.html"
PASS: list = []
FAIL: list = []

# 夹具里 data(qid) 都是 9000xx；ARIA 页的容器 id 就是 question9000xx，
# EXTRACT_JS 直接拿容器 id 当 qid（隐藏域则用 answer_key = 纯数字）
Q_SINGLE = "question900001"
Q_MULTI = "question900002"
Q_BTYPE = "question900003"

# 期望：字母一律按 **data 属性** 说，不是按屏显文字
EXPECT_SINGLE = "D"          # 屏显第 3 行（文字「C」）的 data 才是 D
EXPECT_MULTI = ["A", "B", "E"]
EXPECT_BTYPE = {1: "C", 2: "A"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))


def build_cfg(profile: Path) -> Config:
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.browser.user_data_dir = str(profile)
    cfg.answer.min_delay_ms = 0
    cfg.answer.max_delay_ms = 0
    cfg.output.dump_dom = False
    return cfg


def main() -> int:
    profile = ROOT / ".state" / "e2e-aria-profile"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    cfg = build_cfg(profile)
    url = FIXTURE.resolve().as_uri()
    print(f"ARIA 仿真答题页：{url}\n")

    with BrowserSession(cfg) as s:
        page = ChaoxingPage(s, cfg)
        s.goto(url)

        # ---------------- 1) 抓题 ----------------
        print("=== 抓题 ===")
        qs = page.extract_questions()
        check("抓到 3 道题（ARIA 选项盒也算可作答）", len(qs) == 3, f"实际 {len(qs)}")
        qs = [q for q in qs if q.qid in (Q_SINGLE, Q_MULTI, Q_BTYPE)]
        by_id = {q.qid: q for q in qs}
        check("三题 id 都解析到", set(by_id) == {Q_SINGLE, Q_MULTI, Q_BTYPE}, str(sorted(by_id)))

        if Q_SINGLE not in by_id or Q_MULTI not in by_id or Q_BTYPE not in by_id:
            print("\n抓题就没过，后面不用测了")
            print(f"通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
            return 1

        # 题型认 `typename` 属性（页面没有 data-type）
        check(f"#{Q_SINGLE} 题型=single（读 typename）",
              by_id[Q_SINGLE].qtype.value == "single", by_id[Q_SINGLE].qtype.value)
        check(f"#{Q_MULTI} 题型=multiple（读 typename）",
              by_id[Q_MULTI].qtype.value == "multiple", by_id[Q_MULTI].qtype.value)
        check(f"#{Q_BTYPE} 题型=match（读 typename）",
              by_id[Q_BTYPE].qtype.value == "match", by_id[Q_BTYPE].qtype.value)

        # 每题必须被认成「可作答」，否则 cli 会当成复习页跳过回填
        check("三题 has_input 都为真（不会被误判成复习页）",
              all(q.has_input for q in qs), str([(q.qid, q.has_input) for q in qs]))
        check("抽取端上报 has_choice_ui",
              all(int(q.locator.get("has_choice_ui") or 0) > 0 for q in qs),
              str([(q.qid, q.locator.get("has_choice_ui")) for q in qs]))

        # 选项字母必须来自 data 属性
        letters = [o.letter for o in by_id[Q_SINGLE].options]
        check(f"#{Q_SINGLE} 选项字母按 data 解析，= ['E','B','D','A']",
              letters == ["E", "B", "D", "A"], str(letters))
        texts = [o.text for o in by_id[Q_SINGLE].options]
        check(f"#{Q_SINGLE} 选项正文来自 .answer_p",
              texts == ["合成选项文字戊", "合成选项文字乙", "合成选项文字丁", "合成选项文字甲"],
              str(texts))
        check(f"#{Q_SINGLE} 屏显字母与 data 确实错位（夹具自检）",
              letters != ["A", "B", "C", "D"], "夹具的错位设计失效了，断言就没意义")

        ml = [o.letter for o in by_id[Q_MULTI].options]
        check(f"#{Q_MULTI} 五个选项字母 = A-E", ml == ["A", "B", "C", "D", "E"], str(ml))

        check(f"#{Q_BTYPE} 两个小题",
              len(by_id[Q_BTYPE].match_items) == 2, str(by_id[Q_BTYPE].match_items))
        check(f"#{Q_BTYPE} 小题文字干净（没被备选答案文字污染）",
              by_id[Q_BTYPE].match_items == ["合成小题文字一", "合成小题文字二"],
              str(by_id[Q_BTYPE].match_items))
        check(f"#{Q_BTYPE} 备选答案 4 项且字母 A-D",
              by_id[Q_BTYPE].match_options == ["合成备选答案甲", "合成备选答案乙",
                                               "合成备选答案丙", "合成备选答案丁"]
              and [o.letter for o in by_id[Q_BTYPE].options] == ["A", "B", "C", "D"],
              f"opts={by_id[Q_BTYPE].match_options}")

        # answer_key 必须拿到隐藏域用的纯数字（不是 question900001）
        check("answer_key 是隐藏域用的纯数字",
              [by_id[k].answer_key for k in (Q_SINGLE, Q_MULTI, Q_BTYPE)]
              == ["900001", "900002", "900003"],
              str([(k, by_id[k].answer_key) for k in (Q_SINGLE, Q_MULTI, Q_BTYPE)]))

        # ---------------- 2) 回填 ----------------
        print("\n=== 回填 ===")
        answers = [
            Answer(qid=Q_SINGLE, value=EXPECT_SINGLE, confidence=0.9, source="fixture"),
            Answer(qid=Q_MULTI, value=EXPECT_MULTI, confidence=0.9, source="fixture"),
            Answer(qid=Q_BTYPE, value="1:C,2:A", confidence=0.9, source="fixture",
                   pairs={1: "C", 2: "A"}),
        ]
        stats = page.apply_answers(qs, answers)
        check("填答统计 filled=3 / failed=0",
              stats["filled"] == 3 and stats["failed"] == 0,
              json.dumps({k: v for k, v in stats.items() if k != "details"}, ensure_ascii=False))

        # ★ 核心：页面自己的 handler 写出来的隐藏域，必须和期望完全一致
        hid = s.page.evaluate("""() => ({
          single: document.getElementById('answer900001').value,
          multi: document.getElementById('answer900002').value,
          btype: document.getElementById('answer900003').value,
        })""")
        check(f"★ 单选隐藏域 = {EXPECT_SINGLE}",
              hid["single"] == EXPECT_SINGLE, f"实际 {hid['single']!r}（自己写字符串/点了屏显文字都会错）")

        multi = "".join(sorted(c for c in hid["multi"].upper() if c.isalpha()))
        check(f"★ 多选隐藏域字母集 = {''.join(sorted(EXPECT_MULTI))}（**不是只剩一个字母**）",
              multi == "".join(sorted(EXPECT_MULTI)),
              f"实际 {hid['multi']!r} —— 这就是真机上检出的「多选题只剩最后一个字母」回归")

        try:
            bparsed = json.loads(hid["btype"])
        except Exception:
            bparsed = None
        check("★ B 型题隐藏域是 JSON 数组 [{name,content},...]（**不是空的**）",
              isinstance(bparsed, list) and len(bparsed) == 2,
              f"实际 {hid['btype']!r} —— 真机上 4 道 B 型题就是全空")
        if isinstance(bparsed, list):
            got_pairs = {}
            for row in bparsed:
                try:
                    got_pairs[int(row.get("name"))] = str(row.get("content") or "").upper()
                except Exception:
                    pass
            check("★ B 型题两小题内容 = 1:C, 2:A", got_pairs == EXPECT_BTYPE, str(got_pairs))

        # 选中态落在 data 字母上（证明点的是对的选项，不是屏显文字那一个）
        sel = s.page.evaluate("""() => {
          const out = {};
          document.querySelectorAll('.answerBg').forEach(b => {
            if (!b.querySelector('.check_answer, .check_answer_dx')) return;
            const sp = b.querySelector('.num_option, .num_option_dx');
            const qid = b.getAttribute('qid');
            (out[qid] = out[qid] || []).push(sp ? sp.getAttribute('data') : '');
          });
          return out;
        }""")
        check("★ 单选选中态落在 data=D（不是屏显那个 C）",
              sel.get("900001") == ["D"], str(sel.get("900001")))
        check("★ 多选选中态落在 data=A,B,E",
              sorted(sel.get("900002") or []) == ["A", "B", "E"], str(sel.get("900002")))

        # 读回选中态的方法要能和隐藏域对上（夹具就在主页面里，直接用主 frame）
        frame = s.page.main_frame
        back = page._read_selected_letters(page._item_locator(frame, by_id[Q_MULTI]),
                                          by_id[Q_MULTI])
        check("_read_selected_letters 读回多选 = {A,B,E}",
              {str(x).upper() for x in back} >= set(EXPECT_MULTI), str(back))

        # 框架不该自己写隐藏域：详情里 value 是「页面最终值」的校验结果而不是它写入的
        details = {d.get("qid"): d for d in (stats.get("details") or [])}
        check("详情里三题都是 ok",
              all((details.get(k) or {}).get("status") == "ok" for k in (Q_SINGLE, Q_MULTI, Q_BTYPE)),
              json.dumps(details, ensure_ascii=False)[:300])

        # 幂等 / toggle 保护：再填一次同样的答案，隐藏域不能变成空
        stats2 = page.apply_answers(qs, answers)
        hid2 = s.page.evaluate("""() => ({
          single: document.getElementById('answer900001').value,
          multi: document.getElementById('answer900002').value,
        })""")
        check("★ 重复回填不会把已选项 toggle 掉（单选仍是 D）",
              hid2["single"] == EXPECT_SINGLE, f"实际 {hid2['single']!r}")
        check("★ 重复回填不会把已选项 toggle 掉（多选仍是 A,B,E）",
              "".join(sorted(c for c in hid2["multi"].upper() if c.isalpha()))
              == "".join(sorted(EXPECT_MULTI)), f"实际 {hid2['multi']!r}")
        check("第二次填答也没有失败项", stats2["failed"] == 0,
              json.dumps({k: v for k, v in stats2.items() if k != "details"}, ensure_ascii=False))

        # ★ 页面残留选中项必须被取消 —— 真机 Q68/Q69 的第二、三轮就是栽在这里：
        # 只「勾上想要的字母」而不取消多余的，隐藏域会一直多出字母（AC 变成 ABCE），
        # 而「want 里的字母都在隐藏域里」这种包含式判据根本发现不了。
        print("\n=== 残留选中项（真机 Q68/Q69 的坑） ===")
        s.page.evaluate("""() => {
          document.querySelectorAll('.answerBg[qid="900002"]').forEach((b) => {
            const sp = b.querySelector('.num_option, .num_option_dx');
            const d = sp ? (sp.getAttribute('data') || '') : '';
            if (d === 'C' || d === 'D') sp.click();   // 页面自己的 handler（切换语义）
          });
        }""")
        stale = s.page.evaluate("() => document.getElementById('answer900002').value")
        check("夹具能造出「页面残留 C、D」的初始状态",
              sorted(c for c in stale.upper() if c.isalpha()) == list("ABCDE"),
              f"实际 {stale!r}")

        page.apply_answers([by_id[Q_MULTI]], [answers[1]])
        hid3 = s.page.evaluate("""() => {
          const rows = [];
          document.querySelectorAll('.answerBg[qid="900002"]').forEach((b) => {
            if (b.querySelector('.check_answer, .check_answer_dx')) {
              const sp = b.querySelector('.num_option, .num_option_dx');
              rows.push(sp ? sp.getAttribute('data') : '');
            }
          });
          return {value: document.getElementById('answer900002').value,
                  on: rows};
        }""")
        check("★ 回填会把页面残留的多余选中项取消（隐藏域恰好 A,B,E）",
              sorted(c for c in hid3["value"].upper() if c.isalpha())
              == sorted(EXPECT_MULTI),
              f"实际 {hid3['value']!r} —— 只勾不取消就会变成 ABCE（真机 Q68/Q69 的症状）")
        check("★ 取消后选中态也只剩 A,B,E（没有残留高亮）",
              sorted(hid3["on"]) == sorted(EXPECT_MULTI), str(hid3["on"]))

        # ---------------- 3) 暂存 ----------------
        print("\n=== 暂存 ===")
        saved = page.save_progress()
        check("点到「暂时保存」", saved)
        check("页面记录了暂存标志（window.__saved）", bool(s.page.evaluate("() => window.__saved")))
        check("★ 暂存绝不触发提交（window.__submitted 仍为 false）",
              not s.page.evaluate("() => window.__submitted"))
        check("答案在暂存后仍在页面上",
              s.page.evaluate("() => document.getElementById('answer900002').value") != "")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    print(f"耗时 {time.time() - t0:.1f}s")
    raise SystemExit(rc)
