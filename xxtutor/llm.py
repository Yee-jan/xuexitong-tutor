"""LLM 客户端：OpenAI 兼容协议（DeepSeek / 阿里 / 月之暗面 / 智谱 / Ollama / vLLM 均可）。

只用标准库 urllib，不引入 openai SDK，避免版本冲突。
内置一个 mock 后端，用于离线自测整条流水线（不消耗任何 token）。

省 token 的设计：
    - system prompt 极简（约 120 字），且固定不变以便命中厂商的 prompt 缓存
    - 批量提交：一次请求塞多道题，共同分摊 system prompt
    - 结构化短输出：只回 {"i":序号,"a":答案}，不回题干、不回选项、不解释
    - 关闭思维链/长推理输出
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("xxtutor.llm")


class LLMError(RuntimeError):
    """所有 LLM 调用失败的统一异常。"""


class TokenBudgetExceeded(LLMError):
    """本次作业消耗超过配置上限，主动熔断。"""


# --------------------------------------------------------------------------- #
# 结果与用量
# --------------------------------------------------------------------------- #

@dataclass
class LLMResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    latency_s: float = 0.0
    raw: Optional[dict] = None
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Usage:
    """一次作业内的累计用量，用于统计与熔断。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, r: LLMResult) -> None:
        with self._lock:
            self.requests += 1
            self.prompt_tokens += r.prompt_tokens
            self.completion_tokens += r.completion_tokens
            self.cached_tokens += r.cached_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> Dict[str, int]:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class LLMClient:
    """最小可用的 OpenAI 兼容 chat/completions 客户端。"""

    def __init__(self,
                 base_url: str,
                 api_key: str = "",
                 model: str = "deepseek-chat",
                 temperature: float = 0.1,
                 max_tokens: int = 1024,
                 timeout_s: int = 90,
                 retries: int = 3,
                 retry_backoff_s: float = 2.0,
                 extra_body: Optional[Dict[str, Any]] = None,
                 disable_reasoning: bool = True) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout_s = int(timeout_s)
        self.retries = max(0, int(retries))
        self.retry_backoff_s = float(retry_backoff_s)
        self.extra_body = dict(extra_body or {})
        self.disable_reasoning = bool(disable_reasoning)
        self.usage = Usage()

    @classmethod
    def from_config(cls, cfg: Any) -> "LLMClient":
        lc = cfg.llm
        client = cls(
            base_url=lc.base_url,
            api_key=lc.api_key,
            model=lc.model,
            temperature=lc.temperature,
            max_tokens=lc.max_tokens,
            timeout_s=lc.timeout_s,
            retries=lc.retries,
            retry_backoff_s=lc.retry_backoff_s,
            extra_body=getattr(lc, "extra_body", None),
            disable_reasoning=getattr(lc, "disable_reasoning", True),
        )
        return client

    # ------------------------------------------------------------------ #
    @property
    def is_mock(self) -> bool:
        return self.base_url.startswith("mock:") or self.model == "mock"

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # 部分网关（含国内中转）会校验 UA
            "User-Agent": "xxtutor/0.1 (+playwright)",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _build_body(self, messages: List[Dict[str, str]],
                    json_mode: bool, max_tokens: Optional[int],
                    temperature: Optional[float]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else int(max_tokens),
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.disable_reasoning:
            # 部分模型（Qwen3 / GLM 等）支持该开关，不支持时会被忽略或以 extra_body 传入
            body.setdefault("enable_thinking", False)
            body.setdefault("thinking", {"type": "disabled"})
        body.update(self.extra_body)
        return body

    # ------------------------------------------------------------------ #
    def chat(self,
             messages: List[Dict[str, str]],
             *,
             json_mode: bool = True,
             max_tokens: Optional[int] = None,
             temperature: Optional[float] = None,
             retries: Optional[int] = None) -> LLMResult:
        """调用一次 chat/completions，带指数退避重试。"""
        if self.is_mock:
            res = self._mock_response(messages, max_tokens=max_tokens)
            self.usage.add(res)
            return res

        body = self._build_body(messages, json_mode, max_tokens, temperature)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        attempts = self.retries if retries is None else int(retries)
        last_err: Optional[Exception] = None

        for attempt in range(attempts + 1):
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    self._endpoint(), data=payload, headers=self._headers(), method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                result = self._parse_response(data, latency=time.time() - t0)
                self.usage.add(result)
                return result
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                last_err = LLMError(f"HTTP {e.code} {e.reason}: {detail}")
                # 4xx（除 429）通常重试也没用
                if 400 <= e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = LLMError(f"{type(e).__name__}: {e}")
            except Exception as e:  # pragma: no cover - 兜底
                last_err = LLMError(f"{type(e).__name__}: {e}")

            if attempt < attempts:
                wait = self.retry_backoff_s * (2 ** attempt)
                log.warning("LLM 调用失败（第 %d 次）：%s；%.1fs 后重试",
                            attempt + 1, last_err, wait)
                time.sleep(wait)

        raise LLMError(f"LLM 调用最终失败: {last_err}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_response(data: Dict[str, Any], latency: float = 0.0) -> LLMResult:
        if isinstance(data, dict) and data.get("error"):
            raise LLMError(f"接口返回错误: {data['error']}")

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"接口未返回 choices: {str(data)[:300]}")
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if content is None:
            # 少数模型把结果放在 reasoning_content 里
            content = msg.get("reasoning_content") or ""
        if isinstance(content, list):
            # 多段内容拼起来
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        u = data.get("usage") or {}
        prompt_tok = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        completion_tok = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        cached = int(u.get("prompt_cache_hit_tokens")
                     or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                     or 0)

        return LLMResult(
            content=content or "",
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            cached_tokens=cached,
            model=data.get("model") or "",
            latency_s=latency,
            raw=data,
            finish_reason=choice.get("finish_reason") or "",
        )

    # ------------------------------------------------------------------ #
    # mock 后端：离线跑通全流程用，不消耗 token
    # ------------------------------------------------------------------ #
    def _mock_response(self, messages: List[Dict[str, str]],
                       max_tokens: Optional[int] = None) -> LLMResult:
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break
        payload = _extract_json(user)
        items = (payload or {}).get("qs") or []
        out = []
        for it in items:
            i = it.get("i")
            t = it.get("t", "single")
            opts = it.get("o") or []
            if t == "judge":
                # 用题面里是否含否定词做个假判断，保证格式正确
                text = it.get("q", "")
                out.append({"i": i, "a": not any(k in text for k in ("不", "错误", "否"))})
            elif t == "multiple":
                out.append({"i": i, "a": [chr(65 + k) for k in range(min(2, len(opts) or 1))]})
            elif t == "match":
                # 配伍题：给每个小题按顺序配一个备选字母
                n_items = len(it.get("m") or [])
                if n_items:
                    out.append({"i": i, "a": ",".join(
                        f"{k + 1}:{chr(65 + (k % max(1, len(opts) or 1)))}"
                        for k in range(n_items))})
                else:
                    out.append({"i": i, "a": "1:A"})
            elif t == "single":
                out.append({"i": i, "a": "A"})
            elif t == "fill":
                out.append({"i": i, "a": "【mock填空】"})
            else:
                out.append({"i": i, "a": "【mock简答】这里填写你的作答内容。"})
        content = json.dumps({"ans": out}, ensure_ascii=False)
        approx_in = max(1, len(user) // 3)
        return LLMResult(
            content=content,
            prompt_tokens=approx_in,
            completion_tokens=max(1, len(content) // 3),
            model="mock",
            latency_s=0.0,
            finish_reason="stop",
        )

    # ------------------------------------------------------------------ #
    def ping(self) -> Dict[str, Any]:
        """连通性自检，用于 CLI 的 doctor 命令。"""
        if self.is_mock:
            return {"ok": True, "backend": "mock", "model": "mock", "detail": "离线 mock 后端"}
        try:
            r = self.chat(
                [{"role": "user", "content": '只输出 {"ok":true} 这个 JSON，不要任何其他内容。'}],
                json_mode=True, max_tokens=32, temperature=0.0, retries=0,
            )
            return {
                "ok": True,
                "backend": self.base_url,
                "model": r.model or self.model,
                "latency_s": round(r.latency_s, 2),
                "detail": r.content.strip()[:120],
            }
        except LLMError as e:
            return {"ok": False, "backend": self.base_url, "model": self.model,
                    "detail": str(e)}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _extract_json(text: str) -> Optional[dict]:
    """从可能带 markdown 围栏 / 前后缀的文本里抠出第一个 JSON 对象。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for idx in range(start, len(s)):
            ch = s[idx]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:idx + 1])
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    return None


# 供 solver 使用的别名
extract_json = _extract_json


def build_client(cfg: Any) -> LLMClient:
    return LLMClient.from_config(cfg)
