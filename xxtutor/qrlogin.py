"""学习通扫码登录（无头也可用）。

场景：框架跑在没人值守的机器上（headless），但登录需要人工。做法是把登录页上的
二维码抓下来存成 PNG，你用手机「学习通 App」扫一下并在手机上确认，Cookie 就落进
`browser.user_data_dir` 了 —— 账号密码全程不用交给框架。

真实页面结构（2024 版 passport2）：`<img id="quickCode" src="/createqr?uuid=...&fid=-1">`，
uuid 在 `<input id="uuid" >` 里；扫码后前端会定期轮询，成功后页面自动跳走，
所以这里只要盯住「URL 是否离开登录页」就够。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

from .browser import BrowserSession
from .config import Config

log_dbg = logging.getLogger("xxtutor.qr")

# 二维码图片的选择器，按优先级试；真实页面上第一条即命中
QR_SELECTORS = [
    "img#quickCode",
    "#quickCode img",
    ".quickCode img",
    "img[src*='createqr']",
    "img[src*='qr']",
    "[id*='qrcode'] img",
    "[class*='qrcode'] img",
    "[class*='ewm'] img",
]


def _qr_bytes(sess: BrowserSession) -> Optional[bytes]:
    """从当前页面取出二维码图片的字节；取不到返回 None。"""
    page = sess.page
    for sel in QR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            src = loc.get_attribute("src") or ""
            if not src:
                continue
            if src.startswith("data:image"):
                return base64.b64decode(src.split(",", 1)[1])
            if src.startswith("/"):
                src = urljoin(page.url, src)
            if not src.startswith("http"):
                continue
            # 用浏览器上下文里的 request 去拿，Cookie / 代理与浏览器一致
            resp = sess.context.request.get(src, timeout=20000)
            if resp.ok:
                return resp.body()
        except Exception:
            continue
    return None


def _save_png(path: Path, data: bytes, log: Callable[[str], None] = print) -> bool:
    """把二维码写盘。**写不进去也绝不中断登录流程**。

    真实踩到的坑：第一次生成后你用图片查看器打开了它，或杀软/网盘同步占着句柄，
    直接 `path.write_bytes()` 会抛 `OSError: [Errno 22] Invalid argument`，
    把整个等待扫码的循环炸掉（登录明明还能成功）。所以这里先写临时文件再 replace
    （replace 在 Windows 上能覆盖被占用的目标），失败就退到带序号的文件名，
    最后才放弃并只打一行警告。
    """
    candidates = [path]
    stem, suffix = path.stem, path.suffix or ".png"
    candidates += [path.with_name(f"{stem}-{i}{suffix}") for i in (1, 2, 3)]
    for i, target in enumerate(candidates):
        try:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            if i > 0:
                log(f"（原文件被占用，二维码改存到 {target}）")
            return True
        except OSError:
            continue
    log("警告：二维码图片写盘失败（文件被占用？），但你仍然可以直接在浏览器里扫码 —— "
        "扫码成功后本命令会自动检测到并退出。")
    return False


def qr_login(
    cfg: Config,
    wait_s: int = 300,
    refresh_s: int = 60,
    out_dir: Optional[Path] = None,
    headless: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    """抓二维码 -> 等你扫码 -> 登录态落盘。返回 0 成功 / 1 超时。

    `headless=False` 会开一个真实窗口：二维码照样会存成 PNG，但**页面的轮询
    由真实窗口自己跑**，能规避「无头环境下服务端判定扫码会话无效」的情况。
    """
    out_dir = Path(out_dir) if out_dir else (cfg.user_data_dir.parent / "qr")
    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / "login-qr.png"

    headless_backup = cfg.browser.headless
    cfg.browser.headless = bool(headless)
    deadline = time.time() + max(30, wait_s)
    got = False
    try:
        with BrowserSession(cfg) as sess:
            page = sess.page
            # 把页面对 /getauthstatus/v2 的轮询结果记下来：扫码后到底是
            # 「未扫」「已扫待确认」还是「已确认」，全靠这个接口的返回。
            # 没有它就只能瞎猜（实测第一条弯路就是这么走的）。
            seen_status: list[str] = []
            poll_count = 0

            def _on_response(resp) -> None:  # type: ignore[no-untyped-def]
                nonlocal poll_count
                if "getauthstatus" not in resp.url:
                    return
                try:
                    data = resp.json()
                except Exception:
                    return
                poll_count += 1
                status = data.get("status")
                typ = data.get("type")
                if status:
                    key = f"status={status}"
                else:
                    key = f"type={typ} mes={data.get('mes', '')}"
                if not seen_status or seen_status[-1] != key:
                    seen_status.append(key)
                    # status 为真 = 已确认登录，页面马上就要跳走了
                    log(f"扫码状态变化：{key}（第 {poll_count} 次轮询）")
                else:
                    log_dbg.info("轮询第 %s 次：%s", poll_count, key)

            page.on("response", _on_response)
            sess.goto(cfg.login.login_url, wait="domcontentloaded")
            page.wait_for_timeout(1500)
            next_refresh = 0.0
            last_note = 0.0
            last_verify = 0.0
            while time.time() < deadline:
                url = page.url
                if "passport2.chaoxing.com" not in url and "/login" not in url.lower():
                    log(f"登录成功！当前地址：{url}")
                    log(f"登录态已写入：{cfg.user_data_dir}")
                    return 0

                # 有些登录路径下页面自己不会跳走，只把 Cookie 种下了。
                # 所以定期主动去访问首页，用「首页是否还有效」当第二判据。
                if time.time() >= last_verify:
                    last_verify = time.time() + 6.0
                    try:
                        page.goto(cfg.login.home_url,
                                  wait_until="domcontentloaded", timeout=20000)
                        now = page.url
                        if "passport2.chaoxing.com" not in now and "/login" not in now.lower():
                            log(f"登录成功！当前地址：{now}")
                            log(f"登录态已写入：{cfg.user_data_dir}")
                            return 0
                        page.goto(cfg.login.login_url, wait_until="domcontentloaded",
                                  timeout=20000)
                        page.wait_for_timeout(800)
                    except Exception as exc:  # noqa: BLE001
                        log(f"（访问首页校验登录态时出错，继续等：{exc}）")

                if not got or time.time() >= next_refresh:
                    png = _qr_bytes(sess)
                    if png:
                        saved = _save_png(qr_path, png, log)
                        got = True
                        next_refresh = time.time() + max(20, refresh_s)
                        if saved:
                            log(f"二维码已保存：{qr_path}")
                        log("请用手机「学习通 App」→ 扫一扫 → 扫描该图片，并在手机上点确认。")
                    elif time.time() - last_note > 10:
                        last_note = time.time()
                        log(f"还没找到二维码元素，当前页面：{page.url}（{page.title()}）")
                time.sleep(2.0)
    finally:
        cfg.browser.headless = headless_backup

    log(f"等待超时（{wait_s}s）。二维码在 {qr_path}；"
        "也可以在有人值守时跑 `python -m xxtutor login` 用有头浏览器登录。")
    return 1
