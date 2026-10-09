"""Playwright 会话封装：持久化登录态、反自动化标记、资源拦截。

设计要点：
    * 用 launch_persistent_context，把 Cookie/localStorage 留在 .state/browser，
      人工登录一次后长期复用（学习通登录态通常能撑很久）。
    * 启动参数里关掉 navigator.webdriver 等自动化特征，降低被风控识别的概率。
    * 默认拦截图片/字体/媒体，页面加载快很多；遇到图片题可以关掉。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("xxtutor.browser")

# 去掉最明显的自动化特征（navigator.webdriver 是最常被检测的一个）
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-dev-shm-usage",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
]

STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
"""

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class BrowserSession:
    """一个持久化的浏览器上下文。用 with 语句管理生命周期。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._playwright = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        bc = self.cfg.browser
        user_dir = self.cfg.user_data_dir
        user_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        channel = (bc.channel or "").strip()
        # 用户可能写 "edge" / "msedge" / "chrome"（Playwright 只认 msedge/chrome）
        aliases = {"edge": "msedge", "msedge": "msedge", "chrome": "chrome",
                   "Google Chrome": "chrome", "chromium": ""}
        if channel:
            channel = aliases.get(channel, channel)

        # 不再硬编造 Chrome/131 的 UA：那个 UA 和真实内核版本对不上，
        # 学习通这类站点很容易据此判定「不是正常浏览器」（实测扫码会话被秒判失效）。
        # 优先用内核自己的真实 UA；只有用户显式配了 user_agent 才覆盖。
        real_ua = ""
        try:
            probe = (self._playwright.chromium.launch(headless=True, channel=channel or None)
                     if channel else self._playwright.chromium.launch(headless=True))
            try:
                real_ua = probe.new_context().new_page().evaluate("navigator.userAgent")
            finally:
                probe.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("探测真实 UA 失败（用默认值）: %s", exc)

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(user_dir),
            "headless": bool(bc.headless),
            "slow_mo": int(bc.slow_mo_ms or 0),
            "args": STEALTH_ARGS,
            "locale": bc.locale or "zh-CN",
            "viewport": {"width": int(bc.viewport_width), "height": int(bc.viewport_height)},
            "ignore_default_args": ["--enable-automation"],
        }
        if bc.user_agent:
            launch_kwargs["user_agent"] = bc.user_agent
        elif real_ua:
            launch_kwargs["user_agent"] = real_ua
        # 真实踩到的坑：这里原写成 `else: channel = ""`，意思是「探测不到 UA 就不用系统内核」。
        # 结果是探测一旦失败就**悄悄退回自带 chromium**（用户明明配了 msedge，起的是 chrome），
        # 而且日志里 channel 也被抹成空串，完全看不出问题。用户指定的通道必须照办。
        if channel:
            launch_kwargs["channel"] = channel

        log.info("启动浏览器（headless=%s, channel=%s, profile=%s）",
                 bc.headless, channel or "bundled-chromium", user_dir)
        try:
            self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            # 用户目录被上一个进程占用时最常见的报错，给出可执行的提示
            msg = str(e)
            if "ProcessSingleton" in msg or "SingletonLock" in msg or "user data directory is already in use" in msg:
                raise RuntimeError(
                    f"浏览器用户目录被占用：{user_dir}\n"
                    "请先关闭所有由本框架启动的 Chrome 窗口（或删掉 "
                    f"{user_dir / 'SingletonLock'} 后重试）。"
                ) from e
            raise

        self._context.set_default_timeout(int(bc.timeout_ms))
        self._context.set_default_navigation_timeout(max(60000, int(bc.timeout_ms)))
        self._context.add_init_script(STEALTH_INIT_JS)
        self._install_route_blocker()

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self

    #: 允许拦截的资源类型（Playwright 的 request.resource_type 取值）
    BLOCKABLE = frozenset({
        "image", "media", "font", "stylesheet", "script",
        "xhr", "fetch", "websocket", "manifest", "other",
    })

    def _install_route_blocker(self) -> None:
        raw = self.cfg.browser.block_resources
        if isinstance(raw, bool):
            # 兼容把 block_resources 写成 true/false 开关的配置；true = 用默认两类
            blocked = {"font", "media"} if raw else set()
        elif raw is None:
            blocked = set()
        elif isinstance(raw, str):
            blocked = {p.strip() for p in raw.split(",") if p.strip()}
        else:
            blocked = {str(p).strip() for p in raw if str(p).strip()}
        # 白名单过滤：万一配置里写了 "document"/"all" 之类，会让页面根本打不开。
        # 这里只收 Playwright 真正会发出的资源类型，避免配置错误把整站拦死。
        unknown = blocked - self.BLOCKABLE
        if unknown:
            log.warning("block_resources 里有无法识别的类型，已忽略: %s", sorted(unknown))
            blocked &= self.BLOCKABLE
        if not blocked or self._context is None:
            log.debug("未启用资源拦截")
            return
        log.debug("已启用资源拦截: %s", sorted(blocked))

        def _handler(route, request):  # type: ignore[no-untyped-def]
            try:
                if request.resource_type in blocked:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        self._context.route("**/*", _handler)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception as e:  # pragma: no cover
            log.debug("关闭上下文异常: %s", e)
        finally:
            self._context = None
            self._page = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        finally:
            self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 访问器
    # ------------------------------------------------------------------ #
    @property
    def page(self):  # type: ignore[no-untyped-def]
        if self._page is None or self._page.is_closed():
            assert self._context is not None, "浏览器未启动"
            self._page = self._context.new_page()
        return self._page

    @property
    def context(self):  # type: ignore[no-untyped-def]
        return self._context

    @property
    def pages(self) -> List[Any]:
        return list(self._context.pages) if self._context else []

    # ------------------------------------------------------------------ #
    # 常用操作
    # ------------------------------------------------------------------ #
    def goto(self, url: str, *, wait: str = "domcontentloaded",
             timeout_ms: Optional[int] = None) -> None:
        log.info("打开 %s", url)
        self.page.goto(url, wait_until=wait,
                       timeout=timeout_ms or max(60000, int(self.cfg.browser.timeout_ms)))

    def goto_new_tab(self, url: str, *, wait: str = "domcontentloaded",
                     timeout_ms: Optional[int] = None) -> Any:
        """在新标签页打开 url，**并把它设为当前页面**。

        为什么要把新页设为当前页：回填全部走 `self.page`，
        如果只在后台开一个标签页而 `self.page` 还指着旧页，
        就会把答案填到旧页面上（原实现的 bug）。
        """
        assert self._context is not None, "浏览器未启动"
        page = self._context.new_page()
        page.goto(url, wait_until=wait,
                  timeout=timeout_ms or max(60000, int(self.cfg.browser.timeout_ms)))
        self._page = page
        return page

    # 查询一组选择器里第一个命中的（一次往返问完，避免逐个 count()）
    _FIRST_HIT_JS = """(sels) => {
      for (const s of sels) {
        try { if (document.querySelector(s)) return s; } catch (e) {}
      }
      return null;
    }"""

    def wait_ready(self, selectors: List[str], timeout_s: float = 15.0,
                   root: Any = None) -> Optional[str]:
        """等一组选择器里任意一个**出现**，返回命中的那个（超时返回 None）。

        与 `wait_any` 的关键区别：这里用 Playwright 自己的
        `wait_for_function`，由页面的事件循环驱动（默认 raf 轮询），
        元素一出现立刻返回；而 `wait_any` 是 Python 侧每 300ms 发一批
        `locator().count()` 往返。**固定 sleep 换成它就等于「等条件」而不是
        「猜时长」** —— 既快（通常 100~400ms 就够）又不会因为浏览器慢而抢跑。
        """
        scope = root if root is not None else self.page.main_frame
        sels = [s for s in selectors if s]
        if not sels:
            return None
        try:
            scope.wait_for_function("(sels) => sels.some((s) => {"
                                    " try { return !!document.querySelector(s); }"
                                    " catch (e) { return false; } })",
                                    arg=sels, timeout=max(200, int(timeout_s * 1000)))
        except Exception:
            return None
        try:
            return scope.evaluate(self._FIRST_HIT_JS, sels)
        except Exception:
            return sels[0]

    def wait_any(self, selectors: List[str], timeout_s: float = 15.0) -> Optional[str]:
        """等一组选择器里任意一个出现，返回命中的那个。

        现在直接走 `wait_ready`（事件驱动、一次往返问完整组）。
        保留这个名字是因为 page.py / 测试里到处在用。
        """
        return self.wait_ready(selectors, timeout_s=timeout_s)

    def first_present(self, selectors: List[str], timeout_ms: int = 0):  # type: ignore[no-untyped-def]
        """按顺序返回第一个可见的元素，找不到返回 None。"""
        deadline = time.time() + max(0.0, timeout_ms / 1000.0)
        while True:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def click_first(self, selectors: List[str], timeout_ms: int = 5000,
                    force: bool = False) -> bool:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=timeout_ms, force=force)
                return True
            except Exception as e:
                log.debug("点击失败 %s: %s", sel, e)
        return False

    def text_of(self, selector: str, default: str = "") -> str:
        try:
            loc = self.page.locator(selector).first
            if loc.count() > 0:
                return (loc.inner_text() or "").strip()
        except Exception:
            pass
        return default

    def scroll_bottom(self, steps: int = 12, pause_ms: int = 250) -> None:
        """滚动整页到底，触发懒加载（题目/作业列表常需要）。"""
        for _ in range(steps):
            try:
                self.page.mouse.wheel(0, 1500)
            except Exception:
                break
            self.page.wait_for_timeout(pause_ms)

    def screenshot(self, path: str | Path) -> Optional[Path]:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(p), full_page=True)
            return p
        except Exception as e:
            log.warning("截图失败: %s", e)
            return None

    def dump_html(self, path: str | Path) -> Optional[Path]:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(self.page.content(), encoding="utf-8")
            return p
        except Exception as e:
            log.warning("保存 DOM 失败: %s", e)
            return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def reset_profile(user_dir: Path) -> None:
        """删掉持久化目录（登录态彻底失效时用）。"""
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)

    @staticmethod
    def profile_exists(user_dir: Path) -> bool:
        return user_dir.exists() and any(user_dir.iterdir())
