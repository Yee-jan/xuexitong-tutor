"""配置加载：JSON 文件 + 环境变量覆盖。

配置优先级（低 -> 高）：
    内置默认值  ->  config.json  ->  环境变量

刻意只用标准库 json，避免额外依赖。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_NAME = "config.json"
EXAMPLE_CONFIG_NAME = "config.example.json"


# --------------------------------------------------------------------------- #
# 分段配置
# --------------------------------------------------------------------------- #

@dataclass
class BrowserConfig:
    # 持久化用户目录：登录态、Cookie、localStorage 都留在这里，只需人工登录一次
    user_data_dir: str = ".state/browser"
    # 首次登录必须有头（要扫码 / 输账密）；登录态有效后可 headless
    headless: bool = False
    slow_mo_ms: int = 0
    # 单页操作超时
    timeout_ms: int = 30000
    viewport_width: int = 1440
    viewport_height: int = 900
    # 复用系统已装的 Chrome/Edge 可以少下一个内核；留空则用 Playwright 自带 chromium
    channel: str = ""            # "" | "chrome" | "msedge"
    user_agent: str = ""
    locale: str = "zh-CN"
    # 拦截字体/媒体请求，加快加载。
    # 注意：**默认不拦图片** —— 实测拦 image 会让学习通登录页的「登录」按钮和二维码
    # 一起变白（按钮图标是图片），页面根本没法用。要极致提速再自行把 "image" 加回来。
    block_resources: List[str] = field(default_factory=lambda: ["font", "media"])


@dataclass
class LoginConfig:
    login_url: str = "https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"
    home_url: str = "https://i.chaoxing.com"
    # 登录成功的判定：出现这些选择器之一即认为已登录
    logged_in_selectors: List[str] = field(default_factory=lambda: [
        ".user-info", "#userInfo", ".personal-info", ".header-user",
        "a[href*='passport2.chaoxing.com/logout']",
    ])
    # 等待人工登录的最长秒数（有头模式下会暂停等你操作）
    manual_login_timeout_s: int = 300
    # 是否自动填账号密码（用 .env / 环境变量 XX_USERNAME/XX_PASSWORD；留空则纯扫码）
    auto_fill_credentials: bool = False


@dataclass
class LLMConfig:
    # 预设名仅作提示，真正生效的是下面三个字段
    preset: str = "deepseek"          # deepseek | openai | dashscope | moonshot | ollama | mock | custom
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""                 # 优先读环境变量 XX_API_KEY
    api_key_env: str = "XX_API_KEY"
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout_s: int = 90
    retries: int = 3
    retry_backoff_s: float = 2.0
    # 并发请求数（多题批量提交时用）
    concurrency: int = 3
    # 单次请求最多塞几道题：越大越省 token，但准确率与失败重试成本越高
    batch_size: int = 6
    # 单份作业最多允许消耗的 token（总 prompt+completion），超了直接停下
    max_tokens_per_assignment: int = 60000
    # 关闭思考链式长输出（部分模型支持），省 token
    disable_reasoning: bool = True
    # 自定义 system prompt；留空用 solver.SYSTEM_PROMPT
    system_prompt: str = ""
    extra_body: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StoreConfig:
    path: str = ".state/answers.db"
    # 相似题复用阈值（题干 3-gram Jaccard），0 表示只做完全指纹匹配。
    # 实测：完全同题=1.0；加了「【改版】」之类噪音≈0.68；无关题=0.0。
    # 所以 0.85 只放行小改动，不会把不同题混进来（换选项另有 option_sig 拦截）。
    similarity_threshold: float = 0.85
    # 是否把 LLM 答案写回题库
    persist: bool = True


@dataclass
class SolverConfig:
    # 缓存顺序：fingerprint -> similarity -> llm
    use_cache: bool = True
    use_similarity: bool = True
    # 低置信度答案是否丢弃（丢弃后填答阶段跳过该题，留给你手填）
    min_confidence: float = 0.0
    # 填空题是否允许一题多空
    fill_multiple_blanks: bool = True
    # 简答题最长保留字符数，超长截断（防止把整段复制进输入框）
    short_answer_max_chars: int = 1200
    # 是否允许在缓存命中时跳过 LLM
    skip_llm_on_cache_hit: bool = True
    # 每道题最多重试几次 LLM 解析
    parse_retry: int = 1
    # 复习页（已提交作业的答卷页，mooc-ans/mooc2 work/view）会把标准答案印在
    # .rightAnswerContent 上。用页面答案代替模型问答：零 token 且更权威。
    use_page_answers: bool = True
    # 是否把页面答案写回本地题库，供以后相同题目复用
    save_page_answers: bool = True


@dataclass
class AnswerConfig:
    # 自动化填答/提交整体的总开关
    enabled: bool = True
    # 默认不提交：跑完保存，由你人工确认后再提交
    submit: bool = False
    # 提交前二次确认（有头模式下暂停）
    confirm_before_submit: bool = True
    # 逐题填答之间的随机停顿，模拟人工
    min_delay_ms: int = 300
    max_delay_ms: int = 1200
    # 每填完一题是否触发页面保存（学习通部分题型需要 blur 才保存答案）
    save_each_question: bool = True
    # 单选题点击失败时回退到 JS 直接设值
    js_fallback: bool = True


@dataclass
class OutputConfig:
    dir: str = "out"
    # 保存抓到的原始题目
    dump_questions: bool = True
    # 保存答案（人工核对用）
    dump_answers: bool = True
    # 保存整页 DOM 快照，选择器失效时用来定位
    dump_dom: bool = False
    # 控制台日志级别
    log_level: str = "INFO"


@dataclass
class Config:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    login: LoginConfig = field(default_factory=LoginConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    answer: AnswerConfig = field(default_factory=AnswerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    # 运行期注入，不写进配置文件
    config_path: Optional[str] = None
    root: Path = field(default_factory=Path.cwd)

    # ------------------------------------------------------------------ #
    def resolve(self, p: str) -> Path:
        """把配置里的相对路径按项目根目录展开。"""
        path = Path(os.path.expanduser(str(p)))
        return path if path.is_absolute() else (self.root / path)

    @property
    def user_data_dir(self) -> Path:
        return self.resolve(self.browser.user_data_dir)

    @property
    def store_path(self) -> Path:
        # 允许 ":memory:" 这类 SQLite 特殊值，不当作路径处理
        if str(self.store.path).startswith(":"):
            return Path(self.store.path)
        return self.resolve(self.store.path)

    @property
    def store_is_memory(self) -> bool:
        p = str(self.store.path)
        return p == ":memory:" or p.startswith("file::memory:")

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.output.dir)

    def ensure_dirs(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if not self.store_is_memory:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 加载 / 合并 / 校验
# --------------------------------------------------------------------------- #

_ENV_MAP = {
    # 环境变量名 -> (分段, 字段)
    "XX_API_KEY": ("llm", "api_key"),
    "XX_BASE_URL": ("llm", "base_url"),
    "XX_MODEL": ("llm", "model"),
    "XX_LLM_PRESET": ("llm", "preset"),
    "XX_BATCH_SIZE": ("llm", "batch_size"),
    "XX_MAX_TOKENS": ("llm", "max_tokens"),
    "XX_CONCURRENCY": ("llm", "concurrency"),
    "XX_HEADLESS": ("browser", "headless"),
    "XX_TIMEOUT_MS": ("browser", "timeout_ms"),
    "XX_CHROME_CHANNEL": ("browser", "channel"),
    "XX_USER_DATA_DIR": ("browser", "user_data_dir"),
    "XX_STORE_PATH": ("store", "path"),
    "XX_SIMILARITY": ("store", "similarity_threshold"),
    "XX_SUBMIT": ("answer", "submit"),
    "XX_LOG_LEVEL": ("output", "log_level"),
    "XX_OUTPUT_DIR": ("output", "dir"),
}

_BOOL_TRUE = {"1", "true", "yes", "y", "on", "是"}
_BOOL_FALSE = {"0", "false", "no", "n", "off", "否"}


def _coerce(target_type: Any, value: Any, key: str) -> Any:
    """把字符串/原始值转成 dataclass 字段声明的类型。"""
    if value is None:
        return None
    if target_type is bool or target_type == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
        raise ValueError(f"配置项 {key} 需要布尔值，收到 {value!r}")
    if target_type is int:
        return int(float(value))
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _no_duplicate_keys(pairs: List[Any]) -> Dict[str, Any]:
    """JSON 里同名键只留最后一个 —— 静默丢配置，排查起来非常痛苦。

    真实踩到的坑：`"channel": "msedge"` 和后面的 `"channel": ""` 同时存在，
    结果是空串生效（Python 的 json 保留最后一个），浏览器照旧启动 chromium，
    现象却是「明明改了配置没反应」。这里直接报错，让问题暴露在启动瞬间。
    """
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(
                f"配置文件里有重复的键：{k!r}。请删掉多余的那个"
                "（同名键只有最后一个会生效，容易让人以为配置没起作用）。"
            )
        out[k] = v
    return out


def _merge_section(obj: Any, data: Dict[str, Any], prefix: str) -> List[str]:
    """把 dict 合并进 dataclass 实例，返回未识别的键（用于报错）。"""
    unknown: List[str] = []
    valid = {f.name: f for f in fields(obj)}
    for k, v in (data or {}).items():
        if str(k).startswith("_"):
            # 允许用 "_说明" / "_xxx说明" 这类键写注释
            continue
        if k not in valid:
            unknown.append(f"{prefix}.{k}")
            continue
        cur = getattr(obj, k)
        if is_dataclass(cur) and isinstance(v, dict):
            unknown.extend(_merge_section(cur, v, f"{prefix}.{k}"))
            continue
        if v is None:
            continue
        if isinstance(cur, list) and isinstance(v, list):
            setattr(obj, k, v)
            continue
        if isinstance(cur, dict) and isinstance(v, dict):
            merged = dict(cur)
            merged.update(v)
            setattr(obj, k, merged)
            continue
        try:
            setattr(obj, k, _coerce(type(cur), v, f"{prefix}.{k}"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"配置项 {prefix}.{k} 类型错误: {e}") from e
    return unknown


def load_config(path: Optional[str] = None, *, root: Optional[Path] = None,
                strict: bool = True) -> Config:
    """加载配置。path 为空时按 ./config.json -> ./config.example.json 顺序查找。"""
    root = Path(root or Path.cwd()).resolve()
    cfg = Config(root=root)

    candidate: Optional[Path] = None
    if path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            raise FileNotFoundError(f"找不到配置文件: {candidate}")
    else:
        for name in (DEFAULT_CONFIG_NAME, EXAMPLE_CONFIG_NAME):
            p = root / name
            if p.exists():
                candidate = p
                break

    unknown: List[str] = []
    if candidate and candidate.exists():
        try:
            # utf-8-sig：记事本/PowerShell 存出来的 config.json 常带 BOM，用严格 utf-8 会直接报错
            raw = json.loads(candidate.read_text(encoding="utf-8-sig"),
                             object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件不是合法 JSON: {candidate} -> {e}") from e
        except ValueError as e:
            raise ValueError(f"配置文件有问题: {candidate} -> {e}") from e
        # 允许写 "//" 注释风格的键（"_comment"）
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        unknown = _merge_section(cfg, raw, "config")
        cfg.config_path = str(candidate)

    # 环境变量覆盖
    for env, (section, fname) in _ENV_MAP.items():
        if env not in os.environ:
            continue
        sub = getattr(cfg, section)
        cur = getattr(sub, fname)
        setattr(sub, fname, _coerce(type(cur), os.environ[env], f"{section}.{fname}"))

    if cfg.llm.api_key_env and not cfg.llm.api_key:
        cfg.llm.api_key = os.environ.get(cfg.llm.api_key_env, "")

    if strict and unknown:
        raise ValueError(
            "配置中存在无法识别的键（请对照 config.example.json）: " + ", ".join(sorted(unknown))
        )
    return cfg


# --------------------------------------------------------------------------- #
# 预设
# --------------------------------------------------------------------------- #

PRESETS: Dict[str, Dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    "mock": {"base_url": "mock://local", "model": "mock"},
}


def apply_preset(cfg: Config) -> Config:
    """当 preset 不是 custom 时，用预设覆盖 base_url/model。"""
    p = (cfg.llm.preset or "").lower()
    if p and p != "custom" and p in PRESETS:
        for k, v in PRESETS[p].items():
            setattr(cfg.llm, k, v)
    return cfg


DEFAULT_CONFIG_DICT: Dict[str, Any] = {
    "browser": {
        "user_data_dir": ".state/browser",
        "headless": False,
        "timeout_ms": 30000,
        "channel": "",
        "block_resources": ["image", "media", "font"],
    },
    "login": {
        "login_url": LoginConfig.login_url,
        "home_url": LoginConfig.home_url,
        "auto_fill_credentials": False,
        "manual_login_timeout_s": 300,
    },
    "llm": {
        "preset": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
        "api_key_env": "XX_API_KEY",
        "temperature": 0.1,
        "max_tokens": 1024,
        "batch_size": 6,
        "concurrency": 3,
        "max_tokens_per_assignment": 60000,
    },
    "store": {
        "path": ".state/answers.db",
        "similarity_threshold": 0.92,
        "persist": True,
    },
    "solver": {
        "use_cache": True,
        "use_similarity": True,
        "min_confidence": 0.0,
    },
    "answer": {
        "enabled": True,
        "submit": False,
        "confirm_before_submit": True,
        "min_delay_ms": 300,
        "max_delay_ms": 1200,
        "save_each_question": True,
    },
    "output": {
        "dir": "out",
        "dump_questions": True,
        "dump_answers": True,
        "dump_dom": False,
        "log_level": "INFO",
    },
}
