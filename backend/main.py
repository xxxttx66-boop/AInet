"""FastAPI 应用：登录/档案 + xterm.js 终端页 + WebSocket 桥接 console。

- GET  /                  终端页（未登录跳 /login；?port= 选择端口，默认 2000）。
- GET  /login /profile    登录页 / 档案页。
- POST /api/login /api/logout，GET /api/me，GET|POST /api/students。
- GET  /api/topologies（拓扑文件列表，供管理端）；GET /api/topology/current、
  GET /api/topology/status：按 eNSP 中运行的设备实时确定的当前拓扑与各设备状态。
- WS   /ws/console/{port} 需登录；订阅 ConsoleHub（每个 console 端口只保持一条设备连接，多个标签
  共享；关标签只退订，不断开设备连接，见 console_hub.py）。
  端口白名单 = 当前拓扑（state=running）中非 0 的 com_port；其他状态不放行任何端口；
  练习进行中只放行该场景的 accessible_devices。
- 实验后端（lab.py，AINET_LAB_PROVIDER=ensp|enspro）：GET /api/lab；POST /api/lab/devices/start|stop|reboot
  （只有 eNSP Pro 能由 ainet 启停设备；练习 / 考试中不能停本题设备）；POST /api/lab/relogin（eNSP Pro 会话被挤掉后重新登录）。
  白名单与 /ws/console/{port} 的 port 是统一设备标识 console 键（eNSP = com_port，eNSP Pro = deviceId）。
  GET /api/lab/baseline：当前拓扑各设备有没有「干净基线」；POST /api/lab/baseline {devices?: [设备名]}：把设备当前配置
  记为干净基线（eNSP Pro：每题开始前与基线比对，不一致先复位，应对 YunShan 自动保存，spec §10.4）。
- GET  /api/scenarios；POST /api/practice/start|check|hint|end；GET /api/practice/current（里程碑 3，
  见 practice.py）。
- GET  /api/practice/result/{id}、POST /api/practice/rescore/{id}：评分状态与结果、重新评分（里程碑 5，
  只能看 / 重评自己的记录，见 grading.py）。结束练习后后台异步评分；提示由 hints.py 生成（AI / 静态回退）。
- 考试（里程碑 6，见 exam.py）：GET|POST /api/exam/templates、GET|PUT|DELETE /api/exam/templates/{id}（模板管理，
  目前任何登录用户都能用，以后加管理员权限）；POST /api/exam/start|submit|next|finish|hint|retry_start、
  GET /api/exam/current；GET /api/exams、GET /api/exam/{id}/report|replay（交卷后、只能看自己的）。
  页面 /exam/templates（模板管理）、/exam/report?id=（成绩单 + 命令回放）。
- Agent 面板（spec §16，见 agent.py）：POST /api/agent/chat（SSE 流式；考试进行中 403）、GET /api/agent/history。
- AI 接口设置（里程碑 6.18）：GET|POST /api/ai/settings、POST /api/ai/settings/test（URL / 模型存库，Key 写 secrets 文件，
  只回显有无与掩码；保存后立即生效，见 ai.py）。
- 课程页（spec §16.3，里程碑 6.9）：GET /courses（登录后默认进入）；GET /api/scenarios 另带 chapters（scenarios/chapters.json）
  与每题 progress（passed / failed / none）；POST /api/exam/start_single {scenario_id}（单题考试：临时单题模板，默认规则）；
  GET|POST /api/lab/devices/{console}/host（PC 设置气泡：eNSP Pro 终端主机的 IP / 掩码 / 网关 / DNS；练习 / 考试中写操作记录、
  结束时随复位还原；eNSP 经典版 400）。
- 接入设置（spec §5.2，里程碑 6.12）：GET /api/lab/environments/{key}/endpoint、POST 同路径 {value}（改接入地址，空 = 回到缺省；
  练习 / 考试进行中 409）、POST …/endpoint/test {value?}（测试连接，不保存）。GET /api/lab/environments 每个环境另带
  reason_code（unreachable / tls / auth / missing_secret / other）、endpoint、secrets（只有有无）、runtime_local。
- 启动看门狗参数按环境配置（spec §5.2 / §10.5，里程碑 6.16）：GET|POST /api/lab/environments/{key}/watchdog
  {start_timeout?, start_retries?, early_check?, probe_timeout?}（null = 回到缺省；范围不对 422；进行中 409）；
  环境列表与接入设置里每个环境另带 watchdog（没有看门狗的环境为 null）。
- 用户添加的环境实例（里程碑 6.19，课程页环境列「+」）：POST /api/lab/environments {type, label, url, device_password?,
  start_timeout? …}（添加；先探测，失败也保存并给 warning）、POST /api/lab/environments/probe {type, url}（添加前测试连接）、
  PATCH|POST /api/lab/environments/{key} {label?, device_password?, clear_password?}、DELETE /api/lab/environments/{key}
  （只删用户添加的；当前环境 / 进行中 409）。POST /api/lab/prepare 可带 environment（同一平台有几台 eNSP Pro 时指定）。
- 自动识别 https（里程碑 6.20）：eNSP Pro 地址可不写协议（host:port；只有主机名补 :8443）。添加 / 添加前测试连接 / 改地址 /
  测试连接给的是 http:// 或没写协议时先试 TLS 握手（不登录）：通就改成 https://，返回带 normalized_from（原输入）与
  notice「已自动改为 https」；明文 HTTP 有响应就保留 http 并给 warning；都不通按输入（没写协议按 https）。另带 scheme_check。
  已存的 http 地址不自动迁移：POST …/endpoint/test 不给 value 且当前地址是 http、该端口其实是 https 时返回 suggest。

- 单人部署（里程碑 6.17）：AINET_AUTO_LOGIN=<登录名> 时没有有效会话的请求自动以该学员登录（GET /login 跳课程页，
  GET /api/me 带 auto_login）；GET /api/scenarios 按题目环境的「同时运行设备上限」（lab:watch:<环境> 的 max_devices，
  PC 不计）隐藏超上限的题，附 capacity_hidden / capacity_hidden_chapters / max_devices；按 id 开练 / 单题考试 / 模板考试 /
  准备环境超上限 409。

会话：内存字典 token -> student_id，token 存 cookie 绑定浏览器（spec §2 单人
使用、可切换学生；§12 明文密码仅区分人员）。服务重启后需重新登录（自动登录模式下自动重新签发）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import db
import scenarios as scenario_lib
import topology
import lab as lab_lib
import labenv
from connectors import EnspConnector
from console_hub import ConsoleHub, HubConnectError
from practice import PracticeError, PracticeManager
from exam import ExamError, ExamManager, normalize_config, platform_mismatch, template_platforms, template_view
import ai
import agent as agent_lib
import grading
import hints
from custom_labs import CustomLabs
from grading import Scorer

# 本项目日志（ainet.*：提示回退原因、评分失败等）输出到控制台，便于排查；uvicorn 只配置了它自己的 logger
_log = logging.getLogger("ainet")
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)


# 当前实验后端（lifespan 里由 LabManager 按库里记录 / AINET_LAB_PROVIDER 创建，运行时可切换，见 labenv.py）
LAB: lab_lib.LabProvider | None = None
LABMGR: labenv.LabManager | None = None


def lab() -> lab_lib.LabProvider:
    global LAB
    if LAB is None:
        LAB = lab_lib.make_provider()
    return LAB


def connector_factory(port: int):
    """ConsoleHub 用来新建设备连接（测试里替换成假 connector）。port 是统一的 console 键（lab.py）：
    eNSP 经典版为 telnet 端口，eNSP Pro 为 deviceId。"""
    if LAB is not None and LAB.kind != "ensp":
        return LAB.connector(port)
    return EnspConnector(lab_lib.endpoint_value("ensp"), port, config.CONNECT_TIMEOUT)   # 接入地址可在运行时改（spec §5.2）


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LAB, LABMGR
    db.init_db()
    await asyncio.to_thread(setup_auto_login)
    await asyncio.to_thread(ai.load_settings)        # 页面上保存的 AI 接口地址 / 模型 / Key 来源（里程碑 6.18）
    mgr = labenv.LabManager()
    LAB = await asyncio.to_thread(mgr.boot)

    def install(p: lab_lib.LabProvider) -> None:      # 切换模拟环境后：所有持有 provider 的地方都换成新的
        global LAB
        LAB = p
        app.state.lab = p
    mgr.on_install = install
    LABMGR = mgr
    app.state.labmgr = mgr
    app.state.lab = LAB
    # hub / 练习管理器绑定当前事件循环，放在 lifespan 里建（不做成模块级对象）
    # YunShan：undo sysname 之后设备名是 HUAWEI（VRP5 为 Huawei），hub 跟踪提示符要用（只在需要时传，测试替身不受影响）
    extra = {"default_sysname": "HUAWEI"} if LAB.platform == "yunshan" else {}
    hub = ConsoleHub(lambda port: connector_factory(port), idle_close=config.HUB_IDLE_CLOSE,
                     min_reconnect=config.HUB_MIN_RECONNECT, output_max=config.LOG_OUTPUT_MAX, **extra)
    loaded, errors = await asyncio.to_thread(scenario_lib.load_dir, config.SCENARIOS_DIR)
    chapters, _ = await asyncio.to_thread(scenario_lib.load_chapters, config.SCENARIOS_DIR)   # 错误已在 errors 里
    app.state.chapters = chapters or []
    ids = await asyncio.to_thread(db.sync_scenarios, list(loaded.values()))
    practice = PracticeManager(hub, loaded, ids, resolve_current,
                               debounce=config.JUDGE_DEBOUNCE, min_interval=config.JUDGE_MIN_INTERVAL)
    await asyncio.to_thread(practice.recover)
    # 里程碑 5：AI 评分与分级提示（AI 不可用时评分降级为只给结果分、提示回退静态）
    await asyncio.to_thread(db.fail_stale_scoring, "服务重启，评分中断，请点「重新评分」")
    scorer = Scorer()
    practice.scorer = scorer
    practice.hinter = hints.generate
    practice.lab = LAB                 # 终端主机（PC）的地址配置、干净基线
    await practice.resume_ending()     # 重启前后台复位被打断的练习：接着复位
    key, source = ai.load_api_key()
    print(f"[AI] 地址 {ai.effective_base_url()[0]}，模型 {ai.effective_model()[0]}，API Key：{source}（{ai.mask_key(key)}）" if key
          else "[AI] 未配置 DeepSeek API Key：评分只给结果分，提示使用场景静态提示")
    app.state.hub, app.state.practice, app.state.scenario_errors = hub, practice, errors
    app.state.scorer = scorer
    # 里程碑 6：考试（复用练习的一题底层能力；进行中的考试从库里恢复，计时照常）
    exam_mgr = ExamManager(practice)
    await asyncio.to_thread(exam_mgr.ensure_builtin)
    await asyncio.to_thread(exam_mgr.recover)
    exam_mgr.start_loop()
    app.state.exam = exam_mgr
    # Agent 面板：监听 hub 的学员命令（快照用），只读命令走隐藏事务
    app.state.agent = agent_lib.AgentService(hub, practice, exam_mgr, lab)
    app.state.agent.labmgr = mgr
    app.state.custom_labs = CustomLabs(mgr)
    mgr.bind(hub, practice, exam_mgr, app.state.agent)
    mgr.start_background()     # 启动看门狗接着跟踪上次没完成的启动、空闲自动停机（spec §10.5）
    for e in errors:
        print(f"[场景库] 校验失败：{e}")
    try:
        yield
    finally:
        await exam_mgr.close()
        await practice.shutdown()
        await scorer.close()
        await mgr.close()          # eNSP Pro：退出登录，释放单会话
        await hub.close()
        LAB = None
        LABMGR = None


app = FastAPI(title="ainet 局域网运维实操练习", lifespan=lifespan)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# 前端第三方库本地化（局域网机器不依赖 CDN）
app.mount("/vendor", StaticFiles(directory=FRONTEND / "vendor"), name="vendor")
app.mount("/device-icons", StaticFiles(directory=FRONTEND / "device-icons"), name="device-icons")

# 会话：token -> student_id
SESSIONS: dict[str, int] = {}

# 自动登录（AINET_AUTO_LOGIN，里程碑 6.17）：{"login", "sid", "token"}；关闭时 sid 为 None。
# 所有自动签发的会话共用一个 token（避免每个还没带上 cookie 的请求都在 SESSIONS 里加一条）。
AUTO: dict[str, object] = {"login": "", "sid": None, "token": None}


def setup_auto_login() -> None:
    """启动时（lifespan）：AINET_AUTO_LOGIN 设了登录名就确保该学员存在（不存在则创建：显示名同登录名，密码随机且
    不输出——自动登录用不到它，只是占位），准备共用的会话 token。日志只写登录名，不写密码。"""
    login = (config.AUTO_LOGIN or "").strip()
    AUTO.update(login=login, sid=None, token=None)
    if not login:
        return
    st = db.get_student_by_login(login)
    if st is None:
        try:
            st = db.create_student(login, login, secrets.token_urlsafe(24))
        except db.DuplicateLogin:
            st = db.get_student_by_login(login)
    token = secrets.token_urlsafe(32)
    AUTO.update(sid=st["id"], token=token)
    SESSIONS[token] = st["id"]
    _log.warning("自动登录已开启：%s", login)


def auto_login_on() -> bool:
    return AUTO["sid"] is not None


def current_student_id(cookies) -> int | None:
    token = cookies.get(config.SESSION_COOKIE)
    sid = SESSIONS.get(token) if token else None
    if sid is None and AUTO["sid"] is not None:
        return AUTO["sid"]            # 自动登录：没有有效会话就当作该学员（含 WebSocket，cookie 由下面的中间件补发）
    return sid


@app.middleware("http")
async def auto_login_cookie(request: Request, call_next):
    """自动登录模式下，没有有效会话 cookie 的请求在响应里补发自动登录学员的会话 cookie（与 api_login 同一套 SESSIONS）。
    是否有效在处理请求之前判断（不与 api_logout 的删除 cookie 打架）；响应自己设了会话 cookie（api_login）时不补。"""
    if AUTO["sid"] is None:
        return await call_next(request)
    token = request.cookies.get(config.SESSION_COOKIE)
    valid = bool(token) and token in SESSIONS
    response = await call_next(request)
    if not valid:
        own = any(v.startswith(f"{config.SESSION_COOKIE}=") for v in response.headers.getlist("set-cookie"))
        if not own:
            SESSIONS[str(AUTO["token"])] = int(AUTO["sid"])     # 被登出接口删掉过就补回
            response.set_cookie(config.SESSION_COOKIE, str(AUTO["token"]), httponly=True, samesite="lax")
    return response


def require_student_id(request: Request) -> int:
    sid = current_student_id(request.cookies)
    if sid is None or db.get_student(sid) is None:
        raise HTTPException(status_code=401, detail="未登录")
    return sid


async def resolve_current() -> dict:
    cur = await lab().resolve()
    if LABMGR is not None:
        cur = {**cur, "env": LABMGR.env_state()}     # 当前模拟环境 key / 沙箱 / 切换代数（前端据此发现环境切换）
        LABMGR.note_topology(cur)                    # 同时运行设备上限按拓扑里的设备类型计数（里程碑 6.17）
    return cur


def _mgr(request: Request) -> labenv.LabManager:
    return request.app.state.labmgr


class LoginBody(BaseModel):
    login: str
    password: str


class NewStudentBody(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


# ---- 页面 ----

@app.get("/", response_model=None)
async def index(request: Request) -> FileResponse | RedirectResponse:
    if current_student_id(request.cookies) is None:
        return RedirectResponse("login", status_code=303)
    return FileResponse(FRONTEND / "index.html")


# 工作台页面拆出的脚本 / 样式（只放行这几个文件）
_STATIC_FILES = {"app.js", "app.css", "ui.js", "ui.css", "exam.js", "agent.js", "page.js", "labswitch.js", "courses.js", "aiset.js", "custom_lab.js", "custom_lab.css", "device_icons.js"}


@app.get("/static/{name}")
async def static_file(name: str) -> FileResponse:
    if name not in _STATIC_FILES:
        raise HTTPException(status_code=404)
    return FileResponse(FRONTEND / name)


@app.get("/login", response_model=None)
async def login_page() -> FileResponse | RedirectResponse:
    if auto_login_on():               # 自动登录：不需要登录页
        return RedirectResponse("courses", status_code=303)
    return FileResponse(FRONTEND / "login.html")


@app.get("/courses", response_model=None)
async def courses_page(request: Request) -> FileResponse | RedirectResponse:
    """课程选择页（登录后默认进入）：环境 → 章节 → 课题 → 预览，再进操作页（工作台）。"""
    if current_student_id(request.cookies) is None:
        return RedirectResponse("login", status_code=303)
    return FileResponse(FRONTEND / "courses.html")


@app.get("/profile", response_model=None)
async def profile_page(request: Request) -> FileResponse | RedirectResponse:
    if current_student_id(request.cookies) is None:
        return RedirectResponse("login", status_code=303)
    return FileResponse(FRONTEND / "profile.html")


@app.get("/custom-labs", response_model=None)
async def custom_labs_page(request: Request) -> FileResponse | RedirectResponse:
    if current_student_id(request.cookies) is None:
        return RedirectResponse("login", status_code=303)
    return FileResponse(FRONTEND / "custom_lab.html")


@app.get("/exam/templates", response_model=None)
async def exam_templates_page(request: Request) -> FileResponse | RedirectResponse:
    if current_student_id(request.cookies) is None:
        return RedirectResponse("../login", status_code=303)
    return FileResponse(FRONTEND / "exam_admin.html")


@app.get("/exam/report", response_model=None)
async def exam_report_page(request: Request) -> FileResponse | RedirectResponse:
    if current_student_id(request.cookies) is None:
        return RedirectResponse("../login", status_code=303)
    return FileResponse(FRONTEND / "exam_report.html")


# ---- API ----

@app.post("/api/login")
async def api_login(body: LoginBody, request: Request, response: Response) -> dict:
    student = db.verify_password(body.login.strip(), body.password)
    if student is None:
        raise HTTPException(status_code=401, detail="登录名或密码错误")
    # 切换学生：作废本浏览器旧 token，签发新 token
    old = request.cookies.get(config.SESSION_COOKIE)
    if old:
        SESSIONS.pop(old, None)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = student["id"]
    response.set_cookie(config.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return student


@app.post("/api/logout")
async def api_logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        SESSIONS.pop(token, None)
    response.delete_cookie(config.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request) -> dict:
    sid = require_student_id(request)
    profile = db.get_profile(sid)
    if profile is None:
        raise HTTPException(status_code=401, detail="未登录")
    # 自动登录模式：前端隐藏「切换学员」「登出」
    return {**profile, "auto_login": auto_login_on()}


@app.get("/api/students")
async def api_list_students() -> list[dict]:
    # 登录页切换下拉要用，故不要求登录（仅返回 id/login/name，不含密码）
    return db.list_students()


@app.post("/api/students", status_code=201)
async def api_create_student(body: NewStudentBody) -> dict:
    login, name = body.login.strip(), body.name.strip()
    if not login or not name:
        raise HTTPException(status_code=422, detail="登录名和姓名不能为空")
    try:
        return db.create_student(login, name, body.password)
    except db.DuplicateLogin:
        raise HTTPException(status_code=409, detail=f"登录名已存在：{body.login}")


# ---- 拓扑 ----

@app.get("/api/topologies")
async def api_topologies(request: Request) -> list[dict]:
    require_student_id(request)
    return await asyncio.to_thread(topology.list_topologies)


@app.get("/api/topology/current")
async def api_topology_current(request: Request) -> dict:
    require_student_id(request)
    cur = await resolve_current()
    em: ExamManager = request.app.state.exam
    if em.hide_topology() and cur.get("topology"):
        # 考试模板设定不可看拓扑：去掉连线、背景图形和注释，只留设备（终端标签要用设备名 / 端口）
        t = dict(cur["topology"])
        t.update(links=[], shapes=[], notes=[], hidden=True)
        cur = {**cur, "topology": t}
    return cur


@app.get("/api/topology/status")
async def api_topology_status(request: Request) -> dict:
    require_student_id(request)
    return {k: v for k, v in (await resolve_current()).items() if k != "topology"}


# ---- 实验后端：设备启停、重新登录（eNSP Pro） ----

class PowerBody(BaseModel):
    devices: list[int] = Field(min_length=1, max_length=64)    # console 键（eNSP Pro 为 deviceId）


@app.get("/api/lab")
async def api_lab(request: Request) -> dict:
    require_student_id(request)
    return _mgr(request).info()


# ---- 模拟环境注册与切换（spec §5.2） ----

@app.get("/api/lab/environments")
async def api_lab_environments(request: Request, refresh: int = 0) -> dict:
    """全部注册环境与在线状态（并行探测、各自超时、缓存 15 秒；refresh=1 强制重测）、当前环境与沙箱、能否切换。"""
    require_student_id(request)
    return await _mgr(request).environments(force=bool(refresh))


class EnvBody(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    sandbox: int | None = Field(default=None, ge=0)


@app.post("/api/lab/environment")
async def api_lab_environment(body: EnvBody, request: Request) -> dict:
    """显式切换模拟环境 / 沙箱（任何已登录用户；单人使用，spec §2）：练习 / 考试进行中 409；目标离线 409；
    会断开所有终端。"""
    require_student_id(request)
    try:
        res = await _mgr(request).switch(body.key, body.sandbox, sandbox_given="sandbox" in body.model_fields_set)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return res


# ---- 接入设置（spec §5.2「接入设置」）：每个环境的接入地址可在运行时改；凭据只在 ainet 主机上，接口只报有无 ----

class EndpointBody(BaseModel):
    value: str = Field(max_length=300)               # 空串 = 删掉库里的记录，回到环境变量 / 缺省值


class EndpointTestBody(BaseModel):
    value: str | None = Field(default=None, max_length=300)   # 不给 = 测当前地址


@app.get("/api/lab/environments/{key}/endpoint")
async def api_lab_endpoint(key: str, request: Request) -> dict:
    """{environment, label, current, endpoint{kind, label, value, source, default, env_var, runtime_local}, secrets{名: 有无},
    runtime_local, locked, can_edit}。"""
    require_student_id(request)
    try:
        return await asyncio.to_thread(_mgr(request).endpoint_info, key)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/api/lab/environments/{key}/endpoint")
async def api_lab_endpoint_set(key: str, body: EndpointBody, request: Request) -> dict:
    """改接入地址（eNSP Pro：http/https URL；eNSP：console 主机）。练习 / 考试 / 准备环境进行中 409，格式不对 422；
    改的是当前环境时断开全部终端并重建后端。"""
    require_student_id(request)
    try:
        return await _mgr(request).set_endpoint(key, body.value)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/api/lab/environments/{key}/endpoint/test")
async def api_lab_endpoint_test(key: str, body: EndpointTestBody, request: Request) -> dict:
    """测试连接（不保存）：{available, reason, reason_code, value, checked_at[, runtime_local]}；不登录、不连设备 console。"""
    require_student_id(request)
    try:
        return await _mgr(request).test_endpoint(key, body.value)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


class WatchBody(BaseModel):
    """只改给出的参数；null = 删掉库里的记录（回到环境变量 / 缺省值）。范围校验在 lab.normalize_watch（422 中文说明）。"""
    start_timeout: float | None = None
    start_retries: float | None = None
    early_check: float | None = None
    probe_timeout: float | None = None
    max_devices: float | None = None          # 同时运行设备上限（0~64 整数，0 = 不限；里程碑 6.17）


@app.get("/api/lab/environments/{key}/watchdog")
async def api_lab_watchdog(key: str, request: Request) -> dict:
    """启动看门狗参数（按环境，spec §5.2「接入设置」）：{environment, label, current, watchdog{values, params[{name, label,
    unit, value, source, default, builtin, env_var, min, max, integer, zero_off}]}, locked, can_edit}；没有看门狗的环境 400。"""
    require_student_id(request)
    try:
        return await asyncio.to_thread(_mgr(request).watch_info, key)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/api/lab/environments/{key}/watchdog")
async def api_lab_watchdog_set(key: str, body: WatchBody, request: Request) -> dict:
    """改看门狗参数（start_timeout 60~1800 秒、start_retries 0~10、early_check 0 或 10~600 秒且小于启动超时、
    probe_timeout 30~1800 秒）。练习 / 考试 / 准备环境进行中 409；当前环境立即生效（不重建后端、不断开终端）。"""
    require_student_id(request)
    values = {k: getattr(body, k) for k in body.model_fields_set}
    if not values:
        raise HTTPException(status_code=422, detail="没有要修改的参数")
    try:
        return await _mgr(request).set_watch(key, values)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


# ---- 用户添加的环境实例（里程碑 6.19，课程页环境列「+」）：每台 eNSP Pro 各有地址、设备密码、看门狗参数、设备上限 ----

class EnvAddBody(BaseModel):
    type: str = Field(min_length=1, max_length=32)
    label: str = Field(max_length=100)
    url: str = Field(max_length=300)
    device_password: str | None = Field(default=None, max_length=256)   # 只写不读：写进 secrets 文件，接口只报有无
    start_timeout: float | None = None
    start_retries: float | None = None
    early_check: float | None = None
    probe_timeout: float | None = None
    max_devices: float | None = None


class EnvProbeBody(BaseModel):
    type: str = Field(min_length=1, max_length=32)
    url: str = Field(max_length=300)


class EnvEditBody(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    device_password: str | None = Field(default=None, max_length=256)
    clear_password: bool = False


def _lab_call(exc: lab_lib.LabError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/api/lab/environments/probe")
async def api_lab_env_probe(body: EnvProbeBody, request: Request) -> dict:
    """添加环境前的「测试连接」（不保存、不登录、不连设备 console）：{available, reason, reason_code, value, checked_at}。"""
    require_student_id(request)
    try:
        return await _mgr(request).test_new_env(body.type, body.url)
    except lab_lib.LabError as exc:
        raise _lab_call(exc)


@app.post("/api/lab/environments")
async def api_lab_env_add(body: EnvAddBody, request: Request) -> dict:
    """添加环境实例（目前只支持 type=enspro）：{key, environment{…同环境列表的一项}, probe, warning}。格式不对 422；
    探测失败也保存（warning 写原因）。不切换当前环境。"""
    require_student_id(request)
    watch = {k: getattr(body, k) for k in ("start_timeout", "start_retries", "early_check", "probe_timeout", "max_devices")
             if getattr(body, k) is not None}
    try:
        return await _mgr(request).add_env(body.type, body.label, body.url, password=body.device_password or None,
                                           watch=watch)
    except lab_lib.LabError as exc:
        raise _lab_call(exc)


@app.api_route("/api/lab/environments/{key}", methods=["PATCH", "POST"])
async def api_lab_env_edit(key: str, body: EnvEditBody, request: Request) -> dict:
    """改显示名（只限用户添加的环境）、设置 / 清除设备 console 密码（eNSP Pro 类）：{changed[], environment}。"""
    require_student_id(request)
    if body.label is None and not body.device_password and not body.clear_password:
        raise HTTPException(status_code=422, detail="没有要修改的项")
    try:
        return await _mgr(request).edit_env(key, label=body.label, password=body.device_password or None,
                                            clear_password=body.clear_password)
    except lab_lib.LabError as exc:
        raise _lab_call(exc)


@app.delete("/api/lab/environments/{key}")
async def api_lab_env_delete(key: str, request: Request) -> dict:
    """删除用户添加的环境（内置 400；当前环境、练习 / 考试 / 准备环境进行中 409；不存在 404），清理它的设置、基线与
    secrets 文件里的设备密码行。"""
    require_student_id(request)
    try:
        return await _mgr(request).delete_env(key)
    except lab_lib.LabError as exc:
        raise _lab_call(exc)


@app.get("/api/lab/sandboxes")
async def api_lab_sandboxes(request: Request) -> dict:
    """当前环境（eNSP Pro）的沙箱列表：id、名称、设备数、运行数。"""
    require_student_id(request)
    try:
        return await _mgr(request).sandboxes()
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


class PrepareBody(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=200)
    environment: str | None = Field(default=None, max_length=32)   # 在哪个环境里做（缺省：当前环境平台相同就用它）


@app.post("/api/lab/prepare", status_code=202)
async def api_lab_prepare(body: PrepareBody, request: Request) -> dict:
    """按题目准备环境（课程页「开始练习」）：切到题目平台的环境与沙箱、启动本题设备、等到运行中（后台任务）。
    要求当前没有任何练习 / 考试进行中、目标环境在线；进度用 GET /api/lab/prepare 轮询。"""
    require_student_id(request)
    try:
        return await _mgr(request).start_prepare(body.scenario_id, body.environment)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.get("/api/lab/prepare")
async def api_lab_prepare_state(request: Request) -> dict:
    require_student_id(request)
    return {"job": _mgr(request).job_view()}


@app.post("/api/lab/devices/{action}")
async def api_lab_power(action: str, body: PowerBody, request: Request) -> dict:
    """启动 / 停止 / 重启设备（只有支持启停的实验后端，即 eNSP Pro）。练习或考试进行中不能停止 / 重启本题需要的设备。"""
    require_student_id(request)
    provider = lab()
    if action not in ("start", "stop", "reboot"):
        raise HTTPException(status_code=404, detail="未知操作")
    if not provider.power:
        raise HTTPException(status_code=400, detail=f"{provider.label()} 不支持由 ainet 启停设备，请在模拟器界面上操作")
    pm: PracticeManager = request.app.state.practice
    a = pm.active
    if action in ("stop", "reboot") and a is not None:
        busy = sorted(d for d, k in a.ports.items() if k in body.devices)
        if busy and pm.resetting:
            raise HTTPException(status_code=409, detail="上一次练习正在后台复位，暂时不能停止 / 重启这些设备："
                                + "、".join(busy))
        if busy:
            label = "考试" if a.mode == "exam" else "练习"
            raise HTTPException(status_code=409, detail=f"{label}进行中，不能停止本题需要的设备：{'、'.join(busy)}")
    mgr = _mgr(request)
    mgr.touch("power")
    try:
        # 全部经启动看门狗（spec §10.5）：启动错开发出、遵守停机间隔、卡住自动停止再启动；重启 = 停止 → 等间隔 → 启动
        if action == "start":
            res = await mgr.power.start(list(body.devices), source="api")
        elif action == "reboot":
            res = await mgr.power.reboot(list(body.devices), source="api")
        else:
            res = await mgr.power.stop(list(body.devices))
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    if action in ("stop", "reboot"):
        hub: ConsoleHub = request.app.state.hub
        for k in body.devices:          # 设备要关机：断开 console（标签页提示，之后按需重连）
            await hub.disconnect(k, "\r\n[设备正在停止，终端已断开]\r\n")
    return res


# ---- 自定义实验：新沙箱与拓扑编辑（只操作当前学员在 AInet 中创建的 eNSP Pro 沙箱） ----

class CustomLabBody(BaseModel):
    environment: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=60)


class CustomPointBody(BaseModel):
    x: float = Field(ge=-0.48, le=0.48)
    y: float = Field(ge=-0.48, le=0.48)


class CustomDeviceBody(CustomPointBody):
    name: str = Field(min_length=1, max_length=32)
    model_id: int = Field(gt=0)


class CustomLinkBody(BaseModel):
    left_if: int = Field(gt=0)
    right_if: int = Field(gt=0)


async def _custom(coro):
    try:
        return await coro
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


@app.get("/api/custom-labs")
async def api_custom_labs(request: Request) -> dict:
    sid = require_student_id(request)
    return await asyncio.to_thread(request.app.state.custom_labs.list, sid)


@app.post("/api/custom-labs", status_code=201)
async def api_custom_labs_create(body: CustomLabBody, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.create(sid, body.environment, body.name))


@app.post("/api/custom-labs/{lab_id}/activate")
async def api_custom_labs_activate(lab_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.activate(lab_id, sid))


@app.get("/api/custom-labs/{lab_id}")
async def api_custom_labs_detail(lab_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.detail(lab_id, sid))


@app.get("/api/custom-labs/{lab_id}/models")
async def api_custom_labs_models(lab_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.models(lab_id, sid))


@app.post("/api/custom-labs/{lab_id}/devices")
async def api_custom_labs_add_device(lab_id: int, body: CustomDeviceBody, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.add_device(
        lab_id, sid, body.name, body.model_id, body.x, body.y))


@app.patch("/api/custom-labs/{lab_id}/devices/{device_id}")
async def api_custom_labs_move_device(lab_id: int, device_id: int, body: CustomPointBody, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.move_device(lab_id, sid, device_id, body.x, body.y))


@app.delete("/api/custom-labs/{lab_id}/devices/{device_id}")
async def api_custom_labs_remove_device(lab_id: int, device_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.remove_device(lab_id, sid, device_id))


@app.post("/api/custom-labs/{lab_id}/links")
async def api_custom_labs_add_link(lab_id: int, body: CustomLinkBody, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.add_link(lab_id, sid, body.left_if, body.right_if))


@app.delete("/api/custom-labs/{lab_id}/links/{link_id}")
async def api_custom_labs_remove_link(lab_id: int, link_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.remove_link(lab_id, sid, link_id))


@app.delete("/api/custom-labs/{lab_id}")
async def api_custom_labs_delete(lab_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    return await _custom(request.app.state.custom_labs.delete(lab_id, sid))


# ---- PC 设置气泡（终端主机地址，eNSP Pro） ----

_IPV4 = r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"


class HostBody(BaseModel):
    ip: str = Field(default="", max_length=15)
    mask: str = Field(default="", max_length=15)            # 点分掩码或前缀长度
    gateway: str = Field(default="", max_length=15)
    dns: str | None = Field(default=None, max_length=15)     # 不给 = 不改 DNS


def _host_device(provider: lab_lib.LabProvider, cur: dict, key: int) -> dict:
    """console 键 -> 当前拓扑里的终端主机；经典版 / 不是 PC / 不在拓扑里一律 400 / 404。"""
    if provider.kind == "ensp":
        raise HTTPException(status_code=400, detail="eNSP 经典版的 PC 由 eNSP 软件模拟，ainet 无法配置：请在 eNSP 软件中双击 PC 设置")
    dev = next((d for d in ((cur.get("topology") or {}).get("devices") or []) if lab_lib.console_of(d) == key), None)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"当前拓扑里没有设备 {key}")
    if dev.get("kind") != "pc" or not provider.is_host(key):
        raise HTTPException(status_code=400, detail=f"{dev['name']} 不是终端主机（PC），请在终端里用命令配置")
    return dev


def _host_view(dev: dict, cfg: dict, cur: dict) -> dict:
    return {"device": dev["name"], "console": lab_lib.console_of(dev),
            "status": (cur.get("devices") or {}).get(dev["id"], dev.get("status", "unknown")),
            "config": cfg, "dns_supported": "dns" in cfg}


@app.get("/api/lab/devices/{key}/host")
async def api_lab_host_get(key: int, request: Request) -> dict:
    """PC 设置气泡打开时读当前地址（eNSP Pro 设备属性接口）。"""
    require_student_id(request)
    provider = lab()
    cur = await provider.resolve()
    dev = _host_device(provider, cur, key)
    try:
        cfg = await provider.host_get(key)
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return _host_view(dev, cfg, cur)


@app.post("/api/lab/devices/{key}/host")
async def api_lab_host_set(key: int, body: HostBody, request: Request) -> dict:
    """PC 设置气泡「应用」：设 IP / 掩码 / 网关（/ DNS），等 PC 命令行恢复（ipv4.status 回到 Ready）后返回。
    练习 / 考试中只能改本题开放的设备，每次修改写进操作记录，结束时随复位还原（practice.set_host）。"""
    require_student_id(request)
    import re as _re
    vals = {k: (getattr(body, k) or "").strip() for k in ("ip", "mask", "gateway")}
    for k in ("ip", "gateway"):
        if vals[k] and not _re.fullmatch(_IPV4, vals[k]):
            raise HTTPException(status_code=422, detail=f"{'IP 地址' if k == 'ip' else '网关'}格式不对：{vals[k]}")
    m = vals["mask"]
    if m and not ((m.isdigit() and 0 <= int(m) <= 32) or _re.fullmatch(_IPV4, m)):
        raise HTTPException(status_code=422, detail=f"掩码格式不对：{m}（写 255.255.255.0 或 24）")
    if vals["ip"] and not m:
        raise HTTPException(status_code=422, detail="设置 IP 地址时要同时给出掩码")
    if body.dns is not None:
        vals["dns"] = body.dns.strip()
        if vals["dns"] and not _re.fullmatch(_IPV4, vals["dns"]):
            raise HTTPException(status_code=422, detail=f"DNS 格式不对：{vals['dns']}")
    if _mgr(request).switching:
        raise HTTPException(status_code=409, detail="模拟环境切换中，请稍后再试")
    provider = lab()
    cur = await provider.resolve()
    dev = _host_device(provider, cur, key)
    if (cur.get("devices") or {}).get(dev["id"], dev.get("status")) != "online":
        raise HTTPException(status_code=409, detail=f"{dev['name']} 没有运行，启动后再设置")
    got = await _call(_practice(request).set_host(key, dev["name"], vals))
    return _host_view(dev, got, cur)


class BaselineBody(BaseModel):
    devices: list[str] | None = Field(default=None, max_length=64)   # 设备名；缺省 = 当前拓扑里运行中的全部设备


@app.get("/api/lab/baseline")
async def api_lab_baseline(request: Request) -> dict:
    require_student_id(request)
    return await request.app.state.practice.baselines()


@app.post("/api/lab/baseline")
async def api_lab_baseline_capture(body: BaselineBody, request: Request) -> dict:
    """把设备当前配置记为干净基线（应在设备刚建好 / 确认干净时做；练习 / 考试进行中拒绝）。"""
    require_student_id(request)
    try:
        return await request.app.state.practice.capture_baselines(body.devices)
    except PracticeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/api/lab/relogin")
async def api_lab_relogin(request: Request) -> dict:
    """eNSP Pro 会话被挤掉（同一账号在别处登录）后，由用户确认重新登录。"""
    require_student_id(request)
    try:
        return await _mgr(request).relogin()
    except lab_lib.LabError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


# ---- 场景与练习 ----

class StartBody(BaseModel):
    scenario_id: str
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)   # 参数化场景的种子；不给则随机（可复现）


class HintBody(BaseModel):
    level: int


def _practice(request: Request) -> PracticeManager:
    return request.app.state.practice


async def _call(coro):
    try:
        return await coro
    except PracticeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.get("/api/scenarios")
async def api_scenarios(request: Request) -> dict:
    sid = require_student_id(request)
    pm = _practice(request)
    mgr = _mgr(request)
    q = request.query_params
    if q.get("all") in ("1", "true"):
        platform, key = None, None
    else:
        key = (q.get("env") or mgr.key).strip().lower()
        try:
            platform = lab_lib.get_env(key).platform
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    items = await pm.list(platform)
    n_all = len(items)
    # 草稿题（未真机验证，"draft": true）缺省不列出；进行中的那道照列（操作页按 ?all=1 找当前课题的工单）
    active_id = pm.active.scenario["id"] if pm.active else None
    show_drafts = config.SHOW_DRAFTS
    if not show_drafts:
        items = [it for it in items if not it.get("draft") or it["id"] == active_id]
    drafts_hidden = n_all - len(items)
    # 同时运行设备上限（里程碑 6.17）：超过所浏览环境上限的题不列出（PC 不计），进行中的那道照列；?all=1 不过滤
    max_dev = lab_lib.max_devices(key) if key else 0
    cap_hidden: dict[str, int] = {}
    if max_dev > 0:
        keep = []
        for it in items:
            s = pm.scenarios.get(it["id"])
            c = mgr.capacity(s, key) if s is not None and it["id"] != active_id else {"over": False}
            if c["over"]:
                cap_hidden[it.get("chapter") or ""] = cap_hidden.get(it.get("chapter") or "", 0) + 1
            else:
                keep.append(it)
        items = keep
    progress = await asyncio.to_thread(db.scenario_progress, sid)     # 课程页的完成状态（已通过 / 未通过 / 未开始）
    for it in items:
        it["progress"] = progress.get(it["id"], "none")
    other = 0 if platform is None else sum(s.get("platform", "vrp5") != platform and (show_drafts or not s.get("draft"))
                                           for s in pm.scenarios.values())
    empty = ""
    if platform is not None and not items:
        empty = f"{lab_lib.get_env(key).label}（{scenario_lib.PLATFORMS.get(platform, platform)}）下还没有题目"
        if cap_hidden:
            empty = f"{lab_lib.get_env(key).label} 下的 {sum(cap_hidden.values())} 道题都需要超过 {max_dev} 台设备，本机已隐藏"
    return {"scenarios": items, "errors": request.app.state.scenario_errors,
            "active": pm.active.scenario["id"] if pm.active else None,
            "env": key, "platform": platform, "current_env": mgr.key, "other_platform": other, "empty_reason": empty,
            "chapters": getattr(request.app.state, "chapters", []),
            "show_drafts": show_drafts, "drafts_hidden": drafts_hidden,
            "max_devices": max_dev, "capacity_hidden": sum(cap_hidden.values()),
            "capacity_hidden_chapters": {k: v for k, v in cap_hidden.items() if k}}


def _capacity_guard(request: Request, scenario_ids) -> None:
    """同时运行设备上限（里程碑 6.17）：按 id 开始的题超过本机上限时 409（中文原因，不含 busy，课程页不会自动重试）。"""
    pm = _practice(request)
    for sid_ in scenario_ids:
        s = pm.scenarios.get(sid_)
        if s is None:
            continue
        why = _mgr(request).capacity_reason(s)
        if why:
            raise HTTPException(status_code=409, detail=why)


@app.post("/api/practice/start")
async def api_practice_start(body: StartBody, request: Request) -> dict:
    sid = require_student_id(request)
    if request.app.state.exam.exam is not None:
        raise HTTPException(status_code=409, detail="考试进行中，不能开始练习")
    if _mgr(request).switching:
        raise HTTPException(status_code=409, detail="模拟环境切换中，请稍后再开始")
    _capacity_guard(request, [body.scenario_id])
    return await _call(_practice(request).start(sid, body.scenario_id, body.seed))


@app.post("/api/practice/check")
async def api_practice_check(request: Request) -> dict:
    require_student_id(request)
    return await _call(_practice(request).check())


@app.post("/api/practice/hint")
async def api_practice_hint(body: HintBody, request: Request) -> dict:
    require_student_id(request)
    return await _call(_practice(request).hint(body.level))


@app.post("/api/practice/end")
async def api_practice_end(request: Request) -> dict:
    require_student_id(request)
    return await _call(_practice(request).begin_end())


@app.get("/api/practice/current")
async def api_practice_current(request: Request) -> dict:
    require_student_id(request)
    pm = _practice(request)
    if (pm.active is not None and pm.active.mode == "exam") or request.app.state.exam.exam is not None:
        return {"active": False, "exam_active": True, "last_result": None}   # 考试题不经练习接口暴露
    return pm.state()


def _own_attempt(attempt_id: int, sid: int, request: Request | None = None) -> dict:
    """只能看自己的练习记录；别人的记录与不存在一样返回 404（不暴露是否存在）。
    进行中考试的各题记录在交卷前不给看（评分全文里有参考思路，spec §8 答案保护）。"""
    att = db.get_attempt(attempt_id)
    if att is None or att["student_id"] != sid:
        raise HTTPException(status_code=404, detail="练习记录不存在")
    if request is not None and att.get("exam_id") and request.app.state.exam.running_for(att["exam_id"]):
        raise HTTPException(status_code=409, detail="考试进行中，交卷后在成绩单查看")
    return att


@app.get("/api/practice/result/{attempt_id}")
async def api_practice_result(attempt_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    att = await asyncio.to_thread(_own_attempt, attempt_id, sid, request)
    return grading.result_view(att)


@app.post("/api/practice/rescore/{attempt_id}")
async def api_practice_rescore(attempt_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    att = await asyncio.to_thread(_own_attempt, attempt_id, sid, request)
    pm = _practice(request)
    if pm.active is not None and pm.active.attempt_id == attempt_id:
        raise HTTPException(status_code=409, detail="设备还在复位，复位完成后才会评分" if pm.resetting
                            else "练习尚未结束，结束练习（设备复位）后才会评分")
    if att.get("ended_at") is None:
        raise HTTPException(status_code=409, detail="练习尚未结束")
    scorer: Scorer = request.app.state.scorer
    if scorer.busy(attempt_id) or att.get("score_status") == "scoring":
        raise HTTPException(status_code=409, detail="正在评分，请稍候")
    # 场景以内存中的加载结果为准；文件删掉了就用库里的镜像
    scenario = next((s for k, s in pm.scenarios.items() if pm.scenario_ids.get(k) == att["scenario_id"]), None)
    if scenario is not None:
        scenario = grading.expand_for_attempt(scenario, att)      # 参数化场景按该次的种子展开
    opts = await asyncio.to_thread(request.app.state.exam.score_opts_for_attempt, att)   # 考试题沿用模板权重
    await scorer.schedule(attempt_id, scenario, opts)
    return grading.result_view(await asyncio.to_thread(db.get_attempt, attempt_id))


# ---- 考试（里程碑 6） ----

class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    config: dict


class ExamStartBody(BaseModel):
    template_id: int


def _exam(request: Request) -> ExamManager:
    return request.app.state.exam


async def _ecall(coro):
    try:
        return await coro
    except ExamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except PracticeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def _norm_template(body: TemplateBody, request: Request) -> tuple[str, str]:
    import json as _json
    pm = _practice(request)
    try:
        cfg = normalize_config(body.config, set(pm.scenarios))
    except ExamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    if len(template_platforms(cfg, pm.scenarios)) > 1:
        raise HTTPException(status_code=422, detail="一份考试模板只能包含同一设备平台的题目，请拆分为 VRP5 或 YunShan 模板")
    return body.name.strip(), _json.dumps(cfg, ensure_ascii=False)


@app.get("/api/exam/templates")
async def api_exam_templates(request: Request) -> dict:
    # 考试模板管理：目前没有管理员角色，任何登录用户都能查看 / 编辑（以后在这里加权限，见 README）
    require_student_id(request)
    pm = _practice(request)
    rows = await asyncio.to_thread(db.list_templates)
    titles = {k: s["title"] for k, s in pm.scenarios.items()}
    platform = _mgr(request).env.platform
    out = []
    for r in rows:      # 每个模板标出用到的平台、与当前模拟环境是否匹配（工作台只列匹配的，模板管理页全列）
        v = template_view(r)
        plats = template_platforms(v["config"], pm.scenarios)
        bad = platform_mismatch(v["config"], pm.scenarios, platform)
        out.append({**v, "platforms": plats, "platform_ok": not bad,
                    "mismatch": [{"index": i, "title": t, "platform": p} for i, t, p in bad]})
    return {"templates": out, "scenarios": titles, "platform": platform,
            "platforms": {k: s.get("platform", "vrp5") for k, s in pm.scenarios.items()}}


@app.get("/api/exam/templates/{template_id}")
async def api_exam_template(template_id: int, request: Request) -> dict:
    require_student_id(request)
    row = await asyncio.to_thread(db.get_template, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="考试模板不存在")
    return template_view(row)


@app.post("/api/exam/templates", status_code=201)
async def api_exam_template_create(body: TemplateBody, request: Request) -> dict:
    require_student_id(request)
    name, cfg = _norm_template(body, request)
    tid = await asyncio.to_thread(db.create_template, name, cfg)
    return template_view(await asyncio.to_thread(db.get_template, tid))


@app.put("/api/exam/templates/{template_id}")
async def api_exam_template_update(template_id: int, body: TemplateBody, request: Request) -> dict:
    require_student_id(request)
    name, cfg = _norm_template(body, request)
    if not await asyncio.to_thread(db.update_template, template_id, name, cfg):
        raise HTTPException(status_code=404, detail="考试模板不存在")
    return template_view(await asyncio.to_thread(db.get_template, template_id))


@app.delete("/api/exam/templates/{template_id}")
async def api_exam_template_delete(template_id: int, request: Request) -> dict:
    require_student_id(request)
    em = _exam(request)
    if em.exam is not None and em.exam["template_id"] == template_id:
        raise HTTPException(status_code=409, detail="该模板的考试正在进行，交卷后再删除")
    if not await asyncio.to_thread(db.delete_template, template_id):
        raise HTTPException(status_code=404, detail="考试模板不存在")
    return {"ok": True}


@app.post("/api/exam/start")
async def api_exam_start(body: ExamStartBody, request: Request) -> dict:
    sid = require_student_id(request)
    if _mgr(request).switching:
        raise HTTPException(status_code=409, detail="模拟环境切换中，请稍后再开考")
    row = await asyncio.to_thread(db.get_template, body.template_id)
    if row is not None:
        try:
            qs = json.loads(row["config_json"] or "{}").get("questions") or []
        except ValueError:
            qs = []
        _capacity_guard(request, [q.get("scenario") for q in qs if isinstance(q, dict)])
    return await _ecall(_exam(request).start(sid, body.template_id))


class SingleExamBody(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=200)


@app.post("/api/exam/start_single")
async def api_exam_start_single(body: SingleExamBody, request: Request) -> dict:
    """单题考试（课程页 / 操作页的「单题考试」）：本题的考试，默认规则（15 分钟、无提示、拓扑可见、交卷后出成绩单）。
    有练习 / 考试进行中 409（练习中要先结束练习，前端确认后先结束再开考）；题目没能开始时整场回滚并 409。"""
    sid = require_student_id(request)
    if _mgr(request).switching:
        raise HTTPException(status_code=409, detail="模拟环境切换中，请稍后再开考")
    _capacity_guard(request, [body.scenario_id])
    return await _ecall(_exam(request).start_single(sid, body.scenario_id))


@app.get("/api/exam/current")
async def api_exam_current(request: Request) -> dict:
    sid = require_student_id(request)
    return _exam(request).state(sid)


@app.post("/api/exam/submit")
async def api_exam_submit(request: Request) -> dict:
    sid = require_student_id(request)
    return await _ecall(_exam(request).submit(sid))


@app.post("/api/exam/next")
async def api_exam_next(request: Request) -> dict:
    sid = require_student_id(request)
    return await _ecall(_exam(request).next(sid))


@app.post("/api/exam/retry_start")
async def api_exam_retry_start(request: Request) -> dict:
    sid = require_student_id(request)
    return await _ecall(_exam(request).retry_start(sid))


@app.post("/api/exam/finish")
async def api_exam_finish(request: Request) -> dict:
    sid = require_student_id(request)
    return await _ecall(_exam(request).finish(sid))


@app.post("/api/exam/hint")
async def api_exam_hint(body: HintBody, request: Request) -> dict:
    sid = require_student_id(request)
    return await _ecall(_exam(request).hint(sid, body.level))


@app.get("/api/exams")
async def api_exams(request: Request) -> list[dict]:
    sid = require_student_id(request)
    return await asyncio.to_thread(db.list_exams, sid)


@app.get("/api/exam/{exam_id}/report")
async def api_exam_report(exam_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    try:
        return await asyncio.to_thread(_exam(request).report, exam_id, sid)
    except ExamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.get("/api/exam/{exam_id}/replay")
async def api_exam_replay(exam_id: int, request: Request) -> dict:
    sid = require_student_id(request)
    try:
        return await asyncio.to_thread(_exam(request).replay, exam_id, sid)
    except ExamError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


# ---- Agent 面板（spec §16） ----

class AgentChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=agent_lib.MAX_MESSAGE_CHARS)
    history: list[dict] = Field(default_factory=list, max_length=100)


# ---- AI 接口设置（里程碑 6.18，学员菜单「AI 设置」）----

class AiSettingsBody(BaseModel):
    """省略 = 不改；base_url / model 为 null 或空串 = 回到环境变量 / 缺省；api_key 非空 = 写入 secrets 文件；clear_key = 删掉。"""
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, max_length=512)
    clear_key: bool = False


class AiTestBody(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, max_length=512)


@app.get("/api/ai/settings")
async def api_ai_settings(request: Request) -> dict:
    """AI 接口设置（评分、提示、Agent 面板共用）：{base_url{value, source, default, env_var}, model{…},
    key{configured, source, source_text, masked, file, env_var, page}, force}。Key 只给有无与掩码。"""
    require_student_id(request)
    return await asyncio.to_thread(ai.settings_view)


@app.post("/api/ai/settings")
async def api_ai_settings_set(body: AiSettingsBody, request: Request) -> dict:
    """保存 AI 接口设置，立即生效（下一次 AI 请求用新值；已发出的请求不受影响）。练习 / 考试进行中也允许。格式不对 422。"""
    require_student_id(request)
    given = body.model_fields_set
    kw: dict = {}
    try:
        if "base_url" in given:
            kw["base_url"] = ai.normalize_base_url(body.base_url) if (body.base_url or "").strip() else None
        if "model" in given:
            kw["model"] = ai.normalize_model(body.model) if (body.model or "").strip() else None
        if (body.api_key or "").strip():
            kw["api_key"] = ai.normalize_key(body.api_key)
        elif body.clear_key:
            kw["clear_key"] = True
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not kw:
        raise HTTPException(status_code=422, detail="没有要修改的项")
    changed = await asyncio.to_thread(lambda: ai.apply_settings(**kw))
    return {"changed": changed, **await asyncio.to_thread(ai.settings_view)}


@app.post("/api/ai/settings/test")
async def api_ai_settings_test(body: AiTestBody, request: Request) -> dict:
    """测试连接（不保存）：用给出的 URL / Key / 模型（没给的用当前生效值）发一次最小请求（模型列表，或 1 token 补全）。"""
    require_student_id(request)
    try:
        url = ai.normalize_base_url(body.base_url) if (body.base_url or "").strip() else ai.effective_base_url()[0]
        model = ai.normalize_model(body.model) if (body.model or "").strip() else ai.effective_model()[0]
        key = ai.normalize_key(body.api_key) if (body.api_key or "").strip() else (await asyncio.to_thread(ai.key_info))[0]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not key:
        raise HTTPException(status_code=422, detail="没有可用的 API Key：请先填写 Key")
    res = await ai.test_connection(url, key, model)
    return {**res, "base_url": url, "model": model, "key_masked": ai.mask_tail(key)}


@app.post("/api/agent/chat")
async def api_agent_chat(body: AgentChatBody, request: Request) -> StreamingResponse:
    """流式回答（text/event-stream，事件格式见 agent.py）。场景由服务端判定；考试进行中 403。"""
    sid = require_student_id(request)
    svc: agent_lib.AgentService = request.app.state.agent
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="问题不能为空")
    try:
        svc.precheck(sid)            # 必须在开始流式输出之前（之后改不了状态码）
    except agent_lib.AgentForbidden as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return StreamingResponse(svc.chat(sid, body.message, body.history), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/agent/history")
async def api_agent_history(request: Request) -> dict:
    sid = require_student_id(request)
    svc: agent_lib.AgentService = request.app.state.agent
    if svc.exam_active():
        raise HTTPException(status_code=403, detail="考试进行中，不能使用 Agent")
    return await svc.history(sid)


# ---- 终端 ----

@app.websocket("/ws/console/{port}")
async def ws_console(websocket: WebSocket, port: int) -> None:
    await websocket.accept()

    if current_student_id(websocket.cookies) is None:
        await websocket.send_text("\r\n[拒绝] 未登录，请先登录\r\n")
        await websocket.close(code=1008)
        return

    if LABMGR is not None and LABMGR.switching:
        await websocket.send_text("\r\n[拒绝] 模拟环境切换中，请稍后重连\r\n")
        await websocket.close(code=1013)
        return
    provider = lab()
    cur = await provider.current()
    topo_allowed = provider.allowed(cur)
    practice: PracticeManager = websocket.app.state.practice
    allowed = practice.allowed_filter(topo_allowed)
    if port not in allowed:
        if port in topo_allowed and practice.resetting:
            reason = "上一次练习正在后台复位设备，完成后才能打开其他设备的终端"
        elif port in topo_allowed:
            reason = ("考试" if practice.active is not None and practice.active.mode == "exam" else "练习") + \
                "进行中，本题不开放此设备的终端"
        else:
            reason = provider.deny_reason(cur, port)
        await websocket.send_text(f"\r\n[拒绝] 端口 {port} 不在白名单：{reason}\r\n")
        await websocket.close(code=1008)
        return

    hub: ConsoleHub = websocket.app.state.hub
    try:
        sub = await hub.subscribe(port)
    except HubConnectError as exc:  # 连接失败（设备未上电 / 端口不通等）
        await websocket.send_text(
            f"\r\n[连接失败] {provider.endpoint(port)}：设备 console 暂未就绪"
            f"（可能仍在启动，请稍后点「重连」）。详情：{exc}\r\n"
        )
        await websocket.close(code=1011)
        return

    await websocket.send_text(f"\r\n[已连接] {provider.endpoint(port)}\r\n")

    async def device_to_ws() -> None:
        while True:
            data = await sub.queue.get()
            if data is None:  # 设备连接断开 / 被移出白名单
                break
            await websocket.send_bytes(data)

    async def ws_to_device() -> None:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await hub.write(sub, msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                await hub.write(sub, msg["text"].encode("utf-8", "ignore"))

    d2w = asyncio.create_task(device_to_ws())
    w2d = asyncio.create_task(ws_to_device())
    try:
        # 任一方向结束即收尾
        done, pending = await asyncio.wait(
            {d2w, w2d}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        d2w.cancel()
        w2d.cancel()
        hub.unsubscribe(sub)  # 只退订，设备连接由 hub 保持（空闲超时后才关）
        try:
            await websocket.close()
        except Exception:
            pass
