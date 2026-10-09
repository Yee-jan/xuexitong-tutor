"""xxtutor 命令行入口。

常用：
    python -m xxtutor doctor                 自检（依赖 / 配置 / 模型连通性）
    python -m xxtutor login                  打开浏览器登录一次，登录态长期保存
    python -m xxtutor courses                列出所有课程
    python -m xxtutor homework -c 高等数学    列出某门课的作业
    python -m xxtutor answer -c 高等数学 -w 第三章 --dry-run    只抓题+出答案，不落页面
    python -m xxtutor answer -c 高等数学 -w 第三章              抓题+出答案+填入页面并保存（不提交）
    python -m xxtutor answer -c 高等数学 -w 第三章 --submit     填完并提交（会二次确认）
    python -m xxtutor solve -f questions.json                   离线跑求解器（不碰浏览器）
    python -m xxtutor bank --stats                              查看本地答案库
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from .answerfile import (AnswersFileError, import_answers, load_questions_from_dir,
                         merge_answer_maps)
from .config import Config, PRESETS, apply_preset, load_config
from .llm import LLMClient, LLMError
from .page import ChaoxingPage, Course, HomeworkItem, PageError, answer_to_display
from .solver import SolveReport, Solver
from .store import AnswerStore
from .types import Answer, AnswerValue, QType, Question, Option, parse_pairs

LOG = logging.getLogger("xxtutor")

BANNER = r"""
 __  __ __  __ _   _ _____ ___  ____
 \ \/ /|  \/  | | | |_   _/ _ \|  _ \
  \  / | |\/| | |_| | | || | | | |_) |
  /  \ | |  | |  _  | | || |_| |  _ <
 /_/\_\|_|  |_|_| |_| |_| \___/|_| \_\   v{ver}
 学习通作业自动答题框架（抓题 -> AI 作答 -> 回填，默认不提交）
"""


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[90m", "INFO": "\033[36m", "WARNING": "\033[33m",
        "ERROR": "\033[31m", "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if self.use_color:
            c = self.COLORS.get(record.levelname, "")
            if c:
                return f"{c}{s}{self.RESET}"
        return s


def setup_logging(level: str = "INFO", logfile: Optional[Path] = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(lvl)
    use_color = sys.stdout.isatty() and os.name != "nt" or os.environ.get("XX_COLOR") == "1"
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_ColorFormatter(use_color))
    root.addHandler(sh)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        root.addHandler(fh)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# 结果导出
# --------------------------------------------------------------------------- #

def new_run_dir(cfg: Config, tag: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = cfg.output_dir / f"{stamp}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_run_meta(meta: Dict[str, Any], run_dir: Path) -> Path:
    """每次运行都落一份 run.json（含 work_url），供下次 `--reopen` 直接用。"""
    p = run_dir / "run.json"
    try:
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # 元数据写不下去不该弄挂主流程
        LOG.debug("run.json 写入失败：%s", e)
    return p


def save_questions(questions: Sequence[Question], run_dir: Path) -> Path:
    p = run_dir / "questions.json"
    p.write_text(json.dumps([q.to_dict() for q in questions], ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def write_answer_template(questions: Sequence[Question], run_dir: Path) -> Path:
    """给「外接大脑」用的答案填空题。

    刻意做成纯映射（qid -> 空答案），因为这是各家 AI 最容易回对、也最容易被人手填的形状：
    用户把 questions.json 丢给任意 AI，拿到答案后照这个骨架填空/回贴即可。
    每道题的题型与题干写在 _hint 里（导入时会被忽略，不影响解析）。
    """
    tpl: Dict[str, Any] = {}
    for q in questions:
        if q.qtype == QType.MATCH:
            blank: Any = {Question.match_label(i): "" for i in range(len(q.match_items))}
        elif q.qtype == QType.MULTIPLE:
            blank = []
        else:
            blank = ""
        tpl[q.qid] = blank
    tpl["_hint"] = {
        "说明": "把每题的答案填进对应 qid；单选题填字母 A/B/C，多选题填 [\"A\",\"C\"]，"
                "填空题/简答题填文本，配伍题填 {\"1\":\"A\",\"2\":\"B\"}；判断题填 true/false 或选项原文。",
        "题型": {q.qid: q.qtype.value for q in questions},
        "题干": {q.qid: q.stem[:40] for q in questions},
    }
    p = run_dir / "answers.template.json"
    p.write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    md = run_dir / "answers.template.md"
    lines = ["# 答案回填表（把每题的答案写在 `答案：` 后面）", ""]
    for q in questions:
        lines.append(f"### {q.number}. [{q.qtype.value}] {q.stem}")
        for o in q.options:
            lines.append(f"  - {o.letter}. {o.text}")
        if q.qtype == QType.MATCH and q.match_items:
            lines.append(f"  - 备选：{' '.join(f'{o.letter}.{o.text}' for o in q.options)}")
            for i, it in enumerate(q.match_items):
                lines.append(f"  - {Question.match_label(i)}. {it}")
        lines.append("  **答案**：")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")
    return p


def save_answers(questions: Sequence[Question], answers: Sequence[Answer],
                 report: SolveReport, run_dir: Path, meta: Dict[str, Any]) -> Tuple[Path, Path]:
    amap = {a.qid: a for a in answers}
    rows = []
    for q in questions:
        a = amap.get(q.qid)
        rows.append({
            "number": q.number,
            "qid": q.qid,
            "type": q.qtype.value,
            "stem": q.stem,
            "options": [o.to_dict() for o in q.options],
            "answer": a.value if a else None,
            "answer_display": answer_to_display(a, q) if a else "",
            "confidence": a.confidence if a else 0.0,
            "source": a.source if a else "none",
            "note": a.note if a else None,
        })
    data = {
        "meta": meta,
        "report": report.as_dict(),
        "items": rows,
    }
    ap = run_dir / "answers.json"
    ap.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# {meta.get('course', '')} / {meta.get('homework', '')}", ""]
    lines.append(f"- 生成时间：{meta.get('time', '')}")
    lines.append(f"- 题目数：{report.total}（缓存 {report.from_cache} / 相似 {report.from_memory}"
                 f" / 页面答案 {report.from_page} / 模型 {report.from_llm}）")
    lines.append(f"- token 消耗：{report.total_tokens}（复用率 {report.reuse_ratio:.0%}）")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['number']}. [{r['type']}] {r['stem']}")
        for o in r["options"]:
            mark = "*" if str(r["answer"]).find(o["letter"]) >= 0 else " "
            lines.append(f"  {mark} {o['letter']}. {o['text']}")
        lines.append(f"  **答案**：{r['answer_display']}  "
                     f"（来源 {r['source']}，置信度 {r['confidence']:.2f}）")
        if r["note"]:
            lines.append(f"  > {r['note']}")
        lines.append("")
    mp = run_dir / "answers.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    return ap, mp


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    ok = True
    print(BANNER.format(ver=__version__))

    # 1) Python / Playwright
    print("[1/5] 运行环境")
    print(f"  Python      : {sys.version.split()[0]} ({sys.executable})")
    try:
        import playwright  # noqa: F401
        print("  Playwright  : 已安装")
    except ImportError:
        ok = False
        print("  Playwright  : 未安装  ->  python -m pip install playwright")

    browsers_ok = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            browsers_ok = bool(exe and Path(exe).exists())
            print(f"  浏览器内核  : {'已就绪' if browsers_ok else '缺失'}  {exe or ''}")
    except Exception as e:
        print(f"  浏览器内核  : 检查失败 ({e})")
    if not browsers_ok:
        print("              -> python -m playwright install chromium")

    # 2) 配置
    print("[2/5] 配置")
    print(f"  配置文件    : {cfg.config_path or '（未找到，使用内置默认值）'}")
    print(f"  项目根目录  : {cfg.root}")
    print(f"  浏览器档案  : {cfg.user_data_dir}"
          f"（{'已存在' if cfg.user_data_dir.exists() else '尚未创建'}）")
    print(f"  答案库      : {cfg.store_path}")
    print(f"  输出目录    : {cfg.output_dir}")

    # 3) 模型
    print("[3/5] 模型接口")
    print(f"  预设        : {cfg.llm.preset}")
    print(f"  base_url    : {cfg.llm.base_url}")
    print(f"  模型        : {cfg.llm.model}")
    # 只报告来源与长度，绝不回显 key 的任何片段
    if cfg.llm.api_key:
        key_state = f"已配置（config.json 的 llm.api_key，{len(cfg.llm.api_key)} 字符）"
    elif os.environ.get(cfg.llm.api_key_env or ""):
        key_state = f"已配置（环境变量 {cfg.llm.api_key_env}，{len(os.environ.get(cfg.llm.api_key_env) or '')} 字符）"
    else:
        key_state = f"未配置（可用环境变量 {cfg.llm.api_key_env}）"
    print(f"  API Key     : {key_state}")
    if args.check_llm:
        client = LLMClient.from_config(cfg)
        r = client.ping()
        print(f"  连通性测试  : {'OK' if r.get('ok') else '失败'}  {r.get('detail', '')}")
        if not r.get("ok"):
            ok = False
    else:
        print("  （加 --check-llm 做一次真实连通性测试，会消耗几十 token）")

    # 4) 答案库
    print("[4/5] 本地答案库")
    try:
        with AnswerStore(cfg.store_path if not cfg.store_is_memory else ":memory:",
                         cfg.store.similarity_threshold, cfg.store.persist) as st:
            s = st.stats()
        print(f"  题目数      : {s['questions']}   复用次数: {s['reuse_hits']}")
        print(f"  近 24h 新增 : {s['added_24h']}")
        print(f"  累计 token  : prompt {s['prompt_tokens']} / completion {s['completion_tokens']}"
              f"（{s['llm_requests']} 次请求）")
        if s["by_type"]:
            print(f"  题型分布    : {s['by_type']}")
    except Exception as e:
        ok = False
        print(f"  读取失败    : {e}")

    # 5) 环境变量
    print("[5/5] 可选环境变量")
    for k, desc in (("XX_API_KEY", "模型 API Key"),
                    ("XX_BASE_URL", "覆盖 base_url"),
                    ("XX_MODEL", "覆盖模型名"),
                    ("XX_USERNAME", "学习通账号（仅当开启自动填充时用）"),
                    ("XX_PASSWORD", "学习通密码")):
        print(f"  {k:<14}: {'已设置' if os.environ.get(k) else '未设置'}  ({desc})")

    print()
    print("自检结论：" + ("基本就绪" if ok else "存在待处理项，见上文"))
    return 0 if ok else 1


def _open_browser_session(cfg: Config, *, headless: Optional[bool] = None):
    from .browser import BrowserSession  # 延迟导入，doctor 不装 playwright 也能跑
    if headless is not None:
        cfg.browser.headless = headless
    cfg.ensure_dirs()
    return BrowserSession(cfg)


def cmd_login(args: argparse.Namespace, cfg: Config) -> int:
    if args.reset:
        from .browser import BrowserSession
        print(f"清除登录档案：{cfg.user_data_dir}")
        BrowserSession.reset_profile(cfg.user_data_dir)

    # --wait 覆盖配置里的等待秒数；退出时还原，避免污染同一进程后续命令
    old_wait = cfg.login.manual_login_timeout_s
    wait_s = int(getattr(args, "wait", None) or 0)
    if wait_s > 0:
        cfg.login.manual_login_timeout_s = wait_s
        print(f"本次等待窗口：{wait_s} 秒")
    try:
        with _open_browser_session(cfg, headless=False) as sess:
            page = ChaoxingPage(sess, cfg)
            page.ensure_login(interactive=True)
            print(f"\n登录态已保存到：{cfg.user_data_dir}")
            print("之后可以直接跑：python -m xxtutor answer -c <课程> -w <作业>")
    finally:
        cfg.login.manual_login_timeout_s = old_wait
    return 0


def cmd_qrlogin(args: argparse.Namespace, cfg: Config) -> int:
    """无头扫码登录：把登录页二维码存成 PNG，用户扫一下就登好了。"""
    from .qrlogin import qr_login

    return qr_login(
        cfg,
        wait_s=args.wait,
        refresh_s=args.refresh,
        out_dir=Path(args.out) if args.out else None,
        headless=not args.headed,
        log=print,
    )


def cmd_courses(args: argparse.Namespace, cfg: Config) -> int:
    with _open_browser_session(cfg) as sess:
        page = ChaoxingPage(sess, cfg)
        page.ensure_login(interactive=True)
        courses = page.list_courses()
        if not courses:
            print("没有读到任何课程，登录态可能失效，试试 python -m xxtutor login --reset")
            return 1
        print(f"\n共 {len(courses)} 门课程：")
        for i, c in enumerate(courses, 1):
            print(f"  {i:>3}. {c.name}")
        if args.out:
            Path(args.out).write_text(
                json.dumps([c.to_dict() for c in courses], ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\n已写入 {args.out}")
    return 0


def cmd_homework(args: argparse.Namespace, cfg: Config) -> int:
    with _open_browser_session(cfg) as sess:
        page = ChaoxingPage(sess, cfg)
        page.ensure_login(interactive=True)
        course = page.find_course(args.course)
        print(f"课程：{course.name}")
        page.open_course(course)
        page.open_homework_tab()
        items = page.list_homework()
        if not items:
            print("没有读到作业（可能在别的标签页里，例如「任务点/章节」）")
            return 1
        print(f"\n共 {len(items)} 条作业：")
        for i, it in enumerate(items, 1):
            flag = "已交" if it.done else "未交"
            print(f"  {i:>3}. [{flag}] {it.title}")
        if args.out:
            Path(args.out).write_text(
                json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\n已写入 {args.out}")
    return 0


# --------------------------------------------------------------------------- #

def load_questions_file(path: str | Path) -> List[Question]:
    """从 JSON 读题目（兼容本框架导出的 questions.json）。

    本框架自己写出的 ``questions.json`` 是**裸数组**，而 ``answers.json`` / 外部 AI
    回贴的常见形态是 ``{"items": [...]}``，两种都要认。
    """
    # utf-8-sig：外部 AI/记事本回贴的题库文件常带 BOM
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = (data.get("questions") or data.get("qs") or data.get("items")
                or data.get("list") or [])
    if not isinstance(data, list):
        raise ValueError(f"题目文件格式不认识：{path}（顶层应为数组或 {{'items': [...]}}）")
    out: List[Question] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        opts: List[Option] = []
        for k, o in enumerate(raw.get("options") or raw.get("o") or []):
            if isinstance(o, dict):
                opts.append(Option(letter=str(o.get("letter") or chr(65 + k)),
                                   text=str(o.get("text") or "")))
            else:
                opts.append(Option(letter=chr(65 + k), text=str(o)))
        qtype = QType.from_raw(raw.get("qtype") or raw.get("t"))
        if not opts and qtype == QType.OTHER:
            qtype = QType.SHORT
        match_items = [str(x) for x in (raw.get("match_items") or raw.get("m") or [])]
        match_options = [str(x) for x in (raw.get("match_options") or [])]
        if qtype == QType.MATCH:
            if not match_options:
                match_options = [o.text for o in opts]
            if not match_items and raw.get("stem"):
                match_items = [str(raw["stem"])[:60]]
        out.append(Question(
            qid=str(raw.get("qid") or raw.get("i") or f"q{i + 1}"),
            qtype=qtype,
            stem=str(raw.get("stem") or raw.get("q") or "").strip(),
            options=opts,
            number=str(i + 1),
            locator=raw.get("locator") or {"index": i},
            match_items=match_items if qtype == QType.MATCH else [],
            match_options=match_options if qtype == QType.MATCH else [],
        ))
    return out


def cmd_solve(args: argparse.Namespace, cfg: Config) -> int:
    """不碰浏览器，纯粹跑求解器（用来调 prompt / 验证答案库）。"""
    questions = load_questions_file(args.file)
    if not questions:
        print("题目文件里没有题目")
        return 1
    if args.limit:
        questions = questions[: args.limit]
    print(f"载入 {len(questions)} 道题，开始求解…")

    client = LLMClient.from_config(cfg)
    store = AnswerStore(cfg.store_path if not cfg.store_is_memory else ":memory:",
                        cfg.store.similarity_threshold, cfg.store.persist)
    try:
        solver = Solver(cfg, client, store)
        course_id = args.course_id or "offline"
        answers, report = solver.solve(questions, course_id=course_id,
                                       assignment=str(args.file), allow_llm=not args.no_llm)
        run_dir = new_run_dir(cfg, "solve")
        save_questions(questions, run_dir)
        ap, mp = save_answers(questions, answers, report, run_dir,
                              {"course": course_id, "homework": str(args.file),
                               "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        print()
        print(report.summary_line())
        print(f"答案：{ap}")
        print(f"可读版：{mp}")
        if args.json:
            print(json.dumps([a.to_dict() for a in answers], ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def cmd_bank(args: argparse.Namespace, cfg: Config) -> int:
    store = AnswerStore(cfg.store_path if not cfg.store_is_memory else ":memory:",
                        cfg.store.similarity_threshold, cfg.store.persist)
    try:
        if args.export:
            p = store.export_json(args.export)
            print(f"已导出 {p}")
        s = store.stats()
        print(json.dumps(s, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def cmd_dump_dom(args: argparse.Namespace, cfg: Config) -> int:
    """进入指定作业/URL，保存 DOM 快照与调试信息（选择器失效时排查用）。"""
    with _open_browser_session(cfg) as sess:
        page = ChaoxingPage(sess, cfg)
        if args.url:
            page.ensure_login(interactive=False)
            sess.goto(args.url)
            sess.page.wait_for_timeout(3000)
        else:
            page.ensure_login(interactive=True)
            course = page.find_course(args.course)
            page.open_course(course)
            page.open_homework_tab()
            item = page.find_homework(args.homework)
            page.open_homework(item)

        run_dir = new_run_dir(cfg, "dump")
        html = sess.dump_html(run_dir / "page.html")
        shot = sess.screenshot(run_dir / "page.png")
        info = page.page_debug()
        (run_dir / "debug.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            qs = page.extract_questions()
            save_questions(qs, run_dir)
            print(f"抓到 {len(qs)} 道题")
            for q in qs[:5]:
                print(f"  - [{q.qtype.value}] {q.stem[:60]} 选项{len(q.options)}")
        except PageError as e:
            print(f"抓题失败：{e}")
        print(f"\nDOM : {html}")
        print(f"截图: {shot}")
        print(f"调试: {run_dir / 'debug.json'}")
    return 0


# --------------------------------------------------------------------------- #
# 核心：answer
# --------------------------------------------------------------------------- #

def _interactive_review(questions: Sequence[Question], answers: Dict[str, Answer],
                        store: AnswerStore, course_id: str, edit: bool) -> None:
    """逐题过一遍答案，可以现场改（改完写入答案库）。"""
    print(f"\n共 {len(questions)} 题（仅修改；直接回车跳过）：")
    for q in questions:
        a = answers.get(q.qid)
        print("\n" + "-" * 66)
        print(f"{q.number}. [{q.qtype.value}] {q.stem}")
        if q.qtype == QType.MATCH and q.match_items:
            for k, item in enumerate(q.match_items):
                print(f"    {Question.match_label(k)}. {item}")
            print("    备选：" + " ".join(f"{o.letter}.{o.text}" for o in q.options))
        else:
            for o in q.options:
                print(f"    {o.letter}. {o.text}")
        if a:
            shown = answer_to_display(a, q)
            print(f"  -> 当前答案：{shown!r}（来源 {a.source}，置信度 {a.confidence:.2f}）")
        else:
            print("  -> 当前答案：<缺失>")
        if not edit:
            continue
        try:
            hint = "（配伍题写 1:A,2:B）" if q.qtype == QType.MATCH else ""
            reply = input(f"  新答案{hint}（回车=保留，s=跳过后面所有）> ").strip()
        except EOFError:
            break
        if reply.lower() == "s":
            break
        if not reply:
            continue
        value, pairs = _parse_manual(reply, q)
        answers[q.qid] = Answer(qid=q.qid, value=value, pairs=pairs,
                                confidence=1.0, source="manual")
        try:
            store.correct(q, answers[q.qid], course_id=course_id,
                          pairs=pairs)
            print("  已更新并写入答案库")
        except Exception as e:
            print(f"  写入答案库失败：{e}")


def _parse_manual(text: str, q: Question) -> Tuple[str, Optional[Dict[str, str]]]:
    """解析人工输入，返回 `(value, pairs)`；pairs 只有配伍题才会非空。"""
    if q.qtype == QType.MATCH:
        pairs = parse_pairs(text, q.match_items or [], [o.letter for o in q.options])
        # 人工输入解析不出来就原样留着，至少让人看到自己写了什么
        value = ",".join(f"{k}:{pairs[k]}" for k in sorted(pairs, key=lambda x: int(x))
                         ) if pairs else text.strip()
        return value, (pairs or None)
    if q.qtype == QType.MULTIPLE:
        return [c.upper() for c in text.replace(",", "").replace(" ", "").upper()
                if c.isalpha()], None
    if q.qtype in (QType.SINGLE, QType.JUDGE):
        return (text.strip().upper()[:1] if text.strip() else ""), None
    if q.qtype == QType.FILL and "|" in text:
        return [p.strip() for p in text.split("|")], None
    return text.strip(), None


def _has_api_key(cfg: Config, client: Any = None) -> bool:
    """是否具备真实调用模型的条件（mock 客户端永远算「有」）。"""
    if client is not None and getattr(client, "is_mock", False):
        return True
    return bool(cfg.llm.api_key) or bool(os.environ.get(cfg.llm.api_key_env or ""))


def _load_external_answers(args: argparse.Namespace, questions: Sequence[Question]
                           ) -> Dict[str, Answer]:
    """导入「外接大脑」给的答案（--answers / --answers-from）。

    支持一次给多个文件，后面的覆盖前面的；解析不出来的键会打印出来，
    否则用户会以为答案生效了、实际却被跳过。
    """
    merged: Dict[str, Answer] = {}
    sources = [str(p) for p in (getattr(args, "answers", None) or [])]
    if getattr(args, "answers_from", None):
        # --answers-from：读某一个已完成运行的 answers.json（或 answers.md）
        from_dir = Path(args.from_dir).expanduser() if getattr(args, "from_dir", None) \
            else (Path(args.answers_from).expanduser().parent)
        cand = from_dir / "answers.json"
        sources.append(str(cand if cand.exists() else from_dir / "answers.md"))
    for src in sources:
        try:
            got, skipped = import_answers(src, questions)
        except AnswersFileError as e:
            LOG.error("%s", e)
            continue
        print(f"外部答案文件 {src}：命中 {len(got)}/{len(questions)} 题")
        if skipped:
            LOG.warning("有 %d 个答案键没对上题目（已忽略）：%s",
                        len(skipped), ", ".join(skipped[:10]))
        merged.update(got)
    return merged


def show_missing_api_key(cfg: Config, why: str = "") -> None:
    """统一的「没有 API Key」提示。"""
    LOG.warning("缺少 API Key：设置环境变量 %s，或把 key 填进 config.json 的 llm.api_key",
                cfg.llm.api_key_env)
    if why:
        LOG.warning("%s（这些题会标成未作答）", why)


def read_work_url(run_dir: Path) -> str:
    """从某次运行目录里找回答题页 URL（给 `answer --reopen` 提速用）。

    优先 fill-report.json / run.json 里记下的 work_url（就是真实答题页），
    其次 answers.md / page.html 里能扫到的 doWork 链接。
    传进来的如果本身就是 http 链接，直接用它。
    """
    raw = str(run_dir)
    if raw.startswith("http"):
        return raw
    for name, key in (("fill-report.json", "work_url"),
                      ("run.json", "work_url"),
                      ("answers.json", "work_url")):
        f = run_dir / name
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            v = str(data.get(key) or "").strip()
            if v.startswith("http"):
                return v
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else None
            if meta:
                v = str(meta.get(key) or meta.get("page_url") or "").strip()
                if v.startswith("http"):
                    return v
    for name in ("answers.md", "answers.template.md", "page.html"):
        f = run_dir / name
        if not f.exists():
            continue
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:200000]
        except Exception:
            continue
        m = re.search(r"(https?://[^\s\"'<>]*?/mooc-ans/mooc2/work/[^\s\"'<>]+)", head)
        if m:
            return m.group(1).replace("&amp;", "&")
    return ""


def cmd_answer(args: argparse.Namespace, cfg: Config) -> int:
    # 命令行覆盖配置
    if args.base_url:
        cfg.llm.base_url = args.base_url
        cfg.llm.preset = "custom"
    if args.model:
        cfg.llm.model = args.model
        cfg.llm.preset = "custom"
    if args.max_tokens:
        cfg.llm.max_tokens_per_assignment = args.max_tokens
    if args.batch_size:
        cfg.llm.batch_size = args.batch_size
    if args.headless is not None:
        cfg.browser.headless = args.headless
    if args.submit:
        cfg.answer.submit = True
    if args.no_confirm:
        cfg.answer.confirm_before_submit = False

    client = LLMClient.from_config(cfg)
    # 缺少 API Key **不直接退出**：复习页的标准答案、本地答案库和相似题复用都是零 token 的，
    # 很多作业根本不发模型请求。真正需要模型时才在求解阶段提示（见 _has_api_key）。
    if not _has_api_key(cfg, client) and not args.solve_only:
        LOG.warning("未检测到 API Key（环境变量 %s / config.json 的 llm.api_key）——"
                    "本次只能用「页面答案 + 本地题库」作答", cfg.llm.api_key_env)

    store = AnswerStore(cfg.store_path if not cfg.store_is_memory else ":memory:",
                        cfg.store.similarity_threshold, cfg.store.persist)
    run_dir = new_run_dir(cfg, "answer")
    meta: Dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "course": args.course or "",
        "homework": args.homework or "",
        "model": cfg.llm.model,
        "base_url": cfg.llm.base_url,
    }
    questions: List[Question] = []
    sess = None
    page: Optional[ChaoxingPage] = None
    try:
        # ---------------- 抓题 ---------------- #
        # 能直接定位答题页的两种来源：--reopen（目录或 URL）、--from-dir 目录里
        # 记过的 work_url。提前算出来，好让 --from-dir 单独用也能自动回填。
        reopen_dir = Path(args.reopen).expanduser() if args.reopen else None
        work_url = read_work_url(reopen_dir) if reopen_dir else ""
        if not work_url and args.from_dir:
            work_url = read_work_url(Path(args.from_dir).expanduser())
            if work_url:
                reopen_dir = Path(args.from_dir).expanduser()
        if args.from_dir:
            src = Path(args.from_dir).expanduser()
            try:
                questions = load_questions_from_dir(src)
            except AnswersFileError as e:
                LOG.error("%s", e)
                return 2
            if args.limit:
                questions = questions[: args.limit]
            print(f"复用已抓题目 {len(questions)} 道：{src}")
            if (not args.course and not args.to_url and not work_url
                    and not args.no_fill):
                raise PageError(
                    "--from-dir 要回填就得同时给 -c/--course（或 --to-url/--reopen）"
                    "来重新打开那个作业页；只想离线攒答案库/用外部 AI 答案，"
                    "就加 --no-fill")
            save_questions(questions, run_dir)
        elif args.from_file:
            questions = load_questions_file(args.from_file)
            print(f"从文件载入 {len(questions)} 道题：{args.from_file}")
        else:
            sess = _open_browser_session(cfg).start()
            page = ChaoxingPage(sess, cfg)
            page.ensure_login(interactive=True)

        # ---- 抓题结束（--from-dir / --from-file 两条分支不开浏览器） ----
        # 只给 `-c 课程` 时浏览器还没开，这里补一次（--reopen 与 `-c` 共用一个会话）
        # 只给 -c / --reopen 时，浏览器还没开，这里补一次（与主分支共用一套逻辑）
        if (page is None and not args.no_fill
                and (args.course or work_url)):
            sess = _open_browser_session(cfg).start()
            page = ChaoxingPage(sess, cfg)
            page.ensure_login(interactive=True)

        # 提速路径：直接开上次的答题页，省掉「找课程 → 开课程 → 找作业」三步。
        # `--reopen` 可以给目录（读里面的 run.json / fill-report.json），也可以
        # 直接给一条答题页 http URL；没给 --reopen 时，若 --from-dir 目录里记过
        # work_url，也自动用它（复用题目自然要填回同一页）。
        if page is not None:
            # `reopened` 而不是 `work_url`：旧 URL 上的 enc/standardEnc 是一次性
            # 令牌，绝大多数情况服务端只回一张「作答状态异常！」的提示页。
            # 所以重开**必须先验证**，验证不过就照样去走课程/作业查找——
            # 不能因为「给了 URL」就跳过导航，那会直接抓不到题。
            reopened = False
            if work_url:
                sess.goto(work_url)
                st = page.answer_page_state(timeout_s=5.0)
                if st["ready"]:
                    page.enter_answer_page_if_needed(timeout_s=3.0)
                    reopened = True
                    if reopen_dir is not None:
                        meta["reopened_from"] = str(reopen_dir)
                    print(f"直接重开答题页（跳过课程/作业查找）：{sess.page.url[:110]}")
                else:
                    why = st.get("trouble") or "页面上没有题目"
                    tip = ("，退回按课程/作业名查找"
                           if args.course else
                           "；没给 -c/--course，无法自动退回查找，"
                           "请补上 `-c 课程名 -w 作业名`")
                    print(f"⚠ --reopen 打开的页面不可用（{why}）{tip}")

            if not reopened and reopen_dir is not None and not work_url:
                print(f"⚠ {reopen_dir} 里没有记下答题页 URL，退回按课程/作业名查找")

            if not reopened and args.course:
                course = page.find_course(args.course)
                meta["course"] = course.name
                print(f"课程：{course.name}")
                page.open_course(course)
                # 记下课程页 URL：作业列表 iframe 偶发「无权限的操作！」时，
                # 靠重开这一页拿新的 enc 令牌。
                course_url = sess.page.url
                if args.homework:
                    page.open_homework_tab()
                    # find_homework 内部会自己列一次（并把「无权限的操作！」重试掉），
                    # 这里不再预先 list 一轮——那会白白多花十几秒。
                    item = page.find_homework(
                        args.homework,
                        reload_course=lambda: sess.goto(course_url))
                    meta["homework"] = item.title
                    print(f"作业：{item.title}（{item.status or '状态未知'}）")
                    page.open_homework(item)
                # 没给 -w 时，说明已经在答题页里（比如用 --url 指定了 URL）
            if args.url:
                sess.goto(args.url)
                sess.page.wait_for_timeout(3000)
            # 一进答题页就记下 URL：哪怕后面抓题/回填失败，
            # 下次也能用 `--reopen <这个目录>` 秒开这一页。
            meta["work_url"] = sess.page.url
            save_run_meta(meta, run_dir)
            questions = page.extract_questions(dump_dom=run_dir / "page.html")
            if args.limit:
                questions = questions[: args.limit]
            save_questions(questions, run_dir)
            print(f"\n抓到 {len(questions)} 道题：")
            for q in questions[:10]:
                print(f"  {q.number}. [{q.qtype.value}] {q.stem[:52]}"
                      f"{'  (' + str(len(q.options)) + ' 选项)' if q.options else ''}")
            if len(questions) > 10:
                print(f"  … 其余 {len(questions) - 10} 题见 {run_dir / 'questions.json'}")

        # `--to-url` 是「回填目标页」。用 --from-dir/--from-file 复用题目且没有
        # 课程/答题页可开时，这里补一次会话——否则 --to-url 会被完全忽略。
        if page is None and args.to_url and not args.no_fill:
            LOG.info("按 --to-url 打开回填页面")
            sess = _open_browser_session(cfg).start()
            page = ChaoxingPage(sess, cfg)
            sess.goto(args.to_url)
            sess.page.wait_for_timeout(2000)

        if not questions:
            LOG.error("没有题目可处理")
            return 1

        # 给「外接大脑」用的填空题（无论走哪条路都写一份，方便用户丢给 AI）
        tpl = write_answer_template(questions, run_dir)

        # ---------------- 求解 ---------------- #
        solver = Solver(cfg, client, store)
        course_id = args.course_id or meta.get("course") or ""
        ask_llm = not args.no_llm
        if args.interactive:
            # 交互模式：先只吃缓存，剩下的你手填，最后可选再问模型
            pre_answers, pre_rep = solver.solve(
                questions, course_id=course_id, assignment=meta.get("homework"),
                allow_llm=False)
            amap = {a.qid: a for a in pre_answers}
            print(f"\n答案库命中 {len(pre_answers)}/{len(questions)} 题"
                  f"（命中 {pre_rep.summary_line()}）")
            _interactive_review(questions, amap, store, course_id, edit=True)
            missing = [q for q in questions if q.qid not in amap]
            if missing and ask_llm:
                if not _has_api_key(cfg, client):
                    show_missing_api_key(cfg, f"还有 {len(missing)} 题需要模型作答")
                else:
                    print(f"\n还有 {len(missing)} 题没有答案，"
                          f"按回车交给模型作答，输入 n 放弃：")
                    try:
                        go = input("> ").strip().lower() != "n"
                    except EOFError:
                        go = False
                    if go:
                        more, rep2 = solver.solve(missing, course_id=course_id,
                                                  assignment=meta.get("homework"))
                        for a in more:
                            amap[a.qid] = a
                        pre_rep.from_llm += rep2.from_llm
                        pre_rep.requests += rep2.requests
                        pre_rep.prompt_tokens += rep2.prompt_tokens
                        pre_rep.completion_tokens += rep2.completion_tokens
                        pre_rep.failed += rep2.failed
                        pre_rep.skipped += rep2.skipped
            answers = [amap[q.qid] for q in questions if q.qid in amap]
            report = pre_rep
        elif ask_llm and not _has_api_key(cfg, client):
            LOG.warning("没有配 API Key，本次只用「页面答案 + 本地题库」作答（不发模型请求）")
            show_missing_api_key(cfg, "需要模型作答的题会被跳过")
            answers, report = solver.solve(
                questions, course_id=course_id, assignment=meta.get("homework"),
                allow_llm=False)
        else:
            answers, report = solver.solve(
                questions, course_id=course_id, assignment=meta.get("homework"),
                allow_llm=ask_llm)
        # ---------------- 外接大脑（可选） ---------------- #
        # 用户可以把 questions.json 丢给任意 AI，拿回答案文件再喂进来：
        # 这条路上框架一次模型请求都不发（llm.py 完全不参与）。
        ext = _load_external_answers(args, questions)
        if ext:
            answers = merge_answer_maps(answers, ext, questions, report)
            print(f"已合并外部答案 {len(ext)} 题"
                  f"（其中覆盖其它来源 {getattr(report, 'replaced_from_file', 0)} 题）")

        ap, mp = save_answers(questions, answers, report, run_dir, meta)
        print()
        print(report.summary_line())
        if report.failures:
            print("有题目未拿到答案：")
            for f in report.failures[:10]:
                print(f"  - {f['qid']} {f['stem']}  ({f['reason'][:60]})")

        # 离线模式（--from-file / --from-dir）没有浏览器，到这儿就结束
        if page is None or sess is None:
            print()
            print("离线模式：没有打开浏览器，因此不做回填。")
            print(f"题目与答案已落盘：{run_dir}")
            print(f"  题目（可丢给任意 AI）：{run_dir / 'questions.json'}")
            print(f"  答案回填表：{tpl}")
            print(f"  答案备份：{ap}")
            print(f"  可读版：{mp}")
            print("要把答案填回页面，请加 -c <课程> -w <作业>（或 --to-url <答题页>）"
                  "并带上 --answers <答案文件> 重跑；")
            print("想直接攒进答案库不再回填，可以省掉浏览器（--no-fill，默认就是这样）。")
            return 0

        # ---------------- 回填 ---------------- #
        # 先判断这是「答题页」还是「已提交的复习页」。
        # 复习页（mooc-ans/mooc2/work/view）把标准答案印在页面上，
        # 但**没有任何 input/textarea**，往上面填答案必然全失败
        # （实测会打印「一个选项都没点中」的报错），所以直接跳过回填。
        fillable = sum(1 for q in questions if q.has_input)
        if fillable == 0:
            print()
            print("这是「已提交答卷」的复习页：页面上没有可作答的控件，跳过回填。")
            print(f"页面印着的标准答案已随抓题一起入库（{len(questions)} 题，零 token）。")
            print(f"答案备份：{ap}")
            print(f"可读版：{mp}")
            (run_dir / "readme.txt").write_text(
                "本次打开的是已提交答卷的复习页（无输入控件），只采集了页面标准答案，\n"
                "没有向页面填入任何内容，也没有提交。\n", encoding="utf-8")
            return 0

        target = args.to_url or sess.page.url
        if args.new_tab and args.to_url and sess.page.url != target:
            # 注意：必须把新标签页设为当前页，否则 apply_answers 会填到旧页上。
            LOG.info("在新标签页打开答题页回填")
            sess.goto_new_tab(target)
            sess.page.wait_for_timeout(2000)

        fill_stats = page.apply_answers(questions, answers, dry_run=args.dry_run)
        # 记下答题页 URL：下次可以 `--reopen <这个目录>` 直接开这一页，
        # 省掉「找课程 → 开课程 → 找作业」三步（每步都好几次网络往返）。
        try:
            fill_stats["work_url"] = sess.page.url
        except Exception:
            pass
        (run_dir / "fill-report.json").write_text(
            json.dumps(fill_stats, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.dry_run:
            print("\n--dry-run：没有往页面里填任何内容。")
        else:
            page.save_progress()
            print(f"\n答案已填入页面并尝试保存（未提交）。答案备份：{ap}")
            print(f"可读版：{mp}")

            # ---------------- 提交（默认关闭） ---------------- #
            if cfg.answer.submit:
                done, msg = page.submit(confirm=cfg.answer.confirm_before_submit)
                print(("已提交：" if done else "未提交：") + msg)
                if done:
                    (run_dir / "submitted.txt").write_text(
                        f"{datetime.now().isoformat()} {msg}\n", encoding="utf-8")
            else:
                print("\n提交开关未打开（默认就是这样）。")
                print("请你在浏览器里核对后手动点提交；")
                print("或者确认无误后加 --submit 重跑本命令。")
                if not cfg.browser.headless:
                    print("\n浏览器会保持打开，方便你核对。按回车关闭浏览器离开…")
                    try:
                        input()
                    except EOFError:
                        pass
        return 0
    except (PageError, LLMError) as e:
        LOG.error("%s", e)
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    finally:
        store.close()
        if questions:
            print(f"\n本次运行产物目录：{run_dir}")


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xxtutor",
        description="学习通作业自动答题框架（抓题 -> AI 作答 -> 回填，默认不提交）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-V", "--version", action="version", version=f"xxtutor {__version__}")
    p.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    p.add_argument("--log-level", default=None, help="日志级别 DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--log-file", default=None, help="同时写入日志文件")
    p.add_argument("--root", default=None, help="项目根目录（默认当前目录）")
    p.add_argument("--preset", default=None,
                   help="切换模型预设（写在子命令之前）：deepseek/openai/dashscope/moonshot/zhipu/ollama/mock")

    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--check-llm", action="store_true", help="做一次真实的模型连通性测试")
    d.set_defaults(func=cmd_doctor)

    l = sub.add_parser("login", help="打开浏览器登录一次（登录态长期保存）")
    l.add_argument("--reset", action="store_true", help="先清空既有登录档案")
    l.add_argument("--wait", type=int, default=None,
                   help="在浏览器窗口里等登录的秒数（默认用配置的 login.manual_login_timeout_s）")
    l.set_defaults(func=cmd_login)

    q = sub.add_parser("qrlogin", help="扫码登录：抓二维码存成 PNG，你扫一下即可")
    q.add_argument("--wait", type=int, default=300, help="等待扫码的秒数（默认 300）")
    q.add_argument("--refresh", type=int, default=60, help="二维码过期前重新获取的秒数（默认 60）")
    q.add_argument("--out", default=None, help="二维码 PNG 的输出目录")
    q.add_argument("--headed", action="store_true",
                   help="开真实窗口扫码（无头模式下服务端可能判二维码失效，扫不动就用这个）")
    q.set_defaults(func=cmd_qrlogin)

    c = sub.add_parser("courses", help="列出所有课程")
    c.add_argument("--out", default=None, help="把课程列表写入 JSON")
    c.set_defaults(func=cmd_courses)

    h = sub.add_parser("homework", help="列出某门课的作业")
    h.add_argument("-c", "--course", required=True, help="课程名关键字")
    h.add_argument("--out", default=None, help="把作业列表写入 JSON")
    h.set_defaults(func=cmd_homework)

    a = sub.add_parser("answer", help="主流程：抓题 -> AI 作答 -> 回填（默认不提交）")
    a.add_argument("-c", "--course", default=None, help="课程名关键字（不填则用 --url）")
    a.add_argument("-w", "--homework", default=None, help="作业名关键字")
    a.add_argument("--url", default=None, help="直接跳到这个答题页 URL")
    a.add_argument("--to-url", default=None, help="回填目标 URL（默认当前页面）")
    a.add_argument("--reopen", default=None, metavar="RUN_DIR",
                   help="提速：直接用上次运行留下的答题页 URL 打开作业，"
                        "跳过「找课程 → 开课程 → 找作业」三步（配 --from-dir 最省时）")
    a.add_argument("--new-tab", action="store_true", help="在新标签页打开回填目标")
    a.add_argument("--course-id", default=None, help="写入答案库时用的课程标识")
    a.add_argument("--from-file", default=None, help="跳过浏览器，直接从 JSON 读题目")
    a.add_argument("--from-dir", default=None,
                   help="复用某次运行的题目（answers.template.json / questions.json 所在目录）")
    a.add_argument("--answers", action="append", default=None, metavar="FILE",
                   help="外部 AI 给的答案文件（可重复；支持 json 与 answers.md）")
    a.add_argument("--answers-from", default=None, metavar="RUN_DIR",
                   help="读某次已完成运行目录里的 answers.json 作为答案来源")
    a.add_argument("--limit", type=int, default=0, help="只处理前 N 题（调试用）")
    a.add_argument("--dry-run", action="store_true", help="只抓题+出答案，不往页面填")
    a.add_argument("--no-fill", action="store_true",
                   help="完全不打开浏览器回填（配合 --from-file/--from-dir 离线用）")
    a.add_argument("--submit", action="store_true", help="填完后提交（默认不提交）")
    a.add_argument("--no-confirm", action="store_true", help="提交前不再人工确认")
    a.add_argument("--interactive", "-i", action="store_true", help="逐题人工核对/修正")
    a.add_argument("--no-llm", action="store_true", help="只用本地答案库，不调用模型")
    a.add_argument("--headless", dest="headless", action="store_true", default=None,
                   help="无界面运行（登录态有效时可用）")
    a.add_argument("--headed", dest="headless", action="store_false",
                   help="强制有界面运行")
    a.add_argument("--base-url", default=None, help="覆盖模型 base_url")
    a.add_argument("--model", default=None, help="覆盖模型名")
    a.add_argument("--max-tokens", type=int, default=None, help="本次作业 token 上限")
    a.add_argument("--batch-size", type=int, default=None, help="每次请求最多几道题")
    a.add_argument("--solve-only", action="store_true", help="即使没有 api key 也继续（走缓存）")
    a.set_defaults(func=cmd_answer)

    s = sub.add_parser("solve", help="离线跑求解器（不碰浏览器）")
    s.add_argument("-f", "--file", required=True, help="题目 JSON 文件")
    s.add_argument("--course-id", default=None)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--no-llm", action="store_true", help="只用本地答案库")
    s.add_argument("--json", action="store_true", help="把答案打到标准输出")
    s.set_defaults(func=cmd_solve)

    b = sub.add_parser("bank", help="查看/导出本地答案库")
    b.add_argument("--stats", action="store_true", help="显示统计（默认动作）")
    b.add_argument("--export", default=None, help="导出为 JSON")
    b.set_defaults(func=cmd_bank)

    dd = sub.add_parser("dump-dom", help="保存答题页 DOM/截图，用于排查选择器")
    dd.add_argument("--course", default=None)
    dd.add_argument("--homework", default=None)
    dd.add_argument("--url", default=None)
    dd.set_defaults(func=cmd_dump_dom)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path.cwd()
    try:
        cfg = load_config(args.config, root=root, strict=False)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    setup_logging(args.log_level or cfg.output.log_level,
                  Path(args.log_file) if args.log_file else None)
    # 命令行 --preset 优先级最高（子命令也带 --preset，取先出现的那个）
    preset = getattr(args, "preset", None) or cfg.llm.preset
    if preset and preset != cfg.llm.preset:
        cfg.llm.preset = preset
        LOG.info("按命令行参数切换模型预设 -> %s", preset)
    elif preset:
        cfg.llm.preset = preset
    apply_preset(cfg)

    return int(args.func(args, cfg) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
