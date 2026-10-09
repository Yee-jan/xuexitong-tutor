# -*- coding: utf-8 -*-
"""回归：答案库必须按「选项正文」定位，不能按字母坐标。

真机事故（2026-10-09 发现）：真实 doWork 页**每次加载都会打乱选项的屏显字母**，
而库里存的是字母。于是第二轮起「缓存命中 100%」却把 51/73 题的答案填到了别的选项上
（例：Q59 该选「颈部」，实际填了「锁骨上窝」）。根因是 `Question.fp` 与字母无关、
跨运行稳定，所以命中的行看起来完美，值却换了坐标。

本测试不依赖浏览器，直接构造同一个 `Question` 的两套字母坐标来钉住这个不变量：
  1. 甲坐标系下 put 一个「D=颈部」的答案；
  2. 乙坐标系（正文顺序不变、字母重排）下 lookup，必须仍指向「颈部」。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xxtutor.answer_codec import (letter_to_text, option_text,  # noqa: E402
                                     text_to_letter)
from xxtutor.store import AnswerStore  # noqa: E402
from xxtutor.types import Answer, Option, Question, QType  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


# 真实的 Q59：正文顺序稳定，字母每次加载都变（实测数据）
Q59_TEXTS = ["颌下", "腹股沟", "锁骨上窝", "颈部", "腋窝"]
Q59_OLD = {"A": "颌下", "B": "腹股沟", "C": "锁骨上窝", "D": "颈部", "E": "腋窝"}
Q59_NEW = {"A": "颌下", "E": "腹股沟", "D": "锁骨上窝", "B": "颈部", "C": "腋窝"}
# 真实的 Q68：多选题，老 B=传染性单核细胞增多症，新 map 下 A,B,E 应是淋巴瘤/急慢性淋巴结炎/慢性白血病
Q68_TEXTS = ["淋巴瘤", "传染性单核细胞增多症", "再生障碍性贫血",
             "急、慢性淋巴结炎", "慢性白血病"]
Q68_OLD = {"A": "淋巴瘤", "B": "传染性单核细胞增多症", "C": "再生障碍性贫血",
           "D": "急、慢性淋巴结炎", "E": "慢性白血病"}
Q68_NEW = {"A": "淋巴瘤", "C": "传染性单核细胞增多症", "D": "再生障碍性贫血",
           "E": "急、慢性淋巴结炎", "B": "慢性白血病"}


def make_q(qid: str, stem: str, table: dict, qtype: QType) -> Question:
    """按「字母 -> 正文」表构造 Question。"""
    items = sorted(table.items(), key=lambda kv: Q59_TEXTS.index(kv[1])
                   if kv[1] in Q59_TEXTS else Q68_TEXTS.index(kv[1]))
    return Question(
        qid=qid, number=1, stem=stem, qtype=qtype,
        options=[Option(letter=L, text=t) for L, t in items],
    )


def main() -> int:
    print("== 1. 编解码单元 ==")
    q_old = make_q("question1", "全身浅表淋巴结检查，何处可触及？", Q59_OLD, QType.SINGLE)
    q_new = make_q("question1", "全身浅表淋巴结检查，何处可触及？", Q59_NEW, QType.SINGLE)
    check("两套坐标的 fp 相同（说明命中与否看不出坐标变化）",
          q_old.fp == q_new.fp, f"{q_old.fp} != {q_new.fp}")
    check("甲坐标 D 的是「颈部」", option_text(q_old, "D") == "颈部")
    check("乙坐标 D 的是「锁骨上窝」", option_text(q_new, "D") == "锁骨上窝")

    stored = letter_to_text(q_old, "D")
    check("入库时把字母换成正文 => 颈部", stored == "颈部", repr(stored))
    back = text_to_letter(q_new, stored)
    check("出库时按乙坐标换回字母 => B", back == "B", repr(back))
    check("乙坐标 B 确实是「颈部」", option_text(q_new, "B") == "颈部")
    check("★ 正文不存在时不猜 => None", text_to_letter(q_new, "不存在的选项") is None)

    print("== 2. 多选题 ==")
    m_old = make_q("question2", "淋巴结肿大可见于哪些疾病？", Q68_OLD, QType.MULTIPLE)
    m_new = make_q("question2", "淋巴结肿大可见于哪些疾病？", Q68_NEW, QType.MULTIPLE)
    check("多选两套坐标 fp 相同", m_old.fp == m_new.fp)
    texts = letter_to_text(m_old, ["A", "B", "E"])
    check("老多选 [A,B,E] 存成三堆正文",
          texts == ["淋巴瘤", "传染性单核细胞增多症", "慢性白血病"], repr(texts))
    back_m = text_to_letter(m_new, texts)
    check("★ 新坐标换回来仍是同一组正文（不是只剩最后一个字母）",
          isinstance(back_m, list) and sorted(
              option_text(m_new, L) for L in back_m) == sorted(texts),
          repr(back_m))

    print("== 3. 配伍题 ==")
    bt_old = make_q("question3", "步态与疾病", Q59_OLD, QType.MATCH)
    bt_new = make_q("question3", "步态与疾病", Q59_NEW, QType.MATCH)
    pairs_text = letter_to_text(bt_old, {"1": "D", "2": "A"})
    check("配伍题存成 1:颈部, 2:颌下",
          pairs_text == {"1": "颈部", "2": "颌下"}, repr(pairs_text))
    back_bt = text_to_letter(bt_new, pairs_text)
    check("★ 新坐标换回 1:B, 2:A", back_bt == {"1": "B", "2": "A"}, repr(back_bt))

    print("== 4. 走一遍 AnswerStore（端到端、无浏览器） ==")
    with AnswerStore(":memory:") as store:
        store.persist = True
        store.put(q_old, Answer(qid="question1", value="D", source="llm"),
                  course_id="c1")
        hit_new = store.lookup(q_new, course_id="c1")
        check("★ 换一套字母坐标后仍命中", hit_new is not None)
        if hit_new is not None:
            check("★ 命中值在新坐标里指向「颈部」",
                  option_text(q_new, str(hit_new.value)) == "颈部",
                  f"value={hit_new.value!r} -> {option_text(q_new, str(hit_new.value))!r}")
        raw = None
        with store._conn() as c:  # noqa: SLF001 - 测试直接看库内容
            raw = c.execute("SELECT answer FROM questions WHERE fp = ?",
                            (q_old.fp,)).fetchone()["answer"]
        check("★ 库里存的是正文而不是裸字母", "颈部" in str(raw), repr(raw))

        # 选项正文被改过 => 必须当成未命中，不能瞎填
        q_changed = Question(
            qid="question1", number=1, stem=q_old.stem, qtype=QType.SINGLE,
            options=[Option(letter="A", text="完全换掉的选项"),
                     Option(letter="B", text="也是新的")],
        )
        check("★ 选项被换掉时视为未命中（返回 None）",
              store.lookup(q_changed, course_id="c1") is None)

    print("== 5. 填空/简答（没有选项）不能被坐标换算吃掉 ==")
    fq = Question(qid="q4", qtype=QType.FILL, number="4",
                  stem="十进制数 10 的二进制表示是______。", options=[])
    check("无选项题：按字母存时原样保留", letter_to_text(fq, "1010") == "1010")
    check("无选项题：按正文还原时原样返回（不是 None）", text_to_letter(fq, "1010") == "1010")
    sq = Question(qid="q5", qtype=QType.SHORT, number="5",
                  stem="简述进程与线程的区别。", options=[])
    check("简答题多行文本也不受影响",
          text_to_letter(sq, "进程是资源分配单位\n线程是调度单位")
          == "进程是资源分配单位\n线程是调度单位")
    with AnswerStore(":memory:") as store2:
        store2.persist = True
        store2.put(fq, Answer(qid="q4", value="1010", source="llm"), course_id="c1")
        hit_f = store2.lookup(fq, course_id="c1")
        check("★ 无选项题的缓存命中不会丢", hit_f is not None and hit_f.value == "1010",
              repr(hit_f.value) if hit_f else "None")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
