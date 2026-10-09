"""答案文件导入：把「外接大脑」（任意 AI）给出的答案读回框架。

设计动机
--------
框架自己调模型需要 API Key；而用户手边就有免费的 AI（网页版豆包 / DSH 会话里的
agent / 任何本地或云端模型）。所以把「答题」这一步做成**文件接口**：

    抓题（本框架）→ questions.json → 丢给任意 AI → answers.json → 回填（本框架）

这个模块只负责最后一跳：吃各种形状的答案文件，产出 ``Dict[qid, Answer]``。
它不认识任何一家 AI 的格式，因此换 AI 零成本。

支持的输入形状（越简单越好用，但都不强制）
------------------------------------------
1. 纯映射（external AI 最容易吐出来的形状）::

       {"q1": "A", "q2": ["A", "C"], "q3": {"1": "B", "2": "D"}, "q4": "自由文本"}

   键按顺序匹配：题目 id → 题号（"1"/"第1题"）→ 题干前 N 字。
   值按题型解析：单选取字母、多选取字母数组、填空/简答取文本、
   B 型配伍取 ``{"1": "A", "2": "B"}`` 或 ``"1:A,2:B"``、判断题取 true/false 或选项文字。

2. 本框架导出的 ``answers.json``（可直接回贴）::

       {"items": [{"qid": ..., "answer": ..., "type": ...}, ...]}

3. 本框架导出的 ``answers.md``（人类/AI 都爱读的 Markdown）::

       ### 1. [single] 下列哪项...
         * A. xxx
           B. yyy
         **答案**：A. xxx （来源 llm，置信度 0.90）

4. 其他别名：``questions`` / ``qs`` / ``results`` / ``data`` / ``answers`` 等键。

解析不出来的题不会被静默丢掉：调用方拿到的是 ``skipped`` 列表，可原样打印给用户。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .types import (
    Answer,
    AnswerValue,
    Option,
    QType,
    Question,
    normalize_text,
    parse_letters,
    parse_pairs,
)

__all__ = [
    "AnswersFileError",
    "import_answers",
    "load_answer_file",
    "load_questions_from_dir",
    "merge_answer_maps",
]


class AnswersFileError(Exception):
    """答案文件读不出来（格式/编码问题）。"""


# --------------------------------------------------------------------------- #
# 噪声过滤
# --------------------------------------------------------------------------- #

_HEX_RE = re.compile(r"[0-9a-fA-F]{8,}")

_NOISE_LINE = re.compile(r"^\s*(来源|置信度|解析|说明|注)[:：]")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _to_text(v: Any) -> str:
    """把标量/列表统一成一行文本。

    列表用逗号连接（**不是空串**）：`["A","C"]` -> `"A,C"`、`["1:A","2:B"]` -> `"1:A,2:B"`。
    早先用 `"".join(...)` 拍平，导致多空填空 `["8080","8443"]` 变成 `"80808443"`，
    两个空都填错——而退出码和「成功 6/6」的日志都还是正常的，很难发现。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, set)):
        return ",".join(_to_text(x) for x in v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if float(v).is_integer() else str(v)
    return str(v).strip()


def _letter(value: Any, options: Sequence[Option]) -> str:
    """把答案值逼成单个选项字母。"""
    if isinstance(value, bool):
        return _bool_letter(value, options)
    if isinstance(value, int) and 1 <= value <= len(options):
        return options[value - 1].letter
    text = _to_text(value).strip()
    if not text:
        return ""
    letters = parse_letters(text, options)
    if letters:
        return letters[0]
    # 没字母就按选项正文匹配
    key = normalize_text(text)
    for o in options:
        if key and key == normalize_text(o.text):
            return o.letter
    return ""


def _bool_letter(value: Any, options: Sequence[Option]) -> str:
    """判断题：true/false 落到「正确/错误」两个选项上。"""
    if not options:
        return ""
    first, last = options[0], options[-1]
    yes = bool(re.search(r"正确|对|是|√|T\b", first.text))
    no = bool(re.search(r"错误|不对|否|×|F\b", last.text))
    if not (yes and no):
        return ""
    return first.letter if value else last.letter


def _parse_value(value: Any, q: Question) -> Tuple[AnswerValue, Optional[Dict[str, str]]]:
    """按题型把任意形状的值转成 (value, pairs)。解析不出来返回 ("", None)。"""
    if q.qtype == QType.MATCH:
        if isinstance(value, dict):
            letters = [o.letter for o in q.options]
            pairs = parse_pairs(value, q.match_items, letters)
            if not pairs:  # 模型爱用 1/2/3 当下标
                pairs = parse_pairs({str(int(k) + 1) if str(k).isdigit() else k: v
                                     for k, v in value.items()}, q.match_items, letters)
            if pairs:
                return ",".join(f"{k}:{v}" for k, v in pairs.items()), pairs
            return "", None
        text = _to_text(value)
        pairs = parse_pairs(text, q.match_items, [o.letter for o in q.options])
        if pairs:
            return ",".join(f"{k}:{v}" for k, v in pairs.items()), pairs
        return "", None

    if isinstance(value, bool):
        letter = _bool_letter(value, q.options)
        return (letter, None) if letter else (("true" if value else "false"), None)

    letters = [o.letter for o in q.options]
    if isinstance(value, (list, tuple, set)):
        if q.qtype == QType.MULTIPLE:
            got = [l for l in parse_letters(_to_text(value), q.options) if l in letters]
            if got:
                return got, None
        elif q.qtype == QType.FILL:
            # 多空填空必须**保持列表**：填充层是按列表逐空写入的。
            # 这里一旦拍平成字符串，N 个空就会被塞进第 1 个空（或全空都错）。
            parts = [_to_text(x).strip() for x in value]
            parts = [p for p in parts if p]
            if parts:
                return parts, None
            return "", None
        elif len(value) == 1:
            letter = _letter(list(value)[0], q.options)
            if letter:
                return letter, None
        text = _to_text(value)
        return (text, None) if text else ("", None)

    if q.qtype in (QType.SINGLE, QType.MULTIPLE):
        text = _to_text(value).strip()
        # 先按「选项正文」匹配（AI 常常直接回选项原话）
        if text:
            key = normalize_text(text)
            for o in q.options:
                if key == normalize_text(o.text):
                    return o.letter, None
        got = [l for l in parse_letters(text, q.options) if l in letters]
        if got:
            if q.qtype == QType.MULTIPLE:
                return got, None
            return got[0], None
        if _HEX_RE.search(text):   # 防止把 hash / uuid 当成答案
            return "", None
        # 单个字母但不在选项里（页面字母被重排过的兜底）
        if len(text) == 1 and text.isalpha():
            return text.upper(), None
        return "", None

    text = _to_text(value).strip()
    return (text, None) if text else ("", None)


def _norm_qid(s: str) -> str:
    return re.sub(r"^q(?=\d+$)", "", s.strip().lower())


def _number_key(s: str) -> Optional[str]:
    m = re.search(r"\d+", s or "")
    return m.group(0) if m else None


def _match_by_stem(raw_key: Any, by_stem: Dict[str, Question], stem_chars: int) -> Optional[Question]:
    """按键本身（可能是一整句题干）匹配题目。"""
    key = normalize_text(_to_text(raw_key))
    if not key:
        return None
    if len(key) >= stem_chars:
        q = by_stem.get(key[:stem_chars])
        if q is not None:
            return q
    for head, q in by_stem.items():
        if key.startswith(head) or head.startswith(key):
            return q
    return None


def _match_questions(answers: Dict[str, Any], questions: Sequence[Question], *,
                     stem_chars: int = 12) -> Tuple[Dict[str, Answer], List[str]]:
    """把「键 → 答案值」映射到题目上（依次尝试：题目 id → 归一化 id → 题号 → 题干）。"""
    out: Dict[str, Answer] = {}
    skipped: List[str] = []

    by_id = {q.qid: q for q in questions}
    by_norm: Dict[str, Question] = {}
    for q in questions:
        by_norm.setdefault(_norm_qid(q.qid), q)
    by_num: Dict[str, Question] = {}
    for q in questions:
        for cand in (str(q.number), _number_key(q.qid)):
            if cand and cand not in by_num:
                by_num[cand] = q
    by_stem: Dict[str, Question] = {}
    for q in questions:
        head = normalize_text(q.stem)[:stem_chars]
        if head:
            by_stem.setdefault(head, q)

    for raw_key, raw_value in answers.items():
        key = str(raw_key)
        q = by_id.get(key) or by_norm.get(_norm_qid(key))
        if q is None:
            num = _number_key(key)
            if num:
                q = by_num.get(num)
        if q is None:
            q = _match_by_stem(raw_key, by_stem, stem_chars)
        if q is None:
            skipped.append(key)
            continue
        value, pairs = _parse_value(raw_value, q)
        if value == "" and not pairs:
            skipped.append(key)
            continue
        out[q.qid] = Answer(qid=q.qid, value=value, pairs=pairs, source="file",
                            confidence=1.0, note=f"外部答案文件：{key}")
    return out, skipped


def _coerce_items(data: Any) -> Optional[List[Dict[str, Any]]]:
    """把「items 列表」形态归一化；不是这种形态就返回 None。"""
    if isinstance(data, list):
        if all(isinstance(x, dict) for x in data) and data:
            return list(data)
        return None
    if isinstance(data, dict):
        for key in ("items", "questions", "qs", "results", "list", "data", "answers"):
            v = data.get(key)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return list(v)
    return None


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def _parse_markdown(text: str) -> Dict[str, Any]:
    """从 answers.md 之类的文档里抽「题干/题号 → 答案」。

    布局兼容本框架导出的 answers.md：

        ### 1. [single] 下列哪项...
          * A. xxx
          **答案**：A. xxx （来源 llm，置信度 0.90）
    """
    out: Dict[str, Any] = {}
    last_head: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        head = re.match(r"^#{1,6}\s*(.*)$", line)
        if head:
            title = head.group(1)
            title = re.sub(r"\[[^\]]*\]", " ", title)          # 去掉 [single] 这类题型标注
            title = re.sub(r"^(\d+)\s*[.、)]\s*", r"\1|", title.strip())
            last_head = re.sub(r"\s+", " ", title).strip().rstrip("？?。.：:")
            continue
        if line.startswith("|") or _NOISE_LINE.match(line):
            continue
        m = re.search(r"(?:\*\*)?(?:答案|故选|正确答案|参考答案|Answer)(?:\*\*)?\s*[:：=]?\s*(.+)$",
                      line, flags=re.IGNORECASE)
        if not m:
            continue
        value = m.group(1).strip()
        value = re.sub(r"[（(]\s*来源[^）)]*[）)]\s*$", "", value).strip()
        value = re.sub(r"\s*[（(]\s*置信度[^）)]*[）)]\s*$", "", value).strip()
        if not value:
            continue
        if last_head:
            out[last_head] = value
        else:
            out[f"#{len(out) + 1}"] = value
    return out


# --------------------------------------------------------------------------- #
# 文件读取
# --------------------------------------------------------------------------- #

def _read_text(path: Path) -> str:
    try:
        # utf-8-sig：记事本 / PowerShell 存 UTF-8 会带 BOM，外部 AI 回贴也常带
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")
    except OSError as e:
        raise AnswersFileError(f"读不到答案文件 {path}：{e}") from e


def load_answer_file(path: str | Path) -> Dict[str, Any]:
    """读答案文件 → 「键：答案值」映射（键可能是 qid、题号或题干）。"""
    p = Path(path).expanduser()
    # 用户常常直接给一个 run 目录
    if p.is_dir():
        for name in ("answers.json", "answer.json", "answers.md"):
            cand = p / name
            if cand.exists():
                return load_answer_file(cand)
        raise AnswersFileError(f"目录里找不到 answers.json / answers.md：{p}")
    if not p.exists():
        raise AnswersFileError(f"答案文件不存在：{p}")

    if p.suffix.lower() in (".md", ".markdown", ".txt"):
        return _parse_markdown(_read_text(p))

    raw = _read_text(p).strip()
    if not raw:
        raise AnswersFileError(f"答案文件是空的：{p}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        parsed = _parse_markdown(raw)
        if parsed:
            return parsed
        raise AnswersFileError(
            f"答案文件既不是合法 JSON，也看不出「答案：X」这种行：{p}") from None

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]

    items = _coerce_items(data)
    if items is not None:
        out: Dict[str, Any] = {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            key = (item.get("qid") or item.get("id") or item.get("number")
                   or item.get("index") or item.get("stem") or item.get("q")
                   or f"#{i + 1}")
            if _is_empty(item.get("answer")):
                continue
            out[str(key)] = item.get("answer")
        return out

    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not _is_empty(v)}

    raise AnswersFileError(f"答案文件的结构认不出来（既不是映射也不是题目列表）：{p}")


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    return False


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def import_answers(target: str | Path, questions: Sequence[Question], *,
                   source: str = "file",
                   default_note: str = "外部答案文件") -> Tuple[Dict[str, Answer], List[str]]:
    """把一个答案文件（或答案目录）导入成 ``{qid: Answer}``。

    返回 ``(answers, skipped)``：``skipped`` 是没能对上题目的键，调用方应当打印出来，
    否则用户会以为答案生效了。
    """
    if not questions:
        return {}, []
    mapping = load_answer_file(target)
    out, skipped = _match_questions(mapping, questions)
    for a in out.values():
        a.source = source
        a.confidence = 1.0
        if default_note and not a.note:
            a.note = default_note
    return out, skipped


def merge_answer_maps(base: Sequence[Answer],
                      incoming: Dict[str, Answer],
                      questions: Sequence[Question],
                      report: Any = None) -> List[Answer]:
    """把导入的答案合并进求解结果：**导入的答案优先级最高**（用户/AI 显式给的）。

    同时维护 ``report`` 的计数（从 ``failed`` 里摘掉被补上的题），并保证返回顺序与题目一致。
    """
    amap: Dict[str, Answer] = {a.qid: a for a in base}
    replaced = 0
    added = 0
    for qid, a in incoming.items():
        old = amap.get(qid)
        # 只有「本来就有非空答案」才算覆盖；空壳答案（求解失败占位）算补上
        if old is not None and not getattr(old, "is_empty", False):
            if a.source != old.source or a.value != old.value or a.pairs != old.pairs:
                replaced += 1
        else:
            added += 1
        amap[qid] = a
    if report is not None:
        report.failed = max(0, int(getattr(report, "failed", 0)) - added)
        fails = []
        for f in getattr(report, "failures", []) or []:
            qid = f.get("qid") if isinstance(f, dict) else None
            if qid and qid in incoming:
                continue
            fails.append(f)
        report.failures = fails
        report.from_file = int(getattr(report, "from_file", 0) or 0) + added + replaced
        report.replaced_from_file = int(getattr(report, "replaced_from_file", 0) or 0) + replaced
    return [amap[q.qid] for q in questions if q.qid in amap]


def load_questions_from_dir(directory: str | Path) -> List[Question]:
    """复用某次运行的 ``questions.json``（配合 ``--from-dir``）。

    与 cli.load_questions_file 共用同一套解析，避免两处兼容逻辑漂移。
    """
    from .cli import load_questions_file  # 延迟导入：cli 会 import 本模块

    p = Path(directory).expanduser()
    cand = p / "questions.json" if p.is_dir() else p
    if not cand.exists():
        raise AnswersFileError(f"找不到题目文件：{cand}")
    return load_questions_file(cand)
