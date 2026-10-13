"""集中配置：eNSP 主机、拓扑目录、数据库等。

部署到 eNSP 执行机（Windows 本机）时，设 ENSP_HOST=127.0.0.1。
开发阶段远程连实测设备时默认 192.168.31.72。
"""
import os

# eNSP 设备 console 所在主机。本机部署时为 127.0.0.1。只是缺省值：课程页「接入设置」改过以后以库里的
# lab:endpoint:ensp 为准（lab.endpoint_value，spec §5.2「接入设置」）。
ENSP_HOST: str = os.environ.get("ENSP_HOST", "192.168.31.72")

# 连接设备的超时（秒）。
CONNECT_TIMEOUT: float = 8.0

# ---- 实验后端（LabProvider，见 lab.py）----
# ensp = eNSP 经典版（缺省）；enspro = eNSP Pro（REST + STOMP over WebSocket，见 enspro.py）。
LAB_PROVIDER: str = os.environ.get("AINET_LAB_PROVIDER", "ensp").strip().lower() or "ensp"
# eNSP Pro 服务地址（自签证书，不校验）与当前沙箱 id（拓扑 = 这个沙箱）。地址只是缺省值：课程页「接入设置」改过以后
# 以库里的 lab:endpoint:enspro 为准（lab.endpoint_value）；运行时一律用 lab.endpoint_value("enspro")，不要直接读这里。
ENSPRO_URL: str = os.environ.get("AINET_ENSPRO_URL", "https://192.168.56.101:8443").rstrip("/")
_sb = os.environ.get("AINET_ENSPRO_SANDBOX", "").strip()
ENSPRO_SANDBOX: int | None = int(_sb) if _sb else None
# eNSP Pro 同一账号只能有一个会话：在别处（如网页）登录会把 ainet 挤掉。开启后检测到被挤掉时自动重新登录
# （会反过来挤掉对方，两次自动重登至少间隔 ENSPRO_RELOGIN_INTERVAL 秒）；缺省关闭，页面提示后由用户点「重新登录」。
ENSPRO_AUTO_RELOGIN: bool = os.environ.get("AINET_ENSPRO_AUTO_RELOGIN", "0").strip().lower() in ("1", "true", "yes", "on")
ENSPRO_RELOGIN_INTERVAL: float = float(os.environ.get("AINET_ENSPRO_RELOGIN_INTERVAL", "30"))
# 沙箱详情的缓存秒数（前端每 5 秒轮询状态）。
ENSPRO_CACHE_TTL: float = float(os.environ.get("AINET_ENSPRO_CACHE_TTL", "2"))
# console 应用层心跳间隔（秒）；设备 console 密码见 enspro.device_password()（secrets.env 的 ENSPRO_DEVICE_PASSWORD）。
ENSPRO_HEARTBEAT: float = float(os.environ.get("AINET_ENSPRO_HEARTBEAT", "30"))
# console 登录（回车 → 密码 → 提示符）每一步的等待上限（秒）。
ENSPRO_LOGIN_TIMEOUT: float = float(os.environ.get("AINET_ENSPRO_LOGIN_TIMEOUT", "30"))


def _default_topo_dirs() -> list[str]:
    if os.name != "nt":
        return []
    dirs = [r"C:\Program Files\Huawei\eNSP\examples"]
    home = os.environ.get("USERPROFILE")
    if home:
        dirs.append(home)
    return dirs


def _topo_dirs() -> list[str]:
    raw = os.environ.get("AINET_TOPO_DIRS")
    dirs = raw.split(os.pathsep) if raw is not None else _default_topo_dirs()
    # 转绝对路径：uvicorn 从 backend/ 启动时相对路径也能对上
    return [os.path.abspath(d) for d in dirs if d.strip()]


# eNSP 拓扑（.topo）搜索目录。环境变量 AINET_TOPO_DIRS 覆盖（多个用 os.pathsep 分隔）；
# Windows 缺省 eNSP examples + 用户目录，其他系统缺省为空。
TOPO_DIRS: list[str] = _topo_dirs()



# SQLite 数据库文件。环境变量 AINET_DB_PATH 可覆盖；缺省 backend/data/ainet.db。
DB_PATH: str = os.environ.get(
    "AINET_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ainet.db"),
)

# 库里没有任何学生时自动创建的默认账号（保证装好即可登录）。
DEFAULT_LOGIN: str = "admin"
DEFAULT_PASSWORD: str = "admin"
DEFAULT_NAME: str = "管理员"

# 会话 cookie 名。
SESSION_COOKIE: str = "ainet_session"

# 自动登录（2026-10-07，里程碑 6.17）：设为某个登录名（如 admin）时，没有有效会话的请求一律自动以该学员身份登录
# （该学员不存在就启动时自动创建，密码随机、不输出），登录页直接跳课程页。缺省为空 = 关闭。
# 注意：开启后能访问 ainet 端口的任何人都以该账号身份操作，只适合单人本机 / 局域网使用；没有经接口开启它的方式。
AUTO_LOGIN: str = os.environ.get("AINET_AUTO_LOGIN", "").strip()

# 场景库目录（scenarios/*.json，随仓库版本管理）。环境变量 AINET_SCENARIOS_DIR 覆盖。
SCENARIOS_DIR: str = os.path.abspath(os.environ.get(
    "AINET_SCENARIOS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scenarios"),
))

# ConsoleHub：所有订阅者退出后保持设备连接的秒数；两次连接同一 console 的最小间隔（秒）。
# 频繁连接 / 断开 eNSP console 疑似诱发 eNSP_Client 崩溃，刚断开的会话重连也可能收不到输出（spec §3）。
HUB_IDLE_CLOSE: float = float(os.environ.get("AINET_HUB_IDLE_CLOSE", "600"))
HUB_MIN_RECONNECT: float = float(os.environ.get("AINET_HUB_MIN_RECONNECT", "3"))

# 自动判定：学员回车后静默多少秒跑一次判定；两次判定的最小间隔（秒）。
JUDGE_DEBOUNCE: float = float(os.environ.get("AINET_JUDGE_DEBOUNCE", "3"))
JUDGE_MIN_INTERVAL: float = float(os.environ.get("AINET_JUDGE_MIN_INTERVAL", "10"))

# ---- 命令输出采集（过程评分用，spec §9）----
# 学员每条命令的设备输出摘要写入 attempt_logs.output；超过上限保留头尾、中间截断。
LOG_OUTPUT_MAX: int = int(os.environ.get("AINET_LOG_OUTPUT_MAX", "2000"))


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ---- AI（DeepSeek，OpenAI 兼容接口，spec §4）----
# API Key 不在这里配置：读取顺序见 ai.load_api_key()（环境变量 DEEPSEEK_API_KEY → AINET_SECRETS_FILE
# → backend/data/secrets.env → ~/.config/ainet/secrets.env）。
AI_BASE_URL: str = os.environ.get("AINET_AI_BASE_URL", "https://api.deepseek.com")
AI_MODEL: str = os.environ.get("AINET_AI_MODEL", "deepseek-flash")
AI_MAX_RETRIES: int = int(os.environ.get("AINET_AI_MAX_RETRIES", "2"))      # 超时 / 网络 / 限流 / 5xx 的重试次数
AI_JSON_MODE: bool = _flag("AINET_AI_JSON_MODE", "1")                       # response_format=json_object（已实测支持）

# 评分：异步执行、不阻塞学员，默认开启思考换取更稳的判断；思考也占 max_tokens，所以给足。
AI_SCORE_THINKING: bool = _flag("AINET_AI_SCORE_THINKING", "1")
AI_SCORE_TIMEOUT: float = float(os.environ.get("AINET_AI_SCORE_TIMEOUT", "120"))   # 单次请求超时（秒）
AI_SCORE_MAX_TOKENS: int = int(os.environ.get("AINET_AI_SCORE_MAX_TOKENS", "8000"))
# 提示：学员在等，关闭思考求快；总时长（含重试）不超过 AI_HINT_TIMEOUT，超时回退静态提示。
AI_HINT_THINKING: bool = _flag("AINET_AI_HINT_THINKING", "0")
AI_HINT_TIMEOUT: float = float(os.environ.get("AINET_AI_HINT_TIMEOUT", "15"))
AI_HINT_MAX_TOKENS: int = int(os.environ.get("AINET_AI_HINT_MAX_TOKENS", "800"))
AI_HINTS_ENABLED: bool = _flag("AINET_AI_HINTS", "1")                       # 0 = 只用场景静态提示
AI_SCORING_ENABLED: bool = _flag("AINET_AI_SCORING", "1")                   # 0 = 只给结果分（标注 AI 未启用）
# Agent 面板（spec §16）：流式对话，关闭思考；单次请求两块之间的超时、整轮（含工具调用）总时长上限
AI_AGENT_ENABLED: bool = _flag("AINET_AI_AGENT", "1")
AI_AGENT_TIMEOUT: float = float(os.environ.get("AINET_AI_AGENT_TIMEOUT", "30"))
AI_AGENT_DEADLINE: float = float(os.environ.get("AINET_AI_AGENT_DEADLINE", "120"))
AI_AGENT_MAX_TOKENS: int = int(os.environ.get("AINET_AI_AGENT_MAX_TOKENS", "1500"))
AGENT_MAX_TOOL_CALLS: int = 3                                              # 每轮（一次提问）最多执行的只读命令数
AGENT_TOOL_TIMEOUT: float = float(os.environ.get("AINET_AGENT_TOOL_TIMEOUT", "15"))

# ---- 评分模型（spec §9）----
# 三维满分（合计 100）；结果未达标时过程分乘以 SCORE_FAIL_DISCOUNT。
SCORE_WEIGHT_RESULT: float = float(os.environ.get("AINET_SCORE_WEIGHT_RESULT", "50"))
SCORE_WEIGHT_PROCESS: float = float(os.environ.get("AINET_SCORE_WEIGHT_PROCESS", "35"))
SCORE_WEIGHT_SIDE: float = float(os.environ.get("AINET_SCORE_WEIGHT_SIDE", "15"))
SCORE_FAIL_DISCOUNT: float = float(os.environ.get("AINET_SCORE_FAIL_DISCOUNT", "0.6"))
# 每用一级提示从过程分扣的分数（spec §7「可配扣分」）；缺省 0 = 不扣，提示用量只作为 AI 的参考信息。
SCORE_HINT_PENALTY: float = float(os.environ.get("AINET_SCORE_HINT_PENALTY", "0"))
# 送给 AI 的命令记录上限：最多多少条命令、每条输出多少字符。
SCORE_PROMPT_MAX_COMMANDS: int = int(os.environ.get("AINET_SCORE_PROMPT_MAX_COMMANDS", "80"))
SCORE_PROMPT_OUTPUT_CHARS: int = int(os.environ.get("AINET_SCORE_PROMPT_OUTPUT_CHARS", "800"))

# ---- 参数化场景与考试（里程碑 6）----
# 练习的种子：缺省每次随机（同一种子可复现，记入 attempts.seed）；设为整数则固定（0 = 全部取默认值，
# 与参数化之前的原题一致，真机 e2e / 回归用）。考试的种子由考试模板决定（固定或随机）。
_ps = os.environ.get("AINET_PRACTICE_SEED", "").strip()
PRACTICE_SEED: int | None = int(_ps) if _ps else None
# 草稿题（场景 "draft": true = 未在真机验证，docs/question-bank-plan.md）：缺省不在 GET /api/scenarios 里列出
# （课程页 / 操作页都看不到；进行中的那道照列）。设 1 显示（真机验证、出题时用）；按 id 开始练习不受限制。
SHOW_DRAFTS: bool = os.environ.get("AINET_SHOW_DRAFTS", "0").strip().lower() in ("1", "true", "yes", "on")
# 考试计时器检查间隔（秒）：到时自动提交 / 进入下一题 / 交卷都由它触发（计时以服务端为准）。
EXAM_TICK: float = float(os.environ.get("AINET_EXAM_TICK", "1"))
# 考试题结束时复位暂不可执行（学员停在系统视图等）的重试间隔（秒）。
EXAM_RESET_RETRY: float = float(os.environ.get("AINET_EXAM_RESET_RETRY", "5"))

# ---- 干净基线（spec §10.4，应对 eNSP Pro / YunShan 的自动保存）----
# YunShan 设备会自动保存配置（改动后约 5 分钟），而且停机时一定保存（undo configuration file auto-save 也挡不住，
# 2026-10-06 实测），没撤销的改动会跟着设备重启留下来。所以每台设备记一份「干净基线」（整机配置；PC 为地址配置），
# 练习 / 考试每题开始前先与基线比对，不一致就先复位到基线、再拍开练快照。没有基线的设备：开关打开（缺省）时把这次
# 读到的配置记为基线；也可以用 POST /api/lab/baseline 手动重记。
LAB_BASELINE_AUTO: bool = _flag("AINET_LAB_BASELINE_AUTO", "1")

# ---- 设备启动可靠性（spec §10.5，eNSP Pro；实现见 labpower.py）----
# 启动看门狗：设备启动超过 START_TIMEOUT 秒仍在启动阶段（状态 2 / 6），或接口报错误状态（3 / 5 / 17），自动「停止 → 启动」
# START_RETRIES 次；仍不行就标为「启动失败」（start_failed）并给出原因。正常启动 55~120 秒（2026-10-07 实测）。
ENSPRO_START_TIMEOUT: float = float(os.environ.get("AINET_ENSPRO_START_TIMEOUT", "150"))
ENSPRO_START_RETRIES: int = int(os.environ.get("AINET_ENSPRO_START_RETRIES", "3"))
# 同一台设备距 ainet 上次发出停止不到这么多秒不发启动（排队等够再发），让上一个容器先拆完。只是安全余量：2026-10-07 实测
# 卡住与停机间隔无关（10~145 秒都卡过），停掉卡住的实例后立即再启动反而都正常（spec §14 第 21 项）。
ENSPRO_RESTART_GAP: float = float(os.environ.get("AINET_ENSPRO_RESTART_GAP", "10"))
# 早期卡死检查：启动这么多秒（及 2 倍时）还在「启动系统」，下载一次设备日志（只读），已陷入板卡复位循环就不等
# START_TIMEOUT、立即按卡住处理（spec §14 第 21 项：卡住的容器启动约 15 秒就进入循环）。0 = 关闭。
ENSPRO_EARLY_CHECK: float = float(os.environ.get("AINET_ENSPRO_EARLY_CHECK", "40"))
# 多台设备同时启动时错开发出启动请求的间隔（秒）；0 = 一次批量启动。
ENSPRO_START_STAGGER: float = float(os.environ.get("AINET_ENSPRO_START_STAGGER", "5"))
# 看门狗轮询间隔（秒）。
ENSPRO_WATCH_POLL: float = float(os.environ.get("AINET_ENSPRO_WATCH_POLL", "5"))
# 就绪探测：接口报运行中之后，经 console 隐藏事务试探设备是否还在系统初始化（改配置会报 busy），通过了才算就绪；
# 之前设备状态为 initializing。PROBE_TIMEOUT 秒内一直没通过 = 按卡住处理（自动停止再启动 / 启动失败）。
ENSPRO_PROBE: bool = _flag("AINET_ENSPRO_PROBE", "1")
ENSPRO_PROBE_INTERVAL: float = float(os.environ.get("AINET_ENSPRO_PROBE_INTERVAL", "10"))
ENSPRO_PROBE_TIMEOUT: float = float(os.environ.get("AINET_ENSPRO_PROBE_TIMEOUT", "300"))
# 切换沙箱 / 按题目准备环境切到新沙箱后，自动停掉旧沙箱里运行着的设备（单人使用）。eNSP Pro 停机会保存配置，
# 所以只在没有练习 / 考试进行中（复位已做完）时停——切换本来就要求这一点。
ENSPRO_STOP_OLD: bool = _flag("AINET_ENSPRO_STOP_OLD", "1")
# 空闲自动停机：当前沙箱的设备超过这么多分钟没有任何活动（终端输入 / 输出、练习、考试、Agent 命令、启停），自动停掉；
# 0 = 关闭。练习 / 考试进行中不停；停之前提前 IDLE_WARN 秒给打开着的终端发通知。
ENSPRO_IDLE_STOP_MIN: float = float(os.environ.get("AINET_ENSPRO_IDLE_STOP_MIN", "30"))
ENSPRO_IDLE_WARN: float = float(os.environ.get("AINET_ENSPRO_IDLE_WARN", "60"))
ENSPRO_IDLE_CHECK: float = float(os.environ.get("AINET_ENSPRO_IDLE_CHECK", "15"))
# 同时运行设备上限（不含 PC 类终端，0 = 不限，0~64）：本机 eNSP Pro 最多稳定同时跑几台路由器 / 交换机。超过上限的题
# 课程页缺省不列出、按 id 开练 409；准备环境时先停掉本沙箱里非本题的设备。只是缺省值：按环境存 lab:watch:<环境>
# （课程页「⚙ 接入设置」可改，优先级同看门狗参数，见 lab.ENSPRO_WATCH）。里程碑 6.17。
ENSPRO_MAX_DEVICES: int = int(os.environ.get("AINET_ENSPRO_MAX_DEVICES", "0") or 0)
# 启动时以环境变量（AINET_LAB_PROVIDER / AINET_ENSPRO_SANDBOX）为准，覆盖库里记录的模拟环境与沙箱（并写回库）。
LAB_FORCE: bool = _flag("AINET_LAB_FORCE", "0")
