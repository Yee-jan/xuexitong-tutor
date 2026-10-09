"""浏览器端到端测试：用本地仿真答题页验证「抓题 -> 回填 -> 暂存」。

为什么需要它：真实学习通要登录账号才能测，而这条链路（EXTRACT_JS +
apply_answers + _click_options/_fill_texts）恰恰是最容易因前端改版挂掉的部分。
这里用 `tests/fixtures/answer_page.html`（照抄学习通老版 /work/doWork 的 DOM 结构）
跑一次真实 Chromium，断言「文本 -> 选项 -> 输入框」全都写对了。

运行：python tests/e2e_offline.py
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
from xxtutor.types import Answer, QType             # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "answer_page.html"
PASS: list = []
FAIL: list = []


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


# 期望答案：与 fixture 里的题一一对应
# 注意 key 用页面里的 data-questionid；EXTRACT_JS 对纯数字 id 会补 `q` 前缀
# （避免和 HTML 里其它数字 id 撞车），所以下面是 `q` + data-questionid。
EXPECTED = {
    "q1001": ("single", "C"),
    "q1002": ("multiple", ["A", "B", "D"]),
    "q1003": ("judge", "A"),
    "q1004": ("fill", ["80", "443"]),
    "q1005": ("short", "客户端发 SYN，服务端回 SYN+ACK，客户端再回 ACK，连接建立。"),
    # 配伍题（B 型题）：value 用 "小题号:字母" 的紧凑写法，pairs 供填答层精确点选
    "q1006": ("match", "1:A,2:B,3:C"),
}


def main() -> int:
    profile = ROOT / ".state" / "e2e-profile"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    cfg = build_cfg(profile)
    url = FIXTURE.resolve().as_uri()
    print(f"仿真答题页：{url}\n")

    with BrowserSession(cfg) as s:
        page = ChaoxingPage(s, cfg)
        s.goto(url)

        # ---------- 1) 抓题 ----------
        print("=== 抓题 ===")
        qs = page.extract_questions()
        check("抓到 6 道题", len(qs) == 6, f"实际 {len(qs)}")
        by_id = {q.qid: q for q in qs}

        check("题目 id 解析正确", set(by_id) == set(EXPECTED), str(sorted(by_id)))
        for qid, (qtype, _ans) in EXPECTED.items():
            q = by_id.get(qid)
            if q is None:
                continue
            check(f"#{qid} 题型={qtype}", q.qtype.value == qtype, f"实际 {q.qtype.value}")

        check("#q1001 题干干净（不含选项文字）",
              by_id.get("q1001") is not None
              and by_id["q1001"].stem == "下列哪个不是 Python 的保留字？",
              repr(by_id.get("q1001").stem) if by_id.get("q1001") else "None")
        check("#q1001 四个选项文字正确",
              by_id.get("q1001") is not None
              and [o.text for o in by_id["q1001"].options] == ["def", "class", "value", "lambda"],
              str([o.text for o in by_id.get("q1001").options]) if by_id.get("q1001") else "")
        check("#q1002 是复选框题（4 个选项）",
              by_id.get("q1002") is not None and len(by_id["q1002"].options) == 4)
        check("#q1003 判断题选项是 正确/错误",
              by_id.get("q1003") is not None
              and [o.text for o in by_id["q1003"].options] == ["正确", "错误"],
              str([o.text for o in by_id.get("q1003").options]) if by_id.get("q1003") else "")
        check("#q1004 识别为两个空的填空",
              by_id.get("q1004") is not None
              and int(by_id["q1004"].locator.get("blanks") or 0) == 2,
              str(by_id.get("q1004").locator) if by_id.get("q1004") else "")
        check("#q1005 识别为简答（有文本输入）",
              by_id.get("q1005") is not None
              and bool(by_id["q1005"].locator.get("has_text_input")))
        check("#q1006 配伍题 3 个小题、备选 4 项",
              by_id.get("q1006") is not None
              and len(by_id["q1006"].match_items) == 3
              and len(by_id["q1006"].match_options) == 4,
              (f"items={by_id.get('q1006').match_items} "
               f"opts={by_id.get('q1006').match_options}") if by_id.get("q1006") else "")
        check("#q1006 小题文字正确（没有被选项文字污染）",
              by_id.get("q1006") is not None
              and by_id["q1006"].match_items == ["入睡困难", "体温升高", "血压下降"],
              str(by_id.get("q1006").match_items) if by_id.get("q1006") else "")
        check("#q1006 重复渲染的备选已去重",
              by_id.get("q1006") is not None
              and by_id["q1006"].match_options == ["失眠", "发热", "低血压", "心悸"],
              str(by_id.get("q1006").match_options) if by_id.get("q1006") else "")
        check("#q1006 备选字母重排为 A-D",
              by_id.get("q1006") is not None
              and [o.letter for o in by_id["q1006"].options] == ["A", "B", "C", "D"],
              str([o.letter for o in by_id.get("q1006").options]) if by_id.get("q1006") else "")

        # ---------- 2) 回填 ----------
        print("\n=== 回填 ===")
        answers = []
        for qid, (_t, val) in EXPECTED.items():
            if qid in by_id:
                if _t == "match":
                    # 配伍题同时给 value（紧凑串）和 pairs（精确配对）
                    pairs = {k: v for k, v in
                             (part.split(":") for part in str(val).split(","))}
                    answers.append(Answer(qid=qid, value=val, confidence=0.9,
                                          source="llm", pairs=pairs))
                else:
                    answers.append(Answer(qid=qid, value=val, confidence=0.9, source="llm"))

        stats = page.apply_answers(qs, answers)
        check("全部填答成功", stats["failed"] == 0 and stats["filled"] == len(EXPECTED),
              json.dumps({k: v for k, v in stats.items() if k != "details"}, ensure_ascii=False))

        # 逐项核对页面真实状态（注意 HTML 里的 data-questionid 不带 q 前缀）
        state = s.page.evaluate("""() => {
          const g = (id) => document.querySelector(`.TiMu[data-questionid="${id}"]`);
          const pick = (id) => Array.from(g(id).querySelectorAll('input[type=radio], input[type=checkbox]'))
                                  .filter(i => i.checked)
                                  .map(i => (i.closest('li').querySelector('a.fl') || {}).innerText || '')
                                  .map(t => t.trim());
          const texts = (id) => Array.from(g(id).querySelectorAll('input[type=text], textarea'))
                                  .map(i => i.value);
          const picks = (id) => {
            const out = {};
            g(id).querySelectorAll('input[type=radio]').forEach(i => {
              if (i.checked) out[i.name] = i.value;
            });
            return out;
          };
          return {
            q1001: pick('1001'), q1002: pick('1002'), q1003: pick('1003'),
            t1004: texts('1004'), t1005: texts('1005'),
            m1006: picks('1006'),
            l1001: Array.from(g('1001').querySelectorAll('li.cur')).length,
            l1002: Array.from(g('1002').querySelectorAll('li.cur')).length,
            l1006: Array.from(g('1006').querySelectorAll('li.cur')).length,
            hook1006: JSON.parse(JSON.stringify(window.__matchPicks || {})),
          };
        }""")
        check("#q1001 选中 C(value)", state["q1001"] == ["value"], str(state["q1001"]))
        check("#q1002 选中 A/B/D", state["q1002"] == ["GET", "POST", "PUT"], str(state["q1002"]))
        check("#q1003 选中 正确", state["q1003"] == ["正确"], str(state["q1003"]))
        check("#q1004 两个空分别写入 80 / 443", state["t1004"] == ["80", "443"], str(state["t1004"]))
        check("#q1005 简答写入 textarea",
              bool(state["t1005"]) and state["t1005"][0].startswith("客户端发 SYN"), str(state["t1005"]))
        check("页面 onchange 事件被触发（选中态落到 li.cur）",
              state["l1001"] == 1 and state["l1002"] == 3,
              f"l1001={state['l1001']} l1002={state['l1002']}")
        check("#q1006 配伍题三个小题分别选中 A/B/C",
              state["m1006"] == {"q1006_1": "A", "q1006_2": "B", "q1006_3": "C"},
              str(state["m1006"]))
        check("#q1006 每个小题只选中一项（共 3 个 li.cur）",
              state["l1006"] == 3, f"l1006={state['l1006']}")
        check("#q1006 页面的 change 钩子收到三组配对",
              {k: v for k, v in (state["hook1006"] or {}).items()
               if k.startswith("q1006_")} == {"q1006_1": "A", "q1006_2": "B", "q1006_3": "C"},
              str(state["hook1006"]))

        # ---------- 3) 暂存 ----------
        print("\n=== 暂存 ===")
        saved = page.save_progress()
        check("点到「暂时保存」", saved)
        check("页面记录了暂存标志", bool(s.page.evaluate("() => window.__saved")))
        check("暂存不会触发提交", not s.page.evaluate("() => window.__submitted"))

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
