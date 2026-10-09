"""本地答案库：题库缓存 / 相似题复用。

这是「减少 token 消耗」的主力：同一道题（或高度相似的题）第二次出现时，
直接命中本地库，不产生任何 API 调用。

匹配分三级（成本递增、覆盖面递增）：
    1) fingerprint 完全一致      -> 命中
    2) 题干 3-gram Jaccard 相似  -> 命中（跨作业/跨课程复用）
    3) 都没有                    -> 交给求解器问 LLM，答完回写

存储形态：单文件 SQLite（WAL），零额外依赖。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .answer_codec import letter_to_text, text_to_letter
from .types import (Answer, AnswerValue, Option, Question, QType, fingerprint,
                    jaccard, normalize_text, shingles)

log = logging.getLogger("xxtutor.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    fp          TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL DEFAULT '',
    qtype       TEXT NOT NULL,
    stem        TEXT NOT NULL,
    options     TEXT NOT NULL DEFAULT '[]',
    answer      TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.0,
    source      TEXT NOT NULL DEFAULT 'llm',
    note        TEXT,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_q_course ON questions(course_id);

CREATE TABLE IF NOT EXISTS shingles (
    fp       TEXT NOT NULL,
    course_id TEXT NOT NULL DEFAULT '',
    gram     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_s_course_gram ON shingles(course_id, gram);
CREATE INDEX IF NOT EXISTS idx_s_fp ON shingles(fp);

CREATE TABLE IF NOT EXISTS llm_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    course_id   TEXT,
    assignment  TEXT,
    model       TEXT,
    request_n   INTEGER DEFAULT 0,
    prompt_tok  INTEGER DEFAULT 0,
    completion_tok INTEGER DEFAULT 0,
    cached_tok  INTEGER DEFAULT 0,
    cost_note   TEXT
);
"""

MAX_SHINGLES_PER_QUESTION = 256


class AnswerStore:
    """SQLite 答案库。线程安全（每次操作独立连接 + 写锁）。"""

    def __init__(self, path: str | Path = ":memory:",
                 similarity_threshold: float = 0.92,
                 persist: bool = True) -> None:
        self.path = str(path)
        self.similarity_threshold = float(similarity_threshold)
        self.persist = bool(persist)
        self._lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None

        if self.path == ":memory:" or self.path.startswith("file::memory:"):
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            p = Path(self.path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.commit()

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _conn(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        c = sqlite3.connect(self.path, timeout=30.0)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        return c

    def close(self) -> None:
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    def __enter__(self) -> "AnswerStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def lookup(self, q: Question, course_id: str = "",
               allow_similarity: bool = True) -> Optional[Answer]:
        """按指纹 / 相似度查答案。命中会累加 hits 计数。"""
        fp = q.fp

        with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM questions WHERE fp = ?", (fp,)
                ).fetchone()
                if row:
                    ans = self._row_to_answer(row, source="cache", q=q)
                    if ans is not None:
                        c.execute(
                            "UPDATE questions SET hits = hits + 1, updated_at = ? WHERE fp = ?",
                            (time.time(), fp),
                        )
                        c.commit()
                        return ans

                if not allow_similarity or self.similarity_threshold <= 0:
                    return None

                best = self._find_similar(c, q, course_id)
                if best is None:
                    return None
                row, score = best
                ans = self._row_to_answer(row, source="memory", q=q)
                if ans is None:
                    return None
                c.execute(
                    "UPDATE questions SET hits = hits + 1, updated_at = ? WHERE fp = ?",
                    (time.time(), row["fp"]),
                )
                c.commit()
                sim_pct = round(score * 100, 1)
                ans.note = ((ans.note + " | ") if ans.note else "") + \
                    f"相似题复用 {sim_pct}% ({row['fp'][:8]})"
                log.info("相似命中 %.1f%%  q=%s  <- %s",
                         sim_pct, q.stem[:40], row["stem"][:40])
                return ans

    def _find_similar(self, c: sqlite3.Connection, q: Question,
                      course_id: str) -> Optional[Tuple[sqlite3.Row, float]]:
        grams = q.shingle_set
        if not grams:
            return None

        # 用「最稀有」的若干 gram 做倒排查询：稀有 gram 区分度高，候选集小。
        # 注意：这里**不按 course_id 过滤**——同一道题完全可能出现在不同课程里，
        # 精确指纹命中本来也是全局的；收窄到课程会白白丢掉大量可复用的答案。
        freq: Dict[str, int] = {}
        gram_list = sorted(grams)
        for i in range(0, len(gram_list), 200):
            chunk = gram_list[i:i + 200]
            marks = ",".join("?" * len(chunk))
            for r in c.execute(
                f"SELECT gram, COUNT(*) AS n FROM shingles WHERE gram IN ({marks}) "
                "GROUP BY gram",
                tuple(chunk),
            ).fetchall():
                freq[r["gram"]] = int(r["n"])
        # 库中没有的 gram 视为最稀有（freq=0），优先探测
        probe = sorted(gram_list, key=lambda g: (freq.get(g, 0), g))[:12]
        holders: Dict[str, int] = {}
        for g in probe:
            rows = c.execute(
                "SELECT fp, COUNT(*) AS n FROM shingles WHERE gram = ? GROUP BY fp",
                (g,),
            ).fetchall()
            for r in rows:
                holders[r["fp"]] = holders.get(r["fp"], 0) + r["n"]
        if not holders:
            return None

        # 命中 probe 数最多的前 N 个候选再精算 Jaccard
        cand_fps = [fp for fp, _ in sorted(holders.items(), key=lambda kv: -kv[1])[:40]]
        if not cand_fps:
            return None

        placeholders = ",".join("?" * len(cand_fps))
        cand_rows = c.execute(
            f"SELECT * FROM questions WHERE fp IN ({placeholders})", cand_fps
        ).fetchall()
        if not cand_rows:
            return None

        stored: Dict[str, set] = {}
        for row in cand_rows:
            gs = c.execute(
                "SELECT gram FROM shingles WHERE fp = ?", (row["fp"],)
            ).fetchall()
            stored[row["fp"]] = {g["gram"] for g in gs}

        best_row, best_score = None, 0.0
        for row in cand_rows:
            if row["qtype"] != q.qtype.value:
                continue
            # 题干很像但选项完全换了 -> 是另一道题，必须挡住
            if q.option_sig:
                try:
                    row_opts = json.loads(row["options"] or "[]")
                except Exception:
                    row_opts = []
                if row_opts and fingerprint(*[normalize_text(str(o.get("text", "")))
                                              for o in row_opts]) != q.option_sig:
                    log.debug("相似候选被选项签名挡下: %s", row["stem"][:30])
                    continue
            elif row["options"] not in ("[]", "", None):
                # 题干型题目（填空/简答）不去匹配带选项的题
                continue
            score = jaccard(grams, stored.get(row["fp"], set()))
            if score > best_score:
                best_row, best_score = row, score

        if best_row is not None and best_score >= self.similarity_threshold:
            return best_row, best_score
        return None

    @staticmethod
    def _is_pair_dict(value: Any) -> bool:
        """辨认「配伍题配对结果」：{"1":"A","2":"B"} 这种 小题序号 -> 字母。

        刻意要求宽松（值可以是 "A." 之类），避免 AI 多写个标点就存不进去。
        """
        if not isinstance(value, dict) or not value:
            return False
        for k, v in value.items():
            if not re.fullmatch(r"\d{1,2}", str(k).strip()):
                return False
            s = str(v).strip().upper().rstrip(".．、。")
            if not (len(s) == 1 and "A" <= s <= "Z"):
                return False
        return True

    @staticmethod
    def _row_to_answer(row: sqlite3.Row, source: str = "cache",
                       q: Optional[Question] = None) -> Optional[Answer]:
        """把库里一行还原成 Answer。

        库内 `answer` 存的是**选项正文**（见 `answer_codec` 的说明：真实 doWork 页每次
        加载都会打乱屏显字母，只有正文才跨运行稳定）。给了**当前题目** `q` 时按它的
        选项表把正文换回当前页面的字母；换不回去（题面被改过）就返回 None，当作未命中，
        宁可重新问模型也不要填错选项。
        """
        try:
            value = json.loads(row["answer"])
        except (json.JSONDecodeError, TypeError):
            value = row["answer"]
        # 库内值先按当前题面换算回字母（库内是正文；配伍题换回来就是个配对字典）
        if q is not None:
            mapped = text_to_letter(q, value)
            if mapped is None:
                log.info("答案库里的正文对不上当前题面的选项，视为未命中：%s",
                         (row["stem"] or "")[:40])
                return None
            value = mapped
        pairs = None
        if AnswerStore._is_pair_dict(value):
            # 配伍题：{"1": "A", "2": "B"} —— 解出来同时喂给 value 和 pairs，
            # 这样缓存命中后回填页面依然能按小题逐个点选。
            pairs = {str(k).strip(): str(v).strip().upper().rstrip(".．、。")
                     for k, v in value.items()}
            value = ",".join(f"{k}:{pairs[k]}" for k in sorted(pairs, key=lambda x: int(x)))
        return Answer(
            qid="",
            value=value,
            confidence=float(row["confidence"] or 0.0),
            source=source,
            note=row["note"],
            pairs=pairs,
        )

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def put(self, q: Question, ans: Answer, course_id: str = "") -> None:
        """写入/更新一道题的答案，并重建它的 shingle 索引。"""
        if not self.persist:
            return
        key = q.fp
        grams = sorted(q.shingle_set)[:MAX_SHINGLES_PER_QUESTION]
        now = time.time()
        # 库内一律存**选项正文**（不是字母）：真实页面每次加载都会打乱屏显字母，
        # 存字母会让下一次「缓存命中」静默指向别的选项。见 answer_codec 的说明。
        # 配伍题存 {"1":"正文","2":"正文"}，单选存 str，多选存 list[str]。
        stored_value: Any = dict(ans.pairs) if ans.pairs else ans.value
        stored_value = letter_to_text(q, stored_value)
        payload = json.dumps(stored_value, ensure_ascii=False)

        with self._lock:
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO questions
                        (fp, course_id, qtype, stem, options, answer, confidence,
                         source, note, hits, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,0,?,?)
                    ON CONFLICT(fp) DO UPDATE SET
                        answer=excluded.answer,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        note=excluded.note,
                        qtype=excluded.qtype,
                        stem=excluded.stem,
                        options=excluded.options,
                        updated_at=excluded.updated_at
                    """,
                    (
                        key, course_id or "", q.qtype.value,
                        q.stem,
                        json.dumps([o.to_dict() for o in q.options], ensure_ascii=False),
                        payload, float(ans.confidence or 0.0),
                        ans.source or "llm", ans.note, now, now,
                    ),
                )
                c.execute("DELETE FROM shingles WHERE fp = ?", (key,))
                if grams:
                    c.executemany(
                        "INSERT INTO shingles (fp, course_id, gram) VALUES (?,?,?)",
                        [(key, course_id or "", g) for g in grams],
                    )
                c.commit()

    def put_many(self, pairs: Iterable[Tuple[Question, Answer]], course_id: str = "") -> int:
        n = 0
        for q, a in pairs:
            self.put(q, a, course_id=course_id)
            n += 1
        return n

    def correct(self, q: Question, value: AnswerValue, course_id: str = "",
                note: str = "人工修正", pairs: Optional[Dict[str, str]] = None) -> None:
        """人工修正一条答案（下次自动命中）。

        `value` 既接受 AnswerValue、也接受整个 Answer（方便调用方把 pairs 一起带进来）。
        """
        if isinstance(value, Answer):
            ans = value
            ans.source = "manual"
            ans.confidence = 1.0
            ans.note = ans.note or note
        else:
            ans = Answer(qid=q.qid, value=value, confidence=1.0, source="manual",
                         note=note, pairs=pairs)
        self.persist = True
        self.put(q, ans, course_id=course_id)

    # ------------------------------------------------------------------ #
    # 统计 / 导出
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
            by_type = {
                r["qtype"]: r["n"]
                for r in c.execute(
                    "SELECT qtype, COUNT(*) AS n FROM questions GROUP BY qtype"
                ).fetchall()
            }
            hits = c.execute("SELECT COALESCE(SUM(hits),0) AS h FROM questions").fetchone()["h"]
            recent = c.execute(
                "SELECT COUNT(*) AS n FROM questions WHERE updated_at > ?",
                (time.time() - 86400,),
            ).fetchone()["n"]
            usage = c.execute(
                "SELECT COALESCE(SUM(prompt_tok),0) AS p, "
                "COALESCE(SUM(completion_tok),0) AS c, "
                "COALESCE(SUM(request_n),0) AS r FROM llm_usage"
            ).fetchone()
        return {
            "questions": total,
            "by_type": by_type,
            "reuse_hits": hits,
            "added_24h": recent,
            "llm_requests": usage["r"],
            "prompt_tokens": usage["p"],
            "completion_tokens": usage["c"],
        }

    def export_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            rows = c.execute(
                "SELECT fp, course_id, qtype, stem, options, answer, confidence, "
                "source, note, hits FROM questions ORDER BY updated_at DESC"
            ).fetchall()
        data = []
        for r in rows:
            data.append({
                "fp": r["fp"],
                "course_id": r["course_id"],
                "qtype": r["qtype"],
                "stem": r["stem"],
                "options": json.loads(r["options"] or "[]"),
                "answer": json.loads(r["answer"] or "null"),
                "confidence": r["confidence"],
                "source": r["source"],
                "note": r["note"],
                "hits": r["hits"],
            })
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def log_usage(self, *, course_id: str = "", assignment: str = "", model: str = "",
                  request_n: int = 0, prompt_tok: int = 0, completion_tok: int = 0,
                  cached_tok: int = 0, cost_note: str = "") -> None:
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO llm_usage (ts, course_id, assignment, model, request_n, "
                    "prompt_tok, completion_tok, cached_tok, cost_note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), course_id, assignment, model, request_n,
                     prompt_tok, completion_tok, cached_tok, cost_note),
                )
                c.commit()
