# ainet — 局域网运维实操练习/考试系统

依托 eNSP 真实设备环境的运维实操练习 + 考试系统。完整需求见 [`spec.md`](spec.md)。开发交接（部署现状、代码导航、未完成项）见 [`docs/HANDOFF.md`](docs/HANDOFF.md)。

## 里程碑 1（已实现）：Web 终端打通 console

浏览器 xterm.js 终端 ←WebSocket→ FastAPI 后端 → `DeviceConnector` → eNSP 设备 console（裸 telnet）。

### 目录

- `backend/` Python + FastAPI 后端
  - `config.py` 集中配置（eNSP 主机、拓扑目录、数据库）
  - `connectors/base.py` `DeviceConnector` 抽象接口（spec §5，为 EVE-NG 预留）
  - `connectors/ensp.py` `EnspConnector`：asyncio 连 console，处理 telnet IAC 协商
  - `main.py` FastAPI 应用（登录/档案 API、终端页 + WebSocket 桥）
  - `db.py` SQLite 数据层（里程碑 2）
  - `topology.py` eNSP `.topo` 解析（设备 / 连线 / 背景图形 / 注释）、拓扑发现、按运行中的设备确定当前拓扑
  - `ensp_runtime.py` eNSP 运行时探测：哪些设备（GUID）正在运行（为 EVE-NG 预留同名接口）
  - `console_hub.py` ConsoleHub：每个 console 端口只保持一条设备连接，多个终端标签共享；隐藏事务（里程碑 3）
  - `scenarios.py` 场景库加载、校验、就绪判断（里程碑 3）
  - `practice.py` 练习流程：注入、自动判定、提示、结束还原（里程碑 3）；开练前快照、结束时整机复位（里程碑 4）；复位后触发异步评分（里程碑 5）
  - `config_reset.py` 配置级快速复位：整机配置快照、按 VRP 配置层级比对差异、生成撤销命令（里程碑 4）
  - `ai.py` DeepSeek 客户端：Key 读取与脱敏、重试、错误分类、稳健 JSON 解析（里程碑 5）
  - `prompts.py` AI 提示词模板（评分、三级提示）（里程碑 5）
  - `grading.py` 评分：结果分确定性计算 + AI 过程分 / 规范分、降级、后台评分任务（里程碑 5）
  - `hints.py` AI 分级提示：泄露过滤、回退静态提示（里程碑 5）
  - `params.py` 参数化场景：参数声明、按种子确定性展开、抽样校验（里程碑 6）
  - `exam.py` 考试模式：考试模板校验、考试状态机、服务端计时与恢复、成绩汇总、成绩单与命令回放（里程碑 6）
  - `lab.py` 实验后端抽象 `LabProvider`（当前拓扑、设备状态、终端白名单、设备连接、启停）与统一设备标识 `console`；`EnspProvider`（eNSP 经典版）
  - `enspro.py` eNSP Pro 后端：REST 客户端（离线登录、单会话冲突）、沙箱转拓扑、`EnsproProvider`（启停、console 登录失败记录）
  - `connectors/enspro.py` `EnsproConnector`：eNSP Pro 设备 console（STOMP over WebSocket、心跳、登录状态机）
  - `tests/` pytest 测试（`tests/manual/ai_smoke.py` 为真实 DeepSeek 冒烟脚本，`tests/e2e/` 为真机 e2e，都不进默认 pytest）
- `scenarios/` 场景库（每个场景一个 JSON 文件，随仓库版本管理）
  - `requirements.txt` 依赖
- `frontend/index.html` + `app.js` + `app.css` 工作台：拓扑图 + 多设备终端（xterm.js 已本地化到 `frontend/vendor/`，不依赖 CDN）
- `frontend/login.html` / `frontend/profile.html` 登录页 / 档案页（含考试记录）
- `frontend/exam.js` 工作台「考试」页签；`frontend/exam_report.html` 成绩单 + 命令回放（`/exam/report?id=`）；
  `frontend/exam_admin.html` 考试模板管理（`/exam/templates`）（里程碑 6）

### 装依赖

使用 Python 3.11 或更高版本；CI 覆盖 Python 3.11 / 3.13。

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate
pip install -r requirements.txt
```

### 起服务

```bash
cd backend
python -m launcher
```

Windows 可双击 `启动服务.bat`，后台运行可用 `run_ainet.bat` / `start_silent.bat`。
默认只监听 `127.0.0.1:8000`，不主动开启免登录，也不连接某台个人实验环境。
复制 `backend/local.env.example` 为 `backend/data/local.env` 后填写本机配置；该文件不进 Git，进程环境变量优先。
课程页保存的实验接入设置仍按原有优先级生效。

需要局域网访问时显式配置 `AINET_BIND_HOST`。局域网免登录还需要 `AINET_ALLOW_LAN_AUTO_LOGIN=1`，
此时能访问端口的人都能操作设备。默认账号只用于单人本机实验；共享网络使用前应关闭免登录并设置访问限制。
启动脚本与命令行统一经 `launcher` 检查配置。直接调用 uvicorn 不经过这些启动检查。

### 打开终端

浏览器访问 <http://localhost:8000/>（`?port=2001` 可直接打开对应设备）。未登录会先跳到登录页。
在拓扑图上点设备即打开它的终端，用法见下文「拓扑图 + 多终端」。

### 配置

- `ENSP_HOST` 环境变量：eNSP 设备 console 所在主机，缺省 `127.0.0.1`（只是缺省值：课程页「⚙ 接入设置」改过以后以库里的记录为准，见「接入设置」）。
- 端口白名单：eNSP 当前拓扑（state=running）中所有非 0 的 `com_port`；没有运行的设备、拓扑未保存或设备分属多个拓扑时不放行任何端口，拒绝时说明原因。
- `AINET_TOPO_DIRS` 环境变量：`.topo` 搜索目录，多个用系统路径分隔符（Windows `;`、Linux `:`）。
  Windows 缺省为 `C:\Program Files\Huawei\eNSP\examples` 加 `%USERPROFILE%`（递归，跳过隐藏目录与 AppData，最深 6 层）；其他系统缺省为空。
- `AINET_ENSP_DEVICES_DIRS` 环境变量：eNSP 设备运行目录（`...\eNSP\VBoxServer\devices`），多个用系统路径分隔符。
  缺省依次取当前进程 `%LOCALAPPDATA%\eNSP\VBoxServer\devices` 与 `C:\Users\*\AppData\Local\eNSP\VBoxServer\devices`。
- `AINET_ENSP_RUNNING_IDS` 环境变量：**仅开发测试用**。逗号分隔的设备 GUID，直接当作「正在运行的 VBox 设备」，不做真实探测；
  设为空值表示没有运行的设备。生产环境不要设置。
- `AINET_ENSP_SWITCH_NAMES` 环境变量：**仅开发测试用**。逗号分隔的设备名，直接当作「正在运行的 S 系列交换机」（替代进程检测）。
- `AINET_ENSP_CLIENT` 环境变量：**仅开发测试用**。`1` / `0` 直接指定 eNSP 主程序是否在运行（替代进程检测）。
- `AINET_ENSP_TOPO_HIS` 环境变量：**仅开发测试用**。指定 eNSP 最近打开记录 `topo.his` 的路径（空值表示不读）；缺省取 eNSP 根目录（devices 目录往上两级）下的 `tmp\topo.his`。
- `AINET_DB_PATH` 环境变量：SQLite 数据库文件，缺省 `backend/data/ainet.db`（目录自动创建，已加入 `.gitignore`）。

## 里程碑 2（已实现）：SQLite 数据模型 + 明文登录 + 学生档案

- `backend/db.py` 标准库 `sqlite3` 数据层：启动时 `init_db()` 幂等建齐 spec §13 全部表；学生 CRUD；档案聚合。
- `frontend/login.html` 登录页（已有学生下拉快速切换 + 新建学生）。
- `frontend/profile.html` 档案页：身份 / 进度 / 掌握度 / 最近练习考试 / 错题本（暂无数据时显示「暂无」）。
- 终端页顶部状态栏：当前学生、档案、切换、登出。

### 默认账号

库里没有任何学生时，启动会自动创建 **`admin` / `admin`**（姓名「管理员」），装好即可登录。
之后可在登录页「新建学生」添加更多学生。密码为明文存储，仅用于区分人员（spec §12）。

### 登录与切换学生

- 同一时刻单人使用。登录后服务端在内存里记录会话（token 存浏览器 cookie），**服务重启后需重新登录**。
- 切换学生：终端页点「切换」回到登录页，下拉选学生、输入密码登录即可；新登录会作废本浏览器的旧会话。
- 「登出」清除会话。未登录时终端 WebSocket 会被拒绝（提示后关闭）。

### 自动登录（单人部署，里程碑 6.17）

设环境变量 `AINET_AUTO_LOGIN=<登录名>`（例如 `admin`）后跳过登录页：没有有效会话的页面、接口与终端 WebSocket 请求一律当作该学员，
响应里补发会话 cookie（与 `POST /api/login` 同一套内存会话）；`/login` 直接 303 到课程页；`GET /api/me` 多 `auto_login: true`，
学员菜单里「切换学员」「登出」隐藏（「我的档案」保留；档案 / 成绩单 / 模板页顶栏的「切换」「登出」同样隐藏）。
该登录名的学员不存在时启动自动创建（显示名同登录名，密码随机生成且不输出——只是占位，自动登录不用它）；已存在的
（如默认的 admin）不改密码。启动日志只写一行「自动登录已开启：<登录名>」。缺省不设 = 关闭，行为与原来完全一样。

**安全提示**：自动登录开启后，能访问 ainet 端口的任何人都以该账号身份操作（看档案、开练 / 考试、改接入设置、启停设备），没有任何口令。只适合单人使用的本机 / 局域网部署；不要在共享网络上开，也不要把端口转发到外网。它只能用环境变量开启，没有经接口或页面开启它的方式。

### API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{login, password}`，成功写会话 cookie |
| POST | `/api/logout` | 清会话 |
| GET | `/api/me` | 当前学生档案（未登录 401）；`auto_login`：是否自动登录模式 |
| GET | `/api/students` | 学生列表（不含密码，供切换） |
| POST | `/api/students` | 新建学生 `{login, name, password}`，login 重复返回 409 |

### 测试

```bash
cd backend
.venv/bin/python -m pytest tests -q     # Windows: .venv\Scripts\python -m pytest tests -q
```

测试使用临时数据库，不影响 `backend/data/`。

## 拓扑图 + 多终端（已实现）

### 当前拓扑：按 eNSP 中运行的设备确定

- 后端递归扫描 `AINET_TOPO_DIRS` 下的 eNSP `.topo` 文件（文件清单缓存 15 秒，文件内容按修改时间缓存）。
  开发时可用仓库自带样例：`AINET_TOPO_DIRS=tests/fixtures/topos`（在 `backend/` 下启动）。
- 每台设备的 `<dev id>` 是 GUID，不同拓扑文件之间不重复（直接复制的副本除外）。eNSP 启动设备时会在
  `<LOCALAPPDATA>\eNSP\VBoxServer\devices\<GUID>\` 下建虚机副本，`ensp_runtime.running_device_ids()` 据此判断哪些设备在运行：
  1. `VBoxManage list runningvms`（`C:\Program Files\VBoxManage.exe` 或 Oracle VirtualBox 安装目录），从 VM 名里取 `_Clone_<GUID>`，超时 5 秒。只有服务与 eNSP 属于同一 Windows 用户时才有结果。
  2. 上一步没有结果时：检查每个 `<GUID>\*\Logs\VBox.log` 是否被虚机进程占用（以只读、只允许他人读的方式打开，遇到共享冲突即判为运行中，打开成功立即关闭）。
  设备停止后目录不会删除（关闭拓扑时才删），所以只看目录是否存在不准确；目录不存在按未运行处理。结果缓存约 2 秒。非 Windows 平台返回空集合。
- S 系列交换机（S3700 / S5700）不走 VirtualBox，每台对应一个 `eNSP_Switch.exe` 进程，命令行最后一个参数是设备名。
  用 PowerShell `Get-CimInstance Win32_Process` 读命令行（超时 5 秒，缓存约 2 秒，跨用户可读），按设备名（同一拓扑内唯一）匹配。
  同一次查询还看 eNSP 主程序 `eNSP_Client.exe` 是否在运行：确知它不在运行时（eNSP 已退出），不看残留证据，直接显示「eNSP 未运行」。
- eNSP 最近打开的拓扑：`<eNSP 根目录>\tmp\topo.his`（GBK，`|` 分隔的 .topo 绝对路径，第一个是最近打开的）。
  其中存在的文件都会加入扫描和 GUID 索引（即使不在 `AINET_TOPO_DIRS` 内，例如用户存在 D 盘桌面的拓扑）。
- 判定：运行证据 = 运行中的 VBox 设备 GUID ∪ 运行中的交换机设备名。先核对 topo.his 第一个文件（运行中的 GUID 都属于它、交换机名也都是它的 S 系列设备名）；
  不通过时退回全部文件搜索（交换机名作辅助，his 中靠前、修改时间新的优先）。没有运行证据时仍返回 topo.his 第一个文件，前端画成灰色并提示「eNSP 最近打开：X」。
- 当前拓扑（`GET /api/topology/current` 的 `state`）：
  - `running`：有拓扑文件包含全部运行中设备；多个文件都满足（副本）时按上述优先级选一个，其余列在 `duplicates`。
  - `none`：eNSP 中没有运行的设备（若有 topo.his，`topology` 为最近打开的拓扑，设备全部 stopped / na）。
  - `unknown`：运行中的设备在任何已扫描的 `.topo` 里都找不到（通常是 eNSP 里新建后未保存）。
  - `mixed`：运行中的设备分属不同文件，没有单个文件能包含全部。
- 设备状态：有 console 的设备，运行中（VBox 设备看 GUID，S 系列交换机看进程设备名）为 `online`，未运行为 `stopped`；**不探测 console 端口**（频繁连接/断开 eNSP 的 console 服务可能使其异常，2026-10-04 曾崩溃），刚启动、console 尚未就绪的设备也显示 `online`，打开终端时连不上会提示稍后重连；
  无 console 的设备（PC / STA / Cloud / FRSW 等由 eNSP 主程序模拟，检测不到）为 `na`。
- 手动检查（Windows，eNSP 所在机器）：`cd backend && .venv\Scripts\python tests\manual\check_runtime.py [devices 目录]`，
  打印找到的目录、每个 GUID 的 `VBox.log` 打开结果、交换机进程设备名、topo.his 内容、运行中的 GUID 与所用方法、当前拓扑判定。
- API（均需登录）：`GET /api/topologies`（拓扑文件列表，供管理端）、`GET /api/topology/current`（当前拓扑 + 设备状态 + state / message / method）、
  `GET /api/topology/status`（同上但不含拓扑本体，供前端轮询）。

### 工作台用法

- 环境状态在顶栏题名气泡里（2026-10-07 起，见文末「课程选择页、操作页单层顶栏」）：`eNSP · VRP5`、`拓扑 <名称> · 运行 N/M 台`（悬停看检测详情，没有运行的设备 / 拓扑未保存 / 设备分属多个拓扑时显示原因）。约每 5 秒刷新一次，↻ 立即刷新；拓扑文件变化时重绘。布局与 Agent 面板见文末「工作台布局与 Agent 面板」。
- 左侧拓扑图与 eNSP 一致：白底、每 100 坐标单位一条浅灰虚线网格，按文件画出背景图形（直线 / 矩形 / 椭圆，实心或边框）和注释文字（颜色、底色、多行）。
  图层：网格 → 图形 → 连线 → 设备 → 注释。设备状态：绿色 online 可点；灰色虚线 stopped 不可点；蓝灰色 na（Cloud / PC，不可点）。
  连线两端标注接口名（勾掉「接口」隐藏）；滚轮缩放、拖拽平移、双击或「适配」复位；‹ 收起成 48px 图标栏。
- 标签状态点同时看 WebSocket 与设备状态：绿色仅表示已连接且设备在线；设备停止为灰色，断开为红色。拓扑变化后，设备已不在当前拓扑中的标签划线标为不可用。
- 点设备打开它的终端标签（同一设备只开一个，再点只聚焦）；标签上 ↻ 重连、× 关闭（断开该 WS）。也可用「打开终端…」下拉打开。
- 「单屏 / 左右分屏」切换；分屏时每屏顶部下拉选择显示哪个标签，点标签会显示到当前聚焦的那一屏。切换标签不丢输出。

### 接口名映射

`.topo` 连线只记录接口扁平序号（`interfacePair` 的 `srcIndex` / `tarIndex`）。规则（`topology._interfaces`）：

1. **已实测的型号，主板按真机接口顺序**（`display interface brief` 的顺序）解释序号，不看文件里的 slot 描述：
   AR1220 = `GigabitEthernet0/0/0-1` + `Ethernet0/0/0-7`；S5700 = `GigabitEthernet0/0/1-24`。只在文件中主板接口总数与真机一致时套用。
2. 其他型号、扩展卡按 `<slot><interface>` 顺序展开：`GE`→`GigabitEthernet0/0/n`、`Ethernet`→`Ethernet0/0/n`、`Serial`→`Serial0/0/n`；
   S 系列交换机 / PC / Cloud 从 1 编号；AR 带扩展串口卡时 Serial 取卡所在槽号（如 `Serial1/0/0`）。这部分是推断。

依据（2026-10-04 在 Campus_Network 上实测）：旧格式的 `Campus_Network.topo` 把 AR1220 主板写成 Ethernet×2 + GE×4 + Serial×4（也是 10 个），
按文件顺序会把 AR0–AR2 标成 `Ethernet0/0/1 <-> Ethernet0/0/0`。真机上：AR0–AR2 是 `GE0/0/1 <-> GE0/0/0`、AR0–LSW0 是 `GE0/0/0 <-> GE0/0/1`
（两条都在两端配同网段地址后 ping 通）；AR0 的 GE0/0/0、GE0/0/1 物理 up、Ethernet 全 down，AR2 只有 GE0/0/0 up，LSW0 只有 GE0/0/1 up，
都与「序号按真机接口顺序」一致。新格式文件（如 2-1 OSPF）的主板本来就是 GE×2 + Ethernet×8，结果不变（R1 idx0 仍是 GE0/0/0）。
现在 Campus 拓扑图上的接口标注与真机一致（`tests/e2e/reset_e2e.py` 的 T1 步骤在页面上核对）。

### 浏览器 e2e

`backend/tests/e2e/browser_e2e.py`（不进默认 pytest）：先起服务（`AINET_TOPO_DIRS=tests/fixtures/topos ENSP_HOST=... AINET_DB_PATH=<临时库>`），
Linux 上加 `AINET_ENSP_RUNNING_IDS=<R1 GUID>,<R2 GUID>` 模拟 eNSP 中运行着 2-1 OSPF 的 R1、R2，
再 `python tests/e2e/browser_e2e.py http://127.0.0.1:8000 shot.png`，用无头 Chrome（CDP）验证状态文字、设备状态、拓扑绘制、多标签与分屏；
以 `AINET_ENSP_RUNNING_IDS=`（空值）起服务后加第三个参数 `none` 验证「没有运行的设备」。布局回归见 `tests/e2e/layout_e2e.py`。

## 里程碑 3（已实现）：场景库 + 练习闭环（注入 → 排障 → 自动判定 → 结束还原）

### ConsoleHub：一个 console 端口只保持一条设备连接

实测同一 console 端口的多个 TCP 连接共享输入输出（像共用一根串口），而频繁连接 / 断开 console 既可能让刚断开的会话重连后几分钟收不到输出，
也疑似诱发过 eNSP_Client 崩溃。所以后端（`console_hub.py`）对每个端口懒加载一个 `EnspConnector` 并长期持有：

- 浏览器终端标签是订阅者：设备输出扇出给所有订阅者，订阅者的输入写回设备；新标签先收到最近输出的回放（当前提示符等）。
  只在新建设备连接时发一次回车唤醒，已有连接上新增标签不发送任何字节（以免提交别的标签敲了一半的命令）。
- 关标签只是退订，不断开设备连接；所有订阅者退出并空闲 `AINET_HUB_IDLE_CLOSE` 秒（默认 600）后才关闭。连接断了按需重连，
  两次连接同一端口至少间隔 `AINET_HUB_MIN_RECONNECT` 秒（默认 3）。
- **隐藏事务**（注入 / 判定 / 还原用）：拿到端口锁 → 前提检查（学员当前输入行为空，hub 跟踪自上次回车以来的可见输入并处理退格，
  用过方向键或 Tab 视为非空；设备最后一行是提示符，而不是 `[Y/N]`、`Password:`、`---- More ----`；配置事务还要求在用户视图 `<...>`），
  不满足就返回「暂不可检查」由调用方稍后重试 → 暂停向学员转发设备输出 → 逐条发送命令并读到提示符（遇分页发空格并去掉分页符，
  每条命令有超时，超时发 Ctrl-C 收尾）→ 恢复转发。事务期间学员敲的字符**暂存**，事务结束后按原顺序写回设备（像串口的键盘预输入；
  不丢弃，避免学员以为键盘失灵）。捕获期间的异步日志行（`Oct  4 2026 ... %%01...`）在事务结束后照常转发并重印提示符；
  其余输出（回显、命令输出、提示符）既不发给学员，也不进新标签的回放。配置事务遇到 `Error:` 中止并回到用户视图（还原时改为尽力而为、继续执行）。
- 学员每次回车提交的命令（取设备回显的那一行，Tab 补全 / 历史命令也能拿到实际文本）连同设备交给练习记录（`attempt_logs`）。
- 练习开始时，场景设备的回放缓冲清到只剩当前提示符，已打开的场景终端也清屏，下一位学员 / 下一次练习看不到上一次的命令和答案；
  进行中练习的设备端口不做空闲关闭（学员关掉所有标签思考很久，结束还原时也不必冷启动重连）。

### 场景文件格式（`scenarios/*.json`）

启动时加载并校验，通过的同步进 db 的 `scenarios` 表（按 `id` upsert；attempts 外键引用它，档案 / 错题本能显示标题，文件删了历史记录仍在）。
运行时以内存中的加载结果为准，JSON 文件是单一事实源。目录可用 `AINET_SCENARIOS_DIR` 覆盖。

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识（字母、数字、`_ . -`） |
| `title` / `level` / `knowledge_tags[]` | 标题 / 难度（1 入门 … 5 专家）/ 知识点 |
| `topology` | 拓扑文件名（如 `Campus_Network.topo`），按文件名去扩展名、忽略大小写与当前运行拓扑比对 |
| `required_devices[]` | 本题需要运行的设备 |
| `accessible_devices[]` | 向学员开放终端的设备（⊆ required）；练习期间其他设备在拓扑上不能打开终端，后端白名单同步限制 |
| `symptom` | 工单式描述，给学员看 |
| `baseline{设备:[命令]}` | 把设备配成干净的初始状态（可为空）；每台设备一个配置事务 |
| `inject{设备:[命令]}` | 注入故障；与 baseline 分开执行（各自从用户视图开始，事务结束自动 `return`） |
| `restore{设备:[命令]}` | 撤销 inject 和 baseline；尽力而为，单条出错继续。结束时先执行它，再做整机复位（里程碑 4），所以它不再是唯一的还原手段 |
| `checks[]` | `{device, command, expect: 正则, negate?, desc}`，全部满足即通关；只允许 display / ping / tracert |
| `hints[3]` | 三级静态提示：方向 → 命令 → 揭示（以后接 AI） |
| `reference_path` | 参考排障思路 |
| `solution{设备:[命令]}` / `note` | 可选：参考修复命令 / 出题备注（不给学员看） |
| `draft` | 可选，`true` = 未在真机验证的草稿题：`GET /api/scenarios` 缺省不列出（课程页 / 操作页都看不到，进行中的那道照列），`AINET_SHOW_DRAFTS=1` 时列出并标「草稿 · 未验证」；按 id 开始练习不受限制 |

加载校验（spec §6.1 的规则子集）：必填字段与类型、hints 恰好 3 条、正则能编译；各字段引用的设备都在 `required_devices` 中，
拓扑文件找得到时还要求设备存在且有 console（不校验接口名）；checks 只允许只读命令；
baseline / inject / restore 禁止 `user-interface`、`reboot`、`reset saved-configuration`、`save`、`undo info-center`、`sysname`、`quit` 等
切断管理通道或破坏性的命令（完整列表见 `scenarios.py` 的 `FORBIDDEN`）。校验失败的文件不加载，错误显示在练习面板底部。

自带场景（Campus 题共用 Campus_Network 一个基础拓扑，题与题只靠配置区分，连线按真机：AR0 GE0/0/0–LSW0 GE0/0/1、AR0 GE0/0/1–AR2 GE0/0/0）：
- `campus-ar0-ar2-link`（难度 1，AR0 + AR2，**已真机验证**）：baseline 给 AR0 GE0/0/1、AR2 GE0/0/0 配 10.0.12.1/24、10.0.12.2/24，
  inject 把 AR2 一端改成 10.0.21.2/24；判定 AR0 `ping -c 2 10.0.12.2` 收到包；restore 两端 `undo ip address`。
- `campus-lsw0-access-vlan`（难度 2，AR0 + LSW0，**已真机验证**）：LSW0 管理地址 Vlanif10 = 10.0.10.254 扮演 VLAN 10 的主机，网关 AR0 GE0/0/0；
  inject 把 LSW0 GE0/0/1 的 access VLAN 改成 20；判定 AR0 `ping -c 2 10.0.10.254`。知识点：VLAN、Access 端口、二层排障。
- `campus-ar2-return-route`（难度 3，AR0 + AR2 + LSW0，**已真机验证**）：管理网 10.0.10.0/24 访问 AR2 LoopBack0 的服务器网段 192.168.2.0/24；
  inject 把 AR2 回程静态路由的下一跳改成不存在的 10.0.12.3（AR0 自己 ping 得通，LSW0 不通）；判定 LSW0 `ping -c 2 192.168.2.1`。
- `campus-ar2-acl-block`（难度 3，同上，**已真机验证**）：AR2 GE0/0/0 入方向 ACL 3000 把源写成了管理网（本意是禁访客网段 10.0.20.0/24）；
  判定 LSW0 能 ping 通 192.168.2.1，且 ACL 里仍有禁止 10.0.20.0/24 的规则、接口上仍有入方向过滤（直接删 ACL 不算修好）。
- `ospf-2-1-r1-shutdown`（2-1Single-Area OSPF，R1/R2/R3）：R1 GE0/0/0 被 shutdown，判定接口 up 且 `display ospf peer brief` 有 Full 邻居。
  依赖示例拓扑自带的 OSPF 配置，结构经单元测试覆盖，尚未真机验证。

### 练习流程

工作台拓扑面板顶部有「拓扑 / 练习」两个页签（同一面板内切换，分栏滑块与终端标签栏不受影响）：

1. 场景列表：标题、难度、知识点、就绪状态（spec §6.3：当前拓扑就是该场景的拓扑，且 required 设备都在运行）；不就绪时给出原因，
   如「请在 eNSP 打开 2-1Single-Area OSPF 并启动 R1、R2、R3」。
2. 开始练习：就绪检查 → 创建练习记录 → **给每台 required 设备拍整机配置快照**（`display current-configuration`，存 db 的 `attempt_configs`）
   → 对设备执行 baseline、inject（隐藏事务）→ **注入后先跑一遍 checks**，确认故障确实生效
   （已全部通过说明注入无效，报错并还原；暂不可检查也报错并还原，不把「没检查」当「已生效」）。同一时间只允许一个进行中的练习。
3. 练习中：显示工单、计时、可访问设备（一键打开终端）、「立即检查」「提示（第 N 级）」「结束练习」，以及最近一次检查通过几项（不显示判定命令）。
4. **自动判定**：学员每次回车提交命令后，3 秒内没有新输入就用隐藏事务跑一遍 checks；两次判定至少间隔 10 秒（开始时的故障确认也算一次）；
   设备暂不可检查时跳过、稍后重试；任一设备不可检查就整轮作废，不按部分结果判定。全部通过即通关（记录结束时间、passed=1），
   页面轮询 `/api/practice/current` 显示通关提示。间隔可用 `AINET_JUDGE_DEBOUNCE` / `AINET_JUDGE_MIN_INTERVAL` 调整。
5. 结束练习（通关后或主动放弃）：先执行场景 restore，再按开练前快照**整机复位**（学员额外改的配置也撤销，见下文「里程碑 4」），
   最后比对确认一致；返回是否通关、用时、提示数、命令数、每台设备的复位结果。还原没完成（例如学员停在系统视图、正在输入）时练习保持，
   提示回到用户视图后再点「结束练习」重试。复位后仍有撤不掉的配置时练习照常结束，但 `restored=false`、列出这些配置行并提示在 eNSP 中重载拓扑。
   档案页的最近记录、错题本能看到练习（场景标题、用时、命令数）。
6. 进行中的练习写入 `app_settings`，快照在 db 里，服务重启后可恢复，仍可「结束并还原」（含整机复位，真机验证过）。

API（均需登录）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/scenarios` | 场景列表 + 就绪判断（`ready` / `reason`），不含 checks / inject / 提示内容 |
| POST | `/api/practice/start` | `{scenario_id}`，返回工单、可访问设备、开始时间、故障确认结果 |
| POST | `/api/practice/check` | 立即检查（与自动判定串行，不受最小间隔限制） |
| POST | `/api/practice/hint` | `{level}`，按 1→2→3 依次给提示，记录已用提示数 |
| POST | `/api/practice/end` | 结束并还原 + 整机复位。`active=true`（还原没做完）时可重试；`active=false` 时看 `reset_ok` / `leftovers` / `note` |
| GET | `/api/practice/current` | 当前练习状态（页面刷新后恢复）；无练习时带上一次的结果 |

### 已知局限

- 学员绕开网页、在 eNSP 自带的命令行窗口里操作时，hub 跟踪不到输入行；但设备输出仍经共享 console 进入 hub，
  停在非提示符状态时判定会跳过。命令记录也只覆盖网页终端。
- 隐藏事务期间学员终端约 1~5 秒没有回显（ping 失败时更久），按键会在事务结束后补上。

### 真机 e2e

`backend/tests/e2e/practice_e2e.py`：eNSP 打开 Campus_Network 并启动 AR0、AR2 后，以
`AINET_TOPO_DIRS=tests/fixtures/topos AINET_ENSP_RUNNING_IDS=<AR0 GUID>,<AR2 GUID> AINET_ENSP_CLIENT=1 ENSP_HOST=... AINET_DB_PATH=<临时库> AINET_PRACTICE_SEED=0` 起服务
（里程碑 6 起 Campus 题参数化，练习缺省随机种子；这些真机脚本按原题的默认值写死了故障与修复命令，必须用 `AINET_PRACTICE_SEED=0`，`reset_e2e.py`、`ai_e2e.py` 同理），
再 `python tests/e2e/practice_e2e.py http://127.0.0.1:8770 <截图目录>`。学员命令全部经页面终端（xterm 输入框 + 键盘事件）发出，
覆盖：场景就绪 → 开始（学员终端看不到注入 / 判定命令）→ 不开放的设备不能开终端 → 经终端修复 → 自动判定通关 → 结束还原并用 display 核对
→ 通关路径全程（含通关判定与还原）终端无隐藏命令 → 档案 passed=1，以及「放弃」路径（重开标签无上一次答案残留；不修复直接结束，照样还原干净）。
2026-10-04 在 Campus_Network（AR0、AR2、LSW0 运行）上复跑：11 pass / 0 fail。

## 里程碑 4（已实现）：配置级快速复位（`config_reset.py`）

练习结束时，把每台涉及的设备整机配置还原到开练前的快照，学员额外改的配置也撤销（spec §10、§14 第 1 项）。

**为什么用「快照差异回滚」**：eNSP 的 VRP 没有整份替换 running-config 的命令。2026-10-04 实测：AR1220（V200R003C00）用户视图 / 系统视图下
都没有 `configuration replace`、`rollback configuration`、`commit`、`load`；S5700 只有 `clear configuration interface`（只能清单个接口）。
`reboot` / `reset saved-configuration` 会动保存的配置、也太慢，不考虑。

做法：

1. 开练前 `snapshot(port)`：隐藏 check 事务执行 `display current-configuration`（自动翻页），原文存 db。
2. 结束时 `restore_to(port, 快照)`：抓当前配置 → 按 VRP 配置的层级结构（列 0 的 `#` 分段、视图块、缩进子行；AR 的顶层行本身带 1 个空格，
   所以按「每段第一行的缩进」为基准）与快照比对 → 生成撤销 / 补回命令，在对应视图里执行 → 重新抓取、重新比对，直到一致（最多 4 轮）。
   仍不一致的配置行原样列出，提示在 eNSP 中重载拓扑（spec §10 兜底），不当作成功。
3. 顺序与依赖：先删两边都有的块里多出来的子行（倒序：先删接口上的 `traffic-filter`、`port default vlan`，先删 sub 地址再删主地址），
   再删多出来的顶层行 / 块（倒序；`undo ospf 1`、`undo acl number 3000`、`undo interface LoopBack0` 等整块删除），然后按快照顺序补回缺少的配置，
   sysname 最后单独改回。嵌套块（如 OSPF 的新 area）先删子行再删块头（实测 VRP 要求先删 area 下的 network）。
4. 每组命令从用户视图开始：`system-view` + 逐级进入视图，进入视图的命令报错就跳过整组，避免 undo 落到别的视图。`[Y/N]` 自动回答 y
   （`undo ospf`、`undo vlan batch` 会问；只回答新出现的确认，实测 S5700 回显慢时曾被答两次）。
5. undo 写法不对称时用规则表：`description` → `undo description`、`port link-type` / `port default vlan` / `stp mode` 去掉参数、
   `traffic-filter inbound acl 3000` → `undo traffic-filter inbound`、`rule 10 deny icmp` → `undo rule 10`（后两条是实测报 Too many parameters 后加的）、
   以 `undo ` 开头的配置行反过来去掉 undo。其余先试 `undo <整行>`，失败的下一轮再试去掉末尾 1~2 个参数（`ip address` 行不截断，免得删掉全部地址）。
6. `vlan batch` 按 VLAN 集合比较：只 undo 多出来的、补回缺少的，不会把快照里本来就有的 VLAN 删掉再建。
7. sysname 改名会改变提示符：hub 在事务里跟踪 `sysname X` / `undo sysname`，事务结束后把新提示符重印给学员终端，后续判定按新提示符执行；
   学员自己改名，hub 每次都从当前输出读提示符，不缓存主机名。

**实测结论**（2026-10-04，AR1220 ×2 + S5700，真机）：

| 学员改动类型 | 路由器 | 交换机 | 说明 |
|---|---|---|---|
| 接口 IP 增 / 改 / 删、sub 地址 | 可撤销 | Vlanif 可撤销 | 删掉的快照地址会补回 |
| `shutdown` / `undo shutdown`、`description` | 可撤销 | 可撤销 | |
| 静态路由增 / 删（含 preference） | 可撤销 | 可撤销 | |
| OSPF 进程、area、network | 可撤销 | — | 整个进程 `undo ospf N`（自动确认）；新 area 先删 network |
| ACL（规则增删）+ `traffic-filter` | 可撤销 | 可撤销 | 先撤接口上的过滤再删 ACL |
| LoopBack / Vlanif 等逻辑接口 | 可撤销 | 可撤销 | |
| VLAN 创建 / 删除、`port link-type`、`port default vlan`、trunk allow-pass | — | 可撤销 | VLAN 按集合比较 |
| `stp mode` | — | 可撤销 | |
| `sysname` | 可撤销 | 可撤销 | 提示符随之切回 |
| `user-interface` 块、`aaa` 下的 `local-user` | 排除 | 排除 | 不比对、不复位，单独列出 |

排除理由：`user-interface` 管 console / vty 的认证方式，改错会把学员和系统一起锁在 console 外（场景校验同样禁止碰它），复位命令本身也走 console，
没法安全地自动改回；`local-user` 的密码以不可逆密文显示，恢复需要原文，而且与网络排障题无关。

未覆盖 / 需要注意：没在真机上试过 BGP、RIP、DHCP、NAT、STP 端口参数等；遇到规则表之外、整行和截断写法都 undo 不掉的配置，会列为「无法自动撤销」
并提示重载拓扑。复位耗时：每台设备 3~15 秒（一轮抓配置 + 一组命令 + 一次比对）。

### 真机 e2e（里程碑 4）

`backend/tests/e2e/reset_e2e.py`：eNSP 打开 Campus_Network 并启动 AR0、AR2、LSW0，服务参数同上（`AINET_ENSP_RUNNING_IDS` 加上 LSW0 的 GUID），
另起 `tests/e2e/console_daemon.py serve`（`AINET_E2E_HUBSOCK=<unix socket>`；它内嵌同一套 ConsoleHub + config_reset，每个端口只保持一条连接，
也可单独用来做真机实验：`snapshot` / `restore` / `diff` / `hidden` / `student` 子命令）做独立核对，再
`python tests/e2e/reset_e2e.py http://127.0.0.1:8771 <截图目录> --db <服务的库> --archive <开工前三台设备的 display 存档目录>`。
注意 console_daemon 是同一 console 端口上的第二条连接（输入输出共享）：不要在服务执行隐藏事务（开始 / 判定 / 结束练习）时向它发请求，
否则两边的输入会在设备上交错（实测出现过被打乱的命令）；reset_e2e 只在每道题结束后才用它抓配置。
覆盖：拓扑图接口标注（T1）；三道新题各自开始 → 经页面终端修复 → 自动判定通关 / 放弃；学员在终端里做一批额外改动（接口 IP 增改删、shutdown、LoopBack、
静态路由、OSPF、ACL + traffic-filter、VLAN、trunk、stp、删掉 baseline 配置、sysname）→ 结束后每台设备 `display current-configuration`
与开练前快照**逐行一致**；停在系统视图时结束会提示还原未完成，回到用户视图后重试成功；ACL 题另跑一遍通关路径（C2，按参考答案改 rule 5、
3/3 通关）；最后三台设备与开工前的未配置状态逐行一致。2026-10-04 真机：T1/A/B/C/F 一轮 8 pass / 0 fail，C2 + F 一轮 3 pass / 0 fail；
另用 console_daemon 在 AR0 上验证了删掉整个 OSPF 进程（含两个 area）与 ACL 后按快照整块补回（1 轮，不发 quit）。

## 里程碑 5（已实现）：AI 编排（DeepSeek）——操作分析、评分、AI 分级提示

### AI 配置

- **API Key**（不写进代码、日志、报错和提交；界面与日志最多显示前 3 位加 `***`）。读取顺序：
  0. 页面「AI 设置」保存的 Key（写在下面第 2 / 3 条的第一个文件里，库里只记「由页面设置」；见下面「AI 设置（页面）」）；`AINET_LAB_FORCE=1` 且设了 `DEEPSEEK_API_KEY` 时环境变量优先；
  1. 环境变量 `DEEPSEEK_API_KEY`；
  2. `AINET_SECRETS_FILE` 指向的文件（设了就只读这个文件）；
  3. 缺省依次找 `backend/data/secrets.env`（Windows 部署用这个，`data/` 已被 git 忽略）和 `~/.config/ainet/secrets.env`。

  文件格式为一行 `DEEPSEEK_API_KEY=sk-...`（容忍 BOM、CRLF、引号、`export ` 前缀和 `#` 注释）。服务启动时打印 Key 来源（脱敏）。
- **模型**：DeepSeek OpenAI 兼容接口，openai Python SDK（`requirements.txt` 已加 `openai`），`base_url=https://api.deepseek.com`，
  模型 `deepseek-flash`。`response_format=json_object` 已实测支持，提示词同时约束 JSON 格式，解析容忍代码块包裹与多余文字。
- **思考开关**：`deepseek-flash` 默认先思考再回答，思考也占 `max_tokens`（太小时 content 为空）。请求体传 `thinking.type` 控制：
  - 评分默认**开启**思考（异步执行、不阻塞学员；实测一次评分约 5~10 秒、2~4 千 token），`max_tokens` 给 8000；
  - 提示默认**关闭**思考（学员在等；实测约 1 秒、1 千 token 左右），总时长上限 15 秒，超时回退静态提示。
- 环境变量（均可选）：

| 变量 | 缺省 | 说明 |
|---|---|---|
| `AINET_AI_MODEL` / `AINET_AI_BASE_URL` | `deepseek-flash` / `https://api.deepseek.com` | 模型与接口地址 |
| `AINET_AI_MAX_RETRIES` | 2 | 超时 / 网络 / 限流 / 5xx 的重试次数（SDK 自身重试已关闭） |
| `AINET_AI_JSON_MODE` | 1 | 是否传 `response_format=json_object` |
| `AINET_AI_SCORING` / `AINET_AI_HINTS` | 1 / 1 | 0 = 关闭 AI 评分（只给结果分）/ 关闭 AI 提示（只用静态提示） |
| `AINET_AI_SCORE_THINKING` / `AINET_AI_SCORE_TIMEOUT` / `AINET_AI_SCORE_MAX_TOKENS` | 1 / 120 / 8000 | 评分：是否思考、单次超时（秒）、max_tokens |
| `AINET_AI_HINT_THINKING` / `AINET_AI_HINT_TIMEOUT` / `AINET_AI_HINT_MAX_TOKENS` | 0 / 15 / 800 | 提示：是否思考、总时长上限（秒，含重试）、max_tokens |
| `AINET_SCORE_WEIGHT_RESULT` / `_PROCESS` / `_SIDE` | 50 / 35 / 15 | 三维满分 |
| `AINET_SCORE_FAIL_DISCOUNT` | 0.6 | 结果未达标时过程分的折扣 |
| `AINET_SCORE_HINT_PENALTY` | 0 | 每用一级提示从过程分扣的分（0 = 不扣，提示用量只作为 AI 的参考） |
| `AINET_LOG_OUTPUT_MAX` | 2000 | 每条命令输出摘要的字符上限（超出保留头尾） |

### AI 设置（页面，里程碑 6.18）

学员菜单（课程页与操作页）里的「AI 设置」（考试中不显示）打开小对话框，AI 评分、分级提示、Agent 面板共用：

- **API URL**（OpenAI 兼容接口地址，http / https，去尾斜杠，不能带用户名密码 / 查询串）与**模型**（可选）存 `app_settings` 的 `ai:base_url` / `ai:model`；留空 = 删掉记录，回到环境变量 `AINET_AI_BASE_URL` / `AINET_AI_MODEL` 或内置缺省。
- **API Key** 是 password 输入框，只写不读：保存时写进 ainet 主机的 secrets 文件（`AINET_SECRETS_FILE`，没设时 `backend/data/secrets.env`）——更新或追加 `DEEPSEEK_API_KEY=` 一行、保留其他行、临时文件 + 替换的原子写入；库里只记 `ai:key_page=1`（不存 Key）。接口只返回「已配置 / 未配置」、来源与掩码（如 `sk-***abcd`），绝不回显整串，日志也不写。「清除 Key」从文件删掉这一行，回到环境变量或其他 secrets 文件里的 Key。
- **优先级**：页面保存的值 > 环境变量 > secrets 文件 / 内置缺省；`AINET_LAB_FORCE=1` 时设了的环境变量优先（与接入设置同一规则）。
- **立即生效**：客户端按（Key, URL, 模型）缓存，保存后下一次 AI 请求就用新值，不用重启；已发出的请求照旧完成。练习 / 考试进行中也允许修改。
- **测试连接**（不保存）：用填的（没填的用当前生效值）URL + Key 发一次 `GET {URL}/models`；服务端没有这个接口（404 / 405）时改发一个 `max_tokens=1` 的补全。返回成功（模型数、所填模型在不在列表里）或失败原因（鉴权失败 / 余额不足 / 限流 / 服务端错误 / 网络错误 / 超时），文本不含 Key。

| 接口 | 说明 |
|---|---|
| `GET /api/ai/settings` | `{base_url{value, source, default, env_var}, model{…}, key{configured, source, source_text, masked, file, env_var, page}, force}`；source：`db`（页面）/ `page`（Key 由页面设置）/ `env` / `force` / `file` / `default` / `none` |
| `POST /api/ai/settings {base_url?, model?, api_key?, clear_key?}` | 省略 = 不改，`base_url` / `model` 为 null 或空串 = 回到缺省，`api_key` 非空 = 写入；→ `{changed[], …同 GET}`；格式不对 / 没有改动 422 |
| `POST /api/ai/settings/test {base_url?, api_key?, model?}` | → `{ok, kind?, message, models?, model_listed?, base_url, model, key_masked}`；没有可用的 Key 或格式不对 422 |

测试：`backend/tests/test_ai_settings.py`（URL / 模型 / Key 校验、secrets 文件保留其他行与去重、优先级与 `AINET_LAB_FORCE`、保存后立即换客户端、Key 不出现在响应 / 库 / 日志里、练习中可改、测试连接用 httpx MockTransport 覆盖成功 / 鉴权失败 / 无 /models 改发补全 / 连不上，不发真实外网请求；`conftest.py` 缺省把测试连接的传输层换成「禁止联网」）。

### 命令输出采集

ConsoleHub 在学员每次回车提交命令时开始采集其后学员可见的设备输出，到下一个提示符为止（分页中继续采集），整理成摘要
（去分页符、ANSI 与控制字符、首行回显和末尾提示符，超长保留头尾）写入 `attempt_logs.output`；下一条命令提交时强制结束上一条，
结束练习前把未回到提示符的那条按已收到的内容落库。命令结束后才到的异步日志（如接口 / 邻居 Down）追加到该设备最近一条命令上，
作为它的附带影响；该设备还没有学员命令时的日志（注入故障的余波）不记。**隐藏事务（注入 / 判定 / 还原）的输出不经过可见通道，
不会被记录**；结束流程开始后（还原事务带出的日志）也不再记录。

### 评分（spec §9）

练习结束（通关或放弃）、设备复位（场景 restore + 整机复位，见里程碑 4）之后**异步**评分，不阻塞「结束练习」接口：

- 结果分（50）：场景 checks 是否全部通过，确定性计算，不交给 AI；
- 过程分（35）：AI 根据场景（工单、参考排障路径、评分要点 `scoring_points`、知识点、故障真相）和学员的命令及输出评判方法论
  （先定位再改配置、分层排查、效率、有无盲目试错、修复后是否验证；用了第 3 级提示时最高 60%）；**结果未达标时 ×0.6**；
- 规范/副作用分（15）：先用规则抽取确定性信号（危险或多余的配置命令、报错命令、重复命令、学员改配置之后出现的 Down 日志），
  连同操作记录交给 AI 综合打分。
- AI 只给两个维度 0~100 的「得分率」和理由、整体点评（做得好的 / 问题 / 改进建议）、对照参考路径的标准排障思路；
  权重、截断（越界分数截到 0~100）、折扣都由代码计算。
- 结果存 `attempts.score_total / score_result / score_process / score_side`，状态 `score_status`（`scoring` / `done` / `degraded` / `failed`），
  评分全文（含 AI 点评、信号、token 用量）存 `score_json`。服务重启时遗留的 `scoring` 标为失败（可重新评分）。
- **降级**：AI 不可用（无 Key、鉴权失败、限流、超时、网络、解析失败）时只给结果分，过程分 / 规范分为空（不伪造），
  `score_status=degraded`，界面标注「AI 点评不可用：原因」「仅结果分」，可点「重新评分」。解析失败会先重试一次（关闭思考再试）。

### AI 分级提示（spec §7）

「提示（第 N 级）」按 1 → 2 → 3 依次生成：① 方向 ② 命令建议 ③ 直接揭示，内容结合学员到目前为止的命令和输出（已经排除的不重复、
已看到的异常引导他回头看）。同一级再次查看直接用缓存，不重复调 AI；提示用量沿用 `hints_used`。

- 第 1、2 级的泄露防护：提示词列出禁止出现的内容（solution / inject 里的配置命令、只在故障配置里出现的 IP；checks 判定命令是只读查看命令，允许出现），
  生成结果再用规则检查（忽略大小写和空白），命中就丢弃 AI 结果、改用静态提示。第 3 级可以揭示故障点。
- AI 失败（无 Key、超时、解析失败、被泄露检查拦下）时回退到场景 JSON 的静态 `hints`；返回值与界面都标注来源（AI / 静态）。

### API（新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/practice/hint` | 返回值新增 `source`（`ai` / `static`），回退时带 `fallback_reason` |
| POST | `/api/practice/end` | 返回值新增 `score: {status: "scoring"}`（评分在后台进行） |
| GET | `/api/practice/result/{attempt_id}` | 评分状态与结果（`status`、四个分数、`detail` = 评分全文）；只能看自己的记录，别人的返回 404 |
| POST | `/api/practice/rescore/{attempt_id}` | 重新评分（练习未结束或正在评分时 409；只能重评自己的） |

前端：练习总结里有评分卡（评分中转圈 → 总分、三维分数与理由、点评、标准思路；降级 / 失败显示原因和「重新评分」）；
提示标注「AI / 静态」，生成中按钮显示「提示生成中…」；档案页最近记录显示总分（评分中 / 仅结果分 / 评分失败有标注）。

### 测试

默认 pytest 一律 mock AI（`conftest.py` 屏蔽开发机上的 Key，客户端工厂换成「无 Key」桩），不访问网络：覆盖 JSON 解析稳健性
（多余文字、代码块、字段缺失、分数越界截断）、评分公式与折扣、降级、提示泄露防护与回退、Key 读取顺序、Key 不进日志 / 报错 / 库、
命令输出采集（假设备）、结果 API 权限。真实 API 冒烟：`python tests/manual/ai_smoke.py [score|hint]`（会消耗 token；不打印 Key），
用构造好的 Campus「AR0 与 AR2 互联不通」练习记录分别跑评分（合理排障 / 盲目试错）和三级提示，打印结果与 token 用量。

### 已知局限

- 泄露过滤按命令原文匹配（忽略大小写和空白），VRP 缩写写法（如 `dis ip int br`）或换种说法描述答案匹配不到，主要靠提示词约束。
- OSPF 场景的禁止内容含单词 `shutdown`，第 1、2 级 AI 提示里只要出现这个词就会回退静态提示（宁严勿漏）。
- AI 评分有一定随机性（同一记录多次评分相差几分）；过程分以命令记录为依据，学员在 eNSP 自带命令行里的操作记录不到。
- 评分任务在服务进程内存中运行，服务重启会中断（标为失败，可重新评分）。
- 判定命令不再列入第 1、2 级禁止内容（原先 ACL 题的静态第 2 级提示含 `display acl all`，AI 一次超时回退后被判泄露）。场景加载时校验静态第 1、2 级提示不含修复 / 注入配置和故障特有的值。AI 提示偶有超过 15 秒超时、回退静态提示的情况（2026-10-04 实测一次），回退原因写入服务日志（logger `ainet.hints`）。

## 里程碑 4 + 5 集成（复位之后再评分）

- **结束顺序**：场景 restore → 整机复位并比对（`config_reset`）→ 复位结果写入 `attempts.reset_json` → 触发异步评分。复位暂不可执行（学员停在系统视图、正在输入等）时练习保持进行中、不评分。评分从库里读复位结果（重新评分也一样），写进 `score_json.reset` 并交给 AI：复位失败 / 无法撤销 / 不在范围内的配置是系统问题，**不扣学员的分**，学员自己敲过的多余命令照常评判。评分卡在复位没做干净时注明「不影响你的得分」。
- **输出采集与隐藏事务**：进入隐藏事务（判定、注入、复位）之前先结束学员上一条命令的采集，事务结束时补发的日志与 sysname 改名后重印的提示符不会算进学员的输出；学员刚回车、设备还没回显时事务返回「暂不可执行」。自动 `[Y/N]`、按组执行的命令只在隐藏通道里。
- **S5700 日志**：DATASYNC 配置同步日志（`DS/4/DATASYNC_CFGCHANGE`）只说明配置变了，不作为日志事件、不进输出摘要、不算「造成 Down」之类的副作用；它之后新行上、或直接接在日志行末尾的学员输入（实测改 sysname 后会这样），仍按命令记录；分页符后插进日志时，日志行末尾的擦除序列按换行拆开（以前配置快照会丢一行，集成联调中复现）。
- **场景**：m4 的 3 道 Campus 新题补了 `scoring_points`；`campus-ar0-ar2-link` 同时保留 m4 的 note 与 m5 的 scoring_points。
- **库迁移**：两边新增的表 / 列都保留（`attempt_configs` 表；`attempts.score_status / score_json / reset_json`），`init_db()` 幂等。`tests/test_db_migration_m45.py` 用历史版本的 schema（`tests/fixtures/legacy_db_schemas.json`，从 git 历史原样抽出）分别建库后升级：里程碑 2（aced4c3）、3c91daf、m4 分支、m5 分支。
- **集成测试**：`tests/test_integration_m45.py`（复位之后再评分的顺序、复位结果交给 AI 且不扣分、重新评分读库、隐藏事务与 sysname 改名下的输出采集、DATASYNC 各种插入位置、日志接在分页擦除序列前）。

### 真机 e2e（AI 联调）

`backend/tests/e2e/ai_e2e.py`：前置同 `reset_e2e.py`（三台设备、服务、`console_daemon.py`），服务能读到 DeepSeek Key。
`python tests/e2e/ai_e2e.py http://127.0.0.1:8771 <截图目录> --db <服务的库> [--only P,G] [--g-scenario <题>]`。

- P（ACL 题）：先排查，再请求第 1、2 级提示（必须来自 AI、不含禁止内容与答案）→ 额外改动（AR2 加 LoopBack1、LSW0 改 sysname）与排障 → 修复 rule 5 → 自动判定 3/3 通关 → 结束，三台设备与开练前快照逐行一致 → 评分卡（三维分数、点评、复位结果）→ 命令记录有输出、无隐藏事务命令 → 档案页显示总分。
- G（缺省回程路由题）：不看就改（shutdown、乱加路由、删正确的缺省路由、乱改接入 VLAN）→ 放弃 → 复位逐行一致 → 总分 < 30 且点评指出问题。

2026-10-04 真机（AR0 / LSW0 / AR2），按时间顺序：① P 首轮 `SUMMARY: 7 pass / 0 fail`（89.2 分；当时的 hub 还漏记了一条回显接在日志行末尾的命令，P5 检查也较松）；修复 hub（70e4022）、收紧 P5 后 ② P 复跑 P1~P6 全部通过（88.9 分），随后 G 因 AR2 的 `traffic-filter` 故障无法开始（见下方注意），这一轮没有输出 SUMMARY；③ G 改用回程路由题单独跑 `SUMMARY: 3 pass / 0 fail`（2.4 分）。评分 token（`score_json.ai.usage`）：9122（首轮，第一次解析失败、关闭思考重试）/ 4768 / 5030；提示 token 不经 API 返回，预检脚本实测每级约 1.0~1.3 千。最终代码上复跑 `practice_e2e.py` 11 pass / 0 fail、`reset_e2e.py --only T1,A,B,F` 7 pass / 0 fail（含 C、C2 的完整 9 pass / 0 fail 在 62e8cbb 上跑过，与最终代码只差 hub 的 `input_after_prompt`），最后三台设备与开工前存档逐行一致。
注意：eNSP 的 AR1220 在同一台设备上反复挂 / 撤 `traffic-filter` 几轮后会报 `Add rule failed`（模拟器资源问题，配置里看不出来），之后 ACL 题无法开始（注入失败会自动还原），需要在 eNSP 中重启该路由器；所以 G 缺省不用 ACL 题。

## 里程碑 6（已实现，真机联调待做）：参数化场景 + 考试模式 + 命令回放

### 参数化场景（`backend/params.py`，spec §6）

场景 JSON 可选字段 `params`：`{参数名: 规格}`，按声明顺序求值；文本里用 `{参数名}` 占位（参数名只能是标识符，正则里的 `\d{1,3}` 不受影响）。

| type | 规格 | 取值 |
|---|---|---|
| `int` | `min`、`max`、`default`、`exclude?`（整数或 `"{其他参数}"`） | 区间内整数 |
| `choice` | `values`、`default` | 候选之一 |
| `ip` | `net`（如 `10.0.12.0/24`，可引用前面的参数）、`default`、`exclude?` | 网段内主机地址（不含网络 / 广播地址） |
| `subnet` | `within`、`prefix`、`default`、`exclude?` | 大网段里的子网，值为网络地址 |
| `host` | `net`、`index` | 派生：网段第 index 个地址 |
| `format` | `value`（如 `"10.0.{x}.2"`） | 派生：拼接字符串 |

- 可占位：`title`、`symptom`、baseline / inject / restore / solution 的命令、checks 的 command / expect / desc、`hints`、`reference_path`、`scoring_points`、`note`。`expect` 是正则，代入的值先 `re.escape`。
- **不能参数化**：`id`、`topology`、设备名（required / accessible、各映射的设备键、`checks.device`）和接口名（命令里 `interface` 后面不能有占位符）——真实接口和设备由拓扑决定。
- **种子**：0 = 全部取 `default`（与参数化之前的原题一致，回归用）；其他种子用 `random.Random(f"{场景 id}#{种子}")` 依次抽取，同一种子展开结果完全相同。练习开始时缺省随机生成种子（`POST /api/practice/start` 可带 `seed`；环境变量 `AINET_PRACTICE_SEED` 固定练习种子），考试由模板决定（固定或开考时随机）。种子和本次取值记入 `attempts.seed / param_values_json`；注入、判定、提示（含泄露检查）、评分、重启恢复、重新评分都用按种子展开后的场景。
- **加载校验（抽样）**：结构与引用检查（引用了未声明的参数、参数没被引用、设备名 / 接口名含占位符都是错误）→ 对参数空间抽样：种子 0、种子 1..24、每个随机参数的边界值（最小 / 最大可用值，choice 取全部候选）、「碰撞」取值（其他参数的默认值、solution / checks 里出现的数字和 IP——参数恰好取到正确值时故障不生效，随机抽样很难碰上）。每次展开都要通过原有全部规则（禁止命令、只读 checks、静态第 1、2 级提示不泄露），且含参数的注入命令展开后不能与参考修复命令相同。错误信息标明是哪次展开（如「边界 wrong=99：第 2 级静态提示泄露答案」「碰撞 wrong_vlan=10：…与参考修复命令相同」）。
- 已参数化的 Campus 题（种子 0 与原题逐字一致，只有互联题第 3 级提示把「网段写反了」改成通用说法）：

| 场景 | 参数 |
|---|---|
| `campus-ar0-ar2-link` | `link` 互联网段第三段（11~99，≠20，默认 12）；`wrong` 注入的错误网段（11~99，≠link，默认 21） |
| `campus-lsw0-access-vlan` | `wrong_vlan` 注入的错误接入 VLAN（2~999，≠10，默认 20） |
| `campus-ar2-return-route` | `bad_hop` 错误下一跳 10.0.12.x 的末段（3~254，默认 3） |
| `campus-ar2-acl-block` | `acl` 高级 ACL 编号（3000~3999，默认 3000）；`rule` 误写规则的编号（1~9，排在 rule 10 permit 之前，默认 5） |

### 考试模式（`backend/exam.py`，spec §8）

**考试模板**（`exam_templates.config_json`）：

| 字段 | 含义（缺省） |
|---|---|
| `questions[]` | `{scenario, seed: 整数 \| null（开考时随机）, weight: 1}` |
| `duration_s` / `question_duration_s` | 总时长（1800）/ 单题时长（null = 不限），秒 |
| `hints` | `{allowed: false, penalty: 5（每级从本题总分扣）, max_level: 3}` |
| `weights` | `{result, process, side}` 覆盖默认 50/35/15，按比例缩放到 100 |
| `retries` | 每题提交失败后还能再提交几次（1） |
| `show_topology` / `auto_judge` / `record_replay` / `shuffle` | 显示拓扑（true）/ 自动判定（false）/ 命令回放（true）/ 题目乱序（false） |

首次启动内置示例模板「Campus 基础排障测验」（3 道 Campus 题、随机种子、45 分钟、单题 15 分钟、允许 2 级提示）；删掉后不会再自动建回来。
**模板管理页 `/exam/templates`**（工作台「考试」页签右上角「管理模板」）：列表、新建、编辑（表单 + JSON）、删除。
**目前没有管理员角色，任何登录用户都能管理模板——以后要在这里（`/api/exam/templates*` 与模板管理页）加权限。**

**流程**：
- `POST /api/exam/start {template_id}`：系统里有进行中的练习或考试时拒绝；先按 §6.3 核对每道题的拓扑与设备都就绪，再建 `exams` 记录（学生、模板快照、开始 / 截止时间、状态、各题种子与顺序）并开始第 1 题。
- 每题复用练习的底层能力（快照 → baseline → inject → 确认故障生效 → 隐藏事务 → 命令与输出采集 → restore + 整机复位 → 异步评分），`attempts.mode = 'exam'`，关联 `exam_id / exam_index`。
- **「提交本题」验收**（`POST /api/exam/submit`）：考试缺省不自动判定，学员点「提交本题」后系统跑一遍 checks 作为本题结果（只告诉通过几项）。通过即锁定本题；未通过计一次，提交次数（1 + retries）用完也锁定。设备暂不可检查（学员正在输入等）时不计次数。
- **下一题**（`POST /api/exam/next`）：本题结束——若还有提交机会且未通过，系统按当前设备状态代为提交一次（计入次数）——然后复位、评分、开始下一题。开始失败（注入失败等）时本题停在「待开始」并显示原因，可「重新开始本题」（`POST /api/exam/retry_start`）或跳过（记未作答）。
- **交卷**（`POST /api/exam/finish`，页面上二次确认）：当前题同上代为提交并复位，没开始的题记为未作答（0 分）。
- **计时以服务端为准**：总截止时间、单题截止时间存库（绝对时间），计时器每 `AINET_EXAM_TICK` 秒（缺省 1）检查：总时长到 → 自动交卷；单题超时 → 自动进入下一题。系统注入故障、复位设备的耗时不算学员的时间（加到截止时间上，成绩单显示合计）。**服务重启后从库里恢复进行中的考试，停机期间时间照常流逝**（停机时到点的，恢复后立即处理）。
- **复位暂不可执行**（学员停在系统视图等）：本题停在「复位中」，结果已冻结，计时器每 `AINET_EXAM_RESET_RETRY` 秒（缺省 5）重试，页面提示回到用户视图；复位完成前不会开始下一题。
- **提示**（`POST /api/exam/hint`）：模板禁止时直接拒绝（403），超过 `max_level` 也拒绝；允许时按「每级扣本题总分 penalty」计入（AI 不可用、只有结果分时也照扣）。
- **评分**：每题沿用里程碑 5（结果分确定性 + AI 过程分 / 规范分，未通关过程分折扣），权重用模板的；AI 提示词带上考试背景（提交次数与结果、是否超时、单题限时）。考试总分 = 各题总分按题目权重加权平均（未作答记 0），各题评完后汇总落库。
- **答案保护**：考试进行中，任何接口都不返回 checks / solution / inject / restore / baseline / reference_path / scoring_points，也不返回种子与参数取值；注入 / 复位失败的原因里出现的场景命令替换成「（已隐藏）」；练习接口（check / hint / end / current）对考试题一律拒绝或不暴露；本场考试各题的 `GET /api/practice/result/{id}` 在交卷前返回 409；档案页在交卷前不显示本场各题。`tests/test_exam.py::test_no_answer_fields_leak_during_exam` 遍历全部 `/api` 路由（新增路由必须加入覆盖列表）检查字段名和展开后的命令原文。
- **成绩单 `/exam/report?id=`**（`GET /api/exam/{id}/report`，只能看自己的、交卷后）：总分，各题三项分数与理由、AI 点评、参考思路（AI 讲解或场景 reference_path，不给 checks / solution 原文）、用时、命令数、提示级数、各次提交、超时 / 代提交标记。
- **命令回放**（模板开启 `record_replay` 时，`GET /api/exam/{id}/replay`）：按时间顺序给出每道题学员敲过的命令和输出（`attempt_logs`）；页面可逐条播放（▶ / 上一条 / 下一条，点击任一条跳转）或全部展开滚动查看。
- **档案页**：「考试记录」一块（日期、考试名、状态、总分、成绩单链接）。

**工作台「考试」页签**（拓扑面板的第三个页签，不改变分栏布局）：模板列表 → 作答（总倒计时、第 i / n 题、单题倒计时、工单、本题设备按钮、提交本题（剩余次数）、提示（模板允许时显示）、下一题、交卷）→ 已交卷（成绩单链接）。模板设定不可看拓扑时，`/api/topology/current` 只给设备清单（去掉连线、背景图形、注释），拓扑页签打码显示「本场考试不提供拓扑图」，终端仍从考试页的设备按钮打开。刷新页面后自动恢复进行中的考试。

**数据**：新表 `exams(id, student_id, template_id, template_name, config_json（模板快照）, status, started_at, deadline_at, ended_at, state_json（各题状态 / 种子 / 提交记录）, score_total, score_status)`；`attempts` 补列 `seed, param_values_json, exam_id, exam_index`；`exam_templates` 补列 `created_at, updated_at`。迁移幂等（`tests/test_exam.py` 用历史各版本 schema 升级三次验证）。

### 测试

- `tests/test_params.py`：展开确定性（不受全局随机状态影响）、种子 0 与参数化之前的原题一致（`tests/fixtures/scenarios_m5/`）、抽样计划（种子 + 边界 + 碰撞）、只在边界 / 碰撞取值才出现的违规能被抓到、未声明 / 未引用 / 设备名 / 接口名参数化报错、正则转义、练习记录种子并在服务重启后按种子恢复。
- `tests/test_exam.py`：模板 CRUD 与校验、状态机全流程（提交 → 重试 → 下一题 → 单题超时 → 自动交卷）、总时长到自动交卷、系统操作时间顺延、开始失败（错误信息打码）后重试 / 跳过、复位暂不可执行时停在「复位中」并重试、服务重启恢复（停机时间照常流逝）、禁止提示与提示扣分、加权汇总与模板权重（重新评分沿用）、回放、权限、与练习互斥、拓扑打码、答案保护（遍历全部接口；设备回显写法如点分掩码、合并的 vlan batch 里的注入值也打码）、自动判定模板（不提交也能通过）、乱序（按开考时的顺序种子、成绩单同序）、迁移幂等。AI 一律 mock（不可用 → 只给结果分），设备用假 hub，时钟用假时钟。
- 模拟设备 e2e：`python tests/e2e/exam_fake_e2e.py <截图目录> [单题时长秒]`——本机起 3 台带配置树的假 VRP（`tests/fake_vrp.py` 的 `NetVRP`：ping 按各设备的接口地址、静态路由、access 口 PVID 计算）监听 127.0.0.1:2000/2001/2003，再起服务、跑同一套浏览器步骤。
- 真机 e2e：`backend/tests/e2e/exam_e2e.py`，用法同 `practice_e2e.py`：eNSP 打开 Campus_Network 并启动 AR0、AR2、LSW0，以
  `AINET_TOPO_DIRS=tests/fixtures/topos AINET_ENSP_RUNNING_IDS=<AR0>,<AR2>,<LSW0 GUID> AINET_ENSP_CLIENT=1 ENSP_HOST=... AINET_DB_PATH=<临时库>` 起服务，
  再 `python tests/e2e/exam_e2e.py http://127.0.0.1:8770 <截图目录>`（单题时长缺省 120 秒，`AINET_E2E_QTIME` 可改）。步骤：模板管理页新建模板 → 开考 → 第 1 题提示 + 修复 + 提交通过 → 第 2 题提交失败、修复后重试通过 → 第 3 题不作答等超时 → 成绩单 → 回放 → 档案页 → 不看拓扑的考试 + 交卷二次确认，全程无 JS 异常。

### 已知局限

- 考试题之间（上一题已复位、下一题还在注入）终端白名单短暂放开到整个拓扑（与没有练习时相同）。
- 学员在题目时间到之后、复位完成之前仍可在终端里敲命令（不再记录、不影响已冻结的结果），复位要等学员回到用户视图。
- 服务在「开始一题」的过程中被杀掉时，这道题恢复后显示「开始被中断」，需要学员点「重新开始本题」；中断时设备上可能留有一半的 baseline / inject，重新开始时新拍的快照会把这些残留当成基准（复位只能回到残留状态），这种情况下需要在 eNSP 中重载拓扑。
- 拓扑「打码」只是不给连线 / 图形，设备清单仍然下发（终端标签要用）；之前在页面上看过的拓扑图浏览器里仍有缓存。
- 模板管理接口（`/api/exam/templates*`）在考试中照常可用，模板里**固定的种子**能被看到（没有管理员角色前无法区分出题人与考生）；考试状态接口本身不给种子。要防这一点，考试模板用随机种子（`seed: null`），或等加了权限后再限制。
- 参数化只覆盖文本替换；接口名、设备不能参数化（真实拓扑决定）。抽样校验不能证明全部参数空间都合法，「碰撞」只试 solution / checks 里出现的数字和 IP。

## 第二种实验后端：eNSP Pro（已实现，2026-10-06）

eNSP Pro 是浏览器 + 服务端形态的华为模拟器（本机 KVM 虚机，设备系统为 YunShan OS）。ainet 把原来写死的 eNSP 经典版逻辑
抽成实验后端接口（`backend/lab.py`），eNSP 经典版是其中一种实现（行为不变），新增 eNSP Pro 实现。详见 spec §5、§10.4。
**能否替代经典版的验证结论与迁移建议见 [`docs/enspro-replacement.md`](docs/enspro-replacement.md)**（2026-10-06：YunShan 题库练习 / 考试真机 e2e 全部通过，
建议新部署默认用 eNSP Pro，经典版保留给 VRP5 教材一致性与没有 KVM 主机的场合）。

### 用法

```bash
cd backend
# secrets.env（~/.config/ainet/secrets.env 或 backend/data/secrets.env）里加一行：ENSPRO_DEVICE_PASSWORD=<设备 console 密码>
AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=3 uvicorn main:app --host 127.0.0.1 --port 8000   # 3 = ainet_campus
```

沙箱 `ainet_campus`（id 3）是 Campus 题的 eNSP Pro 版拓扑：AR0 / AR1 / AR2 / R1~R3（ENSP-AR）、LSW0（ENSP-LSW）、PC1（PC-CLIENT），
连线见 `docs/enspro-replacement.md` §1。`ainet_test`（id 2，T1 / T2）只有一道互联题。新建沙箱后先在服务里记一次干净基线
（`POST /api/lab/baseline`，或让第一次开题自动记录，见下面「干净基线」）。

| 环境变量 | 缺省 | 说明 |
|---|---|---|
| `AINET_LAB_PROVIDER` | `ensp` | `ensp` = eNSP 经典版，`enspro` = eNSP Pro（只是缺省值：页面上切换后以库里记录为准） |
| `AINET_ENSPRO_URL` | `https://127.0.0.1:8443` | eNSP Pro 地址（自签证书，不校验）；只是缺省值，课程页「⚙ 接入设置」改过以后以库里的记录为准 |
| `AINET_ENSPRO_SANDBOX` | 无 | 当前沙箱 id；拓扑 = 这个沙箱（同上，只是缺省值；课程页进入课题时按题目切换沙箱） |
| `AINET_ENSPRO_AUTO_RELOGIN` | `0` | 会话被别处登录挤掉后是否自动重新登录（会反过来挤掉对方） |
| `AINET_ENSPRO_RELOGIN_INTERVAL` | `30` | 两次自动重登的最小间隔（秒） |
| `AINET_ENSPRO_HEARTBEAT` / `AINET_ENSPRO_LOGIN_TIMEOUT` / `AINET_ENSPRO_CACHE_TTL` | 30 / 30 / 2 | console 心跳间隔、登录每步等待上限、沙箱详情缓存（秒） |
| `AINET_LAB_BASELINE_AUTO` | `1` | 设备还没有干净基线时，把第一次开题读到的配置记为基线（只对 eNSP Pro） |

**同一账号只能有一个会话**：ainet 运行期间在浏览器里登录 eNSP Pro 网页会把 ainet 挤掉（状态栏提示「会话已失效」，
出现「重新登录 eNSP Pro」按钮）；反过来 ainet 重新登录也会挤掉网页。服务退出时自动登出。

### 统一设备标识

拓扑里每台设备有整数 `console` 键：eNSP 经典版 = `com_port`（telnet 端口），eNSP Pro = `deviceId`。ConsoleHub 通道、
`/ws/console/{console}`、终端白名单、练习 / 考试里的「设备名 -> 端口」都用它，所以练习、考试、复位代码不分后端。

### console 登录

连接器连上后发一次回车，按设备提示应答：全新设备首次登录设置密码（输两遍）；`Password:` **只输一次**密码，失败即停，
之后对该设备不再尝试（终端与状态栏给出原因），直到在 ainet 上重新启动该设备或重启服务；绝不在 `Password:` 后补空回车。
登录成功才把通道交给 ConsoleHub。设备空闲登出后自动再登录一次（期间学员输入被吞掉，不会混进密码）：真机 console 空闲约 5 分钟
登出，输出以 `Please Press ENTER.` 加空行结尾，连接器看到这个提示就自己回车、输密码。PC-CLIENT 没有登录，提示符是 `PC>`。
刚冷启动的设备（尤其多台一起启动）状态已是「运行中」后，console 仍可能几分钟没有任何输出，这时开题会报「console 登录超时」，稍后重试。

### 设备启停

拓扑图节点下方有「▶ 启动 / ■ 停止」按钮（确认框；启动中显示已用时与阶段，约 110 秒）。接口
`POST /api/lab/devices/start|stop|reboot`（`{"devices": [deviceId]}`）只对 eNSP Pro 开放；练习或考试进行中不能停止本题需要的设备。
本机 eNSP Pro 的重启接口返回 HTTP 405，ainet 的重启是「停止 → 等停机间隔 → 启动」。8 台设备一起冷启动约 3.5 分钟。启动看门狗、就绪探测、空闲停机见下面「设备启动可靠性」。

### YunShan 适配要点（AR6700 V600R025 实测，spec §3）

- 单阶段生效（改完立即生效，提示符 `[HUAWEI]`），hub 仍兼容两阶段型号（`[~` / `[*` 时先 `commit` 再 `return`）。
- 配置开头的 `!` 注释行不参与快照比对；`undo sysname` 回到 `HUAWEI`；分页擦除 16 列；ping 输出与 VRP5 同格式，判定正则不用改。
- 接口名只认缩写：AR 为 `GE0/0/x`，交换机（S6700）为 `GE1/0/x`；`interface GigabitEthernet…` 报 Wrong parameter。
- 交换机（ENSP-LSW = S6700）同样单阶段生效；端口缺省 `desirable` / PVID 1；VLAN、access、trunk、Vlanif、stp mode 的撤销写法同 VRP5，整机复位实测干净。
- 设备会自动保存（改动后 5 分钟），停机时有未保存的改动也会保存（交换机也是），关掉自动保存挡不住停机保存，所以 ainet 不关它，
  改用「干净基线」：每题开始前先与基线比对，不一致先复位（见下）。ainet 从不执行 save。

### 终端主机（PC）

PC-CLIENT 的命令行只有 ping / traceroute / ifconfig 等，不能配地址；地址由 ainet 经 eNSP Pro 的设备属性接口配置。场景里把这类设备列进
`hosts`，它们的 baseline / inject / restore 只能写 `ip address <IP> <掩码>`、`ip gateway <IP>`、`undo ip address`、`undo ip gateway`，
判定只能用 ping（Linux 格式，正则写 `\b[12] received\b`）。开练快照与复位按地址配置处理。

### 干净基线

每台设备在库里记一份干净配置（PC 记地址配置）。练习 / 考试每题开始前先比对：一致记 `clean`；不一致先复位到基线（`reset`）再拍开练快照；
复位不干净则拒绝开始；还没有基线时记下这次读到的配置（`captured`）。自动记录会把设备当时的配置当成干净的，生产环境应在确认设备干净后手动记录（或设 `AINET_LAB_BASELINE_AUTO=0`）。结果在 `GET /api/practice/current` 的 `pre_reset`。
`GET /api/lab/baseline` 查看各设备有没有基线，`POST /api/lab/baseline {"devices": [...]}`（缺省全部运行中的设备）重记，练习 / 考试中拒绝。

### 场景

场景可写 `"platform": "yunshan"`（缺省 `vrp5`），`topology` 写沙箱名；平台与当前后端不一致时显示未就绪。
`scenarios/enspro-t1-t2-link.json`：「T1 与 T2 互联不通」（沙箱 ainet_test）。**题名不带平台后缀**（2026-10-07 去掉了「（YunShan）」）：两个平台可以有同名题，环境在课程页分列，档案、成绩单、命令回放、模板管理页在题名旁显示平台标签（`LabSwitch.platformTag`），单题考试名末尾带「 · YunShan / · VRP5」；`(题名, 平台)` 必须唯一（`test_courses.py` 检查）。Campus 4 题与 OSPF 题的 YunShan 版
（沙箱 ainet_campus，参数化同原题，旧 VRP5 版保留）：`campus-ar0-ar2-link-ys`、`campus-lsw0-access-vlan-ys`（另有 PC1 作为 VLAN 10 用户主机）、
`campus-ar2-return-route-ys`、`campus-ar2-acl-block-ys`、`ospf-r1-shutdown-ys`（R1~R3 经 LSW0 的 VLAN 1 组成共享网段，OSPF 由 baseline 下发）。

**题库扩充**（2026-10-07，里程碑 6.14 规划 + 6.15 真机验证，[`docs/question-bank-plan.md`](docs/question-bank-plan.md)、
[`docs/question-bank-verify.md`](docs/question-bank-verify.md)）：按 L01~L12 每章 2~3 道规划，现有沙箱能做的写成 17 道题，先以草稿
（`"draft": true`，课程页缺省不列出，设 `AINET_SHOW_DRAFTS=1` 才看得到）提交，**同日在本机 eNSP Pro 逐题真机验证后全部去掉 draft 上线**
（YunShan 题由 6 道增加到 23 道）：
- ainet_test（5 道）：`enspro-t2-port-shutdown`（L01）、`enspro-t2-ip-wrong-port`（L01）、`enspro-t2-mask-mismatch`（L02）、
  `enspro-t1-static-route-dest`（L03）、`enspro-t1-t2-two-faults`（L12）；
- ainet_campus（12 道）：`campus-lsw0-port-shutdown-ys`（L01）、`campus-pc1-gateway-ys`（L02，PC 地址故障）、
  `campus-ar0-dot1q-trunk-ys`（L04 单臂路由）、`ospf-r3-area-mismatch-ys` / `ospf-r2-hello-mismatch-ys`（L05）、`campus-ar2-acl-telnet-ys`（L06）、
  `campus-ar0-dhcp-select-ys` / `campus-ar0-dhcp-exhausted-ys`（L07，R1 当 DHCP 客户端）、`campus-r1-dns-host-ys`（L08）、
  `campus-ar2-easyip-acl-ys` / `campus-ar2-nat-static-ys`（L09，AR1 当运营商）、`campus-mgmt-two-faults-ys`（L12）。

真机修正了 4 道：两道 DHCP 题的 restore 去掉 `undo dhcp enable`（AR6700 出厂就有）；AR6700 接口下没有 `nat outbound` / `nat static`，
两道 NAT 题改用 `nat-policy`（Easy IP）与 `nat server`（静态），出接口要 `nat enable`。各条写法的实测结论见规划文档 §6。
L10 STP、L11 防火墙要新建沙箱（ainet_l2、ainet_fw），只做了规划。以后新写的草稿题照此流程：真机验证通过后删掉 `draft`，
并把 id 加进 `tests/test_scenarios.py` 的 `VERIFIED`（`DRAFTS_*` = 题库清单 − `VERIFIED`）。

### 测试

- `tests/test_enspro.py`：STOMP 帧解析、REST 登录与会话冲突（挤掉 / 未登录 / 自动重登间隔）、沙箱转拓扑、provider 状态与启停、
  console 登录状态机（首次设密码、正常登录、密码错误只试一次且之后不联网、没配密码、已登录、心跳、回放已在密码提示、
  空闲登出后自动重登吞掉学员输入、hub 断线重连）。假服务在 `tests/fake_enspro.py`（REST 用 httpx.MockTransport，STOMP 用 websockets）。
- `tests/test_yunshan.py`：hub 与复位在 YunShan 格式下（`!` 行、16 列分页擦除、undo sysname、两阶段 commit、ping 输出采集）。
- `tests/test_enspro_api.py`：`AINET_LAB_PROVIDER=enspro` 的整条链路（拓扑接口、按平台就绪、终端 WebSocket、练习开始 → 修复 → 通关 → 复位一致、启停、会话冲突）。
- `tests/test_question_bank_fake.py`：ainet_test 的 5 道题库扩充题（已真机验证上线）在假 eNSP Pro 上逐题跑完整练习（开始 → 确认故障 → 经终端执行参考修复 → 判定通关 → 结束复位与开练前快照一致），以及假网络模型的接口 shutdown；草稿题的隐藏 / 显示见 `tests/test_courses.py`。
- `tests/test_enspro_replace.py`：空闲登出（真机格式）后立即自动重登、PC（`PC>`、设备属性接口、命令行重启吞命令后重发）、重启 405 退回、
  S6700 假交换机（长接口名报错、display port vlan、VLAN / trunk / Vlanif 整机复位）、场景 hosts 校验、练习中的终端主机与干净基线、
  AR6700 异步 traffic-filter 报错。
- 真机：`python tests/e2e/enspro_e2e.py basic|practice|practice-extra [base_url] [截图目录]`（沙箱 ainet_test；前置见脚本说明；跑的时候不要在别处登录 eNSP Pro）。
  2026-10-06：basic 6/6、practice 5/5、practice-extra 5/5。
- 真机：`python tests/e2e/enspro_campus_e2e.py practice|practice-extra <场景 id> | exam | session [base_url] [截图目录]`（沙箱 ainet_campus）。
  2026-10-06：5 道 YunShan 题练习各 5/5、额外乱改 6/6、3 题考试 7/7、会话被挤掉与重新登录 6/6。
  设备号按当前拓扑取，ainet_test 的题也能跑（先 `POST /api/lab/prepare` 切沙箱）；OSPF / DHCP 题开始后等 90 秒再判一次应不通过；
  `AINET_E2E_REQUIRE_BASELINE=1` 要求开练前与手动记录的干净基线比对过，`AINET_E2E_LOG=<文件>` 记下每条命令的完整回显。
  2026-10-07：题库扩充 17 道逐题各 7/7（含等待后的故障确认与开练前基线比对），见 `docs/question-bank-verify.md`。

### 已知局限

- 拓扑图的背景图形 / 文本框按猜测的字段转换（沙箱 2 里没有实例）。
- 实测过 ENSP-AR（AR6700）、ENSP-LSW（S6700）、PC-CLIENT；ENSP-CE、防火墙等其他型号与两阶段生效型号只有假设备测试；交换机的 Eth-Trunk、STP 端口参数未测。
- EXTERNAL-HUB 在本机起不来（停在「分配虚机」），共享网段用交换机 VLAN 1 代替。
- PC 的地址学员在终端里改不了（只有 ainet / eNSP Pro 网页能改），所以 PC 只当观察点、不出「PC 地址配错」的题。
- eNSP Pro 的读配置 / 命令日志接口参数未确认（猜的 body 返回 405），学员在 eNSP Pro 自带终端里的操作 ainet 记录不到。
- 设备会自动保存 / 停机保存配置：练习中途服务异常退出、来不及复位时，改动会跟着设备重启留下来；下次开题时由干净基线比对发现并复位
  （复位不干净会拒绝开始，需要在 eNSP Pro 里删掉重建设备或手工撤销后重记基线）。

## 模拟环境注册与切换（已实现，2026-10-06）

模拟环境（eNSP / eNSP Pro，以后还会加）和 eNSP Pro 的沙箱都能在页面上切换，**不用重启服务**。详见 spec §5.2。目前按单人使用设计：任何已登录用户都能切换。

- **课程页**（2026-10-07 起，环境切换只在这里进行；操作页顶栏的可操作选择器已撤掉）：环境列显示各环境在线 / 离线（离线的置灰、注明「（离线）」，悬停看原因，如「本机不是 Windows…」「连不上 ensp-pro.example:8443…」）；点环境只是浏览题目，点「自由实验 / 开始练习 / 单题考试」时按题目准备环境（切环境、切沙箱、启动设备）。操作页发现切换代数变化时关闭旧终端标签、重画拓扑，Agent 对话里插一条「环境已切换」。练习或考试进行中切换接口 409，课程页提示先回操作页结束。档案、成绩单、模板管理页顶栏只读显示当前环境。
- **持久化**：当前环境与各环境的沙箱存在 `app_settings`（`lab:env`、`lab:sandbox:enspro`），重启后沿用。`AINET_LAB_PROVIDER` / `AINET_ENSPRO_SANDBOX` 只在库里没有记录时作缺省值——**切换过一次以后改环境变量不再生效**（设 `AINET_LAB_FORCE=1` 以环境变量为准，或换一个 `AINET_DB_PATH`、删这两个键）。启动日志写明当前环境来自哪里。
- **题目过滤**：练习列表只列当前环境平台的题（vrp5 题只在 eNSP、yunshan 题只在 eNSP Pro）；考试列表只列题目平台都匹配的模板；模板里有不匹配的题时开考被拦下并逐题说明。
- **前端模块** `frontend/labswitch.js`（`window.LabSwitch`）：`environments / sandboxes / switchTo`、`prepare(root, scenarioId, onProgress, token)` 按题目准备环境、`retryBusy(call, opts)` 开练 / 开考的 busy 重试、`readonly(el, opts)` 只读显示、`stageText / statusText` 显示文字；不依赖 app.js，课程页与操作页共用。

### 接入设置（已实现，2026-10-07，里程碑 6.12）

模拟环境原则上是独立节点（可能在局域网或别的网络里），每个环境的接入地址都能在页面上改，**不用重启服务**（spec §5.2「接入设置」）。

- **课程页**：环境列每个环境右上角「⚙」打开接入设置（离线时也能点；考试中不显示）：当前地址（可编辑）、来源（数据库 / 环境变量 / 缺省值）、缺省值、所需凭据有没有配置、「测试连接」「保存」「恢复缺省地址」（eNSP Pro 另有启动看门狗参数，见下）。离线的环境在「（离线 · 连不上 / TLS 握手失败 / 缺少凭据 …）」下面写出原因全文。
- **地址**：eNSP Pro 为服务 URL（http / https，如 `https://10.0.0.5:8443`，不能带用户名密码）；eNSP 为 console 主机（主机名或 IP，对应 `ENSP_HOST`）。存 `app_settings` 的 `lab:endpoint:enspro` / `lab:endpoint:ensp`；优先级与当前环境相同：库里的记录优先，`AINET_ENSPRO_URL` / `ENSP_HOST` 只在没有记录时生效，`AINET_LAB_FORCE=1` 时以环境变量为准并写回库。改当前环境的地址会断开所有终端并按新地址重建后端（不停设备）；练习 / 考试进行中不能改（409）。
- **凭据**只在 ainet 主机上：`ENSPRO_DEVICE_PASSWORD` 放 secrets.env（`backend/data/secrets.env` 或 `~/.config/ainet/secrets.env`）或环境变量；页面上不能填写，接口只报有无，不进库、不进日志。没配置时 eNSP Pro 显示「离线 · 缺少凭据」。
- **eNSP 在别的机器上**：console 端口远程能连，但运行设备 / 最近拓扑的检测靠本机文件和进程，ainet 不在 eNSP 主机上时显示「设备运行状态无法检测（需在 eNSP 主机上运行 ainet 或以后的节点代理）」并判离线（远程调试仍可设 `AINET_ENSP_CLIENT=1`）。节点代理未实现（spec §14 第 23 项）。
- **测试连接**与在线探测一样不登录 eNSP Pro（单会话）、不连 eNSP 设备 console 端口：eNSP Pro 只做 TCP + TLS 握手、看凭据有无；eNSP 只解析主机名、看本机进程。
- **启动看门狗参数按环境配置**（2026-10-07，里程碑 6.16，spec §5.2）：不同主机上的 eNSP Pro 冷启动快慢差很多（Linux KVM 约 100 秒，某台 Windows 笔记本 187~202 秒），所以 eNSP Pro 的气泡里另有「启动超时（秒）」（60~1800）与「卡住自动重启次数」（0~10），各自注明来源（数据库 / 环境变量 / 缺省值）、缺省值与范围，留空 = 回到缺省。「保存」先存改过的参数、再存地址；参数保存后**立即生效**（不重建后端、不断开终端，正在启动的设备按新值继续计时），改的不是当前环境则切过去时生效。存 `app_settings` 的 `lab:watch:enspro`（JSON，只含设置过的参数），优先级同地址：库里的记录 > `AINET_ENSPRO_START_TIMEOUT` 等环境变量 > 内置缺省，`AINET_LAB_FORCE=1` 时设了的环境变量优先并写回库。早期卡死检查（0 或 10~600 秒，且小于启动超时）与就绪探测上限（30~1800 秒）只经接口改。eNSP 经典版没有看门狗，不显示这些参数。练习 / 考试 / 准备环境进行中不能改（409）。
- **同时运行设备上限**（2026-10-07，里程碑 6.17）：性能有限的主机（如某台 Windows 笔记本最多稳定同时跑 3 台路由器 / 交换机）可在 eNSP Pro 的气泡「设备容量」一节填「同时运行设备上限（不含 PC，0 不限）」（0~64 的整数，缺省 0 = 不限；环境变量 `AINET_ENSPRO_MAX_DEVICES`）。它和看门狗参数存在同一个 `lab:watch:enspro` 里，来源 / 优先级 / 409 规则完全相同（参数名 `max_devices`），但不影响看门狗。
  - **计数口径**：题目 `required_devices` 里路由器 / 交换机 / 防火墙等非 PC 设备的台数。见过本题沙箱（当前环境 resolve 过）时以沙箱里的设备类型为准（eNSP Pro 的 deviceType：PC-CLIENT / SERVER / STA 等为 pc，Cloud 也不计）；没见过时按题目信息：`hosts` 里的终端主机不计、设备名以 PC / CLIENT / SERVER / STA / MCS / Cloud 开头的不计，其余（AR0、LSW0、R1、T1 …）都计。本题设备不在沙箱里 = 判断不了：不隐藏，日志记一次。按此口径，上限 3 时隐藏 NAT 2 道（easyip / nat-static）与 OSPF 3 道（各 4 台），`campus-pc1-gateway-ys`（AR0 / AR2 / LSW0 + PC1）照常列出。
  - **课程页**：`GET /api/scenarios?env=<环境>` 不列出超过该环境上限的题（同草稿题的隐藏方式，进行中的那道照列；`?all=1` 不过滤），附 `max_devices`、`capacity_hidden`（隐藏数）、`capacity_hidden_chapters`（按章节）；章节列下方提示「另有 N 道题需要更多设备（超过同时运行 M 台），本机已隐藏」，全被隐藏的章节显示「设备不足」，课题列在该章提示同一句。
  - **按 id 开始**：`POST /api/practice/start`、`/api/exam/start_single`、`/api/exam/start`（模板里有超上限的题）、`/api/lab/prepare` 超上限时 409，detail 如「「…」需要同时运行 4 台路由器 / 交换机（不含 PC），超过本机上限 3 台，无法开始」（不含 busy，课程页不会自动重试）。
  - **准备环境先停非本题设备（按整个 eNSP Pro 计数）**：要启动本题设备时，数「本题非 PC 设备 + 本沙箱里在运行（online / starting / initializing / error / start_failed）的非本题非 PC 设备 + **其他沙箱**里在运行的非 PC 设备」（其他沙箱经 `getByPage` 沙箱列表一次取全）；超过上限就先停掉全部这些非本题设备（其他沙箱的直接 batchStop；本沙箱的经看门狗停并断开终端；PC 不停），再启动本题设备。准备浮层的阶段文字写明停了哪些（如「同时运行设备上限 3 台（现在共 6 台）：先停止 沙箱 ainet_campus 的 R1、R2、R3、LSW0」），任务返回 `stopped_other`（`[{sandbox, sandbox_name, devices}]`）与 `stopped_extra`（本沙箱停掉的设备名）。本题设备都已在运行时不动。起因（真机）：服务刚启动时没有「上一个沙箱」记录，切换时的「停掉旧沙箱的设备」（`AINET_ENSPRO_STOP_OLD`）不会触发，别的沙箱留着 4 台在跑，T1 / T2 一直卡在启动。上限为 0 时不做这一步，仍只按 STOP_OLD 规则。

### 接口

| 接口 | 说明 |
|---|---|
| `GET /api/lab/environments[?refresh=1]` | `{current, label, platform, sandbox, sandbox_name, gen, switching, locked, can_switch, saved_sandboxes, environments:[{key, label, platform, power, sandboxes, available, reason, reason_code, checked_at, current, endpoint, secrets, runtime_local, warning?}]}`；`reason_code`：unreachable / tls / auth / missing_secret / other（在线为空；当前环境登录被拒 / 会话被挤掉时仍在线、`warning: true`）；`secrets` 只有 `{名: true/false}`；全部环境并行探测（每项 2 秒超时、缓存 15 秒，整体 3 秒内）；`locked` 非空 = 有练习 / 考试进行中 |
| `POST /api/lab/environment {key, sandbox?}` | 切换环境 / 沙箱（不给 sandbox 沿用该环境上次的沙箱）→ `{changed, key, sandbox, gen, switching, label, platform}`；练习 / 考试进行中、准备环境任务进行中、目标离线 409，沙箱不存在 404，未知 key 422 |
| `GET /api/lab/environments/{key}/endpoint` | 接入设置：`{environment, label, current, endpoint{kind, label, value, source, default, env_var, runtime_local}, secrets, runtime_local, locked, can_edit}`；未知 key 422 |
| `POST /api/lab/environments/{key}/endpoint {value}` | 改接入地址（空串 = 恢复缺省）→ `{changed, rebuilt, environment, endpoint, secrets, runtime_local, env}`；格式不对 422，练习 / 考试 / 准备环境任务进行中 409；改当前环境时断开终端、重建后端（`gen + 1`） |
| `GET /api/lab/environments/{key}/watchdog` | 启动看门狗参数：`{environment, label, current, watchdog{values, params[{name, label, unit, value, source, default, builtin, env_var, min, max, integer, zero_off}]}, locked, can_edit}`；没有看门狗的环境（eNSP）400，未知 key 422。环境列表与 `…/endpoint` 里每个环境也带 `watchdog`（没有为 null） |
| `POST /api/lab/environments/{key}/watchdog {start_timeout?, start_retries?, early_check?, probe_timeout?, max_devices?}` | 只改给出的参数，`null` = 回到缺省 → `{changed, applied, environment, watchdog}`（`applied` = 已对看门狗立即生效）；范围不对 / 早期检查不小于启动超时 / 没给参数 422，练习 / 考试 / 准备环境进行中 409 |
| `POST /api/lab/environments/{key}/endpoint/test {value?}` | 测试连接（不保存）：`{available, reason, reason_code, value, checked_at, runtime_local?}`；不登录、不连设备 console |
| `GET /api/lab/sandboxes` | `{environment, current, sandboxes:[{id, name, devices, running, current}]}`；当前环境没有沙箱时 409 |
| `POST /api/lab/prepare {scenario_id, environment?}` | 按题目准备环境（202）：切到题目平台的环境（给了 `environment` 就用它，平台不符 409；不给时当前环境平台相同就用当前环境）、按题目 topology 名找同名沙箱并切过去、启动本题没运行的设备、等到运行中。返回任务 `{id, scenario, title, environment, sandbox, stage, message, devices{名:状态}, done, ok, error, started, ended, elapsed_s}`，stage：`queued → switch / sandbox → start → wait →（卡住时）restart → probe → done | error`（`probe` = 接口已报运行中、等设备不再报 busy）；另有 `stopped_old`（切走时停掉的旧沙箱设备）与 `notes`（各设备进度文字）。同步返回的错误只有：题目不存在 404；练习 / 考试进行中、目标离线、已有任务在跑 409。任务内的失败（找不到同名沙箱、同名多个、设备超时等）看任务的 `ok=false`、`error`、`error_status`（404 / 409 / 504 …）。`ok=true` 表示设备已通过就绪探测（关掉探测时只表示接口「运行中」）；开练时设备仍在 initializing 返回 409（detail 含 initializing / busy），自动重试 |
| `GET /api/lab/prepare` | `{job}`：最近一次任务（轮询进度） |
| `GET /api/scenarios?env=<key>` / `?all=1` | 按环境平台列题（不触发切换），附 `env, platform, current_env, other_platform, empty_reason`，以及 `max_devices, capacity_hidden, capacity_hidden_chapters`（同时运行设备上限，里程碑 6.17） |
| `GET /api/lab` | 原有字段 + `key, sandbox, gen, switching`，以及 `source, watch, idle_stop, stop_old, last_stopped_old`（见「设备启动可靠性」） |

`/api/topology/status|current` 多一个 `env {key, sandbox, gen, label, platform}`；`gen` 变了说明环境被切换过。

### 已知局限

- 「运行中」只是 eNSP Pro 接口状态，ainet 用就绪探测（initializing）兜住 busy；关掉探测时开练仍可能 409 busy。
- 沙箱匹配按题目 `topology` 与沙箱名相同，同名沙箱多个时拒绝自动选择。
- eNSP 经典版只看本机进程，Linux 上开发连远程 Windows 时要设 `AINET_ENSP_CLIENT=1` 才算在线；console 主机不是本机时设备运行状态无法检测（节点代理未实现）。
- 接入设置没有在 Windows 部署机、eNSP Pro 虚机上实测（pytest 只用本机回环端口和 TEST-NET 地址）。
- `auth`（登录被拒 / 会话冲突）只对当前环境可知：探测不登录 eNSP Pro，非当前环境的账号问题要切过去才看得到。

### 接入新的模拟环境

实现 `LabProvider`（+ `DeviceConnector`）→ 写一个不登录、不连 console 的快速探测 → 在 `lab.py` 末尾 `register(LabEnv(key, label, platform, make, probe, power=…, sandboxes=…))` 一行 → 场景 JSON 用对应的 `platform`。其余代码不用改（spec §5.2）。

### 在页面上添加环境（多台 eNSP Pro，里程碑 6.19）

模拟环境是独立节点：可能有多台 eNSP Pro（局域网或公网），每台各有地址、设备 console 密码、启动超时、设备上限。

- **课程页**：环境列标题右侧「+」（考试中不显示）→ 对话框：类型（目前只有 eNSP Pro）、显示名、地址、设备 console 密码（可选，password 框）、启动超时、设备上限、「测试连接」「添加」。添加时先做与「测试连接」相同的探测（只连端口 / TLS 握手，不登录）；连不上也保存，弹出警告。添加后环境列立即出现并做在线检测，**不切换当前环境**。
- **⚙ 接入设置**：eNSP Pro 类的环境多一个「设备 console 密码」框（只写不读，留空 = 不改）与「清除密码」；用户添加的环境另可改显示名、「删除此环境」（当前环境不能删，先切走）；它们没有缺省地址，所以没有「恢复缺省地址」、地址不能清空。
- **存储**：实例列表存 `app_settings` 的 `lab:envs`（`[{key, type, label}]`），key 形如 `enspro-2`、`enspro-3`，编号只增不复用（`lab:envs:seq`）；每个实例沿用按环境配置的键：`lab:endpoint:<key>`、`lab:watch:<key>`（启动超时 / 重启次数 / 设备上限…，**不读全局 `AINET_ENSPRO_*` 环境变量**，缺省 = 内置值）、`lab:sandbox:<key>`；干净基线 `baseline:<key>:<沙箱>:<设备>`。启动时最先注册这些实例，库里记的当前环境是 `enspro-N` 也能恢复。内置的 `enspro` 不变（`lab:env`、`lab:*:enspro`、`baseline:enspro:*` 照旧）。
- **设备 console 密码按实例**：读 `ENSPRO_DEVICE_PASSWORD__<KEY>`（key 大写、`-` 换成 `_`，如 `ENSPRO_DEVICE_PASSWORD__ENSPRO_2`；环境变量 → secrets 文件）；**只有内置 enspro** 再回落到 `ENSPRO_DEVICE_PASSWORD`。页面上填的密码写进 `ai.key_file()`（`AINET_SECRETS_FILE` 或 `backend/data/secrets.env`）的对应行：原子写入、保留其他行（与 AI 设置写 Key 同一个函数）。接口只回显 `device_password{name, configured, source}`，不回显值、不进日志。内置 enspro 在页面上填密码写的是 `…__ENSPRO`，优先于 `ENSPRO_DEVICE_PASSWORD`。
- **题目**：按 `platform` 归属，YunShan 题对所有 eNSP Pro 实例都可见（`GET /api/scenarios?env=enspro-2`）；设备上限按所浏览的实例算。课程页「开始练习」把正在浏览的环境传给 `POST /api/lab/prepare {scenario_id, environment}`；不传时当前环境平台相同就用当前环境。
- **删除**：只能删用户添加的（内置 400）；是当前环境、练习 / 考试 / 准备环境进行中 409。清理 `lab:endpoint|watch|sandbox:<key>`、`baseline:<key>:*`、`ai.key_file()` 里的密码行（环境变量和另一个候选 secrets 文件不动，返回 `secret_in_env` 提示）。

| 接口 | 说明 |
|---|---|
| `POST /api/lab/environments {type, label, url, device_password?, start_timeout?, start_retries?, early_check?, probe_timeout?, max_devices?}` | 添加 → `{key, environment{同环境列表的一项}, probe, warning}`；类型 / 显示名（空、重名）/ 地址 / 参数 / 密码格式不对 422；考试进行中 409 |
| `POST /api/lab/environments/probe {type, url}` | 添加前测试连接（不保存、不登录、不查密码） |
| `PATCH`（或 `POST`）`/api/lab/environments/{key} {label?, device_password?, clear_password?}` | 改显示名（内置 422）、设置 / 清除设备密码 → `{changed[], environment}`；不存在 404；考试进行中 409 |
| `DELETE /api/lab/environments/{key}` | 删除 → `{deleted, label, baselines_removed, secret_removed, secret_in_env}` |

环境列表另带 `types[]`（可添加的类型与参数范围），每个环境另带 `type`、`custom`、`device_password`（eNSP 经典版为 null）。测试：`backend/tests/test_lab_instances.py`（假 eNSP Pro，探测只连本机回环临时端口）。

### 自动识别 https（里程碑 6.20）

eNSP Pro 都是 https（自签证书）。为避免把地址误写成 `http://`（如 `http://ensp-pro.example:8443`，该端口只有 HTTPS）而连不上：

- **可以不写协议**：`ensp-pro.example:8443`、`host:8443` 都行，按 `https://` 处理；只写主机名时补缺省端口 `:8443`。
- **添加、添加前测试连接、改地址、测试连接**时，地址是 `http://` 或没写协议就先试 TLS 握手（不登录）：通 → 存 / 测 `https://…`，并提示「已自动改为 https」（接口返回 `normalized_from` = 原输入、`notice`）；TLS 不通而明文 HTTP 有响应 → 保留 http，给警告；都连不上 → 按输入保存（没写协议按 https）并给出连不上的原因。写了 `https://` 的不探测。保存的始终是规范化后的完整 URL。
- **页面**：添加对话框与 ⚙ 气泡里，地址框失焦或点「测试连接」后输入框会换成规范化后的 URL，下面一行小字说明。
- **已存的 http 地址不自动迁移**；⚙ 打开时如果当前地址是 http、该端口其实是 https，会显示「建议改为 https」与「一键改为 https」（接口：`POST …/endpoint/test` 不给 value 时返回 `suggest`）。
- 测试：`backend/tests/test_lab_scheme.py`（本机回环上的假 TLS / 明文 HTTP 服务，不连外网）。

### 测试与真机 e2e

- `backend/tests/test_lab_endpoint.py`（接入设置）：URL / 主机校验与规范化（拒绝带用户名密码、查询串）、来源与优先级（库里的记录 / `AINET_LAB_FORCE` / 环境变量 / 缺省）、离线原因分类（本机回环临时端口：连不上、TLS 握手失败、缺凭据、在线）、两元组探测兼容、eNSP 远程主机 `runtime_local=false` 与解析失败、凭据只报有无（假密码不出现在接口返回与日志里）、eNSP Pro 登录被拒 / 会话被挤掉 → `auth`、改当前环境的地址重建后端并通知终端（不停旧设备、登出超时不卡）、非当前环境只记库、练习 / 考试 / 准备任务进行中 409、接口读取 / 修改 / 测试连接 / 未登录 401。
- `backend/tests/test_labenv.py`：注册表、探测超时与缓存、全部环境 3 秒内、切换顺序（先断 hub、再登出旧后端、再建新后端）/ 409 / 并发串行 / 等待正在开始的练习、准备环境任务进行中拒绝显式切换、持久化与重启恢复、未登录 401、沙箱列表（假 eNSP Pro 的 `getByPage`）、旧终端收到「已切换」并断开、题目按平台过滤、考试模板平台不匹配被拦、Agent 快照跟着切换、基线键随沙箱变、按题目准备环境（切沙箱 + 启动设备）。
- 真机 + 无头浏览器：`backend/tests/e2e/labswitch_e2e.py`（服务以 `AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=2` 和临时 `AINET_DB_PATH` 起好；会启动 T1、T2、LSW0，结束时停掉）。2026-10-06 结果（本机 eNSP Pro，最终一轮 `9 pass / 0 fail`）：eNSP 经典版按探测置灰「本机不是 Windows…」；按题目准备环境冷启动 T1/T2 114~126 秒后 display version 正常；
  沙箱下拉 2 ↔ 3 切换后旧终端关闭、拓扑重画；LSW0 display version 正常；练习中下拉禁用、切换接口 409；非管理员账号同样可切换；档案页只读显示环境。
  发现 eNSP Pro 偶发：其他沙箱有设备运行时启动 LSW0，会一直停在状态 6（3 次里 2 次），停止再启动后恢复——按题目准备环境对此自动重启一次（spec §14 第 21 项）；
  刚冷启动的 YunShan 设备头一两分钟改配置会报 `Error: The system is busy in building configuration`，开练失败（已还原），稍后重试即可。

## 设备启动可靠性（已实现，2026-10-07，eNSP Pro）

详见 spec §10.5（设计）与 §14 第 21 项（卡在启动中的根因）。实现在 `backend/labpower.py`。

- **启动看门狗**：拓扑图「启动 / 重启」、`/api/lab/devices/start|reboot`、按题目准备环境的启动全部经它。启动超过启动超时（缺省 150 秒，**按环境在课程页「⚙ 接入设置」里改**，见「接入设置」）仍在「启动系统」（或接口报错误状态）→ 自动断开终端、停止再启动，最多 3 次；仍不行 → 设备状态 `start_failed`（拓扑图显示「启动失败」，悬停 / `start_errors` 看原因），再点「▶ 重新启动」= 先停止再启动。最后一轮判卡住时不停设备：只是慢的设备之后接口报运行中、通过就绪探测就**恢复为运行中**（`start_errors` 里不再有它）；对这种已在运行的 start_failed 设备点「启动」不会停止重启（返回 `recovering`）。按题目准备环境遇到 start_failed 仍立即失败（504），设备恢复后重新点「开始练习」即可。重启一律是「停止 → 等停机间隔 → 启动」，不调 batchReboot。
- **早期卡死检查**：设备在「启动系统」满 40 秒（及 80 秒）时读一次设备日志，已陷入板卡复位循环（卡住的确定特征）就不等 150 秒立即重启。
- **错开启动**：多台设备一起启动时每 5 秒发一台（排队中的显示「排队启动」）；同一台设备距上次停机不到 10 秒的启动推迟发出（安全余量，不是根治）。
- **就绪探测**：接口报运行中后先是「系统初始化中」（`initializing`），看门狗经隐藏事务执行只读的 `display current-configuration | include sysname`（刚冷启动时它和改配置在同一时间窗报 busy，实测约 30 秒），不再报 busy 才变成运行中。这时开练 / 开考返回 409（detail 含 initializing / busy），课程页自动重试；终端可以打开。按题目准备环境多一个 `probe` 阶段。
- **切换时停掉旧沙箱的设备**：切换沙箱 / 环境、按题目准备环境切到新沙箱后，旧沙箱里运行着的设备自动停止，接口返回 `stopped_old`（准备环境任务里是 `stopped_old` 列表）。只有没有练习 / 考试进行中才能切换，所以停机时保存的是复位后的干净配置。
- **空闲自动停机**：当前沙箱的设备 30 分钟没有任何活动（终端输入 / 输出、练习、考试、Agent 命令、启停）就自动停掉；停前 60 秒在打开着的终端里提示（按任意键取消）。练习 / 考试进行中不停。
- **服务重启**：未完成的启动跟踪存在库里（`app_settings` 的 `lab:power`），重启后接着跟踪。
- **环境变量优先**：`AINET_LAB_FORCE=1` 时启动以 `AINET_LAB_PROVIDER` / `AINET_ENSPRO_SANDBOX` 为准（覆盖并写回库）；启动日志写明当前环境来自库还是环境变量。

| 环境变量 | 缺省 | 说明 |
|---|---|---|
| `AINET_ENSPRO_START_TIMEOUT` | `150` | 启动超过这么多秒仍在启动阶段 = 卡住。**只是缺省值**：课程页「⚙ 接入设置」按环境改过以后以库里的 `lab:watch:enspro` 为准（下同，`AINET_LAB_FORCE=1` 时环境变量优先） |
| `AINET_ENSPRO_START_RETRIES` | `3` | 卡住后自动停止再启动的次数，用完标 start_failed；按环境可改（同上） |
| `AINET_ENSPRO_MAX_DEVICES` | `0` | 同时运行设备上限（不含 PC，0 = 不限，0~64）：超上限的题不列出、按 id 开始 409、准备环境先停非本题设备；按环境可改（同上，里程碑 6.17） |
| `AINET_AUTO_LOGIN` | 空 | 自动登录的登录名（如 `admin`），空 = 关闭；开启后能访问端口的任何人都以该账号操作，只适合单人本机 / 局域网（见「自动登录」） |
| `AINET_ENSPRO_START_STAGGER` | `5` | 多台启动时相邻两台的间隔（秒），0 = 一次批量 |
| `AINET_ENSPRO_RESTART_GAP` | `10` | 同一台设备停机后至少隔这么多秒才发启动 |
| `AINET_ENSPRO_EARLY_CHECK` | `40` | 启动这么多秒（及 2 倍时）查设备日志判断是否已卡死，0 关闭；按环境可改（接口） |
| `AINET_ENSPRO_WATCH_POLL` | `5` | 看门狗轮询间隔（秒） |
| `AINET_ENSPRO_PROBE` / `_PROBE_INTERVAL` / `_PROBE_TIMEOUT` | `1` / `10` / `300` | 就绪探测开关、间隔、上限（超过按卡住处理）；上限按环境可改（接口） |
| `AINET_ENSPRO_STOP_OLD` | `1` | 切换后停掉旧沙箱的设备 |
| `AINET_ENSPRO_IDLE_STOP_MIN` / `_IDLE_WARN` / `_IDLE_CHECK` | `30` / `60` / `15` | 空闲停机分钟数（0 关闭）、提前通知秒数、检查间隔秒数 |
| `AINET_LAB_FORCE` | `0` | 启动时以环境变量为准 |

接口新增（只增不改）：设备状态 `initializing`、`start_failed`；`/api/topology/current` 多 `start_errors`；`POST /api/lab/environment` 多 `stopped_old`；准备环境任务多 `probe` 阶段、`stopped_old`、`notes`；`GET /api/lab` 多 `source`、`watch`（看门狗参数、跟踪中的设备、最近事件）、`idle_stop`、`stop_old`、`last_stopped_old`；`/api/lab/devices/start` 多 `started` / `queued` / `restarted`，`reboot` 多 `start_after_s`。前端只在 `app.js` 的状态映射里加了这两个状态（文字、按钮、可开终端、轮询）。

### 已知局限

- 卡住的直接原因是 LSW 镜像里 sfed 启动 1 秒内以 17 退出；2026-10-07 下午经 SSH 只读复现 14 次（卡 6 次）确认：**不是上一实例在宿主上的残留、与停机间隔无关**，容器自己的 /dev/shm 与 IPC 每次全新，17 具体指向什么没能确定（判断为镜像内部启动竞态），容器内重启不生效、vpnf 不处理这类复位原因码，所以不会自己恢复（spec §14 第 21 项）。ainet 侧没有可行的预防办法，看门狗只能「发现后重启」：按约 43% 的卡住率，`AINET_ENSPRO_START_RETRIES=3`（2026-10-07 用户决定）时一次准备环境最终 start_failed 的概率约 1/30，再点一次「开始练习 / 启动」即先停后启、计数清零。对虚机的诊断 / 缓解建议见 spec §14 第 21 项（都要改虚机，由用户决定）。
- 早期卡死检查依赖 eNSP Pro 的「下载日志」接口和容器日志里的文件名，换版本可能失效（失效时退回 150 秒超时）。
- 就绪探测只对 ainet 发起（或准备环境接手）的启动做；在 eNSP Pro 网页上启动的设备不会显示 initializing。
- 空闲计时在服务内存里，服务重启后从零开始。

### 测试

- `tests/test_labpower.py`（假 eNSP Pro，`FakeRest.stuck` 模拟卡住 / 错误状态）：超时自动重启后就绪、重启后仍卡住标 start_failed 且原因可读、手动再启动先停后启、错误状态立即重启、错开启动、停机间隔与重启不调 batchReboot、服务重启后接着跟踪（卡住的照样重启、排队的照样发出、换了沙箱不恢复）、别处停掉不再跟踪、就绪探测 initializing → online、探测超时按卡住处理、真实 ConsoleHub 上探测走隐藏事务（学员终端看不到、没有命令记录、能识别 busy）、开练 409（initializing / busy）与准备环境的 probe 阶段、切换停旧沙箱（及开关）、准备环境返回 stopped_old、启停接口经看门狗、空闲停机（假时钟：预告、停机、练习中不停、活动重新计时、异步日志不算、启动中不停、0 关闭）、`AINET_LAB_FORCE`。
- `tests/test_lab_watchdog.py`（看门狗参数按环境配置，里程碑 6.16）：参数校验与范围（含整数、0 = 关闭、早期检查小于启动超时）、来源与优先级（库里的记录 / 环境变量 / 缺省、不合法记录忽略、`AINET_LAB_FORCE` 写回库）、启动时装载、修改对当前环境立即生效与 null 回到缺省、非当前环境只记库而切过去时生效（同环境换沙箱不重设）、409 / 422 / 400、准备环境等待上限随参数变化、进行中的启动按新值继续计时、超时误判的 start_failed 事后恢复为 online（就绪探测通过、`start_errors` 清掉）、对已在运行的 start_failed 设备点启动不再停止重启、接口（读取 / 修改 / 未登录 401 / 练习中 409 / 环境列表带 watchdog）。
- `tests/test_capacity_autologin.py`（里程碑 6.17）：计数口径（hosts / PC 类名字 / Cloud 不计，沙箱设备类型优先，判断不了返回 None 不隐藏）、`max_devices` 校验与来源（库 / 环境变量 / `AINET_LAB_FORCE`）、上限 3 时恰好隐藏 NAT 2 道 + OSPF 3 道（按章节计数、进行中的照列、`?all=1` 不过滤、改回 0 全部列出）、开练 / 单题考试 / 模板考试 / 准备环境 409、准备环境先停非本题设备（停在启动之前、PC 不停）与不超上限时不停、其他沙箱在运行的设备计入并先停（上限 0 或不超时不动）；自动登录（缺省关闭 401、开启后 /login 303、自动建学员且密码不进日志、已有 admin 不改密码、登出后又自动登录、显式登录其他学员照常、无 cookie 的 WebSocket 也认）。
- 其余测试里这些时间类开关缺省关闭（`tests/conftest.py`：一次批量启动、不等停机间隔、不探测、不空闲停机），保持原有用例行为。
- 真机：`python tests/e2e/reliability_e2e.py probe|switch|stuck|gap|idle|stopall [base_url]`（模式说明见脚本开头）。2026-10-07 本机 eNSP Pro 结果：
  - probe 5/0：按题目准备环境冷启动 T1、T2（错开 5 秒）→ 120 秒接口运行中 → `probe` 阶段，探测先报「设备忙：Error: The system is busy in building configuration」，33~42 秒后通过 → 任务完成后立即开练 200（不再 409）→ 结束练习复位干净。
  - switch 4/0：T1、T2 运行中切到沙箱 3 → `stopped_old` = T1、T2，8 秒后沙箱 2 运行数 0。
  - stuck 3/0：LSW0 就绪后点重启 → 新实例卡在「启动系统」→ 看门狗在启动 44 秒时从设备日志认出板卡复位循环、自动停止再启动 → 60 秒运行中 → 探测 45 秒后就绪（重启到可用共 175 秒）。
  - idle 5/0（`AINET_ENSPRO_IDLE_STOP_MIN=2`，同时验证 `AINET_LAB_FORCE=1`：启动日志「来源：环境变量（AINET_LAB_FORCE=1，已覆盖并写回数据库）」）：终端开着不操作，60 秒时收到空闲预告、120 秒时收到停机通知并断开，LSW0 被停止。
  - 结束后三个沙箱运行数均为 0，服务退出时已登出 eNSP Pro。

## 工作台布局与 Agent 面板（已实现，2026-10-06）

布局按已确认的布局稿 `docs/design/ui-layout.html`（spec §16）：

- **顶栏**（2026-10-07 起改为单层，见文末「课程选择页、操作页单层顶栏、PC 设置气泡」）：模式「实验 / 练习 / 考试」、「模式标签 + 题名 ▾」（气泡里是环境、拓扑、本题设备、章节、工单）、练习 = 用时、判定进度、立即检查、结束练习；考试 = 顶栏变红，总倒计时、题号进度、本题计时、提示、提交本题、下一题、交卷（二次确认），工单在题名气泡里；学员名 ▾（我的档案 / 切换学员 / 登出）、「退出到课程」。
- **左**拓扑、**中**终端、**右**面板：两侧都能收起成 48px 图标栏、拖动分隔条调宽（双击恢复默认），宽度与折叠状态存 localStorage。右侧面板：自由实验只有 Agent；练习有「工单详情」（场景列表 / 工单 / 总结与评分卡）和「Agent 导师」（底部三级提示按钮，3 级扣分）；考试作答中不出现。面板收起时有新回复，图标上出圆点。窗口窄于 1100px 时右侧面板变成抽屉。
- 档案、成绩单、模板管理是独立页面，用同一套顶栏样式（导航：课程 / 操作页 / 档案）。

### Agent 面板

- 在右侧输入框提问（Enter 发送，Shift+Enter 换行），回答逐字流式显示，支持代码块、列表等基本 Markdown。
- Agent 能看到当前拓扑和设备状态、你最近在终端里敲的命令和输出、练习工单（只有你也能看到的部分）；需要时会在设备上执行只读命令（只允许 `display` / `ping`，每次提问最多 3 条），对话里显示「已在 AR0 执行（只读）：display …」，可展开看输出。这些命令在后台执行，你的终端里看不到，也不计入你的操作记录；你正在终端里输入半行时不会执行。
- **练习中是导师模式**：只引导思路、不直接给答案；提问不扣分，但导师的回复如果被系统判定含答案级内容（修复命令、故障配置值等），按使用第 3 级提示记一次扣分，并在对话里告诉你。练习中的对话会存档，并作为过程分的参考。
- **考试中没有 Agent**（后端也拒绝：`/api/agent/*` 返回 403）。
- 模型沿用 DeepSeek 配置（`deepseek-flash`，Key 见「AI 配置」）。没有 Key、超时、限流时对话里给出提示，可点「重试」。相关环境变量：`AINET_AI_AGENT`（0 关闭）、`AINET_AI_AGENT_TIMEOUT`（两块之间的超时，缺省 30 秒）、`AINET_AI_AGENT_DEADLINE`（整轮上限，缺省 120 秒）、`AINET_AI_AGENT_MAX_TOKENS`、`AINET_AGENT_TOOL_TIMEOUT`（单条只读命令超时，缺省 15 秒）。

### API

- `POST /api/agent/chat {message, history}` → `text/event-stream`，事件 `meta / delta / tool / penalty / error / done`（见 `backend/agent.py` 文件头）。需登录；考试进行中 403；同一学员上一个问题还没答完时 429。
- `GET /api/agent/history`：当前场景（练习中为本次练习，否则自由实验）的对话记录。

### 测试

- `pytest tests/test_agent.py`：只读命令白名单（含各种绕过写法）、遍历全部场景的快照答案保护、真实 ConsoleHub + 假 VRP 上隐藏执行不进学员终端与命令采集、学员半行时 busy、流式事件顺序、工具上限与非法参数、AI 无 Key / 超时的友好提示、练习导师模式泄露扣分（只扣一次）与提问不扣分、对话落库、评分引用导师对话、考试 403、迁移幂等。AI 一律用假客户端。
- 浏览器（模拟设备，不连 eNSP / eNSP Pro）：
  - `python tests/e2e/ui_e2e.py <截图目录> [--ai]`：4 个场景的布局、折叠 / 展开、拖宽与刷新保持、Agent 对话与只读命令显示、练习导师模式与三级提示、考试时面板消失且接口 403、窄屏抽屉、独立页面顶栏、全程无 JS 异常（`--ai` 用本机 DeepSeek Key，少量调用）。
  - `python tests/e2e/practice_fake_e2e.py <截图目录>`：练习 e2e（`practice_e2e.py`）的模拟设备版。其中步骤 5 / G 经终端核对「unassigned」行，假设备不输出这类行，必然不通过（基线同样如此），脚本另从假设备配置树核对复位结果（F）。
  - `python tests/e2e/fake_lab.py [端口] [--ai]`：只起模拟设备版环境（3 台假 VRP + 临时库），手动调试用。
  - 端口：`AINET_E2E_HTTP_PORT`、`AINET_E2E_DEBUG_PORT`。

### 已知局限

- 泄露检查是字面匹配（与第 1、2 级提示同一套）：导师引用 display 里看到的注入值也会被判为泄露；命令缩写匹配不到。判定在回复流完之后做，所以学员已经看到内容（因此记扣分）。
- 快照里的「最近终端命令」只保存在服务内存里（重启后清空）。
- 只读命令的白名单只认完整的 `display` / `ping`；`tracert`、`display` 缩写不在白名单内。

## 课程选择页、操作页单层顶栏、PC 设置气泡（已实现，2026-10-07，里程碑 6.9）

布局见 `docs/design/ui-layout.html` 的 ⑤ 课程选择、⑥ 单层顶栏（spec §16.1、§16.3、§16.4）。单人使用：任何已登录用户都能切环境。

### 页面与路由

- 登录后默认进 **课程页** `/courses`（未登录跳登录页）；选好课题后进 **操作页**（工作台）`/?mode=lab|practice|exam&s=<场景 id>`。
- 档案 `/profile`、成绩单 `/exam/report`、模板管理 `/exam/templates` 仍是独立页面，顶栏导航改为「课程 / 操作页 / 档案」。
- 前端文件：`frontend/courses.html` + `courses.js`（课程页）、`labswitch.js`（`window.LabSwitch`：环境探测、按题目准备环境、`retryBusy`；不再提供顶栏上的可操作选择器）、`app.js` / `exam.js` / `agent.js`（操作页）。样式都在 `app.css`。

### 课程页（四列）

- **模拟环境**：打开页面时 `GET /api/lab/environments` 探测全部环境；在线的可点，离线的置灰、写「（离线）」（悬停看原因）；「↻ 重新检测环境」带 `refresh=1`。点环境只是浏览（`GET /api/scenarios?env=<key>`），**不切换**。
- **章节**：`scenarios/chapters.json` 的 L01~L12 从易到难，显示题数；没有题的灰色「待补充」；最下面「考试」列出题目全部属于该环境平台的多题考试模板。
- **课题**：题名、一句话症状、难度（★）、预计用时、完成状态（档案里的「已通过 ✓ / 未通过 / 未开始」）。
- **预览**：标题、章节与难度、工单、知识点标签、拓扑预览（当前已加载同一拓扑时画全图、本题设备橙框、未启动虚线；否则只示意本题设备）、将要准备的环境；按钮「自由实验」「开始练习」「单题考试」（模板为「开始考试」）。
- **进入**：eNSP Pro 调 `POST /api/lab/prepare` 并每秒轮询，进度显示在居中的**准备浮层**（见下节）；eNSP 经典版不能远程打开拓扑，所需拓扑没在运行时提示「请在 eNSP 中打开 xxx 拓扑并启动设备」，每 5 秒重新检测，`/api/scenarios` 报告本题就绪才放行。开练 / 开考返回 409 且含 busy（设备刚冷启动）时每 20 秒自动重试、最多 5 次、显示倒计时；都可以「取消」。
- 环境、章节、课题的选择存 `localStorage`（`ainet.course.*`，读写 try/catch）。有进行中的练习 / 考试时顶部提示「回到操作页」，进入按钮禁用。

### 准备浮层（里程碑 6.13，2026-10-07）

点「自由实验 / 开始练习 / 单题考试 / 开始考试」后整页遮罩 + 居中卡片（spec §16.3「准备浮层」），不再是预览区里的一行小字：

- 标题「正在准备练习环境」（实验 / 考试同理）+ 题名 · 环境 · 沙箱；多题考试模板写「第 i / n 题」，进度按题数折算。
- **步骤条**：切换环境 → 查找沙箱 → 启动设备 → 等待运行 → 确认就绪 → 完成（取自任务的 `steps`）。进行中的圆点脉冲，已完成打勾，没走到的「已跳过」（已在目标环境、设备已在运行），出错红色；看门狗自动重启时「等待运行」变警示色。eNSP 经典版只有切换环境 → 核对设备 → 完成。
- **整体进度条**与「已用 m:ss」「预计还需约 N 分钟」：后端的 `progress` / `estimate`，前端只增不减、每秒缓慢推进，完成前不满格；最后 5% 留给开练 / 开考。
- **设备卡片**：未启动 / 启动中 / 系统初始化 / 就绪 / 重启中 / 启动失败（图标 + 颜色 + 文字，状态变化有过渡），下面一行小字是进度（如「40% 9s（启动系统）」「第 1 次自动重启」）。
- 卡住时警示框「T2 启动卡住，正在自动重启（第 1 次，最多 3 次）」；底部轮播提示（首次启动约 2~3 分钟可先读工单、勿在 eNSP Pro 网页端登录、卡住自动重启最多 N 次等），并展示本题工单。
- 出错：浮层里显示后端原因原文 +「重试」「返回课程」；完成：「环境已就绪」停留约 0.8 秒后进入操作页。经典版「请在 eNSP 中打开拓扑」时进度条改为不确定样式，只有「取消」。
- 可访问性：`role=dialog` + `aria-modal`、背景 `inert`、阶段变化 `aria-live` 播报（不播报计时）、进度条 `role=progressbar` + `aria-valuenow`；`prefers-reduced-motion` 关闭动画；颜色用 `app.css` 的 `:root` 变量；390px 宽不溢出。
- 后端（`GET /api/lab/prepare` 的 `job`，旧字段不变）：`env_label`、`power`、`sandbox_name`、`retries`、`steps [{key, label, state}]`、`device_info {设备: {status, note, kind, restarting, restarts, phase, round_s}}`、`progress`（0~1）、`estimate {remaining_s, lo_s, hi_s, max_s}`。预计用时的经验值在 `labenv.py`（`BOOT_S` / `PROBE_S` / `CROWD_S`：AR 120 秒、LSW 65 秒、PC 20 秒，多台同时启动每多一台 +10 秒，就绪探测 35 秒，给 0.8~1.3 倍区间），只用于显示，不影响超时。`LabSwitch.stageText` 新增 `check`「核对设备」，`probe` 改称「确认设备就绪」。
- 测试：`pytest tests/test_labenv.py -k prepare_view`（步骤条与跳过、device_info、progress / estimate、重启次数、经典版步骤与出错）；浏览器 e2e `python tests/e2e/prepare_overlay_e2e.py <截图目录>`（假 eNSP Pro，T2 模拟启动卡住：`fake_enspro_lab(stuck={7: 2})` 停止 2 次后才正常启动），**6 pass / 0 fail**：O1 准备中（启动设备）→ O2 不自动重试时 T2 启动失败的错误态 → O3「重试」后看门狗自动重启 T2 → O4 完成态进入练习 → O5 390px 不溢出 → 无 JS 异常；每个状态在 1300px、390px 各截一张图。`course_e2e.py` 同步后 **17 pass / 0 fail**（C4 设备状态文字改为「就绪」；K1 按 6.12 起的「（离线 · 原因）」放宽）。没有在真机上跑。

### 操作页单层顶栏

- 一行：`ainet`、模式（实验 / 练习 / 考试）、「模式标签 + 题名 ▾」、计时 / 判定 / 操作按钮、学员名 ▾、「退出到课程」。窄屏（< 1000px）自动换行。
- **题名气泡**：模拟环境、拓扑（沙箱）与运行台数、本题设备、章节与难度、工单摘要，「↻ 刷新设备状态」「查看完整工单」（练习中打开右侧「工单详情」）。环境和拓扑不再常驻顶栏，题名按钮上只留一个状态点；eNSP Pro 会话被挤掉时顶栏出现「重新登录 eNSP Pro」。
- **考试**：顶栏变红，题名处「第 n/N 题 ▾」，气泡只放工单（题号、工单、可访问设备、提交记录、已用提示；模板隐藏拓扑时不显示环境和拓扑）。
- **学员菜单**：我的档案 / 切换学员 / 登出（顶栏不再有「档案」模式）。
- **模式切换（同一道题）**：实验 → 练习直接开始本题练习；练习 → 实验要确认，按放弃结束并复位；实验 / 练习 → 考试（本题的单题考试）要确认（练习中会先按放弃结束）；考试中其他模式与「退出到课程」不可用，只能交卷。操作页内不能换题，换题 / 换环境 / 换拓扑都要「退出到课程」（练习中确认后按放弃结束并复位）。
- **单题考试**（`POST /api/exam/start_single {scenario_id}`）：复用考试引擎的临时单题模板（`template_id` 为空，名称「单题考试 · 题名」），默认规则：15 分钟、无提示、无 Agent、每题可重试 1 次、拓扑可见、交卷后出成绩单与命令回放。题目没能开始（设备 busy、故障没生效）时整场回滚并 409，前端按 busy 重试。有练习进行中 409。

### PC 设置气泡

- 点拓扑里的 PC 弹出：显示设备状态，可设 IP、掩码、网关、DNS，「应用」后等 PC 命令行恢复（eNSP Pro 的 `ipv4.status` 回到 Ready）再提示「已生效」，期间提示稍候；「打开终端」。路由器 / 交换机点击照旧打开终端。
- eNSP 经典版：只提示「请在 eNSP 软件中双击 PC 设置」，输入框禁用（接口 400）。
- 接口：`GET /api/lab/devices/{console}/host`（当前地址、状态、`dns_supported`）、`POST /api/lab/devices/{console}/host {ip, mask, gateway, dns?}`（IP / 掩码 / 网关 / DNS 格式校验 422；不是 PC 400；不在拓扑 404；没运行 409）。
- 练习 / 考试中：只能改本题开放的 PC（否则 403）；每次修改写一条伪命令进操作记录 `[PC 配置] PC1 ip=… mask=… gw=… dns=…`（输出为「已生效：…」或「设置失败：…」），命令回放、评分都看得到；开着自动判定时改完排一次判定。结束时 PC 地址随复位还原：场景 `hosts` 与拓扑里是 PC 的本题设备开练时按地址配置拍快照，复位设回快照（服务重启后照样）。
- DNS：属性名 `ipv4.config.dns1`（`enspro.HOST_DNS_ATTR`，2026-10-07 真机核实：虚机里 PC 设备模板的 `ipv4` 组是 `ipv4.config.dns1` / `dns2`，设了之后 PC 的 `/etc/resolv.conf` 出现 nameserver，设空即清空）。从没设过时接口里没有这个键，按空处理；开练快照总带 DNS，学员改了 DNS 结束时会设回去。

### 课程数据

- `scenarios/chapters.json`：`{chapters: [{id, title, order}]}`，L01 链路与接口 … L12 综合排障（草案）。加载场景时跳过这个文件。
- 场景新增可选字段 `chapter`（须在 chapters.json 中）、`difficulty`（1~5）、`est_minutes`、`summary`（一句话，不换行、≤ 80 字）、`tags`；只能放面向学员的内容，加载时与第 1、2 级静态提示同一套检查（注入 / 修复命令、只在故障配置里出现的 IP、判定命令），参数化场景每个抽样都查，也不能用参数占位符。现有 11 道题都已补上。
- `GET /api/scenarios` 另带 `chapters` 与每题 `progress`（`passed / failed / none`，进行中考试的题不算）。

### 测试与浏览器验证

- `pytest tests/test_courses.py`：章节文件与场景字段、字段校验（未知章节、难度、泄露、占位符）、遍历全部场景 × 4 个种子的不泄露检查、`/api/scenarios` 的章节与完成状态、课程页需登录；单题考试（流程、成绩单、开题失败回滚、模板考试行为不变）；模式切换的后端约束；PC 设置（开练快照、伪命令记录、失败也记录、不开放的 PC 403、复位还原、服务重启后还原、DNS 快照与兼容旧快照）；假 eNSP Pro 上的接口（未登录 401、校验 422、非 PC 400、404、已停止 409、DNS 属性）；经典版 400。`test_exam.py` 的考试中答案保护遍历加了新接口。
- `python tests/e2e/course_e2e.py <截图目录>`（只用假环境，不连 eNSP / eNSP Pro）：阶段一用 `tests/e2e/fake_enspro_lab.py`（同进程的假 REST + 假 STOMP console，设备启动有延时，另加一道 e2e 专用题 `e2e-pc-t1t2`），阶段二用 `fake_lab.py`（3 台假 VRP）。覆盖登录进课程页、环境在线 / 离线、章节 / 课题 / 预览联动与 localStorage、开始练习的准备进度、单层顶栏与题名气泡、学员菜单、PC 气泡（练习 / 考试 / 隐藏拓扑的考试）、模式切换确认框、单题考试到成绩单回放、退出到课程、窄屏、经典版的「请在 eNSP 中打开拓扑」与 PC 气泡、全程无 JS 异常。
- `fake_lab.py` / `exam_fake_e2e.py` 把 `AINET_ENSPRO_URL` 指向本机关闭的端口：课程页探测全部环境时不会连到真 eNSP Pro 虚机。
- 原有 `ui_e2e.py`、`exam_fake_e2e.py`、`practice_fake_e2e.py` 已同步新 DOM（课题从 `?s=` 带入、多题考试从课程页开）；真机脚本 `practice_e2e.py`、`exam_e2e.py`、`reset_e2e.py`、`ai_e2e.py`、`enspro_e2e.py`、`enspro_campus_e2e.py`、`labswitch_e2e.py` 同步了选择器，未在真机上重跑；课程页全流程的真机验证见下节 `course_real_e2e.py`。

### 真机联调（里程碑 6.11，2026-10-07）

`tests/e2e/course_real_e2e.py`：本机 eNSP Pro（沙箱 ainet_test、ainet_campus）+ 无头浏览器，服务以 `AINET_LAB_PROVIDER=enspro`、临时库、
`AINET_AI_SCORING=0 AINET_AI_HINTS=0 AINET_AI_AGENT=0` 起好后运行（脚本要读同一个库，`AINET_DB_PATH` 也传给它）：

```
AINET_DB_PATH=<服务的库> python tests/e2e/course_real_e2e.py http://127.0.0.1:8799 <截图目录> [vlan]
```

- 全流程 **12 pass / 0 fail**（约 7.5 分钟）：R0 预置（沙箱 3 启动 PC1）→ R1 课程页（eNSP 离线、eNSP Pro 在线）→ R2「T1 与 T2 互联不通」开始练习：
  排队 → 启动设备（错开 5 秒）→ 等待运行（106~110 秒）→ 检测终端（41 秒后通过）→ 进入练习，旧沙箱的 PC1 被自动停掉 → R3 单层顶栏 56px、
  气泡「eNSP Pro · YunShan」「沙箱 ainet_test · 运行 2/2 台」、T2 终端可用 → R4 修复 → 自动通关 → 退出到课程 → reset_ok、卡片「已通过 ✓」 →
  R5 单题考试（「单题考试 · T1 与 T2 互联不通 · YunShan」）提交通过 → 交卷 → 成绩单带 YunShan 标签、复位成功 → R6 VLAN 题：切到沙箱 3
  （停掉 T1、T2）→ AR0 / LSW0 / PC1 就绪 → R7 PC 设置气泡：读出 10.0.10.10/24、网关 10.0.10.1、DNS 空 → 改成 10.0.10.66、DNS 8.8.4.4 →
  「已生效」，接口读回一致，PC1 终端 `ifconfig` 显示新地址与 `DNS server: 8.8.4.4`（`ipconfig` 不存在）→ R8 修 LSW0 → 通关 → 退出 →
  reset_ok、PC1 回到开练快照（地址与 DNS 都为空）、操作记录有「[PC 配置] PC1 ip=10.0.10.66 mask=24 gw=10.0.10.1 dns=8.8.4.4」→
  R9 档案页平台标签 → R10 三个沙箱运行数 0 → X 无 JS 异常。
- `vlan` 模式只跑 R0、R1、R6~R10，用来多碰几次 LSW0 卡住：两次，各 8 pass / 0 fail；三次运行里 LSW0 都没卡（接口 55~65 秒报运行中、就绪探测再 40~60 秒），课程流程里没能自然碰到看门狗的自动重启——这条路径由 reliability_e2e.py 的 stuck 模式（2026-10-07 上午 3/0）、test_labpower.py 与 §14 第 21 项的 14 次 SSH 复现覆盖。
- 收尾后用 eNSP Pro 接口逐台核对三个沙箱全部「已停止」，服务以 SIGINT 退出、已登出 eNSP Pro。
- 改了平台标签 / 确认框之后复跑假环境的浏览器 e2e：`course_e2e.py` 17 pass / 0 fail，`exam_fake_e2e.py` 10 pass / 0 fail（含模板管理页、成绩单、档案页）。
- 「复位一致」的依据是 ainet 自己的 `reset_ok`（结束时整机复位后与开练前快照逐行比对），没有另做 `display current-configuration` 的独立比对。
- 联调中修的问题：PC 的 DNS 属性名（`ipv4.config.dns1`）与「没设过 DNS」的快照（否则复位撤不掉学员改的 DNS）；已通关后「退出到课程」的确认框文字；
  题名去掉平台后缀、改用平台标签。

### 已知局限

- 课程页的拓扑预览只有在当前环境已加载同一拓扑（沙箱）时才画全图；否则只示意本题设备（未加载的沙箱不切换就拿不到拓扑）。
- 取消「准备环境」只停止页面等待，后台已发出的设备启动会继续完成。
- 操作页的课题来自 URL（`?s=`）；直接打开 `/` 且没有进行中的练习时是「自由实验」，练习 / 考试按钮不可用（回课程页选题）。

## 2026-10-14 运行可靠性与自动检查

- Windows 启动脚本统一经 `backend/launcher.py`，读取 Git 忽略的 `backend/data/local.env`；默认仅监听本机，免登录需显式配置。停止脚本只匹配本 checkout 的 AInet 进程。
- eNSP Pro 手动重登清除旧 SSO cookie，验证模拟器服务并重建终端连接；旧请求返回的会话失效不覆盖新登录。console 自动重登期间的输入先缓存，成功后按顺序发送。
- 各环境 / 沙箱保留独立看门狗、空闲计时与后台 console，停机时刻按环境隔离。同一环境的沙箱共享 REST 客户端，服务退出时统一释放。修改地址或删除环境会关闭该环境所有后台任务。
- `lab:power:<环境>:<沙箱>` 保存启动状态；启动恢复全部有效上下文，并兼容导入旧的 `lab:power`。原有 `AINET_ENSPRO_STOP_OLD` 停旧沙箱规则保留。
- 每道题开题时检查设备容量，覆盖考试下一题和重试；eNSP 经典版在建立练习记录、拍快照和注入故障前检查 console 提示符。
- 准备环境首次遇到 `start_failed` 已有恢复路径，本轮补普通失败重新启动与慢设备已自行上线两个回归用例。
- `.github/workflows/ci.yml` 使用 Linux / Windows 与 Python 3.11 / 3.13，运行假设备 pytest、场景与 Python 语法校验、前端独立及内嵌脚本语法检查。真实设备 e2e 单独执行。

在根目录运行 `python scripts/check_project.py` 与 `node scripts/check_frontend.mjs`；单元 / 集成测试在 backend 运行 `python -m pytest tests --ignore=tests/e2e --ignore=tests/manual -q`。本轮冻结的是六个直接依赖，平台相关的传递依赖由 pip 安装。
