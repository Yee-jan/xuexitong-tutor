"""答题调度器：缓存优先 + 批量压缩 Prompt。

流程：
    questions
        ├─ 本地答案库（指纹）        ──命中──► Answer(source=cache)      0 token
        ├─ 本地答案库（相似题 Jaccard）──命中──► Answer(source=memory)    0 token
        └─ 剩余新题 → 按题型分组批量问 LLM → 解析 → 回写答案库

省 token 的关键都在这里：
    * 只有「没见过」的题才会发出请求
    * 一次请求塞 batch_size 道题，system prompt 只付一次
    * 请求体里只有 题号+题型+题干+选项，没有 DOM、没有定位信息、没有历史对话
    * 回复体只有 {"ans":[{"i":..,"a":..}]}，不含题干复述、不含解释
    * 超过 max_tokens_per_assignment 直接熔断，避免失控烧钱
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .llm import LLMClient, LLMError, TokenBudgetExceeded, Usage, extract_json
from .store import AnswerStore
from .types import (
    Answer,
    AnswerValue,
    QType,
    Question,
    parse_bool,
    parse_letters,
    parse_pairs,
    parse_review_answer,
    split_blanks,
)

log = logging.getLogger("xxtutor.solver")

# 极简 system prompt：固定不变，便于命中厂商侧 prompt 缓存
SYSTEM_PROMPT = (
    "你是答题引擎，只输出 JSON，不解释。"
    '输入 {"qs":[{"i":序号,"t":题型,"q":题干,"o":选项数组(可能没有),"m":配伍题小题数组(可能没有)}]}；'
    '输出 {"ans":[{"i":序号,"a":答案}]}。'
    "答案格式："
    'single（含 A1/A2/A3 型单选题）填选项字母如 "A"；'
    'multiple（含 X 型多选题）填字母数组如 ["A","C"]，多选漏项不得分，务必选全；'
    'judge 填 true/false；fill 多空用 | 连接；short 填简明中文作答；'
    'match（B 型配伍题）给每个小题配一个备选字母，用 "小题序号:字母" 以逗号连接，'
    '如 {"m":["1.失眠","2.发热"],"o":["入睡困难","体温升高"]} -> "1:A,2:B"，'
    "每个字母只能用一个小题，个数必须和小题数一致。"
    "必须回答全部序号，字母必须来自选项，不确定也要给最可能的答案。"
)

_TYPE_HINT = {
    QType.SINGLE: "单选",
    QType.MULTIPLE: "多选",
    QType.JUDGE: "判断",
    QType.FILL: "填空",
    QType.SHORT: "简答",
    QType.MATCH: "配伍",
    QType.OTHER: "",
}


@dataclass
class SolveReport:
    total: int = 0
    from_cache: int = 0          # 指纹精确命中
    from_memory: int = 0         # 相似题命中
    from_llm: int = 0
    from_mock: int = 0
    from_page: int = 0           # 复习页印在页面上的标准答案（零 token）
    from_file: int = 0           # 「外接大脑」答案文件贡献的题数
    replaced_from_file: int = 0  # 其中覆盖了页面/缓存/模型答案的题数
    failed: int = 0
    skipped: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    elapsed_s: float = 0.0
    failures: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def reuse_ratio(self) -> float:
        if not self.total:
            return 0.0
        return (self.from_cache + self.from_memory + self.from_page) / self.total

    def as_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["total_tokens"] = self.total_tokens
        d["reuse_ratio"] = round(self.reuse_ratio, 3)
        return d

    def summary_line(self) -> str:
        extra = ""
        if self.from_file or self.replaced_from_file:
            extra = (f" | 外部答案 {self.from_file}"
                     f"（覆盖 {self.replaced_from_file}）")
        return (
            f"共 {self.total} 题 | 缓存命中 {self.from_cache} | 相似复用 {self.from_memory} | "
            f"页面答案 {self.from_page} | "
            f"LLM {self.from_llm} | 失败 {self.failed} | 请求 {self.requests} 次 | "
            f"token {self.total_tokens}（复用率 {self.reuse_ratio:.0%}）| {self.elapsed_s:.1f}s"
            f"{extra}"
        )


# --------------------------------------------------------------------------- #

class Solver:
    def __init__(self, cfg: Any, client: LLMClient, store: AnswerStore) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self._budget_lock = threading.Lock()
        self._budget_used = 0

    # ------------------------------------------------------------------ #
    def solve(self,
              questions: Sequence[Question],
              *,
              course_id: str = "",
              assignment: str = "",
              allow_llm: bool = True,
              manual: Optional[Dict[str, AnswerValue]] = None) -> Tuple[List[Answer], SolveReport]:
        """解答一批题目，返回顺序与入参一致。"""
        t0 = time.time()
        rep = SolveReport(total=len(questions))
        answers: Dict[str, Answer] = {}
        pending: List[Question] = []
        manual = manual or {}

        # 1) 人工预置答案优先级最高
        for q in questions:
            if q.qid in manual:
                answers[q.qid] = Answer(qid=q.qid, value=manual[q.qid],
                                        confidence=1.0, source="manual")

        # 2) 复习页印在页面上的标准答案（零 token、且是学习通自己批改的结果）
        #    真实 `mooc-ans/mooc2/work/view`（已提交作业的答卷页）会把正确答案
        #    写在 .rightAnswerContent 里，抽取端存进 q.review_answer。
        #    优先级排在人工预置之后、本地答案库之前：它比缓存更权威（缓存是
        #    我们之前存进去的，页面是官方批改），同时顺手把这些答案写回库里复用。
        if getattr(self.cfg.solver, "use_page_answers", True):
            for q in questions:
                if q.qid in answers or not q.review_answer:
                    continue
                ans = parse_review_answer(q.review_answer, q.qtype, q.options,
                                          q.match_items)
                if ans.is_empty:
                    log.debug("复习页答案解析失败，交回模型: %s -> %r",
                              q.stem[:30], q.review_answer[:40])
                    continue
                ans.qid = q.qid
                ans.source = "page"
                ans.note = ans.note or "复习页标准答案"
                answers[q.qid] = ans
                rep.from_page += 1
                if getattr(self.cfg.solver, "save_page_answers", True):
                    try:
                        self.store.put(q, ans, course_id=course_id)
                    except Exception as e:  # 入库失败不影响本次作答
                        log.warning("复习页答案写回题库失败: %s", e)

        # 3) 本地答案库（零 token）
        use_cache = bool(self.cfg.solver.use_cache)
        for q in questions:
            if q.qid in answers:
                continue
            hit: Optional[Answer] = None
            if use_cache:
                hit = self.store.lookup(
                    q, course_id=course_id,
                    allow_similarity=bool(self.cfg.solver.use_similarity),
                )
            if hit is not None:
                hit.qid = q.qid
                answers[q.qid] = hit
                if hit.source == "cache":
                    rep.from_cache += 1
                else:
                    rep.from_memory += 1
            else:
                pending.append(q)

        log.info("答案库命中 %d 题（精确 %d / 相似 %d），页面标准答案 %d 题，待问模型 %d 题",
                 rep.from_cache + rep.from_memory, rep.from_cache, rep.from_memory,
                 rep.from_page, len(pending))

        # 4) 剩余新题交给 LLM
        if pending and allow_llm:
            if not self.client.is_mock:
                self._assert_api_key()
            new_answers = self._solve_new(pending, rep, assignment=assignment)
            for q in pending:
                a = new_answers.get(q.qid)
                if a is None:
                    rep.failed += 1
                    rep.failures.append({"qid": q.qid, "stem": q.stem[:80],
                                         "reason": "模型未返回该题答案"})
                    continue
                answers[q.qid] = a
                if a.source == "mock":
                    rep.from_mock += 1
                else:
                    rep.from_llm += 1
        elif pending:
            rep.skipped = len(pending)

        # 5) 低置信度过滤
        min_conf = float(self.cfg.solver.min_confidence or 0.0)
        if min_conf > 0:
            for q in questions:
                a = answers.get(q.qid)
                if a and a.confidence < min_conf and a.source != "manual":
                    log.warning("置信度过低(%.2f)已丢弃: %s", a.confidence, q.stem[:40])
                    del answers[q.qid]

        ordered = [answers[q.qid] for q in questions if q.qid in answers]
        rep.elapsed_s = time.time() - t0
        log.info(rep.summary_line())
        return ordered, rep

    # ------------------------------------------------------------------ #
    def _assert_api_key(self) -> None:
        # mock 模式（离线自测/演示）不需要真实 Key
        if getattr(self.client, "is_mock", False):
            return
        if not self.client.api_key:
            raise LLMError(
                "缺少 API Key。请设置环境变量 "
                f"{self.cfg.llm.api_key_env}，或在 config.json 的 llm.api_key 里填写。"
            )

    # ------------------------------------------------------------------ #
    def _solve_new(self, questions: List[Question], rep: SolveReport,
                   assignment: str = "") -> Dict[str, Answer]:
        """把新题按题型分组，分组批量请求。"""
        groups: Dict[QType, List[Question]] = {}
        for q in questions:
            groups.setdefault(q.qtype, []).append(q)

        batch_size = max(1, int(self.cfg.llm.batch_size))
        batches: List[List[Question]] = []
        for qtype, qs in groups.items():
            for i in range(0, len(qs), batch_size):
                batches.append(qs[i:i + batch_size])

        results: Dict[str, Answer] = {}
        fail_batches: List[Tuple[List[Question], str]] = []
        concurrency = max(1, int(self.cfg.llm.concurrency))

        if concurrency == 1 or len(batches) == 1:
            for b in batches:
                try:
                    results.update(self._solve_batch(b, rep))
                except TokenBudgetExceeded:
                    raise
                except LLMError as e:
                    fail_batches.append((b, str(e)))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futs = {pool.submit(self._solve_batch, b, rep): b for b in batches}
                for fut in as_completed(futs):
                    b = futs[fut]
                    try:
                        results.update(fut.result())
                    except TokenBudgetExceeded:
                        raise
                    except LLMError as e:
                        fail_batches.append((b, str(e)))

        # 整批失败的题单独重试一次（拆成单题，避免一道坏题拖累整批）
        if fail_batches:
            log.warning("%d 个批次失败，改为逐题重试", len(fail_batches))
            singles: List[Question] = []
            for b, err in fail_batches:
                log.debug("批次失败原因: %s", err)
                singles.extend(b)
            for q in singles:
                try:
                    results.update(self._solve_batch([q], rep))
                except TokenBudgetExceeded:
                    raise
                except LLMError as e:
                    rep.failures.append({"qid": q.qid, "stem": q.stem[:80], "reason": str(e)})

        # 回写答案库
        if self.cfg.store.persist:
            pairs = [(q, results[q.qid]) for q in questions if q.qid in results]
            n = self.store.put_many(pairs)
            log.debug("回写答案库 %d 条", n)
        if rep.requests:
            self.store.log_usage(
                course_id="", assignment=assignment, model=self.client.model,
                request_n=rep.requests, prompt_tok=rep.prompt_tokens,
                completion_tok=rep.completion_tokens, cached_tok=rep.cached_tokens,
            )
        return results

    # ------------------------------------------------------------------ #
    def _solve_batch(self, batch: List[Question], rep: SolveReport) -> Dict[str, Answer]:
        system = (getattr(self.cfg.llm, "system_prompt", "") or "").strip() or SYSTEM_PROMPT
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_payload(batch)},
        ]
        self._check_budget()
        attempts = max(1, int(self.cfg.solver.parse_retry) + 1)
        last_content = ""
        for attempt in range(attempts):
            try:
                res = self.client.chat(
                    messages, json_mode=True,
                    # 输出长度按题量估算，避免模型话痨
                    max_tokens=min(self.cfg.llm.max_tokens,
                                   max(256, 90 * len(batch) + 64)),
                )
            except LLMError as e:
                raise LLMError(str(e)) from e

            with self._budget_lock:
                self._budget_used += res.total_tokens
            rep.requests += 1
            rep.prompt_tokens += res.prompt_tokens
            rep.completion_tokens += res.completion_tokens
            rep.cached_tokens += res.cached_tokens
            last_content = res.content

            parsed = self._parse_answers(res.content, batch)
            if parsed:
                return parsed
            if attempt < attempts - 1:
                log.warning("第 %d 次返回无法解析，重试该批（%d 题）", attempt + 1, len(batch))
                messages = messages + [
                    {"role": "assistant", "content": res.content[:1500]},
                    {"role": "user", "content":
                        '格式错误。只输出 {"ans":[{"i":序号,"a":答案}, ...]} 这个 JSON，'
                        "覆盖全部序号，不要任何解释文字。"},
                ]

        raise LLMError(f"无法解析模型返回: {last_content[:200]!r}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _user_payload(batch: List[Question]) -> str:
        return json.dumps(
            {"qs": [q.to_prompt_dict() for q in batch]},
            ensure_ascii=False, separators=(",", ":"),
        )

    def _check_budget(self) -> None:
        cap = int(self.cfg.llm.max_tokens_per_assignment or 0)
        if cap <= 0:
            return
        with self._budget_lock:
            if self._budget_used >= cap:
                raise TokenBudgetExceeded(
                    f"本次作业已消耗 {self._budget_used} token，达到上限 {cap}，已停止调用模型。"
                    "（可在 config.json 的 llm.max_tokens_per_assignment 调整）"
                )

    # ------------------------------------------------------------------ #
    def _parse_answers(self, content: str, batch: List[Question]) -> Dict[str, Answer]:
        data = extract_json(content)
        if not isinstance(data, dict):
            return {}
        raw = data.get("ans")
        if raw is None:
            for alt in ("answers", "a", "result", "data"):
                if isinstance(data.get(alt), (list, dict)):
                    raw = data[alt]
                    break
        if isinstance(raw, dict):
            items = [{"i": k, "a": v} for k, v in raw.items()]
        elif isinstance(raw, list):
            items = raw
        else:
            return {}

        by_qid = {q.qid: q for q in batch}
        out: Dict[str, Answer] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            key = str(it.get("i", it.get("qid", it.get("id", "")))).strip()
            q = by_qid.get(key)
            if q is None:
                continue
            value = self._coerce(it.get("a", it.get("answer")), q)
            pairs = None
            if value is None and q.qtype == QType.MATCH:
                value, pairs = self._coerce_match(it.get("a", it.get("answer")), q)
            if value is None:
                continue
            conf = it.get("c", it.get("confidence", 0.85))
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.85
            out[q.qid] = Answer(
                qid=q.qid, value=value,
                confidence=max(0.0, min(1.0, conf)),
                source="mock" if self.client.is_mock else "llm",
                used_tokens=0,
                pairs=pairs,
            )
        return out

    @staticmethod
    def _coerce(value: Any, q: Question) -> Optional[AnswerValue]:
        """把模型返回的松散答案转成填答层能直接用的形态。"""
        if value is None:
            return None

        # 配伍题必须走 _coerce_match 才能拿到「小题 -> 字母」的配对关系，
        # 落到下面「简答」分支只会把整串当文本存起来、丢掉配对。
        if q.qtype == QType.MATCH:
            return None

        if q.qtype == QType.JUDGE:
            b = parse_bool(value)
            if b is None:
                return None
            # 有选项时转成选项字母（DOM 是 radio，需要点字母对应项）
            if q.options:
                letter = q.options[0].letter if b else (q.options[-1].letter
                                                        if len(q.options) > 1 else None)
                if letter is None:
                    return None
                return letter
            return b

        if q.qtype in (QType.SINGLE, QType.MULTIPLE):
            letters = parse_letters(value, q.options)
            if not letters:
                return None
            if q.qtype == QType.SINGLE:
                return letters[0]
            # X 型多选：模型可能只回一个字母，那本质就是单选；超过一个才按多选处理
            return letters[0] if len(letters) == 1 else letters

        if q.qtype == QType.FILL:
            blanks = split_blanks(value)
            if not blanks:
                return None
            return blanks[0] if len(blanks) == 1 else blanks

        # 简答及其它
        s = str(value).strip()
        if not s:
            return None
        return s

    @staticmethod
    def _coerce_match(value: Any, q: Question) -> Tuple[Optional[AnswerValue], Optional[dict]]:
        """配伍题（B 型题）：把模型答案解析成 {小题序号: 备选字母}。

        返回 `(展示用字符串, pairs)`。pairs 为空时返回 (None, None)，
        让这道题进入「待答」状态，由填答层的本地文本匹配兜底。
        """
        if value is None:
            return None, None
        letters = [o.letter for o in q.options]
        items = list(q.match_items or [])

        pairs: Dict[str, str] = {}
        if isinstance(value, dict):
            for k, v in value.items():
                m = re.search(r"\d+", str(k))
                if not m:
                    continue
                key = m.group(0)
                s = str(v).strip().upper()
                if len(s) == 1 and s in [x.upper() for x in letters]:
                    pairs[key] = s
        else:
            if isinstance(value, (list, tuple)):
                raw = ",".join(str(x) for x in value)
            else:
                raw = str(value)
            first = raw.strip().upper()[:1]
            if raw.strip() and raw.strip().upper() in [x.upper() for x in letters] and len(items) == 1:
                # 单个小题直接回了字母
                pairs = {Question.match_label(0): raw.strip().upper()}
            elif len(items) == 1 and first in [x.upper() for x in letters]:
                pairs = {Question.match_label(0): first}
            else:
                pairs = parse_pairs(raw, items, letters)

        if not pairs:
            return None, None
        # 权重交给展示层：value 用紧凑的 "1:A,2:B"，pairs 供填答层精确使用
        compact = ",".join(f"{k}:{v}" for k, v in sorted(pairs.items(), key=lambda kv: kv[0]))
        return compact, pairs


# --------------------------------------------------------------------------- #
def merge_and_sort(questions: Sequence[Question], answers: Iterable[Answer]) -> List[Answer]:
    """按题目顺序排列答案，缺失的补空。"""
    m = {a.qid: a for a in answers}
    return [m.get(q.qid, Answer(qid=q.qid, value="", source="empty")) for q in questions]
