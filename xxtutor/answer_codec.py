# -*- coding: utf-8 -*-
"""答案坐标编解码：库内一律存「选项正文」，进出库时换算成「当前页面的字母」。

为什么不能存字母
----------------
真实 doWork 页**每次加载都会随机化选项的屏显字母**：`<span class="num_option">` 里
显示的可能是 A，而真正决定提交内容的 `data` 属性可能是 E（页面常把 A 留给标准答案）。
选项**正文的先后顺序**倒是稳定的（实测 73/73 题跨运行一致）。

`Question.fp` 是「题干 + 按顺序拼接的选项正文」，与字母无关，所以换一次字母后指纹
照样命中；如果库里存的是字母，命中的答案就会**静默指向另一段正文**——症状是
「缓存命中 100%、日志全绿，但填进去的是错选项」。

因此库内统一存**正文**：
- 单选  ->  "心绞痛——强迫坐位"                       （str）
- 多选  ->  ["淋巴瘤", "传染性单核细胞增多症", ...]    （list[str]）
- 配伍  ->  {"1": "慌张步态", "2": "间隙性跛行"}       （dict[str, str]）
出库时按**当前这道题**的选项表把正文换回字母，进库时反过来。
对不上的（题面被改、选项被换）返回 None，让调用方当作未命中，重新问模型。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .types import Question, normalize_text


def _norm(text: str) -> str:
    return normalize_text(text, strip_option_prefix=True)


def option_text(q: Question, letter: str) -> str:
    """取某字母对应的正文（取不到返回空串）。测试与排查常用。"""
    want = str(letter or "").strip().upper()
    for o in q.options:
        if o.letter.upper() == want:
            return o.text
    return ""


def letter_to_text(q: Question, value: Any) -> Any:
    """字母 -> 正文。找不到的字母原样保留（宁可存个可疑值，也不要静默丢答案）。"""
    if not q.options:
        # 填空/简答：答案本来就是文字，不需要、也无法换算
        return value
    if isinstance(value, list):
        return [letter_to_text(q, v) for v in value]
    if isinstance(value, dict):
        return {str(k): letter_to_text(q, v) for k, v in value.items()}
    s = str(value or "").strip().upper()
    if not s:
        return value
    for o in q.options:
        if o.letter.upper() == s:
            return o.text
    return value


def _resolve(q: Question, text: str) -> Optional[str]:
    """正文 -> 当前页面的字母。"""
    want = _norm(text)
    if not want:
        return None
    for o in q.options:
        if _norm(o.text) == want:
            return o.letter.upper()
    # 页面可能在正文前又加了 "A." 之类前缀，或被截断——再放宽一次包含比较
    for o in q.options:
        cand = _norm(o.text)
        if cand and (cand in want or want in cand):
            return o.letter.upper()
    return None


def text_to_letter(q: Question, value: Any) -> Optional[Any]:
    """正文 -> 字母。任何一项对不上就返回 None（整条答案视为未命中）。

    没有选项的题（填空 / 简答）不参与坐标换算：它们的答案本来就是文字，
    原样返回。
    """
    if not q.options:
        return value
    if isinstance(value, list):
        out: List[str] = []
        for v in value:
            L = _resolve(q, str(v))
            if L is None:
                return None
            out.append(L)
        return sorted(set(out))
    if isinstance(value, dict):
        out_d: Dict[str, str] = {}
        for k, v in value.items():
            L = _resolve(q, str(v))
            if L is None:
                return None
            out_d[str(k).strip()] = L
        return out_d
    if not isinstance(value, str):
        return None
    L = _resolve(q, value)
    return L


def normalize_stored(q: Question, value: Any) -> Any:
    """把历史遗留的「字母值」升级成「正文值」，用于一次性数据迁移。"""
    return letter_to_text(q, value)


def looks_like_text_value(q: Question, value: Any) -> bool:
    """值看起来已经是正文（而不是裸字母），用来做迁移的幂等判断。"""
    if isinstance(value, list):
        return all(not (isinstance(v, str) and len(v.strip()) == 1 and v.strip().isalpha())
                   for v in value)
    if isinstance(value, dict):
        return all(not (isinstance(v, str) and len(v.strip()) == 1 and v.strip().isalpha())
                   for v in value.values())
    if isinstance(value, str):
        s = value.strip()
        return not (len(s) == 1 and s.isalpha())
    return False
