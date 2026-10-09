# -*- coding: utf-8 -*-
"""外接大脑（答案文件）链路的离线验证。

不碰浏览器、不发模型请求，只验证：
  1. 模板生成（answers.template.json / .md）
  2. 外部答案文件导入（json / md / 纯映射 / 键别名 / 题号 / 题干匹配）
  3. 合并优先级与报告计数
  4. `answer --from-file --answers` 的离线守卫（不开浏览器也不崩）
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.answerfile import import_answers, load_answer_file, load_questions_from_dir  # noqa: E402
from xxtutor.cli import main, write_answer_template  # noqa: E402
from xxtutor.solver import SolveReport  # noqa: E402
from xxtutor.types import Answer, QType, Question, Option  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[OK] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")


def q_single(qid="q1", number=1, stem="心脏的正常起搏点是？"):
    opts = [Option(letter="A", text="窦房结"), Option(letter="B", text="房室结"),
            Option(letter="C", text="希氏束"), Option(letter="D", text="浦肯野纤维")]
    return Question(qid=qid, qtype=QType.SINGLE, number=number, stem=stem, options=opts)


def q_multi(qid="q2", number=2):
    opts = [Option(letter="A", text="甲"), Option(letter="B", text="乙"),
            Option(letter="C", text="丙"), Option(letter="D", text="丁")]
    return Question(qid=qid, qtype=QType.MULTIPLE, number=number, stem="下列哪些正确？", options=opts)


def q_match(qid="q3", number=3):
    opts = [Option(letter="A", text="失眠"), Option(letter="B", text="发热"),
            Option(letter="C", text="低血压"), Option(letter="D", text="心悸")]
    return Question(qid=qid, qtype=QType.MATCH, number=number, stem="B 型配伍题:",
                    options=opts, match_options=[o.text for o in opts],
                    match_items=["入睡困难", "体温升高"])


def q_judge(qid="q4", number=4):
    opts = [Option(letter="A", text="正确"), Option(letter="B", text="错误")]
    return Question(qid=qid, qtype=QType.JUDGE, number=number, stem="发热是感染的表现。", options=opts)


def main_() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="xxtutor-ext-"))
    try:
        questions = [q_single(), q_multi(), q_match(), q_judge()]

        # ---------- 1. 模板 ----------
        tpl = write_answer_template(questions, tmp)
        check("模板文件已生成", tpl.exists(), str(tpl))
        data = json.loads(tpl.read_text(encoding="utf-8"))
        check("模板含全部题目键", all(q.qid in data for q in questions), str(list(data)))
        check("模板单选是空串", data["q1"] == "", repr(data["q1"]))
        check("模板多选是空数组", data["q2"] == [], repr(data["q2"]))
        check("模板配伍按小题编号", isinstance(data["q3"], dict) and "1" in data["q3"], repr(data["q3"]))
        md = tmp / "answers.template.md"
        check("markdown 模板已生成", md.exists())
        check("markdown 含题干", "心脏的正常起搏点" in md.read_text(encoding="utf-8"))

        # ---------- 2. AI 回复的答案文件（模拟豆包输出）----------
        ai = tmp / "ai-answer.json"
        ai.write_text(json.dumps({
            "q1": "A",
            "q2": ["A", "C"],
            "q3": {"1": "A", "2": "B"},
            "q4": True,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        ext, skipped = import_answers(ai, questions)
        check("导入 4 题", len(ext) == 4, f"{len(ext)} skipped={skipped}")
        check("单选 A", ext["q1"].value == "A", repr(ext["q1"].value))
        check("多选 A/C", list(ext["q2"].value) == ["A", "C"], repr(ext["q2"].value))
        check("配伍 pairs 正确", ext["q3"].pairs == {"1": "A", "2": "B"}, repr(ext["q3"].pairs))
        check("判断题映射成字母", ext["q4"].value == "A", repr(ext["q4"].value))
        check("来源标为 file", all(a.source == "file" for a in ext.values()))
        check("置信度为 1.0", all(a.confidence == 1.0 for a in ext.values()))

        # ---------- 3. 各种输入形态 ----------
        aliases = tmp / "alias.json"
        aliases.write_text(json.dumps({"items": [{"qid": "q1", "answer": "B"}]},
                                      ensure_ascii=False), encoding="utf-8")
        ext2, _ = import_answers(aliases, questions)
        check("items/qid/answer 别名可识别", ext2.get("q1") and ext2["q1"].value == "B",
              repr(ext2.get("q1")))

        bynumber = tmp / "bynumber.json"
        bynumber.write_text(json.dumps({"2": ["B"], "1": "C"}), encoding="utf-8")
        ext3, _ = import_answers(bynumber, questions)
        check("按题号匹配", ext3.get("q1") and ext3["q1"].value == "C" and
              ext3.get("q2") and list(ext3["q2"].value) == ["B"], repr(ext3))

        bystem = tmp / "bystem.json"
        bystem.write_text(json.dumps({"心脏的正常起搏点是？": "D"}, ensure_ascii=False),
                          encoding="utf-8")
        ext4, _ = import_answers(bystem, questions)
        check("按题干匹配", ext4.get("q1") and ext4["q1"].value == "D", repr(ext4.get("q1")))

        bytext = tmp / "bytext.json"
        bytext.write_text(json.dumps({"q1": "窦房结"}, ensure_ascii=False), encoding="utf-8")
        ext5, _ = import_answers(bytext, questions)
        check("按选项正文匹配", ext5.get("q1") and ext5["q1"].value == "A", repr(ext5.get("q1")))

        # 配伍的紧凑字符串形式
        compact = tmp / "compact.json"
        compact.write_text(json.dumps({"q3": "1:A,2:B"}, ensure_ascii=False), encoding="utf-8")
        ext6, _ = import_answers(compact, questions)
        check("配伍紧凑串", ext6.get("q3") and ext6["q3"].pairs == {"1": "A", "2": "B"},
              repr(ext6.get("q3").pairs if ext6.get("q3") else None))

        # 对不上的键要进 skipped
        junk = tmp / "junk.json"
        junk.write_text(json.dumps({"q1": "A", "不存在的题": "B", "deadbeefdeadbeef": "C"},
                                   ensure_ascii=False), encoding="utf-8")
        ext7, sk7 = import_answers(junk, questions)
        check("未匹配键进 skipped", len(ext7) == 1 and len(sk7) == 2, f"{ext7.keys()} {sk7}")

        # answers.md 形态
        mdtext = tmp / "answers.md"
        mdtext.write_text(
            "# 答案\n\n### 1. [single] 心脏的正常起搏点是？\n"
            "- A. 窦房结\n- B. 房室结\n\n**答案**：B （来源 llm，置信度 0.9）\n",
            encoding="utf-8")
        loaded = load_answer_file(mdtext)
        check("answers.md 可解析", any(v == "B" and k.startswith("1|") for k, v in loaded.items()),
              repr(loaded))
        ext8, _ = import_answers(mdtext, questions)
        check("answers.md 能对上题号", ext8.get("q1") and ext8["q1"].value == "B", repr(ext8.get("q1")))

        # ---------- 4. 合并优先级与报告计数 ----------
        from xxtutor.answerfile import merge_answer_maps
        base = [Answer(qid="q1", value="C", source="llm", confidence=0.6),
                Answer(qid="q2", value=["A"], source="cache", confidence=1.0)]
        rep = SolveReport(total=4, failed=2, failures=[{"qid": "q3", "stem": "x", "reason": "no"},
                                                       {"qid": "q4", "stem": "y", "reason": "no"}])
        merged = merge_answer_maps(base, ext, questions, rep)
        m = {a.qid: a for a in merged}
        check("合并顺序与题目一致", [a.qid for a in merged] == [q.qid for q in questions],
              str([a.qid for a in merged]))
        check("导入答案覆盖模型答案", m["q1"].value == "A" and m["q1"].source == "file",
              repr(m["q1"]))
        check("补上的题从 failures 摘掉", [f["qid"] for f in rep.failures] == [], str(rep.failures))
        check("failed 计数被扣减", rep.failed == 0, str(rep.failed))
        check("from_file 计数", rep.from_file == 4, str(rep.from_file))
        check("replaced_from_file 计数", rep.replaced_from_file == 2, str(rep.replaced_from_file))
        check("summary_line 含外部答案", "外部答案" in rep.summary_line(), rep.summary_line())

        # ---------- 5. --from-dir 复用已抓题目 ----------
        run = tmp / "run1"
        run.mkdir()
        (run / "questions.json").write_text(json.dumps(
            {"items": [q.to_dict() for q in questions]}, ensure_ascii=False), encoding="utf-8")
        reused = load_questions_from_dir(run)
        check("--from-dir 复用 4 题", len(reused) == 4, str(len(reused)))
        check("复用后题干一致", reused[0].stem == questions[0].stem, reused[0].stem)
        check("复用后选项一致", [o.letter for o in reused[0].options] == ["A", "B", "C", "D"])
        check("复用后配伍小题保留", reused[2].match_items == ["入睡困难", "体温升高"],
              str(reused[2].match_items))

        # ---------- 6. 端到端：离线跑一次 answer（不开浏览器）----------
        qfile = tmp / "questions_only.json"
        qfile.write_text(json.dumps(
            {"items": [q.to_dict() for q in questions]}, ensure_ascii=False), encoding="utf-8")
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps({
            "llm": {"preset": "mock", "base_url": "http://127.0.0.1:1", "model": "mock"},
            "store": {"path": str(tmp / "bank.db"), "similarity_threshold": 1.01},
            "output": {"dir": str(tmp / "out")},
        }, ensure_ascii=False), encoding="utf-8")
        rc = main(["--config", str(cfg), "--preset", "mock", "answer",
                   "--from-file", str(qfile), "--answers", str(ai), "--course-id", "ext-test"])
        check("离线 answer 退出码 0", rc == 0, str(rc))
        runs = sorted((tmp / "out").glob("*-answer"))
        check("生成了运行目录", bool(runs), str(list((tmp / "out").glob("*"))))
        if runs:
            saved = json.loads((runs[-1] / "answers.json").read_text(encoding="utf-8"))
            items = saved.get("items", [])
            vals = {i["qid"]: i.get("answer") for i in items}
            check("答案文件写入了外部答案", vals.get("q1") == "A", str(vals))
            check("answers.md 存在", (runs[-1] / "answers.md").exists())
            check("离线模式没写 fill-report", not (runs[-1] / "fill-report.json").exists())
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    rc = main_()
    print(f"\n通过 {PASS} / 共 {PASS + FAIL}" + (" 全部通过 ✔" if FAIL == 0 else f" 失败 {FAIL} ✘"))
    sys.exit(rc)
