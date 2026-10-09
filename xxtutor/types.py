"""数据模型：题目、选项、答案。"""

from __future__ import annotations

import enum
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Tuple,
                    Union)

__all__ = [
    "QType",
    "Question",
    "Option",
    "Answer",
    "normalize_text",
    "fingerprint",
    "shingles",
    "AnswerValue",
    "parse_letters",
    "parse_bool",
    "parse_pairs",
    "match_display",
    "match_items_by_text",
    "pair_score",
    "split_blanks",
    "option_letter",
]

# 单个答案值的类型：判断题用 bool，其余用 str / list[str]
AnswerValue = Union[bool, str, List[str]]


class QType(str, enum.Enum):
    """题型。值取自学习通 DOM 的 `data-type` 属性，并保留原始字符串。

    学习通常见的题型编号（含医学题库的 A/B/X 型）：
        0 单选(A1/A2/A3 型) / 1 多选(X 型) / 2 填空 / 3 判断 / 4 简答 / 6 配伍(B 型)
    """

    SINGLE = "single"       # 单选，含 A1/A2/A3 型病历单选题 (data-type=0)
    MULTIPLE = "multiple"   # 多选，即 X 型题 (data-type=1)
    FILL = "fill"           # 填空 (data-type=2)
    JUDGE = "judge"         # 判断 (data-type=3)
    SHORT = "short"         # 简答/论述 (data-type=4)
    MATCH = "match"         # 配伍题，即 B 型题：一组备选 A-E 配多个小题 (data-type=6)
    OTHER = "other"         # 未识别

    @classmethod
    def from_raw(cls, raw: Any) -> "QType":
        """把学习通的各种原始标记归一化成 QType。

        兼容四种写法：
            * 数字："0"~"6"（含配伍题 6）
            * 英文："single"/"multiple"/"judge"/"fill"/"short"/"match"
            * 中文："单选题"/"多选题"/"判断题"/"填空题"/"简答题"/"配伍题"
            * 医学题型：A1/A2/A3 型 -> 单选，X 型 -> 多选，B 型 -> 配伍
        """
        if raw is None:
            return cls.OTHER
        s = str(raw).strip()
        table = {
            "0": cls.SINGLE, "1": cls.MULTIPLE, "2": cls.FILL, "3": cls.JUDGE, "4": cls.SHORT,
            "6": cls.MATCH, "7": cls.MATCH,
            "single": cls.SINGLE, "multiple": cls.MULTIPLE, "fill": cls.FILL,
            "judge": cls.JUDGE, "short": cls.SHORT, "match": cls.MATCH,
            "matching": cls.MATCH,
        }
        if s.lower() in table:
            return table[s.lower()]
        # 配伍判定要放在单选之前："配伍题" 里不含 "单选"，但 "B型题" 可能和
        # "配伍选择题" 混写，所以这里先把配伍相关的字样全部捞出来。
        if "配伍" in s or "匹配" in s or "连线" in s or "B型" in s.upper() or "B1型" in s.upper():
            return cls.MATCH
        # 中文关键字兜底（注意先判多选再判单选："多选题" 不含 "单选"，但保险起见）
        if "多选" in s or "X型" in s.upper():
            return cls.MULTIPLE
        if "判断" in s:
            return cls.JUDGE
        if "填空" in s:
            return cls.FILL
        if "简答" in s or "论述" in s or "问答" in s or "名词解释" in s:
            return cls.SHORT
        if "单选" in s or "A1型" in s.upper() or "A2型" in s.upper() or "A3型" in s.upper():
            return cls.SINGLE
        return cls.OTHER

    @property
    def is_choice(self) -> bool:
        return self in (QType.SINGLE, QType.MULTIPLE, QType.JUDGE)

    @property
    def needs_options(self) -> bool:
        """判断题/填空/简答不需要选项；单选/多选/配伍必须有选项。"""
        return self in (QType.SINGLE, QType.MULTIPLE, QType.MATCH)


@dataclass
class Option:
    """一个选项。`letter` 是 A/B/C/D，`text` 是选项正文（已去掉 A. 前缀）。"""

    letter: str
    text: str

    def to_dict(self) -> dict:
        return {"letter": self.letter, "text": self.text}


@dataclass
class Question:
    """一道题。

    qid 在同一份作业内唯一，默认由 `外部标记 / 题号` 生成，用于和 AI 返回值对齐。
    """

    qid: str
    qtype: QType
    stem: str
    options: List[Option] = field(default_factory=list)
    number: Optional[str] = None
    # 抓题时顺带记下的定位信息（选择器 / 索引），填答时用
    locator: dict = field(default_factory=dict)
    # 原始 DOM 片段，仅用于 --dump-dom 调试
    raw: Optional[str] = None
    # 缓存 / 相似匹配命中的来源，供日志展示
    cache_source: Optional[str] = None
    # 配伍题（B 型题）专用：一组备选字母 + 题组内的若干小题文本。
    # 学习通把配伍题渲染成「选择题题干 + 共享选项列表」，所以 options 是备选，
    # match_items 是「1. xxx」「2. yyy」这样的小题；普通题留空。
    match_options: List[str] = field(default_factory=list)
    match_items: List[str] = field(default_factory=list)
    # 复习/详情页（已提交答卷，`mooc-ans/mooc2/work/view`）会把标准答案印在页面上，
    # 抓题时顺手带回来：review_answer 是页面给的标准答案，review_mine 是本人当时写的。
    # 用途：① 人工核对 AI 答案；② 直接入库复用，避免再问模型（省 token）。
    review_answer: str = ""
    review_mine: str = ""

    @property
    def is_match(self) -> bool:
        return self.qtype == QType.MATCH

    @property
    def has_input(self) -> bool:
        """页面上这道题有没有可作答的控件。

        复习页（已提交答卷）的选项是纯 `<li>` 文本，`has_input == 0` —— 用来判断
        「这是答题页还是复习页」，避免往复习页里空填。注意 `blanks`（填空空数）
        只对有 input 的题有意义，所以这里直接看抽取端记下的控件数。

        新版答题页（2026-10 实测 `mooc-ans/mooc2/work/dowork`）压根没有原生
        `input[type=radio]`：选项是 `<div class="answerBg" role="radio"
        onclick="addChoice(this)">`，点完由页面脚本写进隐藏域 `#answer<qid>`。
        这种情况抽取端会给 `has_choice_ui = 1`，否则会被当成复习页跳过回填。
        """
        loc = self.locator or {}
        return (int(loc.get("has_input") or 0) > 0
                or bool(loc.get("has_text_input"))
                or int(loc.get("has_choice_ui") or 0) > 0)

    @property
    def answer_key(self) -> str:
        """新版 ARIA 答题页隐藏域 `#answer<answer_key>` 里的那串数字。

        抽取端从选项块自己的 ``qid``/``data`` 属性取（形如 215177587），这里再用
        ``self.qid``（形如 ``question215177587``）兜底剥掉前缀；老页面没有隐藏域，
        返回空串，调用方会忽略。
        """
        key = str((self.locator or {}).get("answer_key") or "").strip()
        if key.isdigit():
            return key
        m = re.match(r"^(?:question)?(\d+)$", str(self.qid or "").strip())
        return m.group(1) if m else ""

    @staticmethod
    def match_label(k: int) -> str:
        """配伍题小题的序号：0 -> "1"，1 -> "2"……（prompt 与答案都用它对齐）。"""
        return str(k + 1)

    @property
    def fp(self) -> str:
        """题目指纹：题干归一化 + 选项文本 + 题型。"""
        opt_txt = "|".join(normalize_text(o.text, strip_option_prefix=True) for o in self.options)
        return fingerprint(normalize_text(self.stem, strip_number=True), opt_txt, self.qtype.value)

    @property
    def shingle_text(self) -> str:
        """相似度比较用的文本。

        只用题干：同一题干换一组选项就是另一道题，但实测「题干+选项」的 3-gram
        Jaccard 对「换选项」和「加噪」区分度很差（0.59 vs 0.62），
        所以这里只取题干保证阈值可解释，换选项的情况交给 `option_sig` 精确拦截。
        """
        return normalize_text(self.stem, strip_number=True)

    @property
    def option_sig(self) -> str:
        """选项签名：同一题干换一组选项时用它挡掉误匹配。"""
        return fingerprint(*[normalize_text(o.text, strip_option_prefix=True)
                             for o in self.options]) if self.options else ""

    @property
    def shingle_set(self) -> set:
        return shingles(self.shingle_text)

    def to_prompt_dict(self) -> dict:
        """送给 LLM 的最小结构——刻意不含 DOM/定位信息，省 token。

        配伍题（B 型题）特殊：题干是题组说明，`o` 是共享的备选字母，
        `m` 是小题目文本。为了省 token，小题在 prompt 里统一压缩成
        A/B/C... 的序号形式，答案也用同样的序号回填（见 parse_pairs）。
        """
        d: dict[str, Any] = {"i": self.qid, "t": self.qtype.value, "q": self.stem}
        if self.is_match and self.match_items:
            d["o"] = list(self.match_options or [o.text for o in self.options])
            d["m"] = [self.match_label(k) + "." + t for k, t in enumerate(self.match_items)]
            return d
        if self.options:
            d["o"] = [o.text for o in self.options]
        return d

    def to_dict(self) -> dict:
        return {
            "qid": self.qid,
            "qtype": self.qtype.value,
            "number": self.number,
            "stem": self.stem,
            "options": [o.to_dict() for o in self.options],
            "locator": self.locator,
            "fp": self.fp,
            "match_options": list(self.match_options),
            "match_items": list(self.match_items),
            "review_answer": self.review_answer,
            "review_mine": self.review_mine,
        }


@dataclass
class Answer:
    """一道题的答案。value 的形态随题型而定：

        单选   -> "A"                    （选项字母）
        多选   -> ["A", "C"]             （选项字母列表）
        判断   -> True / False
        填空   -> "答案" 或 ["空1", "空2"]
        简答   -> 文本
        配伍   -> {"1": "B", "2": "A"}   （小题序号 -> 备选字母，见 parse_pairs）
    """

    qid: str = ""
    value: AnswerValue = ""
    confidence: float = 0.0
    source: str = "llm"          # llm | cache | memory | manual | mock | page
    note: Optional[str] = None
    used_tokens: int = 0
    # 配伍题（B 型题）的配对结果：小题序号 -> 备选字母。其它题型为 None。
    pairs: Optional[Dict[str, str]] = None

    def to_dict(self) -> dict:
        return {
            "qid": self.qid,
            "value": self.value,
            "confidence": self.confidence,
            "source": self.source,
            "note": self.note,
            "pairs": self.pairs,
        }

    @property
    def is_empty(self) -> bool:
        if self.pairs:
            return False
        if self.value is None:
            return True
        if isinstance(self.value, str):
            return not self.value.strip()
        if isinstance(self.value, (list, tuple)):
            return len(self.value) == 0
        return False


# --------------------------------------------------------------------------- #
# 文本归一化 / 指纹
# --------------------------------------------------------------------------- #

_PUNCT_RE = re.compile(r"[\s\u3000]+")
# 题号前缀： "1." "1、" "(1)" "第1题" "1）"
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*)?\d+\s*(?:题)?\s*[.、,，:：)）\]】\-]\s*|^\s*[(（\[【]\s*\d+\s*[)）\]】]\s*"
)
# 选项前缀： "A." "A、" "(A)" "A)"  —— 允许前缀后无分隔符
_OPTION_PREFIX_RE = re.compile(r"^\s*[(（\[【]?\s*([A-Za-z])\s*[)）\]】.、,，:：]?\s*")

_PUNCT_MAP = {
    "，": ",", "。": ".", "；": ";", "：": ":", "？": "?", "！": "!",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",",
    "－": "-", "—": "-", "～": "~", "％": "%",
}


def normalize_text(s: Optional[str], *, strip_number: bool = False,
                   strip_option_prefix: bool = False) -> str:
    """归一化文本，用于生成稳定指纹与相似度比较。

    处理：全角转半角、标点统一、去掉所有空白、去掉题号/选项前缀。
    注意这里**不做**小写化以外的语义改写——保留了中文原文，便于相似度比较。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    s = s.strip()
    if strip_number:
        s = _NUMBER_PREFIX_RE.sub("", s).strip()
    if strip_option_prefix:
        s = _OPTION_PREFIX_RE.sub("", s).strip()
    s = _PUNCT_RE.sub("", s)
    return s.lower()


def fingerprint(*parts: str, size: int = 16) -> str:
    """把若干归一化文本拼起来做 sha1 指纹。"""
    h = hashlib.sha1()
    for p in parts:
        h.update(normalize_text(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:size]


def shingles(text: str, n: int = 3) -> set:
    """题干字符 n-gram 集合，用于近似重复题检测（比例匹配，零 token）。"""
    norm = normalize_text(text, strip_number=True)
    if len(norm) <= n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def option_letter(index: int) -> str:
    """0 -> A, 1 -> B, ... 25 -> Z, 26 -> AA"""
    if index < 0:
        return "?"
    letters = ""
    idx = index
    while True:
        letters = chr(ord("A") + idx % 26) + letters
        idx = idx // 26 - 1
        if idx < 0:
            break
    return letters


def parse_letters(value: Any, options: Sequence[Option]) -> List[str]:
    """把 AI 返回的答案松散解析成选项字母列表。

    支持： "A" / "AC" / "A,C" / ["A","C"] / "A. 选项文本" / "选项文本"
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for v in value:
            out.extend(parse_letters(v, options))
        # 去重保序
        seen, uniq = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    s = str(value).strip()
    if not s:
        return []
    if s.lower() in ("true", "t", "yes", "y", "对", "正确", "√", "是"):
        return ["T"]
    if s.lower() in ("false", "f", "no", "n", "错", "错误", "×", "x", "否"):
        return ["F"]

    letters = [o.letter.upper() for o in options]

    # 1) 纯字母 / 字母加分隔符
    compact = re.sub(r"[\s,，、;；/|]+", "", s).upper()
    if compact and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in compact):
        if all(ch in letters for ch in compact) or len(compact) == 1:
            return list(compact)

    # 2) 逐段用选项前缀切割："A.xxx" 或 "A、xxx"
    found: List[str] = []
    for chunk in re.split(r"[\s,，;；]+", s):
        m = _OPTION_PREFIX_RE.match(chunk)
        if m:
            letter = m.group(1).upper()
            if letter in letters and letter not in found:
                found.append(letter)
    if found:
        return found

    # 3) 文本精确/包含匹配
    if options:
        norm_s = normalize_text(s)
        for o in options:
            if normalize_text(o.text) and normalize_text(o.text) == norm_s:
                return [o.letter.upper()]
        for o in options:
            no = normalize_text(o.text)
            if no and (no in norm_s or norm_s in no) and len(no) >= 2:
                return [o.letter.upper()]

    return []


def parse_bool(value: Any) -> Optional[bool]:
    """把 AI 的判断题返回值解析成 bool。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "t", "yes", "y", "1", "对", "正确", "√", "是", "a"):
        return True
    if s in ("false", "f", "no", "n", "0", "错", "错误", "×", "x", "否", "b"):
        return False
    return None


def split_blanks(value: Any) -> List[str]:
    """填空答案换算成「空」列表。支持 "答案1|答案2" / 列表 / 单个字符串。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value]
    s = str(value).strip()
    if not s:
        return []
    if "|" in s:
        return [p.strip() for p in s.split("|")]
    return [s]


def parse_pairs(value: Any, items: Sequence[str], letters: Sequence[str]) -> Dict[str, str]:
    """把配伍题（B 型题）的答案解析成 {小题序号: 备选字母}。

    学习通把 B 型题渲染成一个题组：一组备选（A-E）+ 若干小题，逐小题选一个字母。
    AI 可能返回的形态很杂，这里全部兜住：

        dict          {"1": "B", "2": "A"}
        "1:B,2:A"     小题号:字母
        "B,A,C"       纯字母序列（按下标对应小题）
        ["B", "A"]    列表
        1: "B"        单个字母（只有一道小题时）
        {"1": 2}      数字（1-based，转成字母）

    解析不出来时返回 {}，由调用方决定是否降级（这时仍可退回本地文本匹配）。
    """
    if value is None:
        return {}
    lab = [Question.match_label(k) for k in range(len(items))] or ["1"]

    out: Dict[str, str] = {}
    valid = {str(x).upper() for x in letters} or set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def _norm_letter(raw: Any) -> str:
        """把各种写法归一成大写字母；数字按 1-based 转换。"""
        s = str(raw).strip()
        if not s:
            return ""
        if re.fullmatch(r"\d+", s):
            return option_letter(int(s) - 1)
        s = s.upper()
        if len(s) == 1 and s in valid:
            return s
        # "A." / "(A)" / "A、xxx" 这类
        m = re.match(r"^[（(\[【]?\s*([A-Z])\b", s)
        if m and m.group(1) in valid:
            return m.group(1)
        return ""

    def _norm_key(raw: Any) -> str:
        """把小题标识归一成 1/2/3……"""
        s = str(raw).strip()
        m = re.search(r"\d+", s)
        if m:
            k = m.group(0)
            if k in lab:
                return k
        return ""

    if isinstance(value, dict):
        for k, v in value.items():
            key = _norm_key(k)
            let = _norm_letter(v)
            if key and let:
                out[key] = let
        if out:
            return out

    if isinstance(value, (list, tuple, set)):
        vals = [str(v) for v in value]
        # ["1:A", "2:B"] 或 ["A", "B"]
        if any(re.search(r"[:：=]", v) for v in vals):
            return parse_pairs(";".join(vals), items, letters)
        for k, v in enumerate(vals):
            let = _norm_letter(v)
            if let and k < len(lab):
                out[lab[k]] = let
        return out

    s = str(value).strip()
    if not s:
        return {}

    # "1:A,2:B" / "1.A 2.B" / "1、A；2、B"
    # 也兜住复习页印的 "(1)D (2)A (3)C" —— 序号可能带括号、字母与序号之间可能
    # 没有分隔符（实测真实 work/view 页面就是这种写法）。
    pair_re = re.compile(
        r"[（(\[【]?\s*(\d{1,2})\s*[)）\]】]?\s*[:：.、=]?\s*([A-Za-z])(?![A-Za-z])")
    for m in pair_re.finditer(s):
        key = _norm_key(m.group(1))
        let = _norm_letter(m.group(2))
        if key and let and key not in out:
            out[key] = let
    if out:
        return out

    # 纯字母序列："B,A,C" / "BAC" / "B A C"
    comp = re.sub(r"[\s,，、;；/|]+", "", s).upper()
    if comp and all(ch in valid for ch in comp):
        for k, ch in enumerate(comp):
            if k < len(lab):
                out[lab[k]] = ch
        return out

    # 只有一道小题时，直接给一个字母
    if len(lab) == 1:
        let = _norm_letter(s)
        if let:
            return {lab[0]: let}
    return out


def _pairs_of(text: str) -> set:
    """字符二元组集合（含单字），用于短文本相似度。"""
    t = normalize_text(text)
    if not t:
        return set()
    if len(t) == 1:
        return {t}
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（朴素 DP；配伍题的小题/备选都很短，够用）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, ch2 in enumerate(b, 1):
            if ch == ch2:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def pair_score(item: str, option: str) -> float:
    """小题文本与备选文本的相似度（0~1）。"""
    ni, no = normalize_text(item), normalize_text(option)
    if not ni or not no:
        return 0.0
    if ni in no or no in ni:
        return 1.0
    g = jaccard(_pairs_of(ni), _pairs_of(no))
    lcs = _lcs_len(ni, no) / max(1, min(len(ni), len(no)))
    return max(g, lcs)


def match_items_by_text(items: Sequence[str],
                        bank: Sequence[str]) -> Dict[str, str]:
    """不调模型，用本地文本相似度把小题配到备选字母上。

    贪心且**不做二次分配**：每轮只在「还没被占用的备选」里为当前小题挑最优，
    这样结果必然是合法的一一对应，不会出现两个小题抢同一个字母。
    适用于备选之间差异明显的小题库（医学 B 型题最常见的形态）。
    """
    lab = [Question.match_label(k) for k in range(len(items))]
    letters = [option_letter(i) for i in range(len(bank))]
    results: Dict[str, Tuple[float, str]] = {}
    used: set = set()

    for k, item in enumerate(items):
        best: Optional[Tuple[float, str]] = None
        for i, opt in enumerate(bank):
            if i in used:
                continue
            sc = pair_score(item, opt)
            if best is None or sc > best[0]:
                best = (sc, letters[i])
        if best is None:
            break
        if best[0] <= 0.0:
            # 完全不像：按顺序给一个还没用过的，保证每题都有答案可填
            for i, _opt in enumerate(bank):
                if i not in used:
                    best = (0.0, letters[i])
                    break
        if best is None:
            break
        used.add(letters.index(best[1]))
        results[lab[k]] = best

    return {k: v[1] for k, v in results.items()}


def parse_review_pairs(text: str, items: Sequence[str],
                       letters: Sequence[str]) -> Dict[str, str]:
    """解析复习页印出的配伍题标准答案，形如 ``"(1)D (2)A (3)C"``。

    与 :func:`parse_pairs` 的区别只在容错度：复习页的写法是「(序号)字母」连排，
    序号外面有括号、字母后面直接跟下一个括号。这里按
    「可选左括号 + 序号 + 可选右括号 + 可选分隔符 + 单个字母」逐段匹配。
    """
    s = str(text or "").strip()
    if not s:
        return {}
    valid_letters = {str(x).upper() for x in letters} or set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    out: Dict[str, str] = {}
    # 注意这里的 (?![A-Za-z])：不能让「数字」被当成字母吃掉。
    # 早期版本在字母不在 valid_letters 时直接 continue，会顺手把这一段的字母也
    # 丢掉，于是 "(1)D (2)A" 里 '2' 会被错认成第 1 组的字母 → 结果变成 {'2':'A'}。
    seg = re.compile(r"[（(\[【]?\s*(\d{1,2})\s*[)）\]】]?\s*[:：.、=]?\s*([A-Za-z])(?![A-Za-z])")
    # 小题序号优先按 match_items 的下标一一对应；match_items 缺失或数量对不上时，
    # 直接按页面印出来的序号原样当 key（页面写法 "(1)D (2)A (3)C" 是 1-based）。
    ordinal_to_label = {i + 1: Question.match_label(i) for i in range(len(items))}
    for m in seg.finditer(s):
        num = int(m.group(1))
        let = m.group(2).upper()
        if let not in valid_letters:
            continue
        key = ordinal_to_label.get(num)
        if key is None:
            key = str(num) if num >= 1 else ""
        if key and key not in out:
            out[key] = let
    if out:
        return out
    return parse_pairs(s, items, letters)


def parse_review_answer(text: str, qtype: QType, options: Sequence[Option],
                        match_items: Sequence[str] = ()) -> Answer:
    """把复习页（``mooc-ans`` 的 ``work/view``）印出的「正确答案」解析成 Answer。

    学习通会把自己批改的标准答案直接印在答卷页上（``.rightAnswerContent``）。
    对本次任务这是**免费且权威**的答案来源：直接入库复用，完全省掉 token。
    解析不出来时返回空 Answer，由调用方决定是否再问模型。
    """
    s = str(text or "").strip()
    if not s:
        return Answer()
    letters = [o.letter for o in options]

    if qtype == QType.MATCH:
        pairs = parse_review_pairs(s, match_items, letters or list("ABCDEFGHIJ"))
        if not pairs:
            return Answer()
        compact = ",".join(f"{k}:{v}" for k, v in sorted(pairs.items()))
        return Answer(value=compact, source="page", confidence=1.0, pairs=pairs)

    # 判断题等在页面上印的就是选项文字本身（「正确」「错误」）
    norm = re.sub(r"\s+", "", s)
    for opt in options:
        otext = re.sub(r"\s+", "", opt.text or "")
        if otext and otext == norm:
            return Answer(value=opt.letter, source="page", confidence=1.0)

    picked = parse_letters(s, options)
    if not picked:
        return Answer()
    if qtype == QType.MULTIPLE:
        return Answer(value=list(picked), source="page", confidence=1.0)
    if len(picked) > 1 and qtype == QType.SINGLE:
        picked = picked[:1]
    return Answer(value=picked[0], source="page", confidence=1.0)


def match_display(pairs: Dict[str, str], items: Sequence[str],
                  options: Sequence[Option]) -> str:
    """把配伍题答案渲染成人类可读文本，例如 "1.A（失眠） 2.C（发热）"。"""
    if not pairs:
        return ""
    opt_map = {o.letter.upper(): o.text for o in options}
    lab = [Question.match_label(k) for k in range(len(items))]
    chunks: List[str] = []
    for k, key in enumerate(lab):
        let = pairs.get(key)
        if not let:
            continue
        item = items[k] if k < len(items) else ""
        opt = opt_map.get(let.upper(), "")
        body = "；".join(x for x in (item, opt) if x)
        chunks.append(f"{key}.{let}" + (f"（{body}）" if body else ""))
    return " ".join(chunks)


def as_list(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]
