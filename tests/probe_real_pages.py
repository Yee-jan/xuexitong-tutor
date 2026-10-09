"""真实站点结构勘察：抓公开页面的 DOM 与选择器命中情况。

用途：把「学习通真实 HTML」变成离线夹具，避免只对着手写样例做测试。
不需要登录，只用公开可达的页面（登录页 / 首页）。
输出到 .state/probe-real/：每页一个 html + 一份选择器命中报告。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession          # noqa: E402
from xxtutor.config import load_config              # noqa: E402

OUT = ROOT / ".state" / "probe-real"
PAGES = [
    ("login", "https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"),
    ("home", "https://i.chaoxing.com/"),
    ("space", "https://mooc1.chaoxing.com/visit/interaction"),
]

# 我们真正依赖的选择器，逐个在真实页面上验一遍
PROBE_SEL = [
    "#phone", "#pwd", "#loginBtn", "#uname", "#password",
    ".Mycourse", "li.course", ".courseCard", ".course-list li",
    ".TiMu", ".Zy_TItle", ".Zy_ulTop", ".mark_item",
    ".work-list", ".workList", ".homework-item",
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.browser.user_data_dir = str(ROOT / ".state" / "probe-real-profile")
    cfg.browser.block_resources = ["image", "media", "font"]

    report = {}
    with BrowserSession(cfg) as s:
        for name, url in PAGES:
            entry = {"url": url}
            try:
                s.goto(url, wait="domcontentloaded")
                s.page.wait_for_timeout(1200)
                entry["final_url"] = s.page.url
                entry["title"] = s.page.title()
                html = s.page.content()
                (OUT / f"{name}.html").write_text(html, encoding="utf-8")
                entry["html_len"] = len(html)
                hits = {}
                for sel in PROBE_SEL:
                    try:
                        hits[sel] = s.page.locator(sel).count()
                    except Exception as e:
                        hits[sel] = f"ERR {e}"
                entry["selector_hits"] = hits
                try:
                    entry["debug"] = s.page.evaluate(
                        "() => ({url: location.href, title: document.title,"
                        " forms: Array.from(document.querySelectorAll('form')).map(f => f.action),"
                        " iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src),"
                        " inputs: Array.from(document.querySelectorAll('input')).slice(0,30)"
                        "   .map(i => i.type + '#' + (i.id||'') + '.' + (i.name||''))})")
                except Exception as e:
                    entry["debug"] = f"ERR {e}"
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
            report[name] = entry
            print(f"--- {name} ---")
            print(json.dumps(entry, ensure_ascii=False, indent=2)[:1600])

    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
