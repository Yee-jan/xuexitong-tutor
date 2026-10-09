"""浏览器端到端测试：课程页「我学的课」标签切换的时序坑。

为什么需要它：真站（2026-10）实测 —— 标签元素比它自己的点击处理器**先出现**。
1.5s 时点一下「看起来成功」，页面却毫无反应（`current` 一直留在「我教的课」，
课程卡片一张都不出）；2.6s 再点一次才真的切过去。
`_switch_to_student_courses` 现在会「点完确认生效，没生效就再点」，
这份夹具（`tests/fixtures/course_page_tabs.html`，处理器晚 1.2s 绑定）
把那个行为钉死：只点一次的实现必然在这里失败。

运行：python tests/e2e_course_tabs.py
退出码 0 = 全通过；非 0 = 有失败项。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession          # noqa: E402
from xxtutor.config import Config, load_config      # noqa: E402
from xxtutor.page import ChaoxingPage               # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "course_page_tabs.html"
PASS: list = []
FAIL: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""))


def build_cfg(profile: Path) -> Config:
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.browser.user_data_dir = str(profile)
    cfg.answer.min_delay_ms = 0
    cfg.answer.max_delay_ms = 0
    cfg.output.dump_dom = False
    return cfg


def main() -> int:
    profile = ROOT / ".state" / "e2e-course-profile"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    cfg = build_cfg(profile)
    url = FIXTURE.resolve().as_uri()
    print(f"仿真课程页：{url}\n")

    sess = BrowserSession(cfg).start()
    try:
        sess.goto(url)
        page = ChaoxingPage(sess, cfg)
        frame = sess.page.main_frame
        tab_sel = ".course-tab .tab-item, .tab-item"
        card_sel = "li.course, #courseList li"

        # ---------- 1. 初始状态 ----------
        print("[1] 初始状态（处理器还没绑定）")
        check("初始时「我学的课」没有 current",
              "current" not in (frame.locator(
                  ".tab-item[coursetype='1']").get_attribute("class") or ""))
        check("初始时课程卡片为 0", frame.locator(card_sel).count() == 0,
              f"实际 {frame.locator(card_sel).count()}")
        check("初始时处理器尚未绑定",
              frame.evaluate("() => window.__handlerReady === false"))

        # ---------- 2. 复刻「点太早 = 白点」 ----------
        print("\n[2] 复刻真站的坑：处理器绑定之前点击毫无反应")
        frame.locator(".tab-item[coursetype='1']").click(timeout=4000)
        time.sleep(0.3)
        raw = frame.evaluate("() => window.__rawClicks")
        hits = frame.evaluate("() => window.__clicks")
        check("确实点到了那个标签元素（事件已到元素上）", raw >= 1,
              f"__rawClicks={raw}")
        check("★ 但没有任何处理器接住（__clicks 仍为 0）", hits == 0,
              f"__clicks={hits}")
        check("★ 页面毫无反应：卡片仍然为 0",
              frame.locator(card_sel).count() == 0)
        check("★ 此时 _wait_course_switch 必须判 False（不能自欺欺人）",
              page._wait_course_switch(frame, timeout_s=0.4) is False)

        # ---------- 3. 正确实现：确认生效 + 重试 ----------
        print("\n[3] _switch_to_student_courses：确认生效，没生效就再点")
        t0 = time.time()
        got = page._switch_to_student_courses(frame)
        cost = time.time() - t0
        check("返回命中的标签文字含「我学的课」", "我学的课" in (got or ""),
              f"got={got!r}")
        check("★ 重试后处理器真的接住了点击（__clicks == 1）",
              frame.evaluate("() => window.__clicks") == 1,
              f"__clicks={frame.evaluate('() => window.__clicks')}")
        check("★ current 已挪到「我学的课」",
              "current" in (frame.locator(
                  ".tab-item[coursetype='1']").get_attribute("class") or ""))
        check("★ 课程卡片出现了 3 张", frame.locator(card_sel).count() == 3,
              f"实际 {frame.locator(card_sel).count()}")
        check("★ 重试确实等过第一次失败（耗时 >= 1.0s）", cost >= 1.0,
              f"耗时 {cost:.2f}s")
        check("切换后 _wait_course_switch 立刻为 True",
              page._wait_course_switch(frame, timeout_s=0.5) is True)

        # ---------- 4. 幂等：已经切好就别再点 ----------
        print("\n[4] 幂等：已经是「我学的课」时不再点击")
        before = frame.evaluate("() => window.__clicks")
        again = page._switch_to_student_courses(frame)
        after = frame.evaluate("() => window.__clicks")
        check("再次调用返回「我学的课」", "我学的课" in (again or ""))
        check("没有产生新的点击（__clicks 不变）", after == before,
              f"{before} -> {after}")
        check("卡片仍然是 3 张", frame.locator(card_sel).count() == 3)

        # ---------- 汇总 ----------
        print(f"\n通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
        if FAIL:
            print("\n失败项：")
            for f in FAIL:
                print(f"  - {f}")
            return 1
        print("全部通过 ✔")
        return 0
    finally:
        try:
            sess.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
