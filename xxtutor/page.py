"""学习通页面适配层：登录、进课程、开作业、抓题、填答、提交。

这里的每个方法都尽量容错：找不到元素就抛带上下文的异常，并附带当前 URL，
方便你在选择器失效时快速定位（配合 `python -m xxtutor dump-dom`）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from . import selectors as S
from .browser import BrowserSession
from .extract_js import DEBUG_PAGE_JS, EXTRACT_JS, FILL_EDITOR_JS, FILL_TEXT_JS
from .types import (Answer, AnswerValue, Option, QType, Question,
                    match_items_by_text, match_display, normalize_text,
                    parse_letters, parse_pairs)

log = logging.getLogger("xxtutor.page")

# 单个选项字母（A–H）：只有长这样才采信抽取端给的 letter，
# 否则（空串、"选项"、页面渲染残留）按位置编号兜底。
_LETTER_OK = re.compile(r"^[A-H]$")

# --------------------------------------------------------------------------- #
# ARIA 组件式答题页的填答脚本（新版 mooc-ans /work/doWork）
# --------------------------------------------------------------------------- #
# 这一版页面**没有原生 form 控件**：选项是 <div class="answerBg" role="radio">，
# 点它由页面自己的 onclick="addChoice(this)" / "addMultipleChoice(this)" 处理，
# 页面据选项字母 span 的 **data 属性**（不是屏显文字，页面对多选会打乱屏显顺序）
# 拼出答案写进隐藏域 <input id="answer<qid>">，同时置 isContentChanged=true
# 并调 loadAnswerSheet()。保存时服务端读的就是这个隐藏域。
#
# ⇒ 回填**必须借页面自己的处理函数**，绝不能自己往隐藏域里塞字符串：
#    自己写出来的值页面没有登记（isContentChanged/答题卡都没同步），
#    保存时会被丢掉；而且 B 型题的隐藏域格式是 JSON 数组，不是字母串。
CLICK_CHOICE_JS = r"""
(el, args) => {
  // Locator.evaluate 会把元素本身当第一个参数传进来，args 才是第二个参数；
  // 兼容两种调用形态，避免 "Cannot read properties of undefined"。
  const root = (el && el.nodeType === 1) ? el : args.root;
  const want = (args.want || []).map((s) => String(s).toUpperCase());
  const qid = args.qid || '';
  const multiple = !!args.multiple;
  // force：即使已经选中也要点一次（单选页面多一个残留选中项时靠它复位）。
  const force = !!args.force;
  const boxes = Array.from(root.querySelectorAll(
    '.answerBg, [role=radio], [role=checkbox]'));
  const letterOf = (box) => {
    for (const sp of box.querySelectorAll(
        '.num_option, .num_option_dx, .choice-letter')) {
      const d = (sp.getAttribute('data') || '').trim();
      if (/^[A-Ha-h]$/.test(d)) return d.toUpperCase();
      const t = (sp.innerText || '').trim();
      if (/^[A-Ha-h]$/.test(t)) return t.toUpperCase();
    }
    const d = (box.getAttribute('data') || '').trim();
    return /^[A-Ha-h]$/.test(d) ? d.toUpperCase() : '';
  };
  const isOn = (box) => !!box.querySelector('.check_answer, .check_answer_dx');
  const pagePick = (box) => {
    const oc = box.getAttribute('onclick') || '';
    try {
      if (oc.indexOf('addMultipleChoice') >= 0 &&
          typeof addMultipleChoice === 'function') {
        addMultipleChoice(box);
        return 'addMultipleChoice';
      }
      if (oc.indexOf('addChoice') >= 0 && typeof addChoice === 'function') {
        addChoice(box);
        return 'addChoice';
      }
    } catch (e) { /* 落到下面的原生点击 */ }
    try { box.click(); } catch (e) { /* 忽略 */ }
    try { box.dispatchEvent(new MouseEvent('click',
      {bubbles: true, cancelable: true})); } catch (e) { /* 忽略 */ }
    return 'native';
  };
  // 已选中的字母绝不重点：页面的处理函数是「切换」语义，再点一次会取消。
  const picked = [];
  const skipped = [];
  const how = [];
  const dropped = [];
  for (const box of boxes) {
    const letter = letterOf(box);
    if (!letter || want.indexOf(letter) < 0) continue;
    if (isOn(box) && !force) { skipped.push(letter); continue; }
    how.push([letter, pagePick(box)]);
    picked.push(letter);
  }
  // 反选多余项：页面上被别人（上一次运行 / 手动暂存）留下的选中项不属于
  // 本次答案时必须取消，否则隐藏域里会一直多出字母，而 `want.every(...)`
  // 这种「包含式」判据根本发现不了（真机 Q68/Q69 就是这样存了 ABCE）。
  const hid0 = qid ? (root.querySelector('#answer' + qid)
    || document.getElementById('answer' + qid)) : null;
  const before = hid0 ? String(hid0.value || '') : '';
  if (before) {
    for (const box of boxes) {
      const letter = letterOf(box);
      if (!letter || want.indexOf(letter) >= 0) continue;
      if (!isOn(box)) continue;
      how.push([letter, 'drop:' + pagePick(box)]);
      dropped.push(letter);
    }
  }
  const hid = qid ? (root.querySelector('#answer' + qid)
    || document.getElementById('answer' + qid)) : null;
  const value = hid ? String(hid.value || '') : '';
  const up = value.toUpperCase();
  const hidSel = boxes.filter(isOn).map(letterOf).filter(Boolean);
  // 「隐藏域里的字母集合」必须 **等于** 本次想要的集合：多一个少一个都不算成功。
  const hidLetters = (up.match(/[A-H]/g) || []).filter(
    (v, i, arr) => arr.indexOf(v) === i);
  const sameSet = hidLetters.length === want.length
    && want.every((l) => hidLetters.indexOf(l) >= 0);
  // 注意：wrote 恒为 false —— 框架不再自己写隐藏域，只报「页面写了什么」。
  return {
    found: picked,
    skipped: skipped,
    dropped: dropped,
    how: how,
    hidden_ok: sameSet,
    hidden_value: value,
    hidden_letters: hidLetters,
    selected: hidSel.join(','),
    wrote: false,
  };
}
"""

SYNC_CHOICE_JS = r"""
(el, args) => {
  const root = (el && el.nodeType === 1) ? el : args.root;
  const want = (args.want || []).map((s) => String(s).toUpperCase())
    .filter((v, i, arr) => arr.indexOf(v) === i);
  const qid = args.qid || '';
  const boxes = Array.from(root.querySelectorAll(
    '.answerBg, [role=radio], [role=checkbox]'));
  const letterOf = (box) => {
    for (const sp of box.querySelectorAll(
        '.num_option, .num_option_dx, .choice-letter')) {
      const d = (sp.getAttribute('data') || '').trim();
      if (/^[A-Ha-h]$/.test(d)) return d.toUpperCase();
      const t = (sp.innerText || '').trim();
      if (/^[A-Ha-h]$/.test(t)) return t.toUpperCase();
    }
    const d = (box.getAttribute('data') || '').trim();
    return /^[A-Ha-h]$/.test(d) ? d.toUpperCase() : '';
  };
  const isOn = (box) => !!box.querySelector('.check_answer, .check_answer_dx');
  const pagePick = (box) => {
    const oc = box.getAttribute('onclick') || '';
    try {
      if (oc.indexOf('addMultipleChoice') >= 0 &&
          typeof addMultipleChoice === 'function') {
        addMultipleChoice(box);
        return 'addMultipleChoice';
      }
      if (oc.indexOf('addChoice') >= 0 && typeof addChoice === 'function') {
        addChoice(box);
        return 'addChoice';
      }
    } catch (e) { /* 落到下面的原生点击 */ }
    try { box.click(); } catch (e) { /* 忽略 */ }
    try { box.dispatchEvent(new MouseEvent('click',
      {bubbles: true, cancelable: true})); } catch (e) { /* 忽略 */ }
    return 'native';
  };
  const how = [];
  // 第一遍：勾上想要的（已选中就跳过，页面的处理函数是「切换」语义）
  for (const box of boxes) {
    const letter = letterOf(box);
    if (!letter || want.indexOf(letter) < 0 || isOn(box)) continue;
    how.push([letter, pagePick(box)]);
  }
  // 第二遍：取消页面残留的多余选中项（上次运行 / 手动暂存留下的）
  for (const box of boxes) {
    const letter = letterOf(box);
    if (!letter || want.indexOf(letter) >= 0 || !isOn(box)) continue;
    how.push([letter, 'drop:' + pagePick(box)]);
  }
  const hid = qid ? (root.querySelector('#answer' + qid)
    || document.getElementById('answer' + qid)) : null;
  const value = hid ? String(hid.value || '') : '';
  const hidLetters = ((value || '').toUpperCase().match(/[A-H]/g) || [])
    .filter((v, i, arr) => arr.indexOf(v) === i);
  const sameSet = hidLetters.length === want.length
    && want.every((l) => hidLetters.indexOf(l) >= 0);
  return {how: how, hidden_ok: sameSet, hidden_value: value,
          hidden_letters: hidLetters, wrote: false};
}
"""

CLICK_BTYPE_JS = r"""
(el, args) => {
  const root = (el && el.nodeType === 1) ? el : args.root;
  const plan = args.plan || [];
  const qid = args.qid || '';
  const cts = Array.from(root.querySelectorAll('.B-answer-ct'));
  if (!cts.length) return {ok: false, reason: 'no B-answer-ct'};
  const done = [];
  const how = [];
  for (const p of plan) {
    const ct = cts[p.k];
    if (!ct) continue;
    const want = String(p.letter).toUpperCase();
    const pool = Array.from(ct.querySelectorAll('.B-answerCon span[data]'));
    for (const sp of pool) {
      const d = (sp.getAttribute('data') || '').trim().toUpperCase();
      if (d !== want) continue;
      if ((sp.className || '').indexOf('check_answer') >= 0) {
        done.push(want);
        how.push([p.k, want, 'already']);
        break;
      }
      // B 型题的点击绑的是 jQuery 事件（span 上没有 onclick 属性），
      // 原生 click() 也能冒泡上去，两条路都试。
      let used = 'native';
      try {
        if (window.jQuery) { window.jQuery(sp).trigger('click'); used = 'jq'; }
        else { sp.click(); }
      } catch (e) { try { sp.click(); } catch (e2) { /* 忽略 */ } }
      if ((sp.className || '').indexOf('check_answer') < 0) {
        try { sp.click(); } catch (e) { /* 忽略 */ }
        used = 'click';
      }
      if ((sp.className || '').indexOf('check_answer') >= 0) {
        done.push(want);
        how.push([p.k, want, used]);
      }
      break;
    }
  }
  // B 型题隐藏域是 JSON 数组：[{"name":1,"content":"E"}, ...]
  const hid = qid ? (root.querySelector('#answer' + qid)
    || document.getElementById('answer' + qid)) : null;
  const value = hid ? String(hid.value || '') : '';
  let parsed = null;
  try { parsed = JSON.parse(value); } catch (e) { parsed = null; }
  return {
    ok: done.length === plan.length,
    done: done,
    how: how,
    hidden_value: value,
    parsed_ok: Array.isArray(parsed) &&
      plan.every((p) => {
        const hit = parsed.find((x) => Number(x.name) === p.k + 1);
        return !!hit && String(hit.content || '').toUpperCase() ===
          String(p.letter).toUpperCase();
      }),
    wrote: false,
  };
}
"""


class PageError(RuntimeError):
    """页面适配失败（选择器失效 / 页面结构变化 / 需要人工介入）。"""


@dataclass
class Course:
    name: str
    url: str = ""
    index: int = 0
    params: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "index": self.index, "params": self.params}


@dataclass
class HomeworkItem:
    title: str
    status: str = ""
    url: str = ""
    index: int = 0

    @property
    def done(self) -> bool:
        return any(k in (self.status or "") for k in ("已交", "已完成", "已提交", "已批阅"))

    def to_dict(self) -> dict:
        return {"title": self.title, "status": self.status, "url": self.url,
                "index": self.index, "done": self.done}


# --------------------------------------------------------------------------- #

class ChaoxingPage:
    """封装好的学习通操作对象。"""

    def __init__(self, session: BrowserSession, cfg: Any) -> None:
        self.s = session
        self.cfg = cfg
        self.course_url = ""

    # ================================================================== #
    # 1) 登录
    # ================================================================== #
    def is_logged_in(self, timeout_s: float = 6.0) -> bool:
        page = self.s.page
        # 快路：当前 URL 就已经是登录后的空间（i.chaoxing.com / /space/ 等），
        # 直接判已登录。以前这一步非要先跳首页再找 DOM 特征，慢且容易误判。
        try:
            if any(h in page.url for h in S.LOGGED_IN_URL_HINTS):
                log.info("检测到已登录（URL 命中 %s）", page.url[:80])
                return True
        except Exception:
            pass
        for url in (self.cfg.login.home_url,):
            try:
                if "chaoxing.com" not in page.url:
                    self.s.goto(url)
            except Exception as e:
                log.debug("打开首页失败: %s", e)
        hit = self.s.wait_any(S.LOGIN_LOGGED_IN + [".Mycourse", "li.course"], timeout_s=timeout_s)
        if hit:
            log.info("检测到已登录（命中 %s）", hit)
            return True
        # URL 兜底：不在登录页且能拿到课程卡片，也算已登录
        if "passport2.chaoxing.com" not in page.url and "login" not in page.url.lower():
            if page.locator("li.course, .courseCard, .course-list li").count() > 0:
                log.info("检测到已登录（课程卡片存在）")
                return True
            # 连课程卡片都没有，但 URL 已是登录后空间：也算登录（页面可能还没渲染完）
            try:
                if any(h in page.url for h in S.LOGGED_IN_URL_HINTS):
                    log.info("检测到已登录（URL 在登录后空间，忽略空 DOM）")
                    return True
            except Exception:
                pass
        return False

    def ensure_login(self, interactive: bool = True) -> bool:
        """确保处于登录态。返回 True 表示本次是新登录的。

        速度要点（踩过的坑）：登录态明明还在，`is_logged_in()` 也可能因为
        页面渲染慢而判 False；旧实现在这种情况下会一路等到
        manual_login_timeout_s（默认 300s）才报错，一次白等 5 分钟。
        现在：先复查一次 → 真要等也只等到「短时间内没跳转」就认输。
        """
        if self.is_logged_in():
            return False
        # 复查：换个更松的判据（URL 在登录后空间 / 能拿到任意登录特征）
        if self._looks_logged_in_now():
            log.info("检测到已登录（二次判据）")
            return False

        log.info("未登录，打开登录页：%s", self.cfg.login.login_url)
        self.s.goto(self.cfg.login.login_url, wait="domcontentloaded")

        if self.cfg.login.auto_fill_credentials and self._auto_fill():
            log.info("已用环境变量里的账号密码提交登录")

        if interactive and not self.cfg.browser.headless:
            print("\n" + "=" * 68)
            print("  请在弹出的浏览器窗口里完成登录（扫码 / 手机号+密码均可）")
            print(f"  登录成功后框架会自动继续，最长等待 {self.cfg.login.manual_login_timeout_s} 秒")
            print("=" * 68 + "\n")
        deadline = time.time() + max(30, int(self.cfg.login.manual_login_timeout_s))
        started = time.time()
        login_page_since: Optional[float] = None
        while time.time() < deadline:
            if self._looks_logged_in_now():
                log.info("登录成功，登录态已写入 %s", self.cfg.user_data_dir)
                return True
            # 快失败：还停在登录页且 25 秒内没有任何跳转，基本就是登录态失效，
            # 没必要把 300 秒耗完（有头模式下用户可能正在手动登录，所以给足 25s）。
            try:
                on_login_page = ("passport2.chaoxing.com" in self.s.page.url
                                 or "/login" in self.s.page.url.lower())
            except Exception:
                on_login_page = False
            if on_login_page:
                login_page_since = login_page_since or time.time()
                if time.time() - login_page_since > 25 and time.time() - started > 25:
                    raise PageError(
                        "登录页上等了 25 秒没有任何跳转，登录态应该是失效了。"
                        "请先跑一次 `python -m xxtutor login` 完成登录。"
                        f"（想硬等可以调大 login.manual_login_timeout_s，"
                        f"当前 {self.cfg.login.manual_login_timeout_s}s）")
            else:
                login_page_since = None
            time.sleep(0.5)
        raise PageError(
            f"等待登录超时（{self.cfg.login.manual_login_timeout_s}s）。"
            "可以调大 login.manual_login_timeout_s，或先用 `login` 命令单独登录一次。"
        )

    def _looks_logged_in_now(self) -> bool:
        page = self.s.page
        try:
            url = page.url
        except Exception:
            return False
        if "passport2.chaoxing.com" in url or "/login" in url.lower():
            # 登录页也可能已经跳转，别急着返回 False
            return False
        for sel in S.LOGIN_LOGGED_IN:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        # URL 兜底：登录后会落到 i.chaoxing.com / i.mooc.chaoxing.com / /space/ 等
        if any(h in url for h in S.LOGGED_IN_URL_HINTS):
            return True
        return "chaoxing.com" in url and ("/mycourse" in url or "/space" in url)

    def _auto_fill(self) -> bool:
        import os
        user = os.environ.get("XX_USERNAME", "")
        pwd = os.environ.get("XX_PASSWORD", "")
        if not user or not pwd:
            return False
        # 切到账号密码登录 tab
        self.s.click_first(S.LOGIN_USERNAME_INPUT and S.LOGIN_PHONE_TAB or S.LOGIN_PHONE_TAB,
                           timeout_ms=2000)
        page = self.s.page
        for sels, val in ((S.LOGIN_USERNAME_INPUT, user), (S.LOGIN_PASSWORD_INPUT, pwd)):
            for sel in sels:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.fill(val, timeout=4000)
                        break
                except Exception:
                    continue
        ok = self.s.click_first(S.LOGIN_SUBMIT, timeout_ms=4000)
        page.wait_for_timeout(2500)
        return ok

    # ================================================================== #
    # 2) 课程
    # ================================================================== #
    #: 个人空间里承载课程列表的 iframe 地址片段
    _COURSE_FRAME_HINT = "visit/interaction"

    def _course_scope(self, timeout_s: float = 20.0) -> Any:
        """返回「课程列表所在的执行上下文」——可能是 iframe，也可能是主页面。

        实测：个人空间把课程列表放在 `#frame_content` 这个 iframe 里
        （src 形如 `https://mooc1-1.chaoxing.com/visit/interaction?s=<sid>`），
        而且默认停在「我教的课」。学生必须点「我学的课」才会出课程。
        """
        # 交给 _frame_scope：找 frame 只看缓存的 URL，不用 500ms 空转。
        return self._frame_scope(self._COURSE_FRAME_HINT, timeout_s=timeout_s)

    def _wait_course_switch(self, scope: Any, timeout_s: float = 2.5) -> bool:
        """确认「我学的课」这次点击**真的生效了**。

        判据（任一成立即算成功）：
        - 「我学的课」标签拿到 `current` 类（页面自己的处理器切换时加的）
        - 课程卡片已经出现（`li.course / #courseList li`）

        用 `wait_for_function` 一次事件驱动地等，不做 Python 侧轮询。
        """
        js = """(sel) => {
          const tabs = document.querySelectorAll(sel);
          for (const e of tabs) {
            const txt = (e.innerText || '').trim();
            const ty = e.getAttribute('coursetype');
            if ((txt.indexOf('我学的课') >= 0 || ty === '1')
                && (e.className || '').indexOf('current') >= 0) return true;
          }
          return document.querySelectorAll('li.course, #courseList li').length > 0;
        }"""
        try:
            scope.wait_for_function(js, arg=".course-tab .tab-item, .tab-item",
                                    timeout=max(300, int(timeout_s * 1000)))
            return True
        except Exception:
            return False

    def _switch_to_student_courses(self, scope: Any) -> str:
        """切到「我学的课」。返回实际命中的标签文字（空串表示没找到）。"""
        try:
            tabs = scope.locator(".course-tab .tab-item, .tab-item")
            n = tabs.count()
        except Exception:
            return ""
        for i in range(n):
            try:
                el = tabs.nth(i)
                text = (el.inner_text() or "").strip()
                cls = el.get_attribute("class") or ""
            except Exception:
                continue
            if "我学的课" in text or el.get_attribute("coursetype") == "1":
                if "current" in cls:
                    return text or "我学的课"
                # ⚠ 实测（2026-10）：iframe 里的标签元素常常比它自己的点击处理器
                # **先出现**。1.5s 时点一下「看起来成功」，但页面毫无反应 ——
                # current 一直留在「我教的课」上，课程卡片一张都不出；
                # 2.6s 再点一次才真的切过去（卡片 13 张）。
                # 所以点完必须**确认生效**，没生效就再点，不能只信 click() 不报错。
                for attempt in range(1, 4):
                    try:
                        el.click(timeout=8000)
                    except Exception as e:  # noqa: BLE001
                        log.warning("点击「我学的课」失败: %s", e)
                        return ""
                    if self._wait_course_switch(scope, timeout_s=2.5):
                        if attempt == 1:
                            log.info("已切换到「%s」", text or "我学的课")
                        else:
                            log.info("已切换到「%s」（第 %d 次点击才生效）",
                                     text or "我学的课", attempt)
                        return text or "我学的课"
                    log.info("点了「我学的课」但页面没反应（处理器还没绑定），再点一次…")
                return text or "我学的课"
        return ""

    #: 真正是「课程页」的 URL 特征。`list_courses` 用它判断要不要先回首页。
    _COURSE_PAGE_URL_HINTS = ("i.chaoxing.com", "visit/interaction",
                              "mycourse", "course/list", "space/index")

    def list_courses(self, max_courses: int = 200) -> List[Course]:
        log.info("读取课程列表…")
        try:
            url = self.s.page.url or ""
        except Exception:
            url = ""
        # ⚠ 曾经这里只判 `"chaoxing.com" not in url`，太宽松：答题页和
        # 「作答状态异常！」提示页都在 chaoxing.com 域下，于是会跳过「回首页」，
        # 然后在一张死页面上找课程卡片 —— 必然报「课程列表为空」。
        # `--reopen` 打不开旧 URL 时会退回到这条路上，所以必须判准。
        if not any(k in url for k in self._COURSE_PAGE_URL_HINTS):
            log.info("当前不在课程页（%s），先回首页", url[:80])
            self.s.goto(self.cfg.login.home_url)
            try:
                self.s.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

        card_sel = "li.course, #courseList li"
        any_sel = card_sel + ", .course-tab .tab-item, .tab-item, #courseList > li"
        # ⚠ 别在主文档上等课程卡片：实测课程列表在 `visit/interaction` 这个
        # iframe 里，主文档等它必然白烧满 6 秒。直接按 frame 事件驱动等
        # 「标签栏或课程卡片」任一出现。
        scope = self._frame_scope(self._COURSE_FRAME_HINT, timeout_s=6.0,
                                  must_match=any_sel)
        if scope is None:
            log.warning("课程 frame 里没等到标签或卡片，改在主文档里找")
            self.s.wait_ready([any_sel], timeout_s=4.0)
            scope = self.s.page.main_frame

        # 学生账号默认停在「我教的课」（空），需要切到「我学的课」。
        # `_switch_to_student_courses` 内部会**确认点击真的生效**（标签拿到
        # `current` 或卡片已出现），必要时重点 —— 这是「点太早等于没点」那个
        # 坑的修法。所以这里点完再看一次卡片计数就够了。
        switched = self._switch_to_student_courses(scope)
        if not switched:
            log.debug("没找到「我学的课」标签，按当前页面直接读课程")

        # 定位到真正有课程卡片的上下文（切换后 iframe 可能重载）。
        #
        # ⚠ 带 `must_match` 的 `_frame_scope` 超时会返回 None（这是它的契约），
        # 所以拿到 None 必须自己补一个可用 scope —— 否则下面
        # `scope.evaluate(...)` 会抛 `'NoneType' object has no attribute
        # 'evaluate'`，用户看到的是一句看不懂的报错，而不是
        # 「课程列表为空：可能是登录态失效」。
        try:
            have_cards = scope.locator(card_sel).count() > 0
        except Exception:
            have_cards = False
        if not have_cards:
            got = self._frame_scope(self._COURSE_FRAME_HINT, timeout_s=5.0,
                                    must_match=card_sel)
            if got is not None:
                scope = got
            else:
                log.warning("课程卡片一个都没等到，改在主文档里找")
                self.s.wait_ready([card_sel, "#courseList > li.course"],
                                  timeout_s=4.0)
                scope = self.s.page.main_frame

        try:
            self.s.scroll_bottom(steps=6)
        except Exception:
            pass

        raw = scope.evaluate(
            """(max) => {
              const strip = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const out = [];
              const seen = new Set();
              // 优先精确的课程卡片；退化时再放宽
              let nodes = Array.from(document.querySelectorAll('#courseList > li.course, li.course'));
              if (!nodes.length) {
                nodes = Array.from(document.querySelectorAll(
                  '#courseList li, .course-list li, .course-info, .courseCard, .course-card'));
              }
              nodes.forEach((el, i) => {
                if (out.length >= max) return;
                // 文件夹/新建按钮等非课程项跳过
                if (el.classList.contains('teacher-folder') ||
                    el.classList.contains('new-folder') ||
                    el.classList.contains('course-folder') ||
                    el.id === 'newCourse') return;
                const a = el.querySelector(
                  "a[href*='stucoursemiddle'], a[href*='studentcourse'], a[href*='/course/'], a[href]");
                const nameEl = el.querySelector('.course-name, h3 .course-name, h3, .title');
                // 优先 title 属性：innerText 会把卡片上「移动到/退课」等悬浮按钮文字带进来
                let name = strip(nameEl ? nameEl.getAttribute('title') : '');
                if (!name) name = strip(nameEl ? nameEl.innerText : '');
                if (!name) name = strip(el.getAttribute('title') || '');
                if (!name) name = strip(el.innerText).slice(0, 60);
                name = name.replace(/^移动到\s*/, '').trim();
                if (!name || name.length > 120) return;
                if (seen.has(name)) return;
                seen.add(name);
                out.push({
                  index: i,
                  name: name,
                  url: a ? a.href : '',
                  courseid: el.getAttribute('courseid') || '',
                  clazzid: el.getAttribute('clazzid') || '',
                });
              });
              return out;
            }""",
            max_courses,
        )
        courses: List[Course] = []
        for item in raw or []:
            c = Course(name=item["name"], url=item.get("url") or "",
                       index=int(item.get("index") or 0))
            c.params = extract_course_params(c.url)
            # 卡片上的属性比 URL 里的参数更权威（URL 可能被截断）
            if item.get("courseid"):
                c.params["courseid"] = str(item["courseid"])
            if item.get("clazzid"):
                c.params["clazzid"] = str(item["clazzid"])
            if not any(x.name == c.name for x in courses):
                courses.append(c)
        log.info("共发现 %d 门课程", len(courses))
        return courses

    def find_course(self, keyword: str, courses: Optional[Sequence[Course]] = None) -> Course:
        """按关键字（支持模糊/拼音首字母不行，但支持包含匹配）找课程。"""
        courses = list(courses or self.list_courses())
        if not courses:
            raise PageError("课程列表为空：可能是登录态失效，或首页结构变了。"
                            "试试重新执行 login 命令。")
        kw = normalize_text(keyword)
        if not kw:
            raise PageError("课程关键字为空")

        exact = [c for c in courses if normalize_text(c.name) == kw]
        if exact:
            return exact[0]
        contains = [c for c in courses if kw in normalize_text(c.name)]
        if contains:
            return contains[0]
        # 逐字包含（处理顺序不同的情况）
        scored = sorted(
            courses,
            key=lambda c: -sum(1 for ch in kw if ch in normalize_text(c.name)),
        )
        if scored and sum(1 for ch in kw if ch in normalize_text(scored[0].name)) >= max(1, len(kw) // 2):
            log.warning("课程名未精确匹配，选用最接近的：%s", scored[0].name)
            return scored[0]
        names = "、".join(c.name for c in courses[:20])
        raise PageError(f"没找到包含「{keyword}」的课程。当前课程：{names}")

    def open_course(self, course: Course) -> None:
        """打开课程主页（可能新开标签页），并把 self.s.page 切过去。"""
        page = self.s.page
        ctx = self.s.context
        try:
            if course.url:
                with ctx.expect_page(timeout=6000) as pop:
                    page.evaluate("(u) => window.open(u, '_blank')", course.url)
                newp = pop.value
            else:
                # 没有链接就靠点击卡片
                with ctx.expect_page(timeout=6000) as pop:
                    page.locator(S.HOME_COURSE_CARDS and S.HOME_COURSE_CARDS[0]).nth(course.index).click()
                newp = pop.value
        except Exception:
            # 没有新标签页：直接用当前页跳转
            if course.url:
                self.s.goto(course.url)
            else:
                self.s.click_first(S.HOME_COURSE_CARDS, timeout_ms=5000)
            newp = self.s.page
        self.s._page = newp  # 切换到课程页
        # 等课程页的导航渲染出来（原来是死等 2000ms）。命中不了也不致命，
        # 后面的 open_homework_tab 还会自己等。
        try:
            newp.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        self.s.wait_ready(["a[data-url*='/work/']", "li[dataname='zy'] a",
                           "a[title='作业']", "#courseList", ".course-tab"],
                          timeout_s=6.0)
        self.course_url = self.s.page.url
        log.info("已进入课程：%s -> %s", course.name, self.course_url)

    # ================================================================== #
    # 3) 作业
    # ================================================================== #
    def open_homework_tab(self) -> None:
        """在课程页里点开「作业」列表。

        实测（真实 mooc2-ans 课程页，2026-09）：导航是
        `<li dataname="zy" pageheader="8"><a href="javascript:void(0);"
         title="作业" data-url="https://mooc1.chaoxing.com/mooc2/work/list">…作业…</a></li>`
        —— **href 是 javascript:void(0)，真正的地址在 `data-url` 里**，早先按
        `href*='work/list'` 找永远找不到。这里改为优先取 `data-url` 并点击
        （前端会用 POST 表单带上 courseid/clazzid/cpi 跳转）。
        """
        page = self.s.page
        href_before = page.url

        # 1) 真实结构：视频导航项 a[data-url*='/work/']
        for sel in ("a[data-url*='/work/']", "li[dataname='zy'] a", "a[data-url*='work/list']"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                try:
                    target = loc.get_attribute("data-url") or ""
                except Exception:
                    target = ""
                log.debug("命中作业入口选择器 %s -> data-url=%s", sel, target)
                with self.s.context.expect_page(timeout=6000) as pop:
                    loc.click()
                newp = pop.value
                self.s._page = newp
                # 作业列表在 iframe 里。先等网络静下来（超时也不致命），
                # 剩下的交给 list_homework_info 的 _frame_scope 事件驱动等 —— 原先
                # 这里还额外 sleep 300ms 纯属重复等待。
                try:
                    newp.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                log.info("已打开作业页（新标签）：%s", self.s.page.url)
                return
            except Exception:
                # 没有新标签页：可能是同页跳转（后续 _frame_scope 会等内容）
                if self.s.page.url != href_before or "/work/" in self.s.page.url:
                    log.info("已打开作业页：%s", self.s.page.url)
                    return
                continue

        # 2) 文本兜底：直接点标题是「作业」的导航项
        try:
            loc = page.locator("a[title='作业']").first
            if loc.count() == 0:
                loc = page.locator("li:has-text('作业') a").first
            if loc.count() > 0:
                with self.s.context.expect_page(timeout=6000) as pop:
                    loc.click()
                self.s._page = pop.value
                log.info("已打开作业页（新标签）：%s", self.s.page.url)
                return
        except Exception:
            if self.s.page.url != href_before:
                log.info("已打开作业页：%s", self.s.page.url)
                return

        # 3) 兜底：用课程页参数直接拼地址
        p = parse_qs(urlparse(self.course_url).query)
        courseid = (p.get("courseid") or p.get("courseId") or [""])[0]
        clazzid = (p.get("clazzid") or p.get("classId") or [""])[0]
        cpi = (p.get("cpi") or [""])[0]
        enc = (p.get("enc") or [""])[0]
        if courseid and clazzid:
            url = (f"https://mooc1.chaoxing.com/mooc2/work/list?"
                   f"courseId={courseid}&classId={clazzid}&cpi={cpi}&enc={enc}&ut=s")
            self.s.goto(url)
            log.info("已按参数直达作业页：%s", self.s.page.url)
            return
        raise PageError(f"在课程页找不到「作业」入口。当前 URL：{self.s.page.url}")

    def _frame_scope(self, hint: Any, timeout_s: float = 15.0,
                     must_match: Optional[str] = None) -> Any:
        """按 URL 片段找一个 frame（找不到退回主页面）。

        实测：mooc2-ans 课程页把各板块塞进不同 iframe：
        - 课程列表 → `visit/interaction`
        - 作业列表 → `#frame_content-zy` → `https://mooc1.chaoxing.com/mooc2/work/list?...`
        主页面只有导航条，因此在主页面找作业条目只会抓到「进入空间 / 在线客服」这类噪音。

        `hint` 可以是字符串或字符串元组（多片段任选其一）。
        `must_match` 命中后还要等这个选择器出现（避免抢在 iframe 渲染完成之前
        就拿到空 frame —— 这会造成「作业列表为空」的偶发失败）。

        提速：找 frame 只看 `fr.url`（Playwright 客户端有缓存，**不发往返**），
        所以外圈可以转得很密；等内容用 `wait_for_function`（事件驱动），
        而不是原来的「每轮 sleep 500ms + 每个 frame 各发一次 count()」。
        """
        page = self.s.page
        hints = (hint,) if isinstance(hint, str) else tuple(hint)
        deadline = time.time() + timeout_s
        # 每个候选 frame 单轮最多等这么久，等不到就回头重新扫一遍 frame
        # （iframe 切换/重载很常见），避免吊死在第一个候选上。
        per_wait = max(200, int(min(1.0, max(0.15, timeout_s / 8.0)) * 1000))
        while True:
            for fr in page.frames:
                if not any(h in (fr.url or "") for h in hints):
                    continue
                if not must_match:
                    return fr
                if time.time() >= deadline:
                    break
                try:
                    fr.wait_for_function("(sel) => !!document.querySelector(sel)",
                                         arg=must_match, timeout=per_wait)
                    return fr
                except Exception:
                    pass
            if time.time() >= deadline:
                break
            page.wait_for_timeout(80)
        return None if must_match else page.main_frame

    # 作业列表 iframe 被服务端拒绝时的正文特征（enc 令牌没生效就会这样）
    DENIED_HINTS = ("无权限的操作", "无权限", "请重新登录", "登录已过期")

    def list_homework_info(self) -> Dict[str, Any]:
        """抓一次作业列表，顺带把「是不是被服务端拒了」也带出来。

        返回 `{"items": [...], "denied": bool, "scope_url": str, "body": str}`。
        `denied=True` 说明这一轮 iframe 只回了「无权限的操作！」——重开课程页
        拿新 enc 令牌就能恢复，不必再看 items 是否为空来猜。
        """
        # 作业列表在子 iframe 里（真实页面：work/list 或 #frame_content-zy），
        # 不在主页面。必须等「列表项真的出现了」再抓，否则会偶发地拿到空列表。
        # ⚠️ 实测（2026-10）：真正的选择器是 `div.main.task-list ul li`（ul 不是直接
        # 子元素）。写成 `div.main.task-list > ul > li` 会一条都匹配不到 —— 这正是
        # 早先「作业列表项选择器未命中，白等 37 秒才退回」的原因。
        item_sel = "div.main.task-list ul li, div.task-list li"
        # 探测要**快失败**：被拒时死等 must_match 只是白烧时间。
        # 三档退化里第一档是「等列表项真的出现」，所以命中时**不需要**再补 sleep；
        # 只有退到「只确认 frame 在不在」时才给一小段渲染时间。
        scope = self._frame_scope(("work/list", "frame_content-zy"),
                                  timeout_s=8.0, must_match=item_sel)
        if scope is None:
            scope = self._frame_scope(("work/list", "frame_content-zy"), timeout_s=4.0)
            if scope is None:
                scope = self.s.page.main_frame
            log.info("作业列表项选择器 %s 这一轮未命中，改在 %s 里再试",
                     item_sel, getattr(scope, "url", "")[:100])
            try:
                scope.wait_for_timeout(400)
            except Exception:
                self.s.page.wait_for_timeout(400)

        raw = scope.evaluate(
            """() => {
              const strip = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const out = [];
              const push = (el, i) => {
                const t = strip(el.innerText);
                if (!t || t.length > 300) return;
                // 真实列表项（实测）：
                //   <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?..."
                //       aria-label="腹痛 ; 已完成" role="link">
                //     <p class="overHidden2 fl">腹痛</p> <p class="status fl">已完成</p>
                //     <a class="listSubmit insightBtn" href="...study-knowledge/ans...">智能分析</a>
                //   ⚠️ 作业真地址在 `data` 属性上；页内 <a> 是「智能分析」外链，
                //   早先 a[href*='work'] 优先取到的就是它（错的）。
                //
                // 只认「像一条作业」的列表项：带 data 地址 / 带 p.overHidden2 / 带 p.status /
                // 文本里有状态词。**这一条专治列表底部的分页噪声**：分页条
                // `<li class="pagination...">1 2</li>` 会被 `div.task-list li` 顺带选中，
                // 早先它变成第 13、14 条「作业 1 / 作业 2」混进结果里。
                const titleEl = el.querySelector('p.overHidden2, .right-content p.overHidden2');
                const statusEl = el.querySelector('p.status, .status');
                const dataAttr = el.getAttribute('data');
                const looksLikeTask = !!(dataAttr || titleEl || statusEl);
                if (!looksLikeTask && !/(未交|待完成|已完成|已交|已提交|已批阅|未开始|进行中)/.test(t)) return;
                const a = el.querySelector("a[href*='doWork'], a[href*='work/task'], a[href*='work']");
                out.push({
                  index: i,
                  title: strip(titleEl ? titleEl.innerText : t).slice(0, 120) || t.slice(0, 120),
                  text: t.slice(0, 200),
                  // `p.status` 是「已完成 智能分析」这种拼接，只取第一个词作为状态
                  status: strip(statusEl ? statusEl.innerText : '').split(' ')[0],
                  url: dataAttr || (a ? (a.getAttribute('href') || '') : ''),
                  cls: String(el.className || ''),
                });
              };
              // 实测列表容器：div.main.task-list（作业列表页）。注意 ul 不是直接
              // 子元素，所以这里用后代选择器，并保留几条兼容兜底。
              let nodes = Array.from(document.querySelectorAll(
                'div.main.task-list ul li, div.task-list li, ul.task-list > li'));
              if (!nodes.length) {
                nodes = Array.from(document.querySelectorAll(
                  '.ulDiv > ul > li, ul.work-list > li, div.work-list li, .workList li'));
              }
              nodes.forEach(push);
              const empty = /暂无作业|暂无数据/.test(strip(document.body.innerText));
              return {items: out, empty: empty, url: location.href,
                      body: strip(document.body.innerText).slice(0, 300)};
            }"""
        )
        if isinstance(raw, dict) and raw.get("empty"):
            log.warning("作业列表页显示「暂无作业」(%s)", (raw.get("url") or "")[:110])
        body = ""
        try:
            body = re.sub(r"\s+", " ", str((raw or {}).get("body") or ""))[:160]
        except Exception:
            pass
        items: List[HomeworkItem] = []
        for r in (raw or {}).get("items", []) if isinstance(raw, dict) else []:
            title = re.sub(r"\s+", " ", r.get("title") or "").strip()
            status = re.sub(r"\s+", " ", r.get("status") or "").strip()
            if not status:
                # 老版页面把状态写在标题里（「腹痛 已完成」）
                m = re.search(r"(未交|待完成|已完成|已交|已提交|已批阅|未开始|进行中)", title)
                if m:
                    status = m.group(1)
                    title = title.replace(status, "").strip(" -|")
            if not title:
                title = re.sub(r"\s+", " ", r.get("text") or "").strip()
            if title:
                items.append(HomeworkItem(title=title, status=status,
                                          url=r.get("url") or "", index=r["index"]))
        log.info("作业列表：共 %d 条（已完成 %d）", len(items),
                 sum(1 for it in items if it.done))
        for it in items[:20]:
            log.info("  - %s%s", it.title, f"  [{it.status}]" if it.status else "")
        scope_url = getattr(scope, "url", "") or ""
        denied = bool(body) and any(h in body for h in self.DENIED_HINTS)
        if denied:
            log.warning("作业列表 iframe 被拒：%s（frame=%s）", body, scope_url[:110])
        elif not items:
            log.warning("作业列表抓到 0 条；frame=%s；正文片段=%s", scope_url[:110], body)
        info = {"items": items, "denied": denied, "scope_url": scope_url, "body": body}
        self._last_homework_info = info
        return info

    def list_homework(self) -> List[HomeworkItem]:
        return list(self.list_homework_info().get("items") or [])

    def find_homework(self, keyword: str,
                      items: Optional[Sequence[HomeworkItem]] = None,
                      reload_course: Optional[Any] = None,
                      retries: int = 2) -> HomeworkItem:
        """在作业列表里找一条作业。

        ⚠️ 实测偶发：作业列表 iframe 会直接给出「无权限的操作！」（正文里就这一句），
        一条作业都抓不到。这是 `enc` 令牌没生效，**重开一次课程页**再进作业列表就好。
        这里不等拿到空 items 才猜——`list_homework_info` 会把 `denied` 直接带出来，
        命中就立刻重开课程页，省掉一轮 12 秒的白等。
        """
        items = list(items or [])
        tries = 0
        info: Any = {}
        while (not items or info.get("denied")) and tries <= retries:
            if tries:
                # 被拒时不用等：立刻重开课程页换新 enc 令牌
                log.info("作业列表 %s（第 %d 次），重新打开课程页再试",
                         "被拒（无权限的操作！）" if info.get("denied") else "为空", tries)
                if reload_course is None or not self.course_url:
                    break
                try:
                    reload_course()
                    # 等课程页导航渲染好再点「作业」（原来是死等 1200ms；
                    # 抢跑会点不到入口，等太久又白烧时间）。
                    self.s.wait_ready(["a[data-url*='/work/']", "li[dataname='zy'] a",
                                       "a[title='作业']", ".course-tab"], timeout_s=8.0)
                    self.open_homework_tab()
                except Exception as e:
                    log.warning("重开课程页失败：%s", e)
            info = self.list_homework_info()
            items = list(info.get("items") or [])
            tries += 1
        if not items:
            raise PageError(f"作业列表为空。当前 URL：{self.s.page.url}")
        kw = normalize_text(keyword)
        if not kw:
            raise PageError("作业关键字为空")
        for it in items:
            if kw in normalize_text(it.title):
                return it
        # 关键字里的数字/章节号匹配
        nums = re.findall(r"\d+", keyword)
        if nums:
            for it in items:
                if all(n in it.title for n in nums):
                    return it
        listed = "、".join(it.title for it in items[:20])
        raise PageError(f"没找到包含「{keyword}」的作业。当前作业：{listed}")

    def open_homework(self, item: HomeworkItem) -> None:
        """点进某个作业，进入答题页。

        注意：作业条目在子 iframe（`work/list`）里，点击要在那个 frame 上做。
        """
        scope = self._frame_scope(("work/list", "frame_content-zy"),
                                  timeout_s=15.0,
                                  must_match="div.main.task-list ul li")
        if scope is None:
            scope = self._frame_scope(("work/list", "frame_content-zy"), timeout_s=4.0)
            if scope is None:
                scope = self.s.page.main_frame
        used = False
        loc = None
        for sel in ("div.main.task-list ul li", "div.task-list li",
                    ".ulDiv > ul > li", "li:has(a)"):
            try:
                cand = scope.locator(sel).nth(item.index)
                if cand.count() == 0:
                    continue
                loc = cand
                break
            except Exception:
                continue
        # 下标对不上（列表顺序变了）时按标题文本兜底
        if loc is None and item.title:
            for sel in ("div.main.task-list ul li", "div.task-list li",
                        ".ulDiv > ul > li", "li"):
                try:
                    cand = scope.locator(sel).filter(has_text=item.title[:12]).first
                    if cand.count() > 0:
                        loc = cand
                        break
                except Exception:
                    continue
        if loc is not None:
            try:
                with self.s.context.expect_page(timeout=6000) as pop:
                    loc.click()
                self.s._page = pop.value
                used = True
            except Exception:
                if self.s.page.url != getattr(self, "course_url", ""):
                    used = True
        if not used and item.url:
            self.s.goto(item.url if item.url.startswith("http")
                        else urljoin(self.s.page.url, item.url))
        elif not used:
            raise PageError(f"无法点开作业「{item.title}」。当前 URL：{self.s.page.url}")

        # 有些作业还需要再点一次「开始答题 / 做作业」
        self.enter_answer_page_if_needed()
        log.info("已进入答题页：%s", self.s.page.url)

    # 答题页不成形时服务端会直接回一张很小的「提示」页。分清这些提示，
    # 才能知道「URL 已经失效」还是「页面还在加载」——`--reopen` 靠它快速退回。
    TROUBLE_HINTS = ("作答状态异常", "无权限的操作", "很抱歉，您没有权限访问",
                     "章节任务点未达标", "登录已过期", "请重新登录", "任务点未达标")

    #: 只数题目标记，不做任何解析 —— 用来快速判断「这一页是不是答题页」
    _COUNT_Q_JS = ("() => document.querySelectorAll("
                   "'div.questionLi, .TiMu, .question-item, .mark_item, "
                   "div[id^=\\'question\\']').length")

    #: 两路信号任一出结果就立刻返回：
    #:   答题控件出现 → 'ui'；服务端异常提示出现 → 'trouble'
    #: 用它替代「死等一个 timeout」——旧 URL 会立刻回异常页，
    #: 所以能在几百毫秒内判死，而不是白等到超时。
    _PAGE_KIND_JS = """(arg) => {
      const sels = arg[0], bad = arg[1];
      for (const s of sels) {
        try { if (document.querySelector(s)) return 'ui'; } catch (e) {}
      }
      const t = (document.body && document.body.innerText) || '';
      for (const b of bad) { if (t.indexOf(b) >= 0) return 'trouble'; }
      return '';
    }"""

    START_BTN = ["a:has-text('开始答题')", "a:has-text('做作业')",
                 "a:has-text('继续答题')", ".beginBtn"]

    def answer_page_state(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        """快速判断当前页能不能作答（**不发多余的等待**）。

        返回 `{"ready", "questions", "start", "trouble"}`：
        - `questions`：页面上已有的题目容器数（跨 frame 取最大）
        - `start`    ：有没有「开始答题 / 做作业 / 继续答题」按钮
        - `trouble`  ：命中服务端异常提示时的那句话（如「作答状态异常！」）
        - `ready`    ：`questions > 0 or start`，即「值得继续往下走」

        用途：`--reopen` 拿旧 URL 重开时，旧 URL 上的 `enc` 是一次性令牌，
        服务端通常直接回一张 1142 字节的「作答状态异常！」页。原先的代码会
        先 `wait_for_timeout(800)` 再让 `enter_answer_page_if_needed` 白等 8 秒，
        白烧 9 秒才知道失败。这里一次问完，失败就立刻退回去走课程/作业查找。
        """
        sels = (["div.questionLi", ".answerBg", ".TiMu"] + S.Q_ITEM
                + self.START_BTN)
        try:
            # 事件驱动：控件或异常提示一出现就返回，不再死等满 timeout。
            self.s.page.main_frame.wait_for_function(
                self._PAGE_KIND_JS, arg=[sels, list(self.TROUBLE_HINTS)],
                timeout=max(500, int(timeout_s * 1000)))
        except Exception:
            pass
        count = 0
        for fr in self._candidate_frames():
            try:
                count = max(count, int(fr.evaluate(self._COUNT_Q_JS) or 0))
            except Exception:
                continue
        start = False
        try:
            start = bool(self.s.page.locator(
                ", ".join(self.START_BTN)).count() > 0)
        except Exception:
            pass
        trouble = ""
        if not count and not start:
            try:
                body = self.s.page.main_frame.evaluate(
                    "() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                body = ""
            for hint in self.TROUBLE_HINTS:
                if hint in body:
                    trouble = hint
                    break
            if not trouble and body.strip():
                trouble = re.sub(r"\s+", " ", body).strip()[:60]
        return {"ready": bool(count or start), "questions": count,
                "start": start, "trouble": trouble}

    def enter_answer_page_if_needed(self, timeout_s: float = 8.0) -> bool:
        """若当前页只是作业说明页，点一次「开始答题 / 做作业 / 继续答题」。

        返回 True 表示点了。`open_homework` 和 `--reopen` 提速路径都用它。
        """
        hit = self.s.wait_any(
            [".TiMu"] + S.Q_ITEM + self.START_BTN,
            timeout_s=timeout_s,
        )
        if hit and (("答题" in hit) or hit.startswith("a:")):
            self.s.click_first(self.START_BTN, timeout_ms=4000)
            # 点完之后等答题控件真的出现，而不是死等 2000ms 赌它加载完了
            # （新版 doWork 页是 JS 渲染，慢的时候 2 秒不够、快的时候白等）。
            self.s.wait_ready(["div.questionLi", ".answerBg"] + S.Q_ITEM,
                              timeout_s=8.0)
            return True
        return False

    # ================================================================== #
    # 4) 抓题
    # ================================================================== #
    def _candidate_frames(self) -> List[Any]:
        """答题页可能被塞在 iframe 里，返回可遍历的 frame 列表。"""
        page = self.s.page
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        return frames

    def extract_questions(self, *, dump_dom: Optional[Path] = None) -> List[Question]:
        """扫描所有 frame，取题目数量最多的那个作为答题 frame。"""
        best: Optional[Tuple[List[Question], Any, dict]] = None
        for fr in self._candidate_frames():
            try:
                data = fr.evaluate(EXTRACT_JS)
            except Exception as e:
                log.debug("frame 解析失败 %s: %s", getattr(fr, "url", "?"), e)
                continue
            qs = self._build_questions(data)
            if qs and (best is None or len(qs) > len(best[0])):
                best = (qs, fr, data)
        if best is None or not best[0]:
            if dump_dom:
                self.s.dump_html(dump_dom)
                log.error("没抓到题目，已把页面 DOM 存到 %s", dump_dom)
            raise PageError(
                "没抓到任何题目。可能原因：还没进入答题页 / 题目在 iframe 里未加载完 / 选择器需要更新。"
                f"当前 URL：{self.s.page.url}"
            )
        qs, fr, data = best
        log.info("抓到 %d 道题（frame: %s）", len(qs), getattr(fr, "url", "main"))
        if self.cfg.output.dump_dom and dump_dom:
            self.s.dump_html(dump_dom)
        return qs

    @staticmethod
    def _build_questions(data: Optional[dict]) -> List[Question]:
        out: List[Question] = []
        for i, raw in enumerate((data or {}).get("questions") or []):
            qtype = QType.from_raw(raw.get("qtype"))
            opts: List[Option] = []
            for k, o in enumerate(raw.get("options") or []):
                text = str(o.get("text") or "").strip()
                if not text:
                    continue
                # ⚠️ 必须采信抽取端给的字母（它读的是选项块的 `data` 属性 /
                # 原生 input 的 `value`），**不能按位置重新编号 A、B、C…**。
                # 新版答题页会打乱屏显字母（实测：`data=E` 的选项屏显是「A」，
                # 因为 A 是页面标准答案），而单选/多选回填是拿字母去匹配 `data`
                # 的 —— 一旦在这里重排，字母与 data 就对不上，点中的是别的选项
                # （真机上表现为「多选题隐藏域只剩最后一个字母」）；配伍题更明显：
                # 同一组备选被 3 个小题各渲染一遍，位置编号会把它编成 A–L。
                # 只有抽取端没给字母、或给的不是单个 A–H 字母时才退回按位置编号。
                letter = str(o.get("letter") or "").strip().upper()
                if not _LETTER_OK.match(letter):
                    letter = chr(65 + len(opts))
                # 配伍题（B 型题）每个小题会把同一组备选重复渲染一遍，
                # 这里按字母去重，让 AI 只看到 A~E 一组干净备选
                # （省 token、字母不歧义）。按字母而不是按文字：同一组备选
                # 里文字可能被页面改写，按文字去重会把整组裁成一个。
                if qtype == QType.MATCH and any(x.letter.upper() == letter for x in opts):
                    continue
                opts.append(Option(letter=letter, text=text))
            stem = (raw.get("stem") or "").strip()
            if not stem:
                continue
            # 配伍题（B 型题）：小题文本来自浏览器的 match_items，
            # 若只有 1 个小题（页面按小题拆成了多个容器），把题干补进去。
            match_items = [str(t).strip() for t in (raw.get("match_items") or []) if str(t).strip()]
            if qtype == QType.MATCH and len(match_items) <= 1:
                head = stem
                if len(head) > 60:
                    head = head[:60]
                if head and (not match_items or head != match_items[0]):
                    match_items = [head] + match_items
            q = Question(
                qid=str(raw.get("qid") or f"q{i + 1}"),
                qtype=qtype,
                stem=stem,
                options=opts,
                number=str(i + 1),
                match_options=[o.text for o in opts] if qtype == QType.MATCH else [],
                match_items=match_items if qtype == QType.MATCH else [],
                review_answer=str(raw.get("review_answer") or "").strip(),
                review_mine=str(raw.get("review_mine") or "").strip(),
                locator={
                    "index": i,
                    "sel": str(raw.get("sel") or ""),
                    "sel_index": int(raw.get("sel_index") if raw.get("sel_index") is not None else -1),
                    "raw_id": raw.get("raw_id") or "",
                    "blanks": int(raw.get("blanks") or 0),
                    "has_text_input": bool(raw.get("has_text_input")),
                    "has_input": int(raw.get("has_input") or 0),
                    "has_choice_ui": int(raw.get("has_choice_ui") or 0),
                    # 新版 ARIA 答题页的隐藏域编号：`#answer<answer_key>`。
                    # 抽取端从选项块的 qid/data 属性取，回填时直接用它，
                    # 不能拿 question 前缀的 qid 去拼。
                    "answer_key": str(raw.get("answer_key") or ""),
                },
            )
            out.append(q)
        return out

    def page_debug(self) -> dict:
        try:
            return self.s.page.evaluate(DEBUG_PAGE_JS)
        except Exception as e:
            return {"error": str(e), "url": self.s.page.url}

    # ================================================================== #
    # 5) 填答
    # ================================================================== #
    def _find_answer_frame(self, questions: Sequence[Question]) -> Any:
        for fr in self._candidate_frames():
            try:
                n = fr.evaluate(
                    "() => Math.max(document.querySelectorAll('.TiMu').length,"
                    " document.querySelectorAll('.TiMu, .mark_item, .question-item').length)")
                if n >= max(1, len(questions) // 2):
                    return fr
            except Exception:
                continue
        return self.s.page.main_frame

    def apply_answers(self, questions: Sequence[Question], answers: Sequence[Answer],
                      *, dry_run: bool = False) -> Dict[str, Any]:
        """把答案填进页面。dry_run=True 时只打印不落地。"""
        frame = self._find_answer_frame(questions)
        ans_map = {a.qid: a for a in answers}
        stats = {"filled": 0, "skipped": 0, "failed": 0, "details": []}

        for q in questions:
            a = ans_map.get(q.qid)
            if a is None or a.is_empty:
                stats["skipped"] += 1
                stats["details"].append({"qid": q.qid, "status": "skip", "reason": "无答案"})
                continue
            if dry_run:
                stats["filled"] += 1
                stats["details"].append({"qid": q.qid, "status": "dry-run",
                                         "value": a.value, "source": a.source})
                continue
            try:
                ok, how = self._fill_one(frame, q, a)
                if ok:
                    stats["filled"] += 1
                    stats["details"].append({"qid": q.qid, "status": "ok", "how": how,
                                             "value": a.value, "source": a.source})
                else:
                    stats["failed"] += 1
                    stats["details"].append({"qid": q.qid, "status": "fail",
                                             "value": a.value, "reason": how})
            except Exception as e:
                stats["failed"] += 1
                stats["details"].append({"qid": q.qid, "status": "error", "reason": str(e)})
                log.warning("第 %s 题填答异常：%s", q.number, e)

            if self.cfg.answer.min_delay_ms or self.cfg.answer.max_delay_ms:
                self.s.page.wait_for_timeout(random.randint(
                    int(self.cfg.answer.min_delay_ms), max(int(self.cfg.answer.min_delay_ms),
                                                           int(self.cfg.answer.max_delay_ms))))
        log.info("填答结果：成功 %d / 跳过 %d / 失败 %d",
                 stats["filled"], stats["skipped"], stats["failed"])
        return stats

    # ------------------------------------------------------------------ #
    def _item_locator(self, frame: Any, q: Question):  # type: ignore[no-untyped-def]
        idx = int(q.locator.get("index", 0))
        # 首选：抽取端记下的 (选择器, 下标)。抽取端做过去重/嵌套过滤，
        # 它选中的不一定是「原始选择器命中的第 idx 个」，所以不能只靠 index。
        sel = str(q.locator.get("sel") or "")
        sel_idx = int(q.locator.get("sel_index", -1))
        if sel and sel_idx >= 0:
            try:
                loc = frame.locator(sel)
                if loc.count() > sel_idx:
                    return loc.nth(sel_idx)
            except Exception as e:
                log.debug("按 %s[%s] 定位失败：%s", sel, sel_idx, e)
        # 兜底 1：与抽取端一致的合并容器选择器 + 顺序下标
        try:
            loc = frame.locator(S.Q_ITEM_ALL)
            if loc.count() > idx:
                return loc.nth(idx)
        except Exception as e:
            log.debug("合并容器选择器定位失败：%s", e)
        # 兜底 2：老的多选择器轮询
        for sel2 in S.Q_ITEM:
            try:
                loc = frame.locator(sel2)
                if loc.count() > idx:
                    return loc.nth(idx)
            except Exception:
                continue
        # 兜底 3：元素 id
        raw_id = q.locator.get("raw_id") or ""
        if raw_id:
            try:
                loc = frame.locator(f"#{raw_id}")
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        raise PageError(f"第 {q.number} 题的容器找不到了（选择器可能失效）")

    @staticmethod
    def _judge_letters(value: Any, q: Question) -> List[str]:
        """把「对/错」的各种写法映射到选项字母。

        只在选项文本确实长得像判断题（含 正确/错误、对/错、T/F、√/×）时才动，
        避免把普通单选题的奇怪答案误判。
        """
        if not q.options:
            return []
        head = normalize_text(q.options[0].text)
        tail = normalize_text(q.options[-1].text)
        if not (re.search(r"正确|对|是|√|T\b", head) and re.search(r"错误|不对|否|×|F\b", tail)):
            return []
        if isinstance(value, bool):
            return [q.options[0].letter if value else q.options[-1].letter]
        s = normalize_text(str(value)).upper()
        if s in {"TRUE", "T", "YES", "Y", "1", "对", "正确", "是", "√"}:
            return [q.options[0].letter]
        if s in {"FALSE", "F", "NO", "N", "0", "错", "错误", "否", "×", "X"}:
            return [q.options[-1].letter]
        return []

    def _fill_one(self, frame: Any, q: Question, a: Answer) -> Tuple[bool, str]:
        if q.qtype == QType.MATCH:
            return self._fill_match(frame, q, a)

        if q.qtype in (QType.SINGLE, QType.JUDGE, QType.MULTIPLE):
            # 判断题先走真假映射：parse_letters("true") 会返回伪字母 "T"，
            # 而页面上根本没有 T 选项，直接用它会「一个选项都没点中」。
            letters: List[str] = []
            if q.qtype == QType.JUDGE:
                letters = self._judge_letters(a.value, q)
            if not letters and isinstance(a.value, bool) and q.options:
                letters = [q.options[0].letter if a.value else q.options[-1].letter]
            if not letters:
                letters = parse_letters(a.value, q.options)
                if q.qtype == QType.JUDGE:
                    # 答案写成 "T"/"F"/"TRUE"/"FALSE" 但选项字母是 A/B 的情况
                    fixed: List[str] = []
                    for x in letters:
                        got = self._judge_letters(x, q)
                        fixed.extend(got or [x])
                    letters = [x for x in fixed
                               if any(o.letter.upper() == x.upper() for o in q.options)]
            if not letters:
                return False, f"无法把答案 {a.value!r} 解析成选项"
            return self._click_options(frame, q, letters)

        if q.qtype == QType.FILL:
            values = a.value if isinstance(a.value, list) else [a.value]
            return self._fill_texts(frame, q, [str(v) for v in values], prefer="input")

        # 简答 / 其它
        text = a.value if isinstance(a.value, str) else json.dumps(a.value, ensure_ascii=False)
        limit = int(self.cfg.solver.short_answer_max_chars or 0)
        if limit and len(text) > limit:
            text = text[:limit]
        return self._fill_texts(frame, q, [text], prefer="textarea")

    def _fill_match(self, frame: Any, q: Question, a: Answer) -> Tuple[bool, str]:
        """配伍题（B 型题）：每个小题各点一个备选字母。

        正常路径：AI 已经在答案里给出了 {小题序号: 字母}（`Answer.pairs`）。
        兜底路径：答案是一坨文本 / 字母没对齐时，改用「小题文本 vs 备选文本」
        的本地相似度自己配对（零 token，且对这种「短小题 + 长备选」题型相当准：
        小题「失眠」会匹配到备选「入睡困难、易醒早醒」）。
        """
        item = self._item_locator(frame, q)
        items = [str(x) for x in (q.match_items or [])]
        if len(q.options) < 2:
            return False, "配伍题没有备选项"

        labels = [Question.match_label(k) for k in range(len(items))]
        pairs: Dict[str, str] = {}
        for k, v in (a.pairs or {}).items():
            m = re.search(r"\d+", str(k))
            key = m.group(0) if m else str(k)
            if key in labels and v:
                pairs[key] = str(v).upper()

        source = "llm-pairs"
        if not pairs:
            if isinstance(a.value, list):
                text = ",".join(str(x) for x in a.value)
            else:
                text = str(a.value or "")
            pairs = parse_pairs(text, items, [o.letter for o in q.options])
            source = "llm-parsed"
        if not any(pairs.values()) and items and q.match_options:
            pairs = match_items_by_text(items, q.match_options)
            source = "local-match"
        if not pairs:
            return False, f"配伍题无法得到配对（答案 {a.value!r}）"

        # 小题序号 -> 备选下标
        idx_of = {o.letter.upper(): i for i, o in enumerate(q.options)}

        # 新版答题页的 B 型题没有 input[type=radio]，组内选项是
        # .B-answerCon > span[data=A]，点它由页面脚本写进隐藏域。
        if item.locator(".B-answer-ct").count():
            bplan = []
            for k, lab in enumerate(labels):
                let = (pairs.get(lab) or "").upper()
                if let not in idx_of:
                    return False, f"小题 {lab} 的答案 {let!r} 不在备选项里"
                bplan.append({"k": k, "letter": let})
            try:
                res = item.evaluate(CLICK_BTYPE_JS, {
                    "root": item.element_handle(), "plan": bplan, "qid": q.answer_key,
                })
            except Exception as e:  # noqa: BLE001
                return False, f"B 型题点击失败：{e}"
            # parsed_ok 才是权威：B 型题隐藏域是 JSON 数组，只有逐项对上才算真的填对。
            if res and (res.get("parsed_ok") or res.get("ok")):
                summary = ",".join(f"{p['k'] + 1}:{p['letter']}" for p in bplan)
                return True, f"match[{source}]:{summary}"
            hid_dbg = (res or {}).get("hidden_value") or ""
            return False, (f"B 型题只填好 {res.get('done') if res else 0}/{len(bplan)} 个小题"
                           f"（隐藏域 {hid_dbg!r}）")

        # 同一组备选在每个小题里会重复渲染，且组内顺序未必跟共享备选一致；
        # 用「备选文字」把字母换算成「该组的第几个 input」，比按下标硬点稳得多。
        text_index = {normalize_text(o.text): i for i, o in enumerate(q.options)}
        plan: List[Dict[str, Any]] = []
        for k, lab in enumerate(labels):
            let = (pairs.get(lab) or "").upper()
            if let not in idx_of:
                return False, f"小题 {lab} 的答案 {let!r} 不在备选项里"
            global_idx = idx_of[let]
            text_idx = text_index.get(normalize_text(q.options[global_idx].text))
            plan.append({"k": k, "idx": global_idx,
                         "text_idx": text_idx if text_idx is not None else global_idx,
                         "letter": let})

        ok_all = False
        try:
            ok_all = bool(item.evaluate(
                """(el, args) => {
                  const root = (el && el.nodeType === 1) ? el : args.root;
                  const radios = Array.from(root.querySelectorAll(
                    'input[type=radio], input[type=checkbox]'));
                  const groups = {};
                  const order = [];
                  radios.forEach((r) => {
                    const key = r.name || '__one';
                    if (!groups[key]) { groups[key] = []; order.push(key); }
                    groups[key].push(r);
                  });
                  let ok = 0;
                  for (const p of args.plan) {
                    let inp = null;
                    // 备选是 radio 且按小题分组时，第 k 组对应第 k 个小题
                    if (order.length > 1 && order[p.k]) {
                      const grp = groups[order[p.k]];
                      inp = grp[p.text_idx] || grp[p.idx] || null;
                    }
                    if (!inp) inp = radios[p.idx] || null;
                    if (!inp) continue;
                    inp.checked = true;
                    inp.dispatchEvent(new Event('click', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    if (inp.checked) ok += 1;
                  }
                  return ok === args.plan.length;
                }""",
                {"root": item.element_handle(), "plan": plan},
            ))
        except Exception as e:
            log.debug("配伍题 JS 填答失败：%s", e)

        if ok_all:
            summary = ",".join(f"{p['k'] + 1}:{p['letter']}" for p in plan)
            return True, f"match[{source}]:{summary}"

        # JS 方案失败时逐个用 Playwright 点击（组内按备选文字定位）
        inputs = item.locator("input[type=radio], input[type=checkbox]")
        clicked = 0
        for p in plan:
            try:
                let = p["letter"]
                box = item.locator(
                    f"input[type=radio][value='{let}'], input[type=checkbox][value='{let}']")
                n = box.count()
                target = None
                if n > 1:
                    # 同一组备选在每个小题里重复出现，count 收不到定位信息时落回下标
                    target = box.nth(min(p["k"], n - 1))
                elif n == 1:
                    target = box.first
                else:
                    text = q.options[p["idx"]].text
                    lab = item.locator(f"label:has-text({json.dumps(text)}) input, "
                                       f"li:has-text({json.dumps(text)}) input")
                    if lab.count() == 0:
                        continue
                    target = lab.nth(min(p["k"], lab.count() - 1))
                if target is not None and not target.is_checked():
                    target.click(timeout=4000)
                if target is None:
                    target = inputs.nth(p["text_idx"])
                if target.count() > 0 and target.is_checked():
                    clicked += 1
            except Exception as e:
                log.debug("配伍题第 %s 小题点击失败：%s", p["k"] + 1, e)
        if clicked == len(plan):
            return True, f"match[{source}]:{clicked} 小题"
        return False, f"配伍题只填好 {clicked}/{len(plan)} 个小题"

    def _click_options(self, frame: Any, q: Question, letters: List[str]) -> Tuple[bool, str]:
        item = self._item_locator(frame, q)
        want = {l.upper() for l in letters}
        clicked: List[str] = []

        # ARIA 页（新版 doWork）：整题一次同步 —— 一次调用里既勾上想要的字母，
        # 又取消页面残留的多余选中项，最后用「隐藏域字母集合恰好相等」判定。
        # 不能按字母逐个点：只勾一个时集合还不完整，会被判失败后再去走
        # nth-input / 文本 / 鼠标几条路，把已点好的又点掉。
        if item.locator(S.ARIA_OPTION_SEL).count():
            try:
                res = item.evaluate(SYNC_CHOICE_JS, {
                    "root": item.element_handle(),
                    "want": sorted(want),
                    "qid": q.answer_key,
                })
            except Exception as e:  # noqa: BLE001
                return False, f"选项同步失败：{e}"
            if res and res.get("hidden_ok"):
                tag = ",".join(f"{a}:{b}" for a, b in (res.get("how") or []))
                clicked.append(",".join(sorted(want)) + (f"[{tag}]" if tag else ""))
                return True, "click:" + ",".join(clicked)
            got = res.get("hidden_letters") if res else None
            val = (res or {}).get("hidden_value") or ""
            return False, (f"隐藏域字母为 {got or []}，期望 {sorted(want)}"
                           f"（隐藏域 {val!r}）")

        for i, opt in enumerate(q.options):
            if opt.letter.upper() not in want:
                continue
            ok = False
            # 依次尝试：ARIA 选项 div（走页面自己的 addChoice/addMultipleChoice）
            # -> 容器内第 i 个原生 input -> 文本匹配的 label/li -> 真鼠标点击
            for attempt in ("aria", "nth-input", "text", "mouse"):
                try:
                    if attempt == "aria":
                        if not item.locator(S.ARIA_OPTION_SEL).count():
                            continue
                        res = item.evaluate(CLICK_CHOICE_JS, {
                            "root": item.element_handle(),
                            "want": [opt.letter],
                            "qid": q.answer_key,
                            "multiple": q.qtype == QType.MULTIPLE,
                        })
                        # hidden_ok = 隐藏域里确实出现了这个字母（页面自己写的）
                        ok = bool(res and res.get("hidden_ok"))
                        if ok:
                            tag = (res.get("how") or [[opt.letter, "aria"]])[0][1]
                            if tag and tag != "native":
                                clicked.append(f"{opt.letter}({tag})")
                                break
                    elif attempt == "nth-input":
                        inp = item.locator("input[type=radio], input[type=checkbox]").nth(i)
                        if inp.count() > 0:
                            if not inp.is_checked():
                                inp.click(timeout=4000)
                            ok = True
                    elif attempt == "text":
                        key = normalize_text(opt.text)
                        cand = item.locator("label, li, a, span")
                        n = cand.count()
                        for k in range(n):
                            el = cand.nth(k)
                            try:
                                t = normalize_text(el.inner_text() or "")
                            except Exception:
                                continue
                            if key and (t == key or (len(key) >= 2 and key in t)):
                                el.click(timeout=4000)
                                ok = True
                                break
                    elif attempt == "mouse":
                        ok = self._mouse_click_option(item, q, opt)
                except Exception as e:
                    log.debug("选项 %s 第 %s 种方式失败：%s", opt.letter, attempt, e)
                if ok:
                    break
            if ok:
                if not any(c.startswith(opt.letter) for c in clicked):
                    clicked.append(opt.letter)
            else:
                return False, f"选项 {opt.letter} 点击失败"

        if not clicked:
            return False, "一个选项都没点中"
        # 多选校验：拿页面真实选中态（.check_answer_dx）与隐藏域双重核对。
        if q.qtype == QType.MULTIPLE:
            got = self._read_selected_letters(item, q)
            missing = sorted(want - got)
            if missing:
                return False, f"多选只点中 {sorted(got)}，期望 {sorted(want)}（缺 {missing}）"
        return True, "click:" + ",".join(clicked)

    def _mouse_click_option(self, item: Any, q: Question, opt: Any) -> bool:
        """真鼠标点击某个选项（兜底路径）。

        只按 **data 属性** 认字母：页面对多选题会打乱屏显字母顺序
        （data=A 却显示 B），按屏显文字点必然点错。
        """
        want = opt.letter.upper()
        try:
            boxes = item.locator(S.ARIA_OPTION_SEL)
            n = boxes.count()
            for k in range(n):
                b = boxes.nth(k)
                if (b.get_attribute("data") or "").upper() == want:
                    b.click(timeout=4000)
                    return True
                sp = b.locator(".num_option, .num_option_dx, .choice-letter")
                if sp.count():
                    d = (sp.first.get_attribute("data") or "").upper()
                    if d == want:
                        b.click(timeout=4000)
                        return True
        except Exception as e:
            log.debug("选项 %s 鼠标点击失败：%s", opt.letter, e)
        return False

    def _read_selected_letters(self, item: Any, q: Question) -> set:
        """读回本题页面上真实被选中的字母。

        三种页面都要认：
        - 新版 ARIA 页：选中态是选项 div 上 .check_answer/.check_answer_dx，
          真实答案写在隐藏域 #answer<qid>；
        - 老版页面：选中态是原生 input 的 checked；
        - 再兜底：按选项下标直接看第 i 个原生 input 是否 checked。
        """
        try:
            res = item.evaluate(
                """(el, args) => {
                  const root = (el && el.nodeType === 1) ? el : args.root;
                  const boxes = Array.from(root.querySelectorAll(
                    '.answerBg, [role=radio], [role=checkbox]'));
                  const letterOf = (box) => {
                    for (const sp of box.querySelectorAll(
                        '.num_option, .num_option_dx, .choice-letter')) {
                      const d = (sp.getAttribute('data') || '').trim();
                      if (/^[A-Ha-h]$/.test(d)) return d.toUpperCase();
                      const t = (sp.innerText || '').trim();
                      if (/^[A-Ha-h]$/.test(t)) return t.toUpperCase();
                    }
                    return '';
                  };
                  const on = [];
                  for (const b of boxes) {
                    if (b.querySelector('.check_answer, .check_answer_dx')) {
                      const l = letterOf(b);
                      if (l) on.push(l);
                    }
                  }
                  // 老版页面：li 里的原生控件 + 选项字母链接（a.fl）
                  const nativeOn = [];
                  const inputs = Array.from(root.querySelectorAll(
                    'input[type=radio], input[type=checkbox]'));
                  inputs.forEach((inp) => {
                    if (!inp.checked) return;
                    const li = inp.closest('li');
                    let l = '';
                    if (li) {
                      const a = li.querySelector('a.fl, .num_option, .num_option_dx');
                      if (a) l = (a.innerText || '').trim();
                    }
                    if (!/^[A-Ha-h]$/.test(l)) {
                      const idx = inputs.indexOf(inp);
                      l = String.fromCharCode(65 + idx);
                    }
                    nativeOn.push(l.toUpperCase());
                  });
                  const hid = args.qid
                    ? (root.querySelector('#answer' + args.qid)
                       || document.getElementById('answer' + args.qid)) : null;
                  return {selected: on, native: nativeOn,
                          hidden: hid ? String(hid.value || '') : ''};
                }""",
                {"root": item.element_handle(), "qid": q.answer_key},
            )
        except Exception as e:
            log.debug("读选中态失败：%s", e)
            return set()
        got = {str(x).upper() for x in (res or {}).get("selected") or []}
        got |= {str(x).upper() for x in (res or {}).get("native") or []}
        hid = str((res or {}).get("hidden") or "").upper()
        for ch in hid:
            if "A" <= ch <= "Z":
                got.add(ch)
        return got

    def _fill_texts(self, frame: Any, q: Question, values: List[str],
                    prefer: str = "input") -> Tuple[bool, str]:
        item = self._item_locator(frame, q)
        # 富文本编辑器优先
        editors = item.locator(".edui-body-container, div[contenteditable='true']")
        if editors.count() > 0 and prefer == "textarea":
            done = 0
            for k, v in enumerate(values):
                if k >= editors.count():
                    break
                ed = editors.nth(k)
                try:
                    ed.click(timeout=3000)
                    ed.evaluate(FILL_EDITOR_JS, {"el": ed.element_handle(), "value": v})
                    done += 1
                except Exception as e:
                    log.debug("富文本写入失败：%s", e)
            if done:
                return True, f"editor:{done}"

        fields = item.locator("textarea, input[type=text], input:not([type])")
        n = fields.count()
        if n == 0:
            return False, "找不到输入框"
        done = 0
        for k, v in enumerate(values):
            if k >= n:
                break
            f = fields.nth(k)
            ok = False
            try:
                f.click(timeout=3000)
                f.fill(v, timeout=5000)
                ok = True
            except Exception:
                pass
            if not ok:
                try:
                    ok = bool(f.evaluate(FILL_TEXT_JS, {"el": f.element_handle(), "value": v}))
                except Exception:
                    ok = False
            if ok:
                done += 1
        if done == 0:
            return False, "输入框写入失败"
        if len(values) > 1 and done < len(values):
            return False, f"只写入 {done}/{len(values)} 个空"
        return True, f"text:{done}"

    # ================================================================== #
    # 6) 暂存 / 提交
    # ================================================================== #
    def save_progress(self, *, quiet: bool = False) -> bool:
        """点「暂时保存」（学习通不一定有）。没有就靠输入框 blur 的自动保存。

        注意：这一步只暂存，不代表提交；作业仍可继续修改。
        """
        clicked = self.s.click_first(S.SAVE_BTN, timeout_ms=4000)
        if not clicked:
            for fr in self._candidate_frames():
                try:
                    for sel in S.SAVE_BTN:
                        loc = fr.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.click(timeout=4000)
                            clicked = True
                            break
                except Exception:
                    continue
                if clicked:
                    break
        # 先等页面自己的提示（事件驱动，一到就返回），不要再先死等 1200ms ——
        # 「保存成功」的 toast 通常只显示一两秒，先 sleep 反而容易错过它。
        hit = self.s.wait_any(S.SAVE_DONE_HINTS, timeout_s=6) if clicked else None
        if not hit:
            # 没等到提示（本来就没这个按钮，或提示一闪而过）：给自动保存的那次
            # XHR 留点时间，别在请求还没发出去时就关掉浏览器。
            self.s.page.wait_for_timeout(800)
        if not quiet:
            if clicked:
                log.info("已点击暂存/保存%s", "（页面提示成功）" if hit else "")
            else:
                log.info("页面没有「暂时保存」按钮，依赖输入框自动保存；答案已留在页面上")
        return clicked

    def submit(self, *, confirm: bool = True) -> Tuple[bool, str]:
        """点击提交。confirm=True 时在有头模式下等你按回车确认。"""
        if confirm and not self.cfg.browser.headless:
            print("\n" + "!" * 68)
            print("  答案已全部填好。请先在浏览器里核对一遍。")
            print("  确认无误后按【回车】提交；直接输入 n 取消提交。")
            print("!" * 68)
            try:
                reply = input("> ").strip().lower()
            except EOFError:
                reply = "n"
            if reply in ("n", "no", "q", "退出", "取消"):
                return False, "用户取消提交"

        ok = self.s.click_first(S.SUBMIT_BTN, timeout_ms=6000)
        if not ok:
            # 提交按钮可能在别的 frame
            for fr in self._candidate_frames():
                try:
                    for sel in S.SUBMIT_BTN:
                        loc = fr.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=5000)
                            ok = True
                            break
                except Exception:
                    continue
                if ok:
                    break
        if not ok:
            return False, "找不到提交按钮"

        self.s.page.wait_for_timeout(1200)
        # 二次确认弹窗
        self.s.click_first(S.SUBMIT_CONFIRM_BTN, timeout_ms=4000)
        self.s.page.wait_for_timeout(2500)
        hit = self.s.wait_any(S.SUBMIT_DONE_HINTS, timeout_s=8)
        if hit:
            return True, "已提交（页面出现提交成功提示）"
        return True, "已点击提交，但未捕获到明确的成功提示，请在浏览器里确认"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def extract_course_params(url: str) -> Dict[str, str]:
    """从课程 URL 里抠出 courseId / classId / token 等参数。"""
    if not url:
        return {}
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for k, v in qs.items():
        if v:
            out[k] = v[0]
    if not out:
        # 形如 /mycourse/studentcourse?courseid=xxx&clazzid=yyy 之外的老式 hash 路由
        m = re.search(r"courseId=(\d+)", url)
        if m:
            out["courseId"] = m.group(1)
        m = re.search(r"clazzid=(\d+)", url)
        if m:
            out["classId"] = m.group(1)
    return out


def answer_to_display(a: Answer, q: Question) -> str:
    """把答案渲染成人类可读的一行，用于日志/导出。"""
    if q.qtype == QType.MATCH and a.pairs:
        text = match_display(a.pairs, q.match_items, q.options)
        if text:
            return text
    v = a.value
    if isinstance(v, bool):
        return "对" if v else "错"
    if isinstance(v, list):
        parts = []
        for x in v:
            letter = None
            for o in q.options:
                if o.letter.upper() == str(x).upper():
                    letter = o
                    break
            parts.append(f"{letter.letter}.{letter.text}" if letter else str(x))
        return " / ".join(parts)
    s = str(v)
    for o in q.options:
        if o.letter.upper() == s.upper():
            return f"{o.letter}. {o.text}"
    return s
