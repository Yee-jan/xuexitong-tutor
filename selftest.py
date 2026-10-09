"""离线自测：不碰浏览器、不联网，验证「解析 -> 求解 -> 缓存复用 -> 落库 -> 导出」全链路。

跑法：
    python selftest.py

它同时充当 mock 模型，用来验证请求体拼装、答案解析、答案库命中、token 统计。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from xxtutor.config import Config, load_config          # noqa: E402
from xxtutor.llm import LLMClient, extract_json          # noqa: E402
from xxtutor.solver import Solver, SYSTEM_PROMPT         # noqa: E402
from xxtutor.store import AnswerStore                    # noqa: E402
from xxtutor.types import (Answer, Option, QType, Question,  # noqa: E402
                           fingerprint, jaccard, match_display, match_items_by_text,
                           normalize_text, parse_letters, parse_pairs,
                           parse_review_answer, shingles)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))


def banner(t: str) -> None:
    print(f"\n=== {t} ===")


# --------------------------------------------------------------------------- #
def sample_questions() -> list[Question]:
    return [
        Question(qid="q1", qtype=QType.SINGLE, number="1",
                 stem="1. 计算机中数据的最小单位是（ ）。",
                 options=[Option("A", "字节"), Option("B", "位"), Option("C", "字"), Option("D", "块")]),
        Question(qid="q2", qtype=QType.MULTIPLE, number="2",
                 stem="2. 下列属于操作系统的是（ ）。",
                 options=[Option("A", "Windows"), Option("B", "Linux"), Option("C", "Word"), Option("D", "macOS")]),
        Question(qid="q3", qtype=QType.JUDGE, number="3",
                 stem="3. TCP 是面向连接的可靠传输协议。",
                 options=[Option("T", "正确"), Option("F", "错误")]),
        Question(qid="q4", qtype=QType.FILL, number="4",
                 stem="4. 十进制数 10 的二进制表示是______。", options=[]),
        Question(qid="q5", qtype=QType.SHORT, number="5",
                 stem="5. 简述进程与线程的区别。", options=[]),
    ]


def test_types() -> None:
    banner("types 工具函数")
    check("normalize_text 去空白/大小写", normalize_text(" A B ") == "ab")
    check("normalize_text 去题号", normalize_text("1、下列", strip_number=True) == "下列")
    fp1 = fingerprint("hello world", "A|B")
    check("fingerprint 稳定", fp1 == fingerprint("hello world", "A|B") and len(fp1) == 16)
    check("fingerprint 对差异敏感", fp1 != fingerprint("hello worlds", "A|B"))
    s1, s2 = shingles("这是一个用于测试相似度的题目内容"), shingles("这是一个用于测试相似度的题目内容啊")
    check("jaccard 相似度接近 1", jaccard(s1, s2) > 0.8, f"{jaccard(s1, s2):.3f}")
    opts = [Option("A", "字节"), Option("B", "位"), Option("C", "字"), Option("D", "块")]
    check('parse_letters "AC"', parse_letters("AC", opts) == ["A", "C"])
    check("parse_letters list", parse_letters(["A", "c"], opts) == ["A", "C"])
    check("parse_letters 文本", parse_letters("位", opts) == ["B"])
    check("parse_letters 判断题", parse_letters(True, [Option("T", "正确"), Option("F", "错误")]) == ["T"])
    check("QType.from_raw 中文", QType.from_raw("多选题") == QType.MULTIPLE)
    check("QType.from_raw 数字", QType.from_raw("3") == QType.JUDGE)
    # 医学题库：A1/A2/A3 型单选、B 型配伍、X 型多选
    check("QType.from_raw A1 型单选", QType.from_raw("A1型题") == QType.SINGLE)
    check("QType.from_raw A3 型单选", QType.from_raw("A3型题") == QType.SINGLE)
    check("QType.from_raw X 型多选", QType.from_raw("X型题") == QType.MULTIPLE)
    check("QType.from_raw B 型配伍", QType.from_raw("B型题") == QType.MATCH)
    check("QType.from_raw B1 型配伍", QType.from_raw("B1型题") == QType.MATCH)
    check("QType.from_raw 配伍二字", QType.from_raw("配伍选择题") == QType.MATCH)
    check("QType.from_raw 数字 6", QType.from_raw("6") == QType.MATCH)
    check("QType.from_raw 数字 7", QType.from_raw("7") == QType.MATCH)
    check("QType.MATCH 需要选项", QType.MATCH.needs_options)
    # 配伍题配对解析
    mq = Question(qid="m1", qtype=QType.MATCH, stem="配伍题", number="1",
                  options=[Option("A", "入睡困难"), Option("B", "体温升高"), Option("C", "低血压")],
                  match_options=["入睡困难", "体温升高", "低血压"],
                  match_items=["1. 失眠", "2. 发热"])
    check("match_label 1-based", Question.match_label(0) == "1" and Question.match_label(2) == "3")
    check('parse_pairs 紧凑串', parse_pairs("1:A,2:B", mq.match_items,
                                            [o.letter for o in mq.options]) == {"1": "A", "2": "B"})
    check('parse_pairs "B,A" 顺序回填', parse_pairs("B,A", mq.match_items,
                                                    [o.letter for o in mq.options]) == {"1": "B", "2": "A"})
    check("parse_pairs dict 1-based 数字",
          parse_pairs({"1": 2, "2": 1}, mq.match_items, ["A", "B"]) == {"1": "B", "2": "A"})
    check("parse_pairs 中文顿号", parse_pairs("1：A、2：B", mq.match_items,
                                              [o.letter for o in mq.options]) == {"1": "A", "2": "B"})
    check("parse_pairs 乱输入返回空", parse_pairs("不知道", mq.match_items, ["A", "B"]) == {})
    local = match_items_by_text(["失眠", "发热"], ["入睡困难", "体温升高", "低血压"])
    check("本地相似度配出 1:A 2:B", local == {"1": "A", "2": "B"}, str(local))
    check("配伍题展示串", "A" in match_display({"1": "A"}, mq.match_items, mq.options))
    # 复习页印出来的标准答案（真实页面写法 "(1)D (2)A (3)C"）。
    # 注意这里用 4 个备选（A-D）：真实 B 型题就是「一组共享备选 + 若干小题」，
    # 备选数通常多于小题数，'D' 必须是合法备选字母。
    ropts = [Option("A", "甲"), Option("B", "乙"), Option("C", "丙"), Option("D", "丁")]
    ra = parse_review_answer("(1)D (2)A (3)C", QType.MATCH, ropts,
                             ["胆总管结石", "急性胆囊炎", "慢性胆囊炎"])
    check("复习页配伍答案 1:D 2:A 3:C", ra.pairs == {"1": "D", "2": "A", "3": "C"},
          str(ra.pairs))
    check("复习页配伍答案 value 紧凑", ra.value == "1:D,2:A,3:C", str(ra.value))
    check("复习页配伍答案 source=page + 满置信度",
          ra.source == "page" and ra.confidence == 1.0)
    sq = Question(qid="s1", qtype=QType.SINGLE, stem="单选",
                  options=[Option("A", "甲"), Option("B", "乙"), Option("C", "丙"),
                           Option("D", "丁")])
    check("复习页单选答案 D", parse_review_answer("D", QType.SINGLE, sq.options).value == "D")
    check("复习页单选答案带噪声 'D（解析）'",
          parse_review_answer("D（解析：略）", QType.SINGLE, sq.options).value == "D")
    mq2 = Question(qid="s3", qtype=QType.MULTIPLE, stem="多选",
                   options=[Option("A", "甲"), Option("B", "乙"), Option("C", "丙")])
    check("复习页多选答案 AB", sorted(parse_review_answer("AB", QType.MULTIPLE,
                                                          mq2.options).value) == ["A", "B"])
    jq = Question(qid="j1", qtype=QType.JUDGE, stem="判断",
                  options=[Option("A", "正确"), Option("B", "错误")])
    check("复习页判断答案按选项文字", parse_review_answer("错误", QType.JUDGE, jq.options).value == "B")
    check("复习页空答案 -> 空 Answer", parse_review_answer("", QType.SINGLE, sq.options).is_empty)
    check("复习页乱答案 -> 空 Answer",
          parse_review_answer("见教材", QType.SINGLE, sq.options).is_empty)


def test_llm_parse() -> None:
    banner("llm.extract_json 容错")
    check("裸 JSON", extract_json('{"a":1}') == {"a": 1})
    check("markdown 围栏", extract_json('```json\n{"ans":[{"i":1,"a":"A"}]}\n```')["ans"][0]["a"] == "A")
    check("前后有话", extract_json('好的：{"ans":[{"i":2,"a":["A","C"]}]} 完毕')["ans"][0]["i"] == 2)
    check("非法输入返回 None", extract_json("完全不是 json") is None)


def test_store() -> None:
    banner("store 答案库读写 + 相似题复用")
    path = ROOT / ".state" / "selftest.db"
    if path.exists():
        path.unlink()
    st = AnswerStore(str(path), similarity_threshold=0.65, persist=True)
    qs = sample_questions()
    st.put(qs[0], Answer(qid=qs[0].qid, value="B", confidence=0.9, source="llm"), course_id="c1")

    hit = st.lookup(qs[0], course_id="c1")
    check("精确命中 fp", hit is not None and hit.value == "B" and hit.source == "cache",
          repr(hit))

    # 换一门课、题干多一点噪音 -> 走相似题
    near = Question(qid="n1", qtype=QType.SINGLE, number="1",
                    stem="计算机中数据的最小单位是（ ）。【改版加字】",
                    options=qs[0].options)
    hit2 = st.lookup(near, course_id="c2")
    check("相似题命中", hit2 is not None and hit2.source == "memory" and hit2.value == "B",
          repr(hit2))

    # 同一题干但换了一组选项 -> 必须不命中（否则会给出错误答案）
    swapped = Question(qid="s1", qtype=QType.SINGLE, number="1",
                       stem="计算机中数据的最小单位是（ ）。",
                       options=[Option("A", "秒"), Option("B", "米"),
                                Option("C", "克"), Option("D", "升")])
    st2_ = st.lookup(swapped, course_id="c2")
    check("换选项不误命中", st2_ is None, repr(st2_))

    st.correct(qs[0], "C", course_id="c1")
    hit3 = st.lookup(qs[0], course_id="c1")
    check("人工修正覆盖", hit3 is not None and hit3.value == "C")

    stats = st.stats()
    check("stats 有题目数", stats["questions"] >= 1, json.dumps(stats, ensure_ascii=False))
    st.close()
    print(f"  stats: {json.dumps(stats, ensure_ascii=False)}")


def test_solver_mock() -> None:
    banner("solver + mock 模型（端到端，不联网）")
    os.environ["XX_LLM_PRESET"] = "mock"
    cfg = load_config(root=ROOT, strict=False)
    from xxtutor.config import apply_preset
    apply_preset(cfg)
    check("mock 预设生效", cfg.llm.base_url.startswith("mock:"), cfg.llm.base_url)

    path = ROOT / ".state" / "selftest2.db"
    if path.exists():
        path.unlink()
    st = AnswerStore(str(path), similarity_threshold=0.85, persist=True)
    client = LLMClient.from_config(cfg)
    solver = Solver(cfg, client, st)

    qs = sample_questions()
    answers, rep = solver.solve(qs, course_id="c1", assignment="自测作业")
    amap = {a.qid: a for a in answers}
    check("全部有答案", len(answers) == len(qs), f"{len(answers)}/{len(qs)}")
    check("单选题返回 A/B/C/D 之一",
          str(amap["q1"].value)[:1] in "ABCD", repr(amap["q1"].value))
    check("多选题返回列表", isinstance(amap["q2"].value, list), repr(amap["q2"].value))
    check("填空题返回字符串", isinstance(amap["q4"].value, str))
    check("报告统计合理", rep.total == len(qs) and rep.from_llm + rep.from_mock == len(qs),
          rep.summary_line())
    check("消耗了 token", rep.total_tokens > 0, str(rep.total_tokens))

    # 第二次求解：应当全部命中答案库，零 token
    answers2, rep2 = solver.solve(qs, course_id="c1", assignment="自测作业")
    check("二次求解走缓存", rep2.from_cache == len(qs) and rep2.requests == 0,
          rep2.summary_line())
    check("二次求解0 token", rep2.total_tokens == 0)
    check("缓存答案一致",
          {a.qid: a.value for a in answers2} == {a.qid: a.value for a in answers})

    # 人工答案优先级最高
    answers3, _ = solver.solve(qs, course_id="c1", manual={"q1": "D"})
    check("manual 覆盖缓存", {a.qid: a.value for a in answers3}["q1"] == "D")

    # no-llm 时新题应被跳过而不是报错
    st2_path = ROOT / ".state" / "selftest3.db"
    if st2_path.exists():
        st2_path.unlink()
    st2 = AnswerStore(str(st2_path), persist=True)
    solver2 = Solver(cfg, client, st2)
    _, rep3 = solver2.solve(sample_questions(), allow_llm=False)
    check("allow_llm=False 时跳过", rep3.skipped == len(qs) and rep3.from_llm == 0,
          rep3.summary_line())
    st2.close()

    # ------------------------------------------------------------------ #
    # 配伍题（B 型题）全链路：mock 模型 -> 配对解析 -> 落库 -> 取回
    # 这一条专门盯住「_coerce 把配伍题当简答吞掉、pairs 丢失」那类回归。
    # ------------------------------------------------------------------ #
    mq = Question(
        qid="qm1", qtype=QType.MATCH, number="6", stem="配伍题（B 型题）",
        options=[Option("A", "失眠"), Option("B", "发热"),
                 Option("C", "低血压"), Option("D", "心悸")],
        match_options=["失眠", "发热", "低血压", "心悸"],
        match_items=["入睡困难", "体温升高", "血压下降"],
    )
    m_path = ROOT / ".state" / "selftest-match.db"
    if m_path.exists():
        m_path.unlink()
    mst = AnswerStore(str(m_path), persist=True)
    msolver = Solver(cfg, client, mst)
    m_answers, _rep = msolver.solve([mq], course_id="c-match")
    ma = {a.qid: a for a in m_answers}.get("qm1")
    check("配伍题拿到答案", ma is not None)
    if ma is not None:
        check("配伍题带 pairs 配对", ma.pairs == {"1": "A", "2": "B", "3": "C"}, repr(ma.pairs))
        check("配伍题 value 是紧凑串", ma.value == "1:A,2:B,3:C", repr(ma.value))
    cached = mst.lookup(mq, course_id="c-match")
    check("配伍题缓存命中且带 pairs",
          cached is not None and cached.pairs == {"1": "A", "2": "B", "3": "C"},
          repr(cached.pairs) if cached else "None")
    mst.close()
    st.close()


def test_cli_offline() -> None:
    banner("CLI solve（离线，含报告落盘）")
    qfile = ROOT / ".state" / "selftest_questions.json"
    qfile.parent.mkdir(parents=True, exist_ok=True)
    qfile.write_text(json.dumps([
        {"qid": "a1", "qtype": "single", "stem": "1. HTTP 默认端口是？",
         "options": [{"letter": "A", "text": "21"}, {"letter": "B", "text": "80"},
                     {"letter": "C", "text": "443"}]},
        {"qid": "a2", "qtype": "judge", "stem": "2. HTTPS 使用 443 端口。",
         "options": [{"letter": "T", "text": "正确"}, {"letter": "F", "text": "错误"}]},
        {"qid": "a3", "qtype": "short", "stem": "3. 简述三次握手过程。"},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    from xxtutor.cli import main
    rc = main(["--preset", "mock", "solve", "-f", str(qfile), "--course-id", "selftest"])
    check("solve 退出码 0", rc == 0, f"rc={rc}")

    runs = sorted((ROOT / "out").glob("*-solve"))
    check("生成了输出目录", bool(runs), str(runs))
    if runs:
        d = runs[-1]
        ajson = d / "answers.json"
        amd = d / "answers.md"
        check("answers.json 存在", ajson.exists())
        check("answers.md 存在", amd.exists())
        if ajson.exists():
            data = json.loads(ajson.read_text(encoding="utf-8"))
            check("answers.json 结构完整",
                  data["items"] and all("answer_display" in i for i in data["items"]))
            check("报告含 token 统计", "total_tokens" in data["report"])
        if amd.exists():
            check("markdown 有答案段", "**答案**" in amd.read_text(encoding="utf-8"))


def test_selector_and_page_static() -> None:
    banner("页面/选择器模块静态检查")
    from xxtutor import page as P
    from xxtutor import selectors as S
    from xxtutor.extract_js import EXTRACT_JS, DEBUG_PAGE_JS, FILL_TEXT_JS, FILL_EDITOR_JS
    check("extract JS 非空且形如箭头函数", EXTRACT_JS.strip().startswith("()") and len(EXTRACT_JS) > 800)
    check("debug JS 非空", len(DEBUG_PAGE_JS) > 200)
    check("fill JS 非空", len(FILL_TEXT_JS) > 100 and len(FILL_EDITOR_JS) > 100)
    check("关键选择器存在", bool(S.Q_ITEM) and bool(S.SUBMIT_BTN) and bool(S.HOMEWORK_ITEMS))
    check("extract_course_params 解析",
          P.extract_course_params("https://mooc1.chaoxing.com/mycourse/studentcourse?courseId=123&clazzid=456&token=abc")
          == {"courseId": "123", "clazzid": "456", "token": "abc"})
    check("build_questions 容错",
          len(P.ChaoxingPage._build_questions({"questions": [
              {"qid": "x", "qtype": "0", "stem": "题干", "options": [{"letter": "A", "text": "甲"}]},
              {"qid": "y", "qtype": "0", "stem": "", "options": []},
          ]})) == 1)
    try:
        import playwright  # noqa: F401
        check("playwright 可导入", True)
    except ImportError:
        check("playwright 可导入", False, "未安装 -> python -m pip install playwright")


def test_cli_help() -> None:
    banner("CLI 参数完整性")
    from xxtutor.cli import build_parser
    # 注意：这里用 parse_known_args —— 多个子命令验收共用一个 parser 时，
    # 历史子命令会把后面子命令的短选项当成「多余参数」直接 SystemExit(2)，
    # 那是 argparse 的正常行为，不是缺参数，所以不能据此判定失败。
    p = build_parser()
    for cmd in ("doctor", "login", "courses", "homework", "answer", "solve", "bank", "dump-dom"):
        try:
            ns, _extra = p.parse_known_args(
                [cmd] + (["-c", "x"] if cmd == "homework" else
                         (["-f", "x"] if cmd == "solve" else []))
            )
            ok = getattr(ns, "cmd", None) == cmd and getattr(ns, "func", None) is not None
            check(f"子命令 {cmd}", ok)
        except SystemExit:
            check(f"子命令 {cmd}", False)
    ns, _extra = p.parse_known_args(
        ["answer", "-c", "高等数学", "-w", "第三章", "--dry-run", "--interactive"])
    check("answer 参数解析", ns.course == "高等数学" and ns.homework == "第三章"
          and ns.dry_run and ns.interactive and ns.func is not None)


def main() -> int:
    print("xxtutor 离线自测（不联网、不碰浏览器）")
    test_types()
    test_llm_parse()
    test_store()
    test_solver_mock()
    test_selector_and_page_static()
    test_cli_help()
    test_cli_offline()

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
    raise SystemExit(main())
