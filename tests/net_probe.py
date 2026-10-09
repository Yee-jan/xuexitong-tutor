"""联网冒烟测试：确认学习通站点可达、登录表单选择器还有效。

不登录、不答题，只做三件事：
    1. 打开 passport2 登录页，检查账号/密码输入框与登录按钮是否存在；
    2. 截图存到 .state/net-probe/ 供人工核对页面形态；
    3. 打开 i.chaoxing.com，判断当前是否已登录、能否列出课程。

网络不通或选择器失效时会把实际情况打出来，方便决定补哪个选择器。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor import selectors as S                 # noqa: E402
from xxtutor.browser import BrowserSession          # noqa: E402
from xxtutor.config import load_config              # noqa: E402
from xxtutor.page import ChaoxingPage               # noqa: E402

OUT = ROOT / ".state" / "net-probe"


def line(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.browser.user_data_dir = str(ROOT / ".state" / "probe-profile")
    cfg.browser.timeout_ms = 45000

    profile = Path(cfg.browser.user_data_dir)
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    with BrowserSession(cfg) as s:
        page = ChaoxingPage(s, cfg)

        line("=== 1) 登录页 ===")
        try:
            s.goto(cfg.login.login_url, wait="domcontentloaded")
            line(f"  已打开：{s.page.url}")
            line(f"  标题  ：{s.page.title()}")
        except Exception as e:
            line(f"  !! 打不开登录页：{e}")
            return 1

        for name, sels in (
            ("账号输入框", S.LOGIN_USERNAME_INPUT),
            ("密码输入框", S.LOGIN_PASSWORD_INPUT),
            ("登录按钮  ", S.LOGIN_SUBMIT),
        ):
            hit = None
            for sel in sels:
                try:
                    if s.page.locator(sel).count() > 0:
                        hit = sel
                        break
                except Exception:
                    continue
            line(f"  {name}: {'命中 ' + hit if hit else '未命中任何候选'}")

        shot = OUT / "login.png"
        s.screenshot(shot)
        s.dump_html(OUT / "login.html")
        line(f"  截图/DOM：{shot}")

        line("\n=== 2) i.chaoxing.com（登录态判断）===")
        try:
            s.goto(cfg.login.home_url, wait="domcontentloaded")
            s.page.wait_for_timeout(2500)
            line(f"  当前 URL：{s.page.url}")
            logged = page.is_logged_in(timeout_s=5)
            line(f"  已登录  ：{logged}")
            if logged:
                courses = page.list_courses()
                line(f"  课程数  ：{len(courses)}")
                for c in courses[:5]:
                    line(f"    - {c.name}")
        except Exception as e:
            line(f"  !! 打不开个人空间：{e}")

        s.screenshot(OUT / "home.png")
        s.dump_html(OUT / "home.html")
        line(f"  截图/DOM：{OUT / 'home.png'}")

    line("\n探针结束。若登录页选择器未命中，请打开 .state/net-probe/login.html 核对新 DOM。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
