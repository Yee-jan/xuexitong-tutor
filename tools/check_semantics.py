# -*- coding: utf-8 -*-
"""权威核对：迁移后的答案库，其「指向的选项正文」是否与首轮（可信语义）逐题一致。

首轮的 `answers.json` 里字母 = 屏显字母（第一轮 `_build_questions` 按位置编号），
直接查它的选项表就能拿到「首轮答案真正指向的正文」。
今天库内一律存正文，所以只要把库里的值解出来比较正文即可 —— 字母怎么变都无所谓。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.answer_codec import option_text  # noqa: E402
from xxtutor.store import AnswerStore  # noqa: E402
from xxtutor.types import Option, Question, QType  # noqa: E402

RUN1 = ROOT / "out" / "20261008-235953-answer"
TODAY = ROOT / "out" / "20261009-121838-answer"


def load(p):
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    return {str(it["number"]): it for it in data["items"]}


run1 = load(RUN1 / "answers.json")
today = load(TODAY / "answers.json")

store = AnswerStore(str(ROOT / ".state" / "answers.db"))
same = diff = miss = 0
detail = []
for num, it in sorted(today.items(), key=lambda kv: int(kv[0])):
    q1 = run1[num]
    t1 = {o["letter"].upper(): o["text"] for o in q1["options"]}
    a1 = q1["answer"]
    if q1["type"] == "match":
        # 首轮产物里配伍题有两种写法：{"1":"C","2":"E"} 或 "1:C,2:E"
        if isinstance(a1, dict):
            want = {str(k): t1[v.upper()] for k, v in a1.items()}
        else:
            want = {p.split(":")[0].strip(): t1[p.split(":")[1].strip().upper()]
                    for p in str(a1).split(",")}
    elif isinstance(a1, list):
        want = sorted(t1[L.upper()] for L in a1)
    else:
        want = t1[str(a1).upper()]

    q = Question(
        qid=str(it["qid"]), number=it["number"], qtype=QType.from_raw(it["type"]),
        stem=it["stem"],
        options=[Option(letter=o["letter"], text=o["text"]) for o in it["options"]],
    )
    hit = store.lookup(q, course_id="")
    if hit is None:
        miss += 1
        detail.append(f"Q{num}: 未命中")
        continue
    if hit.pairs:
        got = {str(k): option_text(q, v) for k, v in hit.pairs.items()}
    elif isinstance(hit.value, list):
        got = sorted(option_text(q, v) for v in hit.value)
    else:
        got = option_text(q, str(hit.value))
    if got == want:
        same += 1
    else:
        diff += 1
        detail.append(f"Q{num}: 首轮={want!r}  库内解析={got!r}")

print(f"指向正文与首轮一致：{same} 题 | 不一致 {diff} | 未命中 {miss}"
      f"（共 {len(today)}）")
for d in detail[:12]:
    print("  " + d)
store.close()
print("结论：" + ("全部一致 ✔" if diff == 0 and miss == 0 else "有问题 ✘"))
