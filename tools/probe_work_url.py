# -*- coding: utf-8 -*-
"""探测：doWork 答题页的哪种 URL 形态能稳定重开（只读，不填不保存不提交）。

背景：`--reopen` 直接 goto 上次记下的 work_url 时，服务端返回 1142 字节的
      「提示 作答状态异常！」；URL 里带 answerId=<记录id>、standardEnc、enc，
      这些令牌看起来是一次性的。这里逐个变体试，看哪个能拿到 73 道题。

用法：python tools\\probe_work_url.py [out\\某次运行目录]
"""
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from xxtutor.browser import BrowserSession     # noqa: E402
from xxtutor.config import load_config         # noqa: E402
from xxtutor.page import ChaoxingPage          # noqa: E402


def variant(url, **over):
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    for k, v in over.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    return urlunparse(u._replace(query=urlencode(q)))


def summarize(page, frame):
    """返回 (题目数, 页面标题, 正文摘要)。"""
    main = page.s.page.main_frame
    n = 0
    for fr in [main] + [f for f in page.s.page.frames if f != main]:
        try:
            n = max(n, fr.locator("div.questionLi").count())
        except Exception:
            pass
    title = ""
    body = ""
    try:
        title = main.title() or ""
    except Exception:
        pass
    try:
        body = main.evaluate(
            "() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        pass
    body = re.sub(r"\s+", " ", body).strip()
    return n, title, body[:110]


def main(argv):
    src = Path(argv[1]) if len(argv) > 1 else sorted(
        (WS / "out").glob("*-answer"), key=lambda p: p.stat().st_mtime)[-1]
    import json
    url = json.loads((src / "run.json").read_text(encoding="utf-8"))["work_url"]
    print(f"基准 URL（来自 {src.name}）：\n  {url}\n")

    cases = [
        ("原样（--reopen 现在的行为）", url),
        ("answerId=0", variant(url, answerId="0")),
        ("去掉 standardEnc", variant(url, standardEnc=None)),
        ("去掉 enc", variant(url, enc=None)),
        ("去掉 standardEnc + enc", variant(url, standardEnc=None, enc=None)),
        ("answerId=0 + 去掉两个令牌",
         variant(url, answerId="0", standardEnc=None, enc=None)),
    ]

    cfg = load_config(str(WS / "config.json"))
    sess = BrowserSession(cfg).start()
    try:
        page = ChaoxingPage(sess, cfg)
        page.ensure_login(interactive=False)
        for label, u in cases:
            t0 = time.perf_counter()
            try:
                sess.goto(u)
                sess.page.wait_for_timeout(1500)
                page.enter_answer_page_if_needed(timeout_s=3.0)
                sess.page.wait_for_timeout(500)
                n, title, body = summarize(page, None)
                verdict = f"✅ {n} 道题" if n >= 10 else "❌ 失败"
                print(f"{verdict:<10} {label:<28} "
                      f"({time.perf_counter() - t0:.1f}s) title={title!r} "
                      f"body={body!r}")
            except Exception as e:  # noqa: BLE001
                print(f"{'💥 异常':<10} {label:<28} {type(e).__name__}: {e}")
    finally:
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
