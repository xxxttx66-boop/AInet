"""DeepSeek 客户端封装（spec §4：OpenAI 兼容接口，openai Python SDK 指向 DeepSeek base_url）。

- API Key 读取顺序：环境变量 DEEPSEEK_API_KEY → AINET_SECRETS_FILE 指向的文件（设了就只读它）→
  缺省依次找 backend/data/secrets.env（Windows 部署用，data/ 已被 git 忽略）、~/.config/ainet/secrets.env。
  文件格式 `DEEPSEEK_API_KEY=...`（容忍 BOM、CRLF、引号、`export ` 前缀、# 注释）。
- Key 绝不写进日志 / 报错 / 返回值：对外最多显示前 3 位加 ***（mask_key）；异常文本统一过 redact()。
- chat_json()：强制 JSON 输出（提示词约束 + response_format=json_object，已实测 deepseek-flash 支持），
  稳健解析（容忍代码块包裹、前后多余文字）；超时 / 网络 / 限流 / 5xx 有限次重试；解析失败（含 content 为空、
  finish_reason=length）重试一次（开着思考时改为关闭思考再试），仍失败抛 AIError("parse")。
- 错误分类：no_key / auth / quota / rate_limit / timeout / network / server / bad_request / parse / unknown。
- SDK 自身的重试关掉（max_retries=0），只保留这里的重试，超时可控、错误好分类。
- 用 AsyncOpenAI，不阻塞事件循环（console 转发与 AI 请求在同一个事件循环里）。
- AI 接口设置（2026-10-08，里程碑 6.18，页面「AI 设置」）：API URL、模型存 app_settings（ai:base_url / ai:model），
  API Key 写到 secrets 文件（key_file()，缺省 backend/data/secrets.env；原子写入、保留其他行），库里只记「Key 由页面设置」
  （ai:key_page）。优先级：页面保存的值 > 环境变量 > secrets 文件 / 内置缺省；AINET_LAB_FORCE=1 且设了环境变量时环境变量
  优先。客户端按 (Key, URL, 模型) 缓存，保存后下一次请求即用新值（已发出的请求不受影响）。Key 对外只给「已配置」与掩码
  （mask_tail：前 3 位 + *** + 末 4 位），绝不回显整串、不进日志。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import config

log = logging.getLogger("ainet.ai")

KEY_ENV = "DEEPSEEK_API_KEY"
SECRETS_ENV = "AINET_SECRETS_FILE"


# ---- API Key ----

def secrets_candidates() -> list[str]:
    """secrets 文件候选路径（运行时计算，测试可用环境变量 / monkeypatch 改变）。"""
    explicit = os.environ.get(SECRETS_ENV)
    if explicit:
        return [explicit]
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(here, "data", "secrets.env"),
            os.path.join(os.path.expanduser("~"), ".config", "ainet", "secrets.env")]


def read_secrets_file(path: str, name: str = KEY_ENV) -> str | None:
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, sep, v = line.partition("=")
        if not sep or k.strip() != name:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        return v.strip() or None
    return None


# ---- 页面上的 AI 接口设置（里程碑 6.18）----

SETTING_BASE_URL = "ai:base_url"
SETTING_MODEL = "ai:model"
SETTING_KEY_PAGE = "ai:key_page"          # "1" = Key 由页面写进 key_file()，优先于环境变量
URL_ENV, MODEL_ENV = "AINET_AI_BASE_URL", "AINET_AI_MODEL"
# 库里的记录（lifespan 里 load_settings() 装载，保存时更新）：没装载时等同于没有记录，行为与以前一样
_settings: dict[str, Any] = {"base_url": None, "model": None, "key_page": False}


def key_file() -> str:
    """页面保存 Key 时写的文件：AINET_SECRETS_FILE 指定的文件，否则 backend/data/secrets.env。"""
    return secrets_candidates()[0]


def load_settings() -> None:
    """启动时从 app_settings 装载 AI 接口设置（库里的值不合法时忽略并告警）。"""
    import db
    _settings.update(base_url=None, model=None, key_page=False)
    raw = db.get_setting(SETTING_BASE_URL)
    if raw:
        try:
            _settings["base_url"] = normalize_base_url(raw)
        except ValueError as exc:
            log.warning("库里记录的 AI 接口地址不合法（%s），改用环境变量 / 缺省值", exc)
    raw = db.get_setting(SETTING_MODEL)
    if raw:
        try:
            _settings["model"] = normalize_model(raw)
        except ValueError as exc:
            log.warning("库里记录的 AI 模型名不合法（%s），改用环境变量 / 缺省值", exc)
    _settings["key_page"] = db.get_setting(SETTING_KEY_PAGE) == "1"


def _forced(env_var: str) -> bool:
    """AINET_LAB_FORCE=1 且设了该环境变量：环境变量优先于页面设置。"""
    return bool(config.LAB_FORCE and os.environ.get(env_var, "").strip())


def effective_base_url() -> tuple[str, str]:
    """(当前生效的 API URL, 来源 db / force / env / default)。"""
    if _forced(URL_ENV):
        return config.AI_BASE_URL.rstrip("/"), "force"
    if _settings["base_url"]:
        return _settings["base_url"], "db"
    return config.AI_BASE_URL.rstrip("/"), "env" if os.environ.get(URL_ENV, "").strip() else "default"


def effective_model() -> tuple[str, str]:
    if _forced(MODEL_ENV):
        return config.AI_MODEL, "force"
    if _settings["model"]:
        return _settings["model"], "db"
    return config.AI_MODEL, "env" if os.environ.get(MODEL_ENV, "").strip() else "default"


def key_info() -> tuple[str | None, str, str]:
    """(key, 来源代码 page / force / env / file / none, 来源描述)。来源描述不含 key。"""
    env_v = os.environ.get(KEY_ENV, "").strip()
    if env_v and config.LAB_FORCE:
        return env_v, "force", f"环境变量 {KEY_ENV}（AINET_LAB_FORCE=1）"
    if _settings["key_page"]:
        path = key_file()
        v = read_secrets_file(path)
        if v:
            return v, "page", f"页面设置（文件 {path}）"
    if env_v:
        return env_v, "env", f"环境变量 {KEY_ENV}"
    for path in secrets_candidates():
        v = read_secrets_file(path)
        if v:
            return v, "file", f"文件 {path}"
    return None, "none", "未找到（页面设置 / 环境变量 / secrets 文件都没有）"


def load_api_key() -> tuple[str | None, str]:
    """返回 (key, 来源描述)。来源描述不含 key。"""
    key, _, desc = key_info()
    return key, desc


def normalize_base_url(raw: Any) -> str:
    """API URL：http / https、要有主机名、不带用户名密码 / 查询串 / 片段，去尾斜杠。不合法抛 ValueError（中文）。"""
    v = str(raw or "").strip()
    if not v:
        raise ValueError("API URL 不能为空")
    if len(v) > 300 or any(c.isspace() or ord(c) < 32 for c in v):
        raise ValueError("API URL 里不能有空白或控制字符（最长 300 个字符）")
    try:
        u = urlsplit(v)
        port = u.port
    except ValueError:
        raise ValueError(f"API URL 格式不对：{v}") from None
    if u.scheme.lower() not in ("http", "https"):
        raise ValueError("API URL 要以 http:// 或 https:// 开头，如 https://api.deepseek.com")
    if "@" in u.netloc:
        raise ValueError("API URL 里不能带用户名或密码")
    if not u.hostname:
        raise ValueError("API URL 里缺少主机名")
    if u.query or u.fragment or "?" in v or "#" in v:
        raise ValueError("API URL 里不能有查询串（?）或片段（#）")
    if port == 0:
        raise ValueError("端口不能为 0")
    return f"{u.scheme.lower()}://{u.netloc}{u.path}".rstrip("/")


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")


def normalize_model(raw: Any) -> str:
    v = str(raw or "").strip()
    if not _MODEL_RE.match(v):
        raise ValueError("模型名只能是字母、数字和 . _ : / @ + -（最长 100 个字符），如 deepseek-flash")
    return v


def normalize_key(raw: Any) -> str:
    """API Key：8~512 个可打印 ASCII 字符，不能有空白、引号、#（写进 secrets 文件的一行）。"""
    v = str(raw or "").strip()
    if not 8 <= len(v) <= 512 or any(ord(c) <= 32 or ord(c) >= 127 or c in "\"'#" for c in v):
        raise ValueError("API Key 格式不对：应为 8~512 个字符，不能有空格、引号或 #")
    return v


def write_key_file(path: str, key: str | None, name: str = KEY_ENV) -> None:
    """更新 / 追加（key 为 None 时删除）secrets 文件里的 name 行（缺省 DEEPSEEK_API_KEY；eNSP Pro 实例的设备密码
    ENSPRO_DEVICE_PASSWORD__<KEY> 也用它写，里程碑 6.19），保留其他行；临时文件 + os.replace 原子写入。"""
    KEY_ENV = name                                   # noqa: N806 - 下面沿用原来的变量名
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, done = [], False
    for raw in lines:
        line = raw.strip()
        body = line[len("export "):].strip() if line.startswith("export ") else line
        if not line.startswith("#") and "=" in body and body.partition("=")[0].strip() == KEY_ENV:
            if key is not None and not done:
                out.append(f"{KEY_ENV}={key}")
                done = True
            continue                                 # 重复的旧行一并去掉
        out.append(raw)
    if key is not None and not done:
        out.append(f"{KEY_ENV}={key}")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        try:
            os.chmod(tmp, 0o600)
        except OSError:                              # pragma: no cover - Windows 上 chmod 只管只读位
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mask_key(key: str | None) -> str:
    return f"{key[:3]}***" if key else "(无)"


def mask_tail(key: str | None) -> str:
    """接口里展示的掩码：前 3 位 + *** + 末 4 位（太短只给 ***），绝不给整串。"""
    if not key:
        return ""
    return f"{key[:3]}***{key[-4:]}" if len(key) >= 12 else "***"


_KEYLIKE_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")


def redact(text: Any, key: str | None = None) -> str:
    """把文本里出现的 key（以及形似 sk-xxx 的串）替换成前 3 位加 ***。"""
    s = str(text)
    if key:
        s = s.replace(key, mask_key(key))
    return _KEYLIKE_RE.sub(lambda m: mask_key(m.group(0)), s)


# ---- 错误 ----

KIND_TEXT = {
    "no_key": "未配置 AI API Key",
    "auth": "鉴权失败（API Key 无效或无权限）",
    "quota": "账户余额不足",
    "rate_limit": "请求被限流",
    "timeout": "请求超时",
    "network": "网络错误（连不上 AI 服务）",
    "server": "AI 服务端错误",
    "bad_request": "请求被拒绝",
    "parse": "AI 返回内容无法解析",
    "disabled": "AI 功能已关闭",
    "unknown": "未知错误",
}
RETRYABLE = {"timeout", "network", "rate_limit", "server"}


class AIError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind if kind in KIND_TEXT else "unknown"
        self.detail = redact(detail)[:300]
        super().__init__(self.message)

    @property
    def message(self) -> str:
        base = KIND_TEXT[self.kind]
        return f"{base}：{self.detail}" if self.detail else base


def classify(exc: BaseException, key: str | None = None) -> AIError:
    """SDK / 网络异常 -> AIError（文本已脱敏）。"""
    if isinstance(exc, AIError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return AIError("timeout")
    try:
        import openai
    except ImportError:            # pragma: no cover - 依赖缺失
        return AIError("unknown", type(exc).__name__)
    status = getattr(exc, "status_code", None)
    brief = f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")
    if isinstance(exc, openai.APITimeoutError):
        return AIError("timeout", brief)
    if isinstance(exc, openai.APIConnectionError):
        return AIError("network", brief)
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return AIError("auth", brief)
    if isinstance(exc, openai.RateLimitError):
        return AIError("rate_limit", brief)
    if status == 402:
        return AIError("quota", brief)
    if isinstance(exc, openai.APIStatusError):
        if status is not None and status >= 500:
            return AIError("server", brief)
        return AIError("bad_request", f"{brief} {redact(getattr(exc, 'message', ''), key)[:160]}")
    return AIError("unknown", f"{brief} {redact(exc, key)[:160]}")


# ---- 稳健 JSON 解析 ----

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _balanced_objects(text: str):
    """依次产出文本中每个括号配平的 {...} 片段（考虑字符串与转义）。"""
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return
        depth, in_str, esc = 0, False, False
        for j in range(start, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:j + 1]
                    break
        else:
            return
        i = start + 1


def parse_json_loose(text: str | None) -> dict[str, Any]:
    """从模型输出里取出 JSON 对象：直接解析 → 代码块里的 → 第一个括号配平且能解析的 {...}。失败抛 ValueError。"""
    if not text or not text.strip():
        raise ValueError("内容为空")
    s = text.strip()
    candidates = [s] + [m.group(1).strip() for m in _FENCE_RE.finditer(s)]
    for c in candidates:
        try:
            v = json.loads(c, strict=False)      # strict=False：容忍字符串里的原始换行
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    for frag in _balanced_objects(s):
        try:
            v = json.loads(frag, strict=False)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    raise ValueError("找不到 JSON 对象")


def clamp(v: Any, lo: float, hi: float, default: float | None = None) -> float | None:
    """数值截断到 [lo, hi]；不是数字返回 default。容忍 "85"、"85分"、"85/100" 这类写法。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        if not m:
            return default
        v = float(m.group(0))
    if not isinstance(v, (int, float)) or v != v:   # NaN
        return default
    return float(min(hi, max(lo, v)))


# ---- 客户端 ----

@dataclass
class AIResult:
    data: dict[str, Any]
    model: str
    thinking: bool
    attempts: int
    elapsed: float
    usage: dict[str, int] = field(default_factory=dict)
    content: str = ""


def _add_usage(acc: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        acc[k] = acc.get(k, 0) + int(getattr(usage, k, 0) or 0)
    det = getattr(usage, "completion_tokens_details", None)
    rt = getattr(det, "reasoning_tokens", None) if det is not None else None
    if rt:
        acc["reasoning_tokens"] = acc.get("reasoning_tokens", 0) + int(rt)


class DeepSeekClient:
    def __init__(self, api_key: str, *, base_url: str | None = None, model: str | None = None,
                 max_retries: int | None = None, json_mode: bool | None = None, sdk: Any = None,
                 backoff: float = 1.0) -> None:
        self._key = api_key
        self.model = model or effective_model()[0]
        self.base_url = base_url or effective_base_url()[0]
        self.max_retries = config.AI_MAX_RETRIES if max_retries is None else max_retries
        self.json_mode = config.AI_JSON_MODE if json_mode is None else json_mode
        self.backoff = backoff
        if sdk is None:
            from openai import AsyncOpenAI
            sdk = AsyncOpenAI(api_key=api_key, base_url=self.base_url, max_retries=0)
        self._sdk = sdk

    def __repr__(self) -> str:       # 不暴露 key
        return f"DeepSeekClient(model={self.model!r}, key={mask_key(self._key)})"

    async def _once(self, messages: list[dict[str, str]], thinking: bool, max_tokens: int,
                    timeout: float) -> Any:
        kw: dict[str, Any] = {
            "model": self.model, "messages": messages, "max_tokens": max_tokens, "timeout": timeout,
            "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
        }
        if self.json_mode:
            kw["response_format"] = {"type": "json_object"}
        # SDK 的 timeout 管单次 HTTP；外面再包一层，防止 SDK 内部卡住
        return await asyncio.wait_for(self._sdk.chat.completions.create(**kw), timeout + 5)

    async def chat_json(self, system: str, user: str, *, thinking: bool, max_tokens: int, timeout: float,
                        deadline: float | None = None,
                        validate: Callable[[dict[str, Any]], None] | None = None) -> AIResult:
        """请求一次 JSON 回答。deadline：总时长上限（秒，含重试）；validate 抛 ValueError 视为解析失败。"""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        end = t0 + deadline if deadline else None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        usage: dict[str, int] = {}
        attempts = transport_retries = 0
        parse_retried = False
        last_err: AIError | None = None
        while True:
            attempts += 1
            per = timeout if end is None else max(1.0, min(timeout, end - loop.time()))
            try:
                resp = await self._once(messages, thinking, max_tokens, per)
            except Exception as exc:
                err = classify(exc, self._key)
                last_err = err
                if err.kind in RETRYABLE and transport_retries < self.max_retries:
                    wait = self.backoff * (2 ** transport_retries) * (2 if err.kind == "rate_limit" else 1)
                    if end is None or loop.time() + wait + 1 < end:
                        transport_retries += 1
                        log.warning("AI 请求失败（%s），%.1f 秒后重试", err.message, wait)
                        await asyncio.sleep(wait)
                        continue
                raise err from None
            _add_usage(usage, getattr(resp, "usage", None))
            choice = resp.choices[0] if getattr(resp, "choices", None) else None
            content = (getattr(choice.message, "content", None) or "") if choice else ""
            finish = getattr(choice, "finish_reason", None) if choice else None
            try:
                if finish == "length":
                    raise ValueError("输出被 max_tokens 截断")
                data = parse_json_loose(content)
                if validate is not None:
                    validate(data)
            except ValueError as exc:
                last_err = AIError("parse", str(exc))
                if not parse_retried and (end is None or loop.time() + 1 < end):
                    parse_retried = True
                    if thinking:
                        thinking = False      # 思考把 token 用完 / 输出跑偏时，关掉思考再试一次
                    log.warning("AI 返回无法解析（%s），重试一次", exc)
                    continue
                raise last_err from None
            return AIResult(data=data, model=getattr(resp, "model", None) or self.model, thinking=thinking,
                            attempts=attempts, elapsed=round(loop.time() - t0, 2), usage=usage, content=content)

    async def chat_stream(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                          max_tokens: int = 1200, timeout: float = 30.0):
        """流式对话（Agent 面板用）：逐块产出 {"type": "delta", "text"}，最后产出
        {"type": "end", "tool_calls": [{id, name, arguments}], "finish": finish_reason, "usage": {...}}。

        - 关闭思考（开着思考时多轮工具调用要回传 reasoning_content，没有必要）；不用 json_object。
        - 只在拿到第一块之前对可重试错误重试（流开始后重试会让前端重复显示）；两块之间超过 timeout 秒算超时。
        - 出错抛 AIError（文本已脱敏）。
        """
        kw: dict[str, Any] = {
            "model": self.model, "messages": messages, "max_tokens": max_tokens, "timeout": timeout,
            "stream": True, "extra_body": {"thinking": {"type": "disabled"}},
        }
        if tools:
            kw["tools"] = tools
        retries = 0
        while True:
            try:
                stream = await asyncio.wait_for(self._sdk.chat.completions.create(**kw), timeout + 5)
                break
            except Exception as exc:
                err = classify(exc, self._key)
                if err.kind in RETRYABLE and retries < min(1, self.max_retries):
                    retries += 1
                    await asyncio.sleep(self.backoff)
                    continue
                raise err from None
        calls: dict[int, dict[str, str]] = {}
        finish = None
        usage: dict[str, int] = {}
        it = stream.__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout)
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    raise classify(exc, self._key) from None
                _add_usage(usage, getattr(chunk, "usage", None))
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                ch = choices[0]
                delta = getattr(ch, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    yield {"type": "delta", "text": text}
                for tc in (getattr(delta, "tool_calls", None) or []) if delta is not None else []:
                    e = calls.setdefault(getattr(tc, "index", 0) or 0, {"id": "", "name": "", "arguments": ""})
                    if getattr(tc, "id", None):
                        e["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            e["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            e["arguments"] += fn.arguments
                if getattr(ch, "finish_reason", None):
                    finish = ch.finish_reason
        finally:
            # 结束或被取消（学员点「停止」、断开）时关闭 HTTP 流；不 await，避免在取消中再次被取消
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    r = close()
                    if asyncio.iscoroutine(r):
                        asyncio.ensure_future(r)
                except Exception:
                    pass
        yield {"type": "end", "tool_calls": [calls[k] for k in sorted(calls)], "finish": finish, "usage": usage}


# 测试替换点：返回一个有 chat_json(...) 的对象；无 key 时抛 AIError("no_key")。
def make_client() -> DeepSeekClient:
    key, source = load_api_key()
    if not key:
        raise AIError("no_key", "请在页面「AI 设置」里填写，或设置环境变量 DEEPSEEK_API_KEY、"
                                "在 backend/data/secrets.env 写入 DEEPSEEK_API_KEY=...")
    base_url, model = effective_base_url()[0], effective_model()[0]
    log.info("AI API Key 来源：%s（%s），地址 %s，模型 %s", source, mask_key(key), base_url, model)
    return DeepSeekClient(key, base_url=base_url, model=model)


_client_cache: tuple[tuple[str, str, str], Any] | None = None


def get_client() -> Any:
    """取客户端（按 (key, URL, 模型) 缓存，任何一项变了重建——页面保存后下一次请求即生效）。make_client 被替换时每次都调用它。"""
    global _client_cache
    if make_client is not _default_make_client:
        return make_client()
    key, _ = load_api_key()
    sig = (key or "", effective_base_url()[0], effective_model()[0])
    if _client_cache is not None and key and _client_cache[0] == sig:
        return _client_cache[1]
    c = make_client()
    _client_cache = (sig, c)
    return c


def settings_view() -> dict[str, Any]:
    """GET /api/ai/settings：各项的当前值与来源；Key 只给有无、来源与掩码（mask_tail），绝不给整串。"""
    url, us = effective_base_url()
    model, ms = effective_model()
    key, ks, desc = key_info()
    return {
        "base_url": {"value": url, "source": us, "default": config.AI_BASE_URL.rstrip("/"), "env_var": URL_ENV},
        "model": {"value": model, "source": ms, "default": config.AI_MODEL, "env_var": MODEL_ENV},
        "key": {"configured": bool(key), "source": ks, "source_text": desc, "masked": mask_tail(key),
                "file": key_file(), "env_var": KEY_ENV, "page": bool(_settings["key_page"])},
        "force": bool(config.LAB_FORCE),
    }


def apply_settings(*, base_url: Any = ..., model: Any = ..., api_key: str | None = None,
                   clear_key: bool = False) -> list[str]:
    """保存页面上的设置（参数省略 = 不改；base_url / model 为 None 或空串 = 删掉记录、回到环境变量 / 缺省）。
    值须已校验。Key 写进 key_file()（原子写入），库里只记 ai:key_page。返回改动了的项名。日志不写 Key。"""
    import db
    changed: list[str] = []
    for name, setting, v in (("base_url", SETTING_BASE_URL, base_url), ("model", SETTING_MODEL, model)):
        if v is ...:
            continue
        v = v or None
        if v != _settings[name]:
            db.set_setting(setting, v)
            _settings[name] = v
            changed.append(name)
    if api_key:
        old = read_secrets_file(key_file()) if _settings["key_page"] else None
        write_key_file(key_file(), api_key)
        db.set_setting(SETTING_KEY_PAGE, "1")
        _settings["key_page"] = True
        if old != api_key:
            changed.append("api_key")
    elif clear_key:
        if _settings["key_page"] or read_secrets_file(key_file()):
            write_key_file(key_file(), None)
            changed.append("api_key")
        db.set_setting(SETTING_KEY_PAGE, None)
        _settings["key_page"] = False
    if changed:
        log.info("AI 接口设置已修改：%s（地址 %s，模型 %s，Key %s）", "、".join(changed), effective_base_url()[0],
                 effective_model()[0], "已配置" if key_info()[0] else "未配置")
    return changed


# ---- 测试连接（不保存）----

_TEST_TRANSPORT: Any = None          # 测试替换点：httpx 传输层（MockTransport），None = 真实网络


async def test_connection(base_url: str, key: str, model: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """用给定的 URL + Key 发一次最小请求：先 GET {url}/models；服务端没有该接口（404 / 405）时改发 1 token 的补全。
    返回 {ok, kind?, message, models?, model_listed?}；文本已脱敏，不含 Key。"""
    import httpx
    headers = {"Authorization": f"Bearer {key}"}
    kw: dict[str, Any] = {"timeout": timeout}
    if _TEST_TRANSPORT is not None:
        kw["transport"] = _TEST_TRANSPORT

    def fail(kind: str, detail: str = "") -> dict[str, Any]:
        e = AIError(kind, redact(detail, key))
        return {"ok": False, "kind": e.kind, "message": e.message}

    def status_fail(r: Any) -> dict[str, Any]:
        st = r.status_code
        kind = ("auth" if st in (401, 403) else "quota" if st == 402 else "rate_limit" if st == 429
                else "server" if st >= 500 else "bad_request")
        try:
            msg = (r.json().get("error") or {}).get("message") or ""
        except Exception:
            msg = ""
        return fail(kind, f"HTTP {st} {str(msg)[:160]}".strip())

    try:
        async with httpx.AsyncClient(**kw) as c:
            r = await c.get(f"{base_url}/models", headers=headers)
            if r.status_code == 200:
                try:
                    ids = [str(m.get("id")) for m in (r.json().get("data") or []) if isinstance(m, dict)]
                except Exception:
                    ids = []
                listed = (model in ids) if ids else None
                note = f"，模型 {model} 不在服务端的模型列表里" if listed is False else ""
                return {"ok": True, "message": f"连接成功（{len(ids)} 个模型{note}）", "models": len(ids),
                        "model_listed": listed}
            if r.status_code not in (404, 405):
                return status_fail(r)
            r = await c.post(f"{base_url}/chat/completions", headers=headers,
                             json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1})
            if r.status_code == 200:
                return {"ok": True, "message": f"连接成功（模型 {model} 可用）", "models": None, "model_listed": True}
            return status_fail(r)
    except httpx.TimeoutException:
        return fail("timeout")
    except httpx.HTTPError as exc:
        return fail("network", type(exc).__name__)


_default_make_client = make_client


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
