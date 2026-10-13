# 局域网运维实操练习/考试系统 — 需求规格 (spec)

> 2026-10-14 实现修正：启动脚本默认仅本机访问，部署配置移至 Git 忽略文件；后台运行状态改为按环境 / 沙箱隔离，同环境沙箱共享 REST 会话；每题开始前检查设备容量与经典版 console 提示符。后续历史里程碑描述保留原验收背景，现行约定以 docs/HANDOFF.md 为准。

> 单一事实源。创建 2026-10-03，最近更新 2026-10-07（课程选择页、操作页单层顶栏、PC 设置气泡 §16.3 / §16.4，出题规则 §6.2，里程碑 6.9；此前 2026-10-06：模拟环境注册与运行时切换 §5.2；第二种实验后端 eNSP Pro + YunShan 适配；eNSP Pro 替代验证与题库迁移，见 `docs/enspro-replacement.md`）。每条需求实现后在本文件勾选。

## 1. 项目目标

面向运维人员的局域网实操**练习跟练 + 考试**系统，依托 eNSP 真实设备环境：
- 学习/练习常见企业组网的交换机/路由器命令行操作；
- 练习企业组网故障排查与消障；
- 接入 AI 服务（DeepSeek V4 Flash，见 §4）：根据描述生成故障题库、控制环境注入故障、接收学员在虚拟环境的操作并分析合理性、对整体排障打分、或分步引导教学。

## 2. 使用形态

- **同一时刻单人使用**；可切换登录不同学生，载入各自档案。
- 前后端均运行在 eNSP 执行机（Windows）本机。
- 开发阶段可经 SSH 直接在该 Windows 机上开发调试。

## 3. 环境事实（已实测）

- 主机 Windows 10 build 26200，Ryzen 7 7840U 16 线程 / 29GB，硬件虚拟化开启。
- eNSP：界面版本 V1.3.00.100（ensp-coach 文档记载），`eNSP_Client.exe` 文件版本 1.2.0.500、构建于 2019-03；VRP AR1200 V200R003C00。VirtualBox 7.2.20，经 `C:\ProgramData\ensp-vbox-shim` 兼容层运行。AR 路由器在本机冷启动约 3~5 分钟。
- 设备 console 为 telnet，端口取自 `.topo` 中设备的 `com_port`（2000 起），监听 `0.0.0.0`（属主 eNSP_Client），本机 localhost 直连。
- eNSP 以 Windows 用户 `15111` 运行（桌面在 `D:\Users\15111\Desktop`）；SSH 开发账号是 `xixi`。服务由学员双击 `start.bat` 启动，与 eNSP 同属一个用户、同一个桌面会话。
- **当前运行拓扑的判定依据**：`.topo` 中每台设备的 `<dev id>` 是 GUID，不同文件之间不重复（直接复制的副本除外）。eNSP 启动设备时，会在 `%LOCALAPPDATA%\eNSP\VBoxServer\devices\<GUID>\` 下建虚机副本。设备运行时，其 `Logs\VBox.log` 被虚机进程占用，停止后释放；设备停止后目录保留，但在 eNSP 中关闭该拓扑时目录会被删除（2026-10-04 实测：切换到 Campus_Network 后，2-1 OSPF 三台设备的目录已不存在）。由此可以从「正在运行的设备」唯一确定 eNSP 打开的是哪个文件。S3700/S5700 交换机不走 VirtualBox：每台对应一个 `eNSP_Switch.exe` 进程，命令行形如 `"...\eNSP_Switch.exe" "sim" "system_mac=…" "LSW0"`，最后一个参数是设备名；老格式 `.topo` 中交换机没有 `system_mac` 属性，所以按设备名匹配。**eNSP 以管理员权限运行**，而 start.bat 启动的服务是普通权限，读不到这些命令行（SSH 的 xixi 账号带管理员令牌所以能读，测试时容易被掩盖）。不受权限影响的信号：运行中的交换机占用 `%LOCALAPPDATA%\eNSP\VBoxServer\workspaces\<GUID>\slot17\flash.dat`，按 GUID 识别，作为交换机的主判定方法。
- **eNSP 最近打开的拓扑记录**：`%LOCALAPPDATA%\eNSP\tmp\topo.his`，GBK 编码，用 `|` 分隔的 .topo 绝对路径，第一个是最近打开的文件。它只是历史记录，不代表文件仍处于打开状态，需要用运行中的设备核对。其中可能含用户保存在别处的拓扑（如 `D:\Users\15111\Desktop\ensp-lab\…`），这些文件也要纳入扫描。
- 当前拓扑判定 = 以 topo.his 第一个文件为候选，用运行证据（VBox 设备 GUID ∪ 交换机进程设备名）核对；不符时退回 GUID 索引搜索。注册表和配置文件里没有「当前打开文件」的记录。
- `.topo` 的文件关联是 `eNSP_Client.exe "%1"`。eNSP 已打开拓扑时再打开另一个文件，会先弹「是否保存」，点过之后才载入新拓扑。
- eNSP 没有启动、停止设备的命令行接口，只能在界面上操作。
- PC、STA、Cloud、FRSW 等设备没有虚机，也没有 console（`com_port=0`），由 eNSP 主程序模拟，外部无法配置，也无法读取它们的运行状态。`.topo` 的 `settings` 属性里只存有上次保存的配置（如 `-simpc_ip`、`-simpc_mask`、`-simpc_gateway`）。（补充：PC/Server 实为 eNSP 插件目录下的独立进程 `eNSP_PCClient.exe`，能否从外部读取其状态尚未研究，见 §14 第 12 项）
- 网络：Windows 只通过 **Wi-Fi**（WLAN）联网，没有在用的有线网卡；另有 VirtualBox Host-Only 网卡（192.168.56.1/24）。Windows 已开启虚拟化平台（HypervisorPresent=True），未安装 WSL。VirtualBox 7.2 借助 `C:\ProgramData\ensp-vbox-shim` 兼容层支撑 eNSP，比较脆弱，改动网卡或虚拟化配置前要先验证。
- 开发用 Linux 主机（192.168.31.69）：Docker 29，32 核、124GB 内存，可承载 containerlab 或 EVE-NG；containerlab 未安装。
- `.topo` 格式要点：XML 声明写的是 `encoding="UNICODE"`，实际是 ASCII 或 GBK；`<shapes>` 中 type 0 为直线、1 为矩形、2 为椭圆，`filloption=1` 为不透明实心填充、0 为只画边框；颜色是 Windows COLORREF（低字节为 R）。画布为白底，每 100 单位一条网格线。
- examples 自带 OSPF/BGP 排障、Campus_Network、HCNA 等拓扑，可作题库底座。
- 全链路 PoC 已通过：远程连 console 跑命令 → 注入 `shutdown` 故障并读回（设备实时吐 OSPF 邻居 Down 日志）→ `undo shutdown` 恢复。
- **同一 console 端口的多个 TCP 连接共享输入输出**（2026-10-04 实测，像共用一根串口）：连接 A、B 同时连 2000，B 发 `display clock`，A 也收到完整回显，反之亦然。所以系统另开连接发命令学员会看到，学员敲到一半时字符还会混在一起；处理方式见 §5 ConsoleHub。
- console 刚断开的会话，接下来几分钟内重新连接可能收不到任何输出；上一个会话断开时若停在 `---- More ----` 上，残留的分页输出会在下一个连接上吐出来。原则：尽量少开、少断 console 连接。
- console 行为细节（2026-10-04 实测）：提示符 `<名字>` 为用户视图、`[名字...]` 为系统或子视图；长输出分页为 `  ---- More ----`，按空格继续，VRP 随后用 `ESC[42D` + 空格 + `ESC[42D` 擦掉分页符；分页时按其他键会结束输出并吞掉这个字符；console 上不能用 `screen-length 0 temporary`（报 "must be VTY"）；配置中 `[V200R003C00]` 这一行形似提示符；异步日志（`Oct  4 2026 11:06:25-08:00 Huawei %%01IFNET/4/LINK_STATE...`）可能插进命令回显中间，VRP 打印日志后会重印提示符和已输入的半截命令；单发 `\r` 即可提交命令（`\r\n` 会多出一个空提示符）；`?` 会把已输入的内容留在行缓冲里。
- **`.topo` 连线的接口序号按真机接口顺序解释**（2026-10-04 实测，§14 第 10 项已解决）：`interfacePair` 的 `srcIndex` / `tarIndex` 是设备接口按 `display interface brief` 顺序的序号。旧格式的 `Campus_Network.topo` 把 AR1220 主板写成 Ethernet×2 + GE×4 + Serial×4（共 10 个），按文件顺序会推出 AR0–AR2 是 `Ethernet0/0/1 <-> Ethernet0/0/0`；真机 AR1220 是 GE0/0/0-1 + Ethernet0/0/0-7（也是 10 个，Ethernet 为二层口，不能配 IP）。锚点：AR0–AR2 = `GE0/0/1 <-> GE0/0/0`、AR0–LSW0 = `GE0/0/0 <-> GE0/0/1`（两条都在两端配同网段地址后 ping 通）；AR0 的两个 GE 口物理 up、Ethernet 全 down，AR2 只有 GE0/0/0 up（对端未启动），LSW0（S5700，GE0/0/1-24）只有 GE0/0/1 up。`topology._interfaces` 对已实测型号（AR1220、S5700）的主板按真机排列解释序号，新格式文件（2-1 OSPF）结果不变。
- **S5700 的 console 细节**（2026-10-04 实测）：改配置后约 7 秒打印不带 `%%` 的日志 `... Huawei DS/4/DATASYNC_CFGCHANGE:OID ...`，而且打印日志后**不重印提示符**（AR 会重印）；停在 `---- More ----` 时也会插进这条日志，设备仍在等空格。`vlan batch`、`stp mode`、`undo interface Vlanif` 会先输出 `Info: This operation may take a few seconds...` 再出结果；`undo vlan batch`、`undo ospf` 会问 `[Y/N]`，S5700 回显 `y` 较慢。ConsoleHub 的日志识别、分页判断、命令完成判断都按这些处理（§5.1）。
- **console 日志的更多细节**（2026-10-04 里程碑 4、5 集成联调实测）：DATASYNC 日志 AR1220 也会打印；日志行末尾有时**不换行**，学员接着敲的字符直接回显在日志行末尾（S5700 `sysname` 改名后实测）；停在 `---- More ----` 时插进的日志之后，设备把擦除序列 `ESC[42D 空格 ESC[42D` 和下一行内容直接接在日志行末尾。AR1220 上反复在接口挂 / 撤 `traffic-filter`、或在已应用的 ACL 里改规则（当天累计约 6 轮）后，任何接口再挂 `traffic-filter` 都报 `Error:Add rule failed, slot 0, policy ?sa ...`（配置里看不出来，属于模拟器资源问题），只能在 eNSP 中重启该设备恢复。
- **eNSP Pro（第二种实验后端，2026-10-06 实测，详见 §10.4）**：跑在开发用 Linux 主机的 KVM 虚机里，`https://ensp-pro.example:8443`（自签证书，只有本机能访问）。离线模式免密码登录；**同一账号只能有一个会话**，别处登录会把当前会话挤掉（接口返回 code `50070014` / `50010005`）。设备 console 走 STOMP over WebSocket（`/web.socket/websocket`），设备 MESSAGE 帧有时一个字符一帧。设备启停有 REST 接口（`batchStart` / `batchStop`），两台 AR 同时冷启动约 110 秒；停止约 1 秒生效。沙箱 `ainet_test`（id 2）有两台 ENSP-AR：T1（deviceId 6）、T2（7），接口 `MEth0/0/0` + `GE0/0/0-9`；2026-10-06 经接口新建连线 T1 GE0/0/1 <-> T2 GE0/0/1（link id 5）。接口返回的设备顺序不固定（两次调用 T1、T2 先后不同），`deviceStartProgress` 在启动全程都是 100（不能当进度；2026-10-07 发现 `progress` 字段在启动中会变：AR 0→3→14→50→71→80、LSW 0→3→12→50，ainet 改用它），启动时状态码 2 → 6 → 4。
- **YunShan OS（eNSP Pro 的 ENSP-AR = AR6700，Huawei YunShan OS 1.25.1.1 / V600R025C10SPC100，2026-10-06 实测）**：
  - 全新设备首次登录 console：`Please configure the login password (8-16)` → `Enter Password:` → `Confirm Password:`；之后登录只问 `Password:`。console 的 WebSocket 关掉后设备侧会话不登出（再连回车直接是提示符）；`user-interface con 0` 为 `authentication-mode password`，配置里没有 idle-timeout 行，但**空闲约 5 分钟会登出**（替代验证实测：输出 `User interface con0 is available` + 空行 + `Please Press ENTER.` + 两个空行，最后一行为空；之后只认单独的回车，带命令的回车没反应）。
  - **单阶段生效**：改完立即生效，系统视图提示符就是 `[HUAWEI]` / `[HUAWEI-GE0/0/1]`，没有 `[~` / `[*`；`return` 不问确认；`commit` 在系统视图可以敲但什么也不做。`quit` 逐级退出（与 VRP5 相同，用户视图 quit 会登出 console）；`undo sysname` 回到 `HUAWEI`（大写，VRP5 为 `Huawei`），配置里显式显示 `sysname HUAWEI`。
  - `display current-configuration`：开头若干 `!` 注释行（`!Software Version …`、`!Last configuration was updated at …`、`!Last configuration was saved at …`、各组件版本），其中 updated / saved 两行每次改配置 / 自动保存都会变；之后同 VRP 的 `#` 分段、缩进子行（块内还有缩进的 ` #`）；`ip address x 24` 显示为 `255.255.255.0`，静态路由同样显示点分掩码；分页 `  ---- More ----` 之后用 `ESC[16D` + 16 个空格 + `ESC[16D` 擦除（VRP5 为 42 列），按 `q` 结束分页。默认配置含 `configuration file auto-save`：**设备会自动保存配置**（实测改配置后 5 分钟（替代验证时逐条核对：09:24:08 改 → 09:29:08 存，最初记的「约 2 分钟」不准）、以及停机前都出现新的 `Last configuration was saved … by _SYSTEM_`，详见下面「配置持久化」），所以未撤销的改动可能在设备重启后保留。
  - `ping -c 2 x`：与 VRP5 同格式（`2 packet(s) received`、`0.00% packet loss`），多一行 `round-trip min/avg/max` 与一个只有空格的空行；首包常因 ARP 丢失（`1 packet(s) received`），现有判定正则 `\b[12] packet\(s\) received` 不用改。
  - 日志格式 `Oct  6 2026 07:05:12 HUAWEI %%01UPDATE/4/DOWNLOAD_FAIL(l):…`（不带时区偏移），现有日志识别可用；接口 up / shutdown 时 console 上没有看到日志。
  - 撤销写法（复位实测接受，均无 `[Y/N]`）：`undo ip address`、`undo ip address 10.0.12.2 24`、`undo shutdown`、`undo description`、`undo interface LoopBack7`、`undo ip route-static 10.8.0.0 255.255.0.0 10.0.87.1`、`sysname HUAWEI`。
- **eNSP Pro 替代验证补充（2026-10-06，沙箱 `ainet_campus` id 3，详见 `docs/enspro-replacement.md`）**：
  - **接口名只认缩写**：AR6700 `GE0/0/x`、S6700 `GE1/0/x`；`interface GigabitEthernet0/0/0` 报 `Error: Wrong parameter found at '^' position.`（YunShan 题一律写缩写）。
  - **ENSP-LSW = S6700（YunShan OS 1.25.1.1 / V600R025C10SPC500）**：单阶段生效（`[HUAWEI]`，`commit` 无作用，无 `[~` / `[*`）；端口缺省 link-type `desirable`、PVID 1、trunk `1-4094`（配置里不显示，`undo port link-type` 回到缺省）；`vlan batch`、`port link-type access|trunk`、`port default vlan`、`port trunk allow-pass vlan`、`port trunk pvid vlan`、`interface Vlanif`、`stp mode`、静态路由的配置与 undo 写法同 VRP5，`undo vlan batch` 问 `[Y/N]` 并连带删 vlan 视图；`display vlan`（VID / UT / TG 段 + 属性段）、`display port vlan`（Port / Link Type / PVID / Trunk VLAN List）格式见替代报告；分页同 AR（`ESC[16D`）；配置里没有 auto-save、没有保存的配置文件，但停机时照样保存。
  - **PC-CLIENT**：console 无登录，提示符 `PC> `（受限 bash：ping / traceroute（全屏刷新）/ arp / ifconfig / ssh / curl，没有配地址的命令）；ping 为 Linux 格式 `2 packets transmitted, 2 received, 0% packet loss`。地址只能经设备属性接口 `GET /service/ensp/device/property/refreshAttrs?deviceId=N` / `POST …/updateAttrs?deviceId=N`（`ipv4.type` / `ipv4.config.ip` / `ipv4.config.ipmask` / `ipv4.config.gateway`）配置，运行中立即生效；改完 `ipv4.status` 先 `PendingUpdate`，此时 PC 命令行重启、吞掉输入。
  - **配置持久化**：设备从 `flash:/1.cfg` 启动（停止 → 启动后改动仍在）；有未保存改动时停机以 `by _SYSTEM_` 保存（AR、LSW 都会；`undo configuration file auto-save` 只挡定时保存，挡不住停机保存）；保存行为不稳定（以「自动保存关闭」启动、运行中再打开的 R3 既没定时保存、多台同时停机时也没保存，再启动带回旧配置）。**结论：不能依赖设备上的保存状态**，ainet 改为每题开始前与「干净基线」比对、不一致先复位（§10.4）。
  - **其他**：`batchReboot` 返回 HTTP 405（只能停止再启动）；EXTERNAL-HUB 启动后一直停在状态 2（分配虚机）超过 17 分钟，不可用；AR6700 删 ACL / traffic-filter 后数秒会在后面某条命令的提示符之后异步打印 `Error: Failed to apply the traffic-filter … (slot=0)` 且不重印提示符；被别处登录挤掉时 eNSP Pro 会关掉该会话的 console WebSocket；多台同时冷启动后，接口状态已是运行中，console 仍可能几分钟完全无输出（8 台同时启动时 AR0 约 5 分钟、AR2 / LSW0 约 11 分钟），单台重启 1 分钟内可登录；系统资源接口 `GET /service/ensp/system/summary`（cpu / memory / disk 比例）与 `…/detail`（top / free 原文）。
  - **资源**（虚机 16 vCPU / 32GB）：设备类型标称 AR / LSW 1 CPU、1.7GB，PC 0.2 CPU、0.25GB；8 台（7 AR + LSW，含 PC 共 8 个）同时冷启动 207 秒全部就绪（LSW 88 秒，AR 128~207 秒），宿主机 qemu 峰值约 10.6 核；全开空闲时虚机内存 21.1 / 32.1GB（72%）、CPU 9~21%，全停时内存 6%；平均每台约 2.4GB。
  - **题库扩充实测（2026-10-07，`docs/question-bank-plan.md` §6）**：AR6700 接口下**没有** `nat outbound` / `nat static`（接口视图 nat 只有 enable、gratuitous-arp），源 NAT 用 `nat-policy` 规则（`action source-nat easy-ip`），一对一发布用 `nat server <名> global … inside …`，两者都要出接口 `nat enable`，不需要 license 与安全区域，改完几秒后生效；`dhcp enable` 是 AR6700 出厂配置；子接口 `dot1q termination vid` 不需要 `arp broadcast enable`；`ospf timer hello 20` 时 Dead 随之为 80；`display acl all` 显示 `destination-port eq telnet`；对端 shutdown 后本端 down/down，管理关闭显示 `*down`；`ping -c 2 -a <源> <目的>`、`ping <静态主机名>`、`display current-configuration | include …` 可用。
- **eNSP 的 VRP 没有整份替换 running-config 的命令**（2026-10-04 实测）：AR1220 V200R003 上没有 `configuration replace` / `rollback configuration` / `commit` / `load`；S5700 只有 `clear configuration interface`（只清单个接口）。`display current-configuration` 连续抓取结果逐字一致（含 `local-user` 密文），可作快照。

## 4. 技术栈

- 后端：**Python + FastAPI**，`uvicorn` 运行。
- 设备连接：**netmiko(`huawei_vrp`) 或原生 asyncio socket**（含 telnet IAC 处理）。**禁用 pexpect**（Windows 无 pty）。
- 前端：**Web 应用，原生 JS + xterm.js，无构建步骤**，FastAPI 一并服务；学员浏览器打开 localhost 使用。终端经 WebSocket 双向转发 console。第三方库一律本地化到 `frontend/vendor/`，**禁止 CDN**（eNSP 机器外网不可靠，cdnjs 曾 404 致终端失效）。原计划 Vue 3(Vite) 暂不引入，以免 Windows 端需要 Node 构建链；页面复杂度上来再评估。
- 数据：**SQLite**（单机文件库，零配置）。
- AI：**DeepSeek V4 Flash**（OpenAI 兼容接口，用 openai Python SDK 指向 DeepSeek base_url；API Key 不入库不入码）。里程碑 5 落地（2026-10-04 实测）：
  - 模型名 `deepseek-flash`（`/models` 另有 `deepseek-v4-pro`），`base_url=https://api.deepseek.com`；模型名、base_url、超时、是否思考、max_tokens 均在 `config.py`，可用环境变量覆盖。
  - 默认先思考再回答，思考也占 `max_tokens`（太小时 content 为空）；请求体 `thinking: {type: disabled}` 关闭思考。评分默认开启思考（异步、求稳），提示默认关闭（求快，总时长 ≤15 秒）。
  - `response_format={"type": "json_object"}` 实测支持；同时用提示词约束格式并稳健解析，解析失败重试一次（关闭思考），仍失败则降级。
  - API Key 读取顺序：环境变量 `DEEPSEEK_API_KEY` → `AINET_SECRETS_FILE` 指向的文件 → `backend/data/secrets.env`（Windows 部署）→ `~/.config/ainet/secrets.env`；日志与报错最多显示前 3 位加 `***`。
- 部署：SSH 在 Windows 建 Python venv 直接开发；或本机 Linux 开发后 git 推送。

- **AI 接口设置**（2026-10-08，里程碑 6.18）：学员菜单「AI 设置」（考试中不显示）可改 API URL、API Key、模型，评分 / 提示 / Agent 共用。URL、模型存 `app_settings`（`ai:base_url` / `ai:model`）；Key 写 ainet 主机的 secrets 文件（`ai.key_file()`：`AINET_SECRETS_FILE` 或 `backend/data/secrets.env`，更新或追加 `DEEPSEEK_API_KEY` 行、保留其他行、原子写入），库里只记 `ai:key_page`，不入库、不进日志，接口只回显有无、来源与末 4 位掩码（`sk-***abcd`）。优先级：页面保存的值 > 环境变量 > secrets 文件 / 内置缺省，`AINET_LAB_FORCE=1` 时设了的环境变量优先。客户端按（Key, URL, 模型）缓存，保存后下一次请求即生效、已发出的请求不受影响，练习 / 考试中也可改。`GET|POST /api/ai/settings`、`POST /api/ai/settings/test`（`GET /models`，没有该接口时发 1 token 补全；不保存）。

## 5. 架构与抽象（为 EVE-NG 预留）

分层：`Web 前端(xterm.js)` ←WS→ `FastAPI 后端` → `DeviceConnector` → 模拟器设备 console；后端旁挂 `LabProvider`(实验起停/复位)、`AI 编排`、`SQLite`、`场景库`。

两个抽象接口对上层屏蔽模拟器差异：
- **DeviceConnector**：`connect / send / read / close`。eNSP 驱动连本机 2000+；EVE-NG 驱动连 EVE 服务器 telnet 端口。
- **LabProvider**：`start / stop / reset / running`。eNSP 实现 = 人工启动设备 + 配置脚本复位 + 按设备 GUID 读取运行状态（§3）；EVE-NG 实现 = 走其官方 HTTP API 自动起停。

上层（出题、终端、评分、教学）只对接口编程，未来接 EVE-NG 不改业务代码。

**已实现（2026-10-06，`backend/lab.py`）**：`LabProvider` 统一「当前拓扑、设备状态、终端白名单、设备连接、设备启停」，`AINET_LAB_PROVIDER=ensp|enspro` 作缺省值（缺省 ensp），运行时可在页面上切换（§5.2）。`EnspProvider` = eNSP 经典版（`topology.py` + `ensp_runtime.py` + telnet，行为不变）；`EnsproProvider` = eNSP Pro（§10.4）。**统一设备标识**：拓扑里每台设备有整数 `console` 键（能开终端时 > 0）——eNSP 经典版 = `com_port`，eNSP Pro = `deviceId`；ConsoleHub 的通道键、`/ws/console/{console}`、终端白名单、练习 / 考试里的「设备名 -> 端口」映射都用它，上层代码与前端只把 `com_port` 换成了 `console`（同一时刻只有一个后端在用，两种整数不会混在一起）。`resolve()` 的返回结构与原 `topology.resolve_running()` 相同，另加 `lab`（kind / platform / power / label）。

### 5.1 ConsoleHub（`backend/console_hub.py`，里程碑 3）
同一 console 端口的连接共享输入输出、频繁连接 / 断开有害（§3），所以 DeviceConnector 之上加一层 ConsoleHub：
- **每个 console 端口只保持一条设备连接**，按需懒加载、长期持有。浏览器终端标签是订阅者：设备输出扇出给所有订阅者，订阅者输入写回设备；新订阅者先收到最近可见输出的回放。只在新建设备连接时发一次回车唤醒，已有连接上新增订阅者不发送任何字节。
- 关标签只退订，不断开设备连接；所有订阅者退出且空闲超过 10 分钟（可配）才关闭；断线按需重连，同一端口两次连接至少间隔 3 秒（可配）。
- **隐藏事务** `run_hidden(port, commands, kind)`：注入 / 还原（config）与判定（check）都走它。拿端口锁 → 前提检查：学员当前输入行为空（hub 跟踪自上次回车以来的可见输入，处理退格；方向键、Tab 视为非空），设备最后一行是提示符（不是 `[Y/N]`、`Password:`、`---- More ----`），config 事务还要求在用户视图 `<...>`（在 `[...]` 里返回「学员处于系统视图，暂不注入」）；不满足返回「暂不可检查」由调用方重试 → 暂停转发 → 逐条发命令读到提示符（命令完成 = 回显按字符顺序出现 + 停在期望提示符上 + 短暂静默；遇分页发空格并去掉分页符；每条命令超时，超时发 Ctrl-C 收尾）→ 恢复转发。
- 事务期间学员的按键**暂存**，结束后按原顺序写回设备（键盘预输入；不丢弃，免得学员以为键盘失灵）。捕获期间的异步日志行在事务结束后照常转发并重印提示符；其余内容（回显、输出、提示符）不发给学员，也不进回放。config 事务遇 `Error:` 中止并 `return` 回用户视图（还原时尽力而为、继续执行）；结束时确保回到开始时的用户视图；check 事务只用 display / ping / tracert，要求结束时的提示符与开始时完全一致。
- 学员每次回车提交的命令（取设备回显的那一行，含 Tab 补全 / 历史命令的实际文本）连同时间、设备交给练习记录（`attempt_logs`），为命令回放与过程评分（§9）准备。
- 练习开始时场景设备的回放缓冲清到当前提示符、已打开的场景终端清屏（不泄露上一次练习的答案）；进行中练习的端口不做空闲关闭。
- 里程碑 4 补充（配置复位用）：config 事务可自动回答 `[Y/N]`（只回答新出现的确认）；可按组执行（每组从用户视图进入对应视图，进视图的命令报错就跳过整组）；事务中执行 `sysname X` / `undo sysname` 时跟踪新主机名，结束后把新提示符重印给学员终端；学员自己改名时 hub 每次从当前输出读提示符，不缓存主机名。识别 S5700 不带 `%%` 的日志，提示符或分页符之后插进日志时仍按设备实际状态判断（§3）。
- 里程碑 5 补充（命令输出采集）：学员每条命令之后学员可见的输出（到下一个提示符为止）整理成摘要交给练习记录；采集之外到达的异步日志作为该设备最近一条命令的附带日志（副作用评分用）。隐藏事务的输出不经过可见通道，不会被采集。
- YunShan 适配（2026-10-06）：提示符识别兼容两阶段型号的 `[~…]` / `[*…]`（AR6700 实测为单阶段，不出现）；config 事务回到用户视图前，提示符是 `[*…]`（有未提交配置）就先 `commit`；确认提示同时识别 `[Y/N]` 与 `[Y(yes)/N(no)/C(cancel)]`；`undo sysname` 之后的设备名按平台（YunShan 为 `HUAWEI`）；hub 在 `Password:` / `Username:` 这类登录提示后绝不补回车（那等于提交空密码）。分页擦除 `ESC[16D` 与日志格式原有规则已覆盖。
- 里程碑 4、5 集成（2026-10-04）：进入隐藏事务之前先结束学员上一条命令的采集（设备已停在提示符上），所以事务结束时补发的日志、sysname 改名后重印的提示符都不会算进学员命令的输出；自动 `[Y/N]`、按组执行、复位命令只在隐藏通道里，不产生命令记录；学员刚回车、设备还没回显时隐藏事务返回「暂不可执行」。命令结束按「日志之前的那一行是提示符」判断（S5700 日志后不重印提示符），学员改 sysname 不影响（提示符从输出里读，不缓存主机名）。DATASYNC 配置同步日志只说明「配置变了」，不作为日志事件、不进输出摘要、不算副作用；日志之后的新行、或直接接在日志行末尾的学员输入，仍按提示符之后的命令记录。分页符后插进日志时，日志行末尾的擦除序列按换行拆开，避免配置快照丢行（§3）。

### 5.2 模拟环境注册与切换（2026-10-06 实现，`backend/lab.py` 注册表、`backend/labenv.py`、`frontend/labswitch.js`）
按单人使用设计（§2）：任何已登录用户都能切换，不区分角色。

**注册表**（`lab.REGISTRY`）：每种模拟环境一个 `LabEnv{key, label, platform, make(options) -> LabProvider, probe() -> (在线, 原因), power, sandboxes}`。现有两项：`ensp`（eNSP，vrp5）、`enspro`（eNSP Pro，yunshan，有沙箱、能启停设备）。`lab.make_provider(key, **options)` 按注册表新建后端（options 目前只有 `sandbox`）。
- **接入一种新环境的步骤**：① 实现 `LabProvider`（`resolve / current / allowed / connector / endpoint`，能启停就实现 `power_action`、有沙箱就实现 `list_sandboxes`、要登出就实现 `close`）和对应的 `DeviceConnector`；② 写一个快速探测 `async probe() -> (bool, 原因)`（不登录、不连设备 console，只看进程 / TCP 端口）；③ 在 `lab.py` 末尾 `register(LabEnv(...))` 一行；④ 场景 JSON 用新的 `platform` 值（并加进 `scenarios.PLATFORMS`），题目即按平台归到该环境。上层（练习、考试、终端、Agent、前端选择器）不用改。
- **可用性探测**（`lab.available(key, force)`）：每项超时 2 秒（`PROBE_TIMEOUT`），结果缓存 15 秒（`PROBE_TTL`），同一环境并发探测只跑一次；`GET /api/lab/environments` 对全部环境并行探测，整体 3 秒内返回。eNSP：只看本机 `eNSP_Client.exe` 进程（`AINET_ENSP_CLIENT=0/1` 可覆盖，供远程调试），**绝不连 console 端口探测**（§3、§14 第 9 项）；非 Windows 直接判离线。eNSP Pro：对接入地址的主机端口做 TCP 连接、https 时再做一次 TLS 握手（不校验自签证书），**不登录**（单会话，登录会挤掉别处），再看设备 console 密码有没有配置。Windows 部署上 192.168.56.101 不可达（它是 Linux 主机的 KVM 网段）时显示「连不上 ensp-pro.example:8443（超时）…」，在课程页「接入设置」里改地址即可（2026-10-07，见下面「接入设置」）。
  - **离线原因分类**（2026-10-07）：探测返回 `(在线, 原因[, 分类])`（旧式两元组照样可用，离线时分类记 other），`available()` 另给 `reason_code`：`unreachable`（连不上 / 超时 / 解析不了主机名；探测整体超时也算）、`tls`（端口能连、TLS 握手失败，提示地址是不是该写 http://）、`auth`（登录被拒 / 会话被挤掉——探测不登录，看不出来，只对当前环境由后端自己报：`LabProvider.health()`，eNSP Pro 取会话失效原因或最近一次登录失败；端口能连时仍算在线（还能浏览题目、在操作页重新登录），带 `warning: true`）、`missing_secret`（缺凭据）、`other`。`reason` 是一条中文说明。课程页环境列在「（离线 · 分类）」下方写原因全文（两行截断，悬停看全文）。
  - **eNSP console 主机不是本机**：先只解析主机名（`getaddrinfo`，不连端口），解析不了 = unreachable；能解析时，本机的进程 / 文件检测（`ensp_runtime.py`）对远程 eNSP 无意义，没设 `AINET_ENSP_CLIENT` 就判离线（other，「设备运行状态无法检测（需在 eNSP 主机上运行 ainet 或以后的节点代理）」），设了照旧以它为准。eNSP 的 console 端口在 eNSP 机器上监听 0.0.0.0，远程能连（§3），但运行设备 / 最近拓扑的检测仍要在 eNSP 主机上（§14 第 23 项）。


**接入设置**（2026-10-07 实现，里程碑 6.12；原则：模拟环境原则上是独立节点，可能在局域网或别的网络里，ainet 所在的操作机本地可能只跑 eNSP）：
- **接入地址可在运行时改**：`LabEnv.endpoint`（`Endpoint{kind, label, env_var, config_attr}`）——eNSP Pro 为服务 URL（`url`：http / https，要有主机名，不能带用户名密码、查询串、片段，去尾斜杠），eNSP 为 console 主机（`host`：主机名或 IP，不带协议和端口；对应 `config.ENSP_HOST`）。存 `app_settings` 的 `lab:endpoint:<环境>`。**优先级与 `lab:env` 一致**：库里的记录优先；没有记录时用环境变量（`AINET_ENSPRO_URL` / `ENSP_HOST`）或内置缺省值；`AINET_LAB_FORCE=1` 且设了该环境变量时，启动时以环境变量为准并写回库。来源 `source`：`db` / `force` / `env` / `default`。启动时 `LabManager.boot()` 装载到内存（`lab._endpoints`），之后 `lab.endpoint_value(key)` 现取，`EnspProvider` 的 console 连接与 `EnsproProvider` 的 REST / WebSocket 地址都从这里来；库里的记录不合法时忽略并告警。启动日志写明各环境地址与来源。
- **修改**（`POST /api/lab/environments/{key}/endpoint {value}`，空串 = 删掉记录、回到环境变量 / 缺省值）：格式不对 422；与切换同一套锁和判断——练习 / 考试进行中 409（「练习进行中，结束练习后才能修改接入地址」）、准备环境任务进行中 409。改的是**当前环境**且地址变了：`gen + 1`，所有终端收到「[eNSP Pro 接入地址已改为 …，终端已断开]」后断开，关闭旧后端（登出最多等 5 秒，旧地址往往已经连不上），按新地址重建后端（与切换同一流程，**不停旧沙箱的设备**）；改的是别的环境只记库、清它的探测缓存。`GET` 同路径读取 `{environment, label, current, endpoint{kind, label, value, source, default, env_var, runtime_local}, secrets, runtime_local, locked, can_edit}`；`POST …/endpoint/test {value?}` 测试连接（不保存、不缓存，与在线探测同样不登录、不连设备 console；eNSP 只解析主机名、看本机进程）。
- **凭据**仍只放 ainet 主机的环境变量 / secrets.env（`lab.read_secret`，与 DeepSeek Key 同一套候选路径），不入库、不经接口写入、不进日志。`LabEnv.secrets` 列出所需凭据名（eNSP Pro：`ENSPRO_DEVICE_PASSWORD`），环境列表与接入设置里给 `secrets: {名: true/false}`，**只有有无，绝不返回值**。缺凭据时探测判离线（`missing_secret`），说明里写配置位置。
- **runtime_local**：设备运行状态靠本机文件 / 进程检测的环境（`LabEnv.local_runtime`，即 eNSP 经典版）标注地址是不是本机（环回、localhost、本机主机名或地址）；不是本机时 `runtime_local: false`，接入设置里提示「设备运行状态无法检测（需在 eNSP 主机上运行 ainet 或以后的节点代理）」。节点代理不在本里程碑范围（§14 第 23 项）。
- **启动看门狗参数按环境配置**（2026-10-07，里程碑 6.16）：ainet 可以部署在多台主机上、各接不同的 eNSP Pro，设备冷启动快慢差得多（Linux KVM 上约 100 秒，一台 Windows 笔记本上 T1 / T2 187~202 秒），全局 150 秒的启动超时让笔记本上的看门狗误判卡住、自动重启 3 次后标 start_failed。所以能由 ainet 启停设备的环境（`LabEnv.watch`，目前只有 eNSP Pro；经典版没有看门狗，接口与气泡里都不出现）的看门狗参数单独配置（`lab.WatchParam`）：`start_timeout` 启动超时（秒，60~1800，环境变量 `AINET_ENSPRO_START_TIMEOUT`，内置 150）、`start_retries` 卡住自动重启次数（0~10 的整数，`AINET_ENSPRO_START_RETRIES`，3）、`early_check` 早期卡死检查（秒，0 = 关闭或 10~600 且小于启动超时，`AINET_ENSPRO_EARLY_CHECK`，40）、`probe_timeout` 就绪探测上限（秒，30~1800，`AINET_ENSPRO_PROBE_TIMEOUT`，300）。存 `app_settings` 的 `lab:watch:<环境>`（JSON，只含页面上设置过的参数；与 `lab:power`——看门狗的跟踪状态——是两回事）。**优先级与接入地址一致、逐个参数算**：库里的记录优先，没有则环境变量，再没有用内置缺省；`AINET_LAB_FORCE=1` 时设了的环境变量优先并写回库；库里不合法的项忽略并告警。来源 `source` 同样是 `db / force / env / default`。`GET /api/lab/environments/{key}/watchdog` → `{environment, label, current, watchdog{values, params[{name, label, unit, value, source, default, builtin, env_var, min, max, integer, zero_off}]}, locked, can_edit}`（经典版 400）；环境列表与 `GET …/endpoint` 里每个环境另带同样的 `watchdog`（没有看门狗为 null）。`POST …/watchdog {start_timeout?, start_retries?, early_check?, probe_timeout?}` 只改给出的参数，`null` = 删掉该项记录、回到环境变量 / 缺省；范围不对、参数名不对、早期检查不小于启动超时都 422（中文说明），练习 / 考试 / 准备环境进行中 409（同接入地址）。**生效**：只有一个 `PowerWatch`，`LabManager` 用 `PowerWatch.configure()` 把属性换成某个环境的参数（记 `configured_for`）——启动时、切到另一个环境时、修改的正是看门狗用着的环境时（立即生效，不重建后端、不断开终端；看门狗每轮现读属性，进行中的启动按新值继续计时）；同一环境内换沙箱不重设。改的是别的环境只记库，切过去时生效；准备任务的「最多 N 次」在切换前就按目标环境的配置。
- **同时运行设备上限**（2026-10-07，里程碑 6.17）：同一个参数集（`lab:watch:<环境>`、`lab.ENSPRO_WATCH`）里另有 `max_devices`（0~64 的整数，0 = 不限，缺省 0，环境变量 `AINET_ENSPRO_MAX_DEVICES`；来源 / 优先级 / 422 / 409 同上；`PowerWatch.configure` 不认识它、跳过）。`lab.max_devices(环境)` 现取。**计数口径**（`scenarios.device_load`）：`required_devices` 中非 PC 设备的台数——有本题拓扑（`LabManager.note_topology` 记下的各环境、各沙箱最近一次 resolve 结果）时按设备 kind，pc / cloud 不计；没有时 `hosts` 与名字为 PC / CLIENT / SERVER / STA / MCS / Cloud 开头的不计，其余都计；本题设备不在拓扑里 = 判断不了，不隐藏、只记一次日志。**过滤**：`GET /api/scenarios?env=` 不列出超过所浏览环境上限的题（进行中的照列，`?all=1` 不过滤），附 `max_devices / capacity_hidden / capacity_hidden_chapters`；**409**：开练、单题考试、模板考试（含超上限的题）、准备环境（`LabManager.capacity_reason`，中文原因，不含 busy）。**准备环境先停非本题设备**（`LabManager._stop_extra`，按整个 eNSP Pro 计数）：第一次要启动本题设备前，本题非 PC 设备 + 本沙箱里在运行（online / starting / initializing / error / start_failed）的非本题非 PC 设备 + 其他沙箱里在运行的非 PC 设备（`EnsproProvider.running_elsewhere`，getByPage 一次取全）超过上限时，先停掉全部这些非本题设备——其他沙箱的经 `stop_in_sandbox`（batchStop 带该沙箱 id，并记停机时刻），本沙箱的经 `PowerWatch.stop` 并断开其终端；PC 不停。阶段文字写明停了哪些，任务记 `stopped_other`（按沙箱）与 `stopped_extra`。起因（2026-10-08 真机）：服务刚启动时没有「上一个沙箱」，stop_old 不触发，另一个沙箱留着 4 台在跑，T1 / T2 资源不足一直卡在启动、看门狗重启也没用。上限为 0 时不做，只按 stop_old 规则。
- **课程页**：每个环境右上角「⚙」（齿轮与环境按钮是兄弟元素，离线时也能点；考试中不显示），在卡片下方展开小气泡：当前地址（可编辑）、来源、缺省值、凭据有无、runtime_local 提示、「测试连接」「保存」「恢复缺省地址」；当前环境注明保存会断开所有终端。有看门狗的环境（eNSP Pro）气泡里另有「启动看门狗」一节：「启动超时（秒）」「卡住自动重启次数」两个数字框，各自注明来源、缺省值与范围，留空 = 回到缺省；「保存」先存改过的看门狗参数、再存地址（地址没改且有参数改动时不再提交地址），「恢复缺省地址」只管地址。早期卡死检查与就绪探测上限只经接口改。另有「设备容量」一节：「同时运行设备上限（不含 PC，0 不限）」（里程碑 6.17）。章节列下方提示「另有 N 道题需要更多设备（超过同时运行 M 台），本机已隐藏」，全部被隐藏的章节标「设备不足」。手机宽度下气泡占满一行、不溢出。
- **页面上添加环境实例**（2026-10-08，里程碑 6.19）：用户的部署原则是「模拟环境是独立节点，可能有多台 eNSP Pro（局域网或公网），每台各有地址、设备 console 密码、启动超时、设备上限」。所以注册表除了代码注册的内置 `ensp` / `enspro`，还有**用户添加的实例**：`lab.ENV_TYPES` 列出可添加的类型（`EnvType`，目前只有 eNSP Pro；以后其他类型加一项），实例存 `app_settings` 的 `lab:envs`（`[{key, type, label}]`），key 为 `<类型>-N`（N 从 2 起、只增不复用，`lab:envs:seq`），`LabManager.boot()` 最先 `lab.load_custom_envs()` 注册（整体替换，不合法的记录忽略并告警），所以库里的当前环境是 `enspro-N` 时重启能恢复。`LabEnv` 多 `type`、`custom`；实例复用全部按环境配置的键：`lab:endpoint:<key>`（必填，没有环境变量缺省，不能清空）、`lab:watch:<key>`（`lab.INSTANCE_WATCH`：范围与内置缺省同 `ENSPRO_WATCH`，但不读全局 `AINET_ENSPRO_*` 环境变量，`AINET_LAB_FORCE` 也不影响它们）、`lab:sandbox:<key>`；`EnsproProvider(env_key=…)` 按实例取地址、密码、显示名，`kind` 恒为 `enspro`（类型判断不变）。内置 `enspro` 完全不变。**设备 console 密码按实例**：`lab.device_password(key)` 读 `ENSPRO_DEVICE_PASSWORD__<KEY>`（大写，非字母数字换 `_`），只有内置 enspro 再回落到 `ENSPRO_DEVICE_PASSWORD`；页面上填的写进 `ai.key_file()` 对应行（`ai.write_key_file(path, value, name)`，原子写入、保留其他行），接口只给 `device_password{name, configured, source}`，探测缺密码时 `missing_secret` 的说明里写实例的键名。**题目按 platform 归属**：同一平台的题对所有实例可见；`LabManager.target_env(s, key)` 决定在哪个实例上做（给定且平台相同 → 它；否则当前环境平台相同 → 当前；否则注册顺序第一个），设备上限、准备环境都按它；`POST /api/lab/prepare` 可带 `environment`（课程页传正在浏览的环境，平台不符 409）。**干净基线按实例**：`baseline:<env_key>:<沙箱>:<设备>`（内置 enspro 的键不变）。**删除**：只删用户添加的（内置 400），当前环境 / 练习 / 考试 / 准备环境进行中 409（与切换同一套锁），清理 `lab:endpoint|watch|sandbox:<key>`、`baseline:<key>:*`（`db.delete_settings_prefix`）、`ai.key_file()` 里的密码行与内存覆盖 / 缓存；环境变量与另一个候选 secrets 文件不动。接口：`POST /api/lab/environments`（添加；先做与「测试连接」相同的探测，失败也保存并给 `warning`；考试中 409）、`POST /api/lab/environments/probe`（添加前测试连接，不查密码）、`PATCH|POST /api/lab/environments/{key}`（显示名 / 密码；考试中 409）、`DELETE /api/lab/environments/{key}`；环境列表另带 `types[]` 与每个环境的 `type / custom / device_password`。课程页：环境列标题右侧「+」（考试中不显示）→ 对话框（类型、显示名、地址、设备密码、启动超时、设备上限、测试连接、添加）；eNSP Pro 类的 ⚙ 气泡多「设备 console 密码」框（只写不读）与「清除密码」，用户添加的另可改显示名、「删除此环境」。**已知局限**：`PowerWatch` 的停机间隔记录按沙箱 id，不区分实例（两台主机沙箱 id 相同时共用停机间隔）；空闲停机 / 看门狗只跟踪当前环境。
- **自动识别 https**（2026-10-08，里程碑 6.20）：起因：用户在「+」添加 eNSP Pro 时把地址写成 `http://ensp-pro.example:8443`，该端口只有 HTTPS（eNSP Pro 都是 https、自签证书），系统没提示，结果连不上。① **可以不写协议**（`url` 类）：`lab.normalize_endpoint` 对不含 `://` 的输入补 `https://`，只有主机名（没有端口）时补缺省端口 `:8443`（`lab.ENSPRO_DEFAULT_PORT`，与 `config.ENSPRO_URL` 缺省一致；裸 IPv6 补方括号）。判断有没有协议看 `://`，不用 `urlsplit`（`enspro.lan:8443` 会被拆成 scheme=enspro.lan）。规范化本身不连网（启动装载库里记录也用它）。② **探测**（`lab.detect_scheme(kind, raw)`）：只在 `url` 类且输入是 `http://` 或没写协议时做（写了 `https://` 的、eNSP 的 `host` 类不探测、不连）；每步上限 `SCHEME_TIMEOUT`（0.9 秒）：先 TCP 连接（连不上直接返回 `unreachable`，不再等后两步）→ TLS 握手（与 `_probe_enspro` 同一个不校验证书的上下文，**不登录**）→ TLS 不通时明文发 `HEAD / HTTP/1.0`，回复以 `HTTP/` 开头才算明文 HTTP（光 TCP 能连不算）。结果：TLS 通 → 用 `https://`，返回 `normalized_from`（原输入）与 `notice`「已自动改为 https」；明文 HTTP → 保留 http（没写协议时改成 `http://…`），给 `warning`「…是明文 HTTP（TLS 握手不通），已按 http 保留…」；都不通（`unreachable` / `unknown`）→ 按输入（没写协议按 https，`notice` 说明没能自动识别），连不上的原因由随后的探测给出。`http://host` 不带端口时按原来的 80 端口试（改成 `https://host:80`，不悄悄换到 443）。另带 `scheme_check`（https / http / unreachable / unknown / skip）。③ **用在**添加（`add_env`：先只校验格式，其他字段都对了再探测）、添加前测试连接（`POST /api/lab/environments/probe`）、改地址（`set_endpoint`，探测在拿锁之前做，网络 I/O 不占切换 / 考试 / 练习的锁）、测试连接（`POST …/endpoint/test {value}`）；保存的始终是规范化后的完整 URL，返回里带 `normalized_from / notice / warning`（添加时 `probe` 里也有，明文 HTTP 的警告并进 `warning`）。④ **已存的 http 地址不自动迁移**（启动装载、在线探测都不改）；`POST …/endpoint/test` 不给 value、当前地址是 http 且该端口其实是 https 时返回 `suggest`（https 地址）。⑤ **课程页**：添加对话框与 ⚙ 气泡的地址框失焦（值不是 `https://` 开头、和上次检查的不同；点按钮 / 关闭引起的失焦不重复探测）或点「测试连接」后，返回 `normalized_from` 就把输入框换成规范化后的 URL，下面一行小字写「已自动改为 https：原输入 → 新地址」（明文 HTTP 的警告也写这里）；⚙ 打开时当前地址是 http 就后台测一次（每个地址只测一次），有 `suggest` 显示「建议改为 https：…」与「一键改为 https」（只改地址，同「保存」）。测试：`backend/tests/test_lab_scheme.py`（本机回环上的假 TLS 服务——openssl 现场生成一次性自签证书，没有 openssl 时跳过——、明文 HTTP 服务、接受即关的服务，或 mock 探测函数；不连外网）。

**实验上下文**（`labenv.LabManager`，进程内唯一）：当前环境 key、当前沙箱、当前 `LabProvider`、切换代数 `gen` 都只在这里（`main.LAB` 只是镜像，供旧代码读）；干净基线按「环境 + 沙箱名 + 设备」存 `app_settings`（§10.4）。
- **持久化**：`app_settings` 的 `lab:env` 与 `lab:sandbox:<环境>`。启动时库里有记录就用记录，没有时用 `AINET_LAB_PROVIDER` / `AINET_ENSPRO_SANDBOX` 作缺省值（所以切换过一次以后，改环境变量不再生效，要在页面上切或删库里的这两个键；或设 `AINET_LAB_FORCE=1`，启动时以环境变量为准并写回库，§10.5）。启动日志写明当前环境的来源。
- **切换流程**（`POST /api/lab/environment {key, sandbox?}`；不给 sandbox 时沿用该环境上次的沙箱）：串行锁 → 依次拿考试锁、练习锁（与开考 / 开练同一顺序，正在开始的练习做完才检查）→ 有练习或考试进行中 409「练习进行中，结束练习后才能切换模拟环境」/「考试进行中…」→ 「按题目准备环境」任务进行中 409 → 同环境同沙箱直接返回 `changed:false` → 换环境时强制重新探测，离线 409；同环境换沙箱时核对沙箱存在（404）→ `gen + 1` → ConsoleHub `reset_all`：所有终端订阅者收到「[模拟环境已切换为 X，终端已断开]」后断开、关闭全部设备连接、清空通道与 pin、按新平台设 `default_sysname` → 关闭旧后端（eNSP Pro 退出登录，释放单会话）→ 新建后端（eNSP Pro 登录是懒的，第一次调接口才登录）→ 清拓扑扫描 / 运行时缓存，练习管理器、Agent、`main.LAB` 换成新后端 → 记库。切换中新开终端（WebSocket 1013）、开练、开考、Agent 提问一律拒绝（409）。
- **前端发现切换**：`/api/topology/status|current` 带 `env {key, sandbox, gen, label, platform}`；`gen` 变了 = 环境被切换（本页或别的标签页），前端关闭全部终端标签、重画拓扑、刷新题目 / 考试列表、Agent 对话里插一条说明。
- **Agent**：环境快照每次提问现组装，跟着新后端走；切换时清空终端命令记录（旧环境的命令不进新快照）；正在进行的对话在切换后不再执行只读命令（「模拟环境已切换为 X，不再在旧环境的设备上执行命令」），回答结束时发 `notice` 事件提示「以上内容基于切换前的环境」。

**题目按平台过滤**：`GET /api/scenarios?env=<key>` 只返回该环境平台的题（不触发切换；缺省 = 当前环境；`all=1` 不过滤），附 `platform`、`current_env`、`other_platform`（其他平台题数）、`empty_reason`（该环境没有题时的说明）。`GET /api/exam/templates` 给每个模板标 `platforms`、`platform_ok`、`mismatch[{index,title,platform}]`；工作台考试列表只列匹配的模板（模板管理页全列）。**开考时**先核对平台：模板里有不匹配当前环境的题就 409，逐题说明（「第 2 题「…」（YunShan OS（eNSP Pro））」），再做原有的就绪核对。

**沙箱**：`GET /api/lab/sandboxes` → `{environment, current, sandboxes:[{id, name, devices, running, current}]}`（只对有沙箱的当前环境；否则 409）。eNSP Pro 用 `POST /service/ensp/sandbox/getByPage {currentPage, pageSize, orderBy:"createTime", orderDirection:"descending", projectId}`（从前端 bundle 确认；2026-10-06 真机：不带 projectId 返回 code 10000「参数缺失」，`projectId=0` 返回系统内部错误；离线账号的 getUserInfo 没有 projectId，沙箱自身 projectId 为 1，所以按 用户信息 → 沙箱详情 → 1 的顺序取）。设备数只数有 console 的设备。干净基线的键里有沙箱名，切换沙箱后自动用新沙箱的基线。

**按题目准备环境**（课程页「开始练习」用，`POST /api/lab/prepare {scenario_id}` → 202 + 任务；`GET /api/lab/prepare` 轮询）：题目 `platform` → 注册表里对应的环境（`lab.env_of_platform`）；**沙箱 = 题目 `topology` 名与沙箱名匹配**（忽略大小写与 `.topo`；YunShan 题的 topology 本来就写沙箱名，不新增字段；找不到、同名多个都在任务里失败，`error_status` 分别为 404、409）。步骤 `switch`（换环境 / 换沙箱，已在目标处则跳过）→ `start`（只启动本题 required 里没运行的设备）→ `wait`（每 3 秒查一次；启动经启动看门狗，卡住的设备 `restart`：自动停止再启动）→ `probe`（接口已报运行中、等设备不再报 busy）→ `done` / `error`（2026-10-07 改，§10.5）。任务进行中显式切换返回 409（真机 e2e 发现：任务等设备期间被切走沙箱，会在新沙箱上空等到超时）。条件（同步检查，POST 直接返回状态码）：题目存在（404）、没有任何练习 / 考试进行中（409）、目标环境在线（409「…当前离线」）、同一时刻只跑一个任务（409）；其余失败在任务里（`ok=false`、`error`、`error_status`）。`ok=true` 表示设备已通过就绪探测（§10.5）；关掉探测（`AINET_ENSPRO_PROBE=0`）时只表示接口状态「运行中」，刚冷启动的 YunShan 设备头一两分钟改配置会报 `Error: The system is busy in building configuration`。开练时设备还在 initializing 返回 409（detail 含 initializing / busy），调用方自动重试。eNSP 经典版不能由 ainet 启动设备：设备没运行时任务失败并提示在 eNSP 中启动。REST 状态「运行中」之后 console 可能还要几分钟才响应（§14 第 20 项），任务完成提示里注明。切到新沙箱时停掉旧沙箱运行着的设备，记在任务的 `stopped_old`（`AINET_ENSPRO_STOP_OLD`，§10.5）。**给课程页准备浮层的字段**（2026-10-07，§16.3，只增不改，旧字段含义不变）：`env_label`、`power`（能否由 ainet 启停设备）、`sandbox_name`（有沙箱的环境才有）、`retries`（看门狗自动重启上限）；`steps` 步骤条 `[{key, label, state}]`，key 依次为 `switch`（切换环境）→ `sandbox`（查找沙箱，只有有沙箱的环境）→ `start` / `wait` / `probe`（能启停设备的环境；关掉就绪探测时没有 `probe`）或 `check`（核对设备，eNSP 经典版）→ `done`，state 为 `pending / active / done / skipped / error`（进入某步时前面没进入过的标 skipped，但启动过设备时没单独停留的 wait / probe 算 done；`restart` 阶段属于 `wait` 步骤；换沙箱的切换记在 `sandbox` 步骤）；`device_info {设备: {status, note, kind, restarting, restarts, phase, round_s}}`（`restarting` 单独报，接口状态里它显示为 starting）；`progress`（0~1，切换 / 查沙箱占前 8%，设备按「接口报运行中 + 就绪探测」两段按经验用时折算，done 时为 1）与 `estimate {remaining_s, lo_s, hi_s, max_s}`（没有可估的设备时为 null；经验值：AR 120 秒、LSW 65 秒、PC 20 秒、其他 100 秒，多台同时启动每多一台 +10 秒，就绪探测 35 秒，区间 0.8~1.3 倍，`max_s` 是任务等待上限的剩余秒数）。restart 阶段的文字带「（第 n 次，最多 N 次）」。eNSP 经典版的任务多一个阶段 `check`。

**前端模块**（`frontend/labswitch.js`，全局 `window.LabSwitch`，不依赖 app.js）：`environments / sandboxes / switchTo`、`prepare(root, scenarioId, onProgress, token)`（轮询任务进度，token.cancelled 停止等待）、`retryBusy(call, {tries, interval, token, onWait})`（开练 / 开考 409 且含 busy 时每 20 秒重试、最多 5 次）、`readonly(el, {root})` 只读显示「环境 X · 沙箱 Y」、`stageText / statusText`（不认识的阶段 / 状态原样显示；2026-10-07 加 `check`「核对设备」）。**2026-10-07 起环境切换只在课程页进行**（§16.3）：工作台顶栏上过渡用的可操作选择器（`mount`）已撤掉；档案 / 成绩单 / 模板管理页顶栏只读显示。

**多人模式（未来，未实现）：每用户独立沙箱**。设想：每个学员一套 eNSP Pro 沙箱（同一套拓扑复制多份），互不干扰。需要改的点：① `LabManager` 从进程内唯一改成按用户（或会话）各一个上下文，`main.LAB` 镜像与 `connector_factory` 改为按请求取上下文；② ConsoleHub 的通道键从 console 键改为（上下文, console 键），或每个上下文一个 hub；③ `PracticeManager` / `ExamManager` 的「同一时刻一个 active」改为按用户；④ eNSP Pro 单会话限制：所有用户共用 ainet 的一个 REST 会话（沙箱按用户区分），或每用户一个 eNSP Pro 账号；⑤ 干净基线键已含沙箱名，沙箱按用户分开后天然独立；⑥ 切换锁与 409 改为只看本用户的练习 / 考试；⑦ 资源：每套 Campus 8 台约 19GB 内存（§3），需按 KVM 主机容量限制并发人数。

## 6. 故障场景库

场景以 JSON 文件存放在仓库 `scenarios/` 目录（版本管理），启动时加载并校验，通过的按 `id` 同步进 db 的 `scenarios` 表（attempts 外键引用、档案 / 错题本显示标题）；运行时以内存加载结果为准。每个场景对象（里程碑 3 实现版）：
- `id` / `title` / `level`（1 入门 … 5 专家）/ `knowledge_tags[]`（OSPF邻居/BGP选路/VLAN/ACL/路由黑洞…）。
- `topology`：拓扑文件名（如 `Campus_Network.topo`，见 §6.2 基础拓扑），按文件名去扩展名、忽略大小写与当前运行拓扑比对。
- `required_devices[]`：本题需要运行的设备，可以是拓扑中的一部分。
- `accessible_devices[]`：本题向学员开放终端的设备（⊆ required）；练习期间不开放的设备不出现在终端白名单中（可用于模拟对端或外部网络故障）。
- `symptom`：症状描述（给学员看的工单式描述）。
- `baseline{设备:[命令]}`：把（未配置的）设备配成干净初始状态，可为空。开练前另拍整机配置快照（复位目标 + 校验基准，§10）。
- `inject{设备:[命令]}`：故障注入配置；与 baseline 分开、每台设备各一个配置事务执行。
- `restore{设备:[命令]}`：撤销 inject 与 baseline（只撤销场景自己的改动）。结束时先执行它，再按开练前快照整机复位（§10），它不再是唯一的还原手段。
- `checks[]`：目标恢复态判定规则 `{device, command, expect(正则), negate?, desc}`，全部满足即通关（如 OSPF 邻居回 Full 且目标 ping 通）；只允许只读命令。
- `hints[3]`：三级静态提示（方向 → 命令 → 揭示）；里程碑 5 起提示由 AI 动态生成，这三条作为 AI 的参考和 AI 不可用时的回退（§7）。第 1、2 级静态提示不能含修复配置、注入配置或故障特有的值（场景加载时校验，AI 超时 / 失败时会回退到它们）；判定命令是只读查看命令，可以出现。
- `reference_path`：参考排障路径（供教学/评分参照）；`solution{设备:[命令]}`（可选）参考修复命令；`note`（可选）出题备注。
- `scoring_points[]`（可选）：评分要点，字符串列表，送给 AI 评分参照，不给学员看（里程碑 5 起两个自带场景已补上）。
- `platform`（可选，2026-10-06）：`vrp5`（缺省，eNSP 经典版 AR / S 系列）或 `yunshan`（eNSP Pro 的 YunShan OS）；就绪判断要求与当前实验后端的平台一致。eNSP Pro 的题目 `topology` 写沙箱名。首道 YunShan 题 `enspro-t1-t2-link`（「T1 与 T2 互联不通」，由 Campus 的 AR0–AR2 题改写，参数化方式相同）。2026-10-06 迁移：Campus 4 题与 OSPF 题的 YunShan 版 `*-ys.json`（沙箱 `ainet_campus`，参数化同原题，旧 VRP5 版保留），见 `docs/enspro-replacement.md` §4。**题名不带平台后缀**（2026-10-07 起去掉「（YunShan）」「（VRP5）」）：环境在课程页分列，档案（`scenario_platform`）、成绩单与命令回放（每题 `platform`）、考试模板管理页在题名旁显示平台标签，单题考试名为「单题考试 · 题名 · YunShan」；`(title, platform)` 必须唯一。
- 课程页字段（可选，2026-10-07，§16.3）：`chapter`（章节 id，必须在 `scenarios/chapters.json` 中；该文件 `{chapters:[{id, title, order}]}`，L01~L12 草案，加载场景时跳过它）、`difficulty`（1~5）、`est_minutes`（预计用时，分钟）、`summary`（一句话症状，不换行、≤ 80 字）、`tags[]`（知识点标签）。**只放面向学员的内容，不能泄露答案**：加载时与第 1、2 级静态提示同一套检查（修复 / 注入配置、只在故障配置里出现的 IP），另查判定命令；参数化场景对每个抽样都查；不能用参数占位符。`scenarios.public()` 带出这些字段，`GET /api/scenarios` 另带 `chapters` 与每题 `progress`（passed / failed / none）。现有全部场景已补上。
- `hosts[]`（可选，⊆ required，2026-10-06）：终端主机（eNSP Pro 的 PC-CLIENT）。主机的命令行只能 ping，baseline / inject / restore 里主机的「命令」只能是 `ip address <IP> <掩码>`、`ip gateway <IP>`、`undo ip address`、`undo ip gateway`，由练习流程翻译成实验后端的设备属性接口；checks 在主机上只能用 ping（Linux 格式，判定写 `\b[12] received\b`）；就绪判断要求拓扑里该设备是 PC。快照 / 复位 / 干净基线对主机按地址配置处理。
- `draft`（可选 bool，2026-10-07）：未在真机验证的草稿题。`GET /api/scenarios` 缺省不列出（课程页、操作页都看不到；进行中的那道照列），`AINET_SHOW_DRAFTS=1` 时列出并在课程卡片标「草稿 · 未验证」；按 id 开始练习 / 单题考试不受限制（真机验证用）。题库规划与草稿题清单见 `docs/question-bank-plan.md`。
- `params{名: 规格}`（可选，里程碑 6，`backend/params.py`）：参数化故障。规格类型 `int{min,max,default,exclude?}`、`choice{values,default}`、`ip{net,default,exclude?}`（网段内主机地址）、`subnet{within,prefix,default,exclude?}`、派生的 `host{net,index}` 与 `format{value}`；按声明顺序求值，规格里可引用前面的参数。title / symptom / baseline / inject / restore / solution / checks（command、expect、desc）/ hints / reference_path / scoring_points / note 里用 `{名}` 占位，expect 中代入的值先做正则转义。**设备名与接口名不能参数化**（由真实拓扑决定）。种子 0 = 全部取默认值（与参数化前的原题一致）；其他种子按 `random.Random(f"{id}#{种子}")` 确定性抽取，同一种子可复现。开始一题时生成种子（练习随机，可由接口 / `AINET_PRACTICE_SEED` 指定；考试由模板固定或开考时随机），种子与取值记入 attempts；注入、判定、AI 提示与泄露检查、评分、重启恢复都用展开后的场景。

### 6.1 生成与校验流程
AI 生成 → **规则校验器过一遍** → 通过即存库固化，运行时直接复用、不再调 AI 生成。
规则校验必查：
1. VRP 命令语法合法；
2. 可回滚（有对应 undo 或可被复位脚本还原）；
3. 不切断 console/管理通道（否则学员与系统都连不上设备）；
4. 目标恢复态能被程序判定。

参数化场景的加载校验（里程碑 6）：引用了未声明的参数、参数没被任何字段引用、设备名 / 接口名含占位符都是错误；再对参数空间抽样——种子 0、种子 1..24、每个随机参数的边界值、「碰撞」取值（其他参数的默认值、solution / checks 中出现的数字与 IP）——每次展开都要通过下面的全部规则，且含参数的注入命令展开后不能与参考修复命令相同（否则该取值下故障不生效）。已参数化：`campus-ar0-ar2-link`（互联网段、注入的错误网段）、`campus-lsw0-access-vlan`（错误接入 VLAN）、`campus-ar2-return-route`（错误下一跳）、`campus-ar2-acl-block`（ACL 编号、误写规则编号）。

已实现的加载校验（里程碑 3，`backend/scenarios.py`）：必填字段与类型、hints 恰好 3 条、正则可编译；各字段引用的设备都在 required 中，拓扑文件找得到时要求设备存在且有 console（不核对接口名，§3）；inject / baseline 动过的设备必须有 restore；checks 只允许 display / ping / tracert；baseline / inject / restore 禁止 `user-interface`、`reboot`、`reset saved-configuration`、`save`、`undo info-center`、`sysname`、`quit` 等切断管理通道或破坏性的命令（禁止列表）。练习开始时还会在注入后先跑一遍 checks，确认故障确实生效（对应第 4 条的运行时校验）。

运行时 AI(DeepSeek V4 Flash) 仅用于：教学引导、操作分析、评分、错题讲解。

### 6.2 出题规范
1. **基于基础拓扑出题**：题目尽量共用少数几个「基础拓扑」（例如一个完整的园区网），题与题之间只靠配置差异区分。这样在一次学习或考试中，换题、注入故障、复位都只改设备配置，不需要回到 eNSP 操作（依赖 §10 的配置快速复位，未验证前不作为强制要求）。
2. **需要终端主机时，用路由器扮演 PC**：给一台路由器配地址和默认路由，必要时作为 DHCP 客户端。这样学员能在终端里 ping、tracert，系统也能通过 console 判定连通性。不使用 eNSP 自带的 PC 作为判定依据，因为系统看不到它的状态（§3）。eNSP Pro 的 PC-CLIENT 有命令行（能 ping），系统能拿它做判定点（场景 `hosts`，§6）。**2026-10-07 起学员能在拓扑上点 PC、用「PC 设置气泡」改地址（§16.4），所以 eNSP Pro 环境下题目可以把故障放在 PC 地址上**（IP / 掩码 / 网关配错，用 `hosts` 的主机命令注入）。出这类题时：判定要覆盖 PC 地址（例如在 PC 上 ping 网关、或从网关 ping PC）；复位要覆盖 PC 地址（PC 写进 `hosts`，`restore` 给出主机命令，开练快照按地址配置拍，结束时设回快照）。eNSP 经典版的 PC 仍由 eNSP 模拟、ainet 改不了，不出这类题。PC 地址在 eNSP Pro 里是设备属性（`refreshAttrs` / `updateAttrs`，2026-10-07 真机核实）：`ipv4.type`（static / dhcp）、`ipv4.config.ip`、`ipv4.config.ipmask`（点分）、`ipv4.config.gateway`、`ipv4.config.dns1`（首选 DNS）、`ipv4.config.dns2`（备用 DNS）；改完 `ipv4.status` 先 PendingUpdate / Pending、约 4~8 秒回到 Ready。DNS 也可以出题（ainet 只管 dns1，§16.4）。
3. **物理层故障用关闭对端接口模拟**，并把对端设备设为不开放（`accessible_devices` 中不含它），学员看到的现象与断线一致。
4. **从 eNSP 示例复制拓扑生成题目时，重新分配所有设备 GUID**，避免副本与原文件 GUID 相同而无法区分（§3）。示例目录中按 GUID 命名的设备配置目录要同步改名。eNSP 能否接受改过 GUID 的文件，需实测确认（§14）。

### 6.3 拓扑一致性判定
- 由正在运行设备的 GUID 确定当前拓扑（§3），网页不提供「选择拓扑」。
- 判定一致：所有正在运行的设备都属于本题的拓扑文件，且 `required_devices` 全部在运行。其余设备开不开都可以。
- 没有设备在运行时无法判定，提示学员启动 `required_devices`。
- 运行中的设备分属多个文件时判为不一致；运行中的 GUID 不属于任何已知文件时，提示「请先在 eNSP 中保存拓扑」。

## 7. 教学模式（练习/跟练）

- **跟练模式**：AI 主导，逐步给出"为什么查这里 / 敲什么命令 / 怎么读输出"，建立分层排查方法论（物理→数据链路→网络→协议）。
- **闯关模式**：学员自排，AI 默认沉默，仅求助/卡住时介入。
- **三级阶梯提示**（可配开关与扣分）：① 方向提示 → ② 命令建议 → ③ 直接揭示。里程碑 5 已实现：由 AI 结合学员当前已执行的命令和看到的输出动态生成（关闭思考，总时长 ≤15 秒，同一级缓存不重复生成）；第 1、2 级不得泄露答案——提示词列出禁止内容（solution / inject 中的配置命令、只在故障配置里出现的 IP），生成结果再按规则检查，命中即改用静态提示。checks 判定命令不在禁止之列：它们都是只读查看命令，说出来不泄露答案，且常常就是最自然的排查命令（如 ACL 题的 `display acl all`）；2026-10-04 真机联调中，曾因禁止判定命令、加上 AI 一次超时回退到含判定命令的静态提示而判失败，据此调整；第 3 级可揭示故障点。AI 不可用时回退场景 JSON 的静态 `hints`，接口与界面标注来源（AI / 静态）。开关 `AINET_AI_HINTS`，扣分 `AINET_SCORE_HINT_PENALTY`（缺省 0）。
- **目标自动判定**（里程碑 3 已实现）：不做定时轮询，而是**由学员操作触发**：学员每次回车提交命令后，3 秒内没有新输入就跑一遍场景 `checks`；两次判定至少间隔约 10 秒（开始时的故障确认也算一次）；设备暂不可检查（学员正在输入、停在分页 / `[Y/N]` / 系统视图外的非提示符状态）时跳过、稍后重试，任一设备不可检查就整轮作废，不按部分结果判定；全部命中即判通关，无需学员声明。另有「立即检查」按钮。**判定命令对学员不可见**：判定、注入、还原都走 ConsoleHub 的隐藏事务（§5.1），学员终端看不到这些命令及其输出，页面只显示「通过几项」，不暴露判定命令。
- UI：主区 xterm.js 真实终端；侧栏 AI 助教实时看命令+输出并点评引导。
- 拓扑图：按 `.topo` 原样绘制（白底网格、背景色块、椭圆和直线、注释），设备状态分为运行中、已停止，以及无虚机设备的「不适用」。不对 console 端口做定时探测（频繁连接/断开曾疑似导致 eNSP 崩溃），console 是否就绪在打开终端时才知道。
- 多终端：每台设备一个标签页，支持左右分屏和可拖动分栏。

## 8. 考试模式

配置项（存为"考试模板"，DB 内一条 JSON）：
- 计时：总时长 / 单题时长；超时自动交卷。
- 禁提示开关。
- 评分权重（见 §9，可配）。
- 允许重试次数。
- 是否可看拓扑图。
- 是否全程命令回放录制。

开始计时前，系统先按 §6.3 核对 eNSP 中的拓扑与本题一致，并且 `required_devices` 都在运行；不一致时给出 eNSP 中的具体操作提示，核对通过才开始。

里程碑 6 落地（`backend/exam.py`，详见 README）：
- 模板配置：题目列表（场景 + 固定种子或开考随机 + 权重）、总时长、单题时长（可空）、提示（是否允许、每级扣本题总分、最高级别）、评分权重（覆盖 §9 默认值）、每题允许重试次数、是否可看拓扑、是否自动判定（考试缺省关闭）、是否记录命令回放、是否乱序。内置示例模板「Campus 基础排障测验」。模板管理页任何登录用户都能用（目前没有管理员角色，以后加权限）。
- 流程：开考（有进行中的练习或考试时拒绝；先核对每道题就绪）→ 逐题作答，每题复用练习的底层能力（快照、baseline / inject、确认故障生效、隐藏事务、命令与输出采集、复位），attempts.mode = exam 并关联考试 → **「提交本题」验收**（吸收 ensp-coach「我修好了」：系统跑一遍 checks 作为本题结果；可在次数内重试；通过或次数用完即锁定）→ 下一题（本题若还有提交机会且未通过，系统按设备当前状态代为提交一次，然后复位、评分）→ 交卷（二次确认；没开始的题记未作答）。
- 计时以服务端为准：截止时间存库，计时器到点自动处理（总时长到 → 交卷；单题超时 → 下一题）；系统注入 / 复位耗时不计入考试时间；服务重启后恢复进行中的考试，停机时间照常流逝。复位暂不可执行时本题停在「复位中」并自动重试，复位完成前不开始下一题。
- 答案保护：考试进行中任何接口都不返回 checks / solution / inject / reference_path / scoring_points（及种子与参数取值），错误信息中的场景命令打码；交卷后的成绩单只给参考思路，不给 checks 原文。
- 评分：每题沿用 §9（模板权重），提示扣分扣在本题总分上；考试总分为各题加权平均（未作答记 0），异步汇总。成绩单含各题三项分数、点评、用时、命令数、提示数；模板开启回放时可按时间顺序逐条回放各题命令与输出。

## 9. 评分模型

三维加权（权重与折扣系数均可配）：
- **结果分 50%**：配置 diff / `target_state` 达成，硬指标。
- **过程分 35%**：命令日志，AI 判方法论——先定位(`display`)后改配置、效率、路径合理性。
- **规范/副作用分 15%**：syslog + 危险/多余操作——有无造成新邻居 Down、误关接口等附带损伤。

关键规则：**结果未达标时，过程分 ×0.6 折扣**，防止"乱试没修好却靠命令数量刷分"。

里程碑 5 落地（`backend/grading.py`）：
- 结果分由场景 checks 是否全部通过确定性计算，不交给 AI；过程分、规范/副作用分由 AI 给 0~100 的得分率和理由，权重、越界截断、未通关折扣、提示扣分都由代码计算（AI 不自行打折）。权重 / 折扣 / 提示扣分可用环境变量配置。
- 过程分依据：学员每条命令及其设备输出摘要（ConsoleHub 采集，§5.1；隐藏事务的输出不记录）、场景 symptom / reference_path / scoring_points / knowledge_tags；判方法论：先定位再改配置、分层排查、效率、有无盲目试错、修复后是否验证；用了第 3 级提示最高 60%。
- 规范/副作用分：先按规则抽取确定性信号（危险或多余的配置命令、报错命令、重复命令、学员第一条配置命令之后出现的接口 / 邻居 Down 日志；之前的日志视为注入余波不计），再交给 AI 综合判断。
- AI 同时输出整体点评（做得好的、问题、改进建议）和对照 reference_path 的标准排障思路讲解。
- 时机：练习结束（通关或放弃）后，先执行场景 restore 和整机复位（§10）、把复位结果写入 `attempts.reset_json`，**再**异步评分，不阻塞「结束练习」；复位暂不可执行（学员停在系统视图等）时练习保持进行中、不评分，重试结束成功后才评分。前端轮询 `GET /api/practice/result/{id}`（只能看自己的），失败可 `POST /api/practice/rescore/{id}`。
- 复位结果交给 AI 作为背景（重新评分时从库里读）：复位成功是系统行为、不算学员功劳；复位失败、无法自动撤销或不在复位范围内的配置是系统问题，**不扣学员的分**；学员自己敲过的多余 / 有害命令仍按操作记录照常评判。评分全文 `score_json.reset` 记录这份摘要。
- 降级：AI 不可用（无 Key、鉴权、限流、超时、网络、解析失败）时只给结果分，过程分 / 规范分留空不伪造，标注「AI 点评不可用：原因」。

## 10. 拓扑/配置复位

- **优先：配置级快速复位**（秒级）——把每台设备配置还原到 §6 的初始快照（撤销故障 + 撤销学员改动）。
- **兜底：整拓扑重载**（重启 VRP 虚机，慢但必回干净态）。
- **已实现（里程碑 4，`backend/config_reset.py`）：快照差异回滚**。VRP 没有整份替换 running-config 的命令（§3 实测），所以：
  1. 开练前（baseline、inject 之前）给每台 required 设备拍 `display current-configuration` 快照，存 db 的 `attempt_configs`（服务重启后正在进行的练习仍能复位）。
  2. 结束时先执行场景 restore，再 `restore_to`：抓当前配置，按 VRP 配置的层级结构（列 0 的 `#` 分段、视图块、缩进子行）与快照比对，生成撤销 / 补回命令，在对应视图里执行；执行后重新抓取、重新比对，直到一致（最多 4 轮，每轮都从新抓的配置重新计算）。
  3. 顺序：先删共同块里多出的子行（倒序），再删多出的顶层行 / 块（倒序，整块 undo 块头），再按快照顺序补回缺少的，sysname 最后改回；嵌套块先删子行再删块头。每组命令从用户视图进入对应视图执行，进视图失败整组跳过；`[Y/N]` 自动确认。undo 写法不对称的用规则表，其余先试 `undo <整行>`、失败再去掉末尾 1~2 个参数；`vlan batch` 按 VLAN 集合比较。
  4. 复位后仍不一致的配置行原样列出，提示「需要在 eNSP 中重载拓扑」（兜底），不当作成功；学员停在系统视图、正在输入等暂不可执行时练习保持进行中，可重试。
  5. 排除：`user-interface` 块与 `aaa` 下的 `local-user` 行不比对、不复位，单独列出（前者改错会把学员和系统锁在 console 外，后者密码是不可逆密文且与排障题无关）。
- 实测可撤销（2026-10-04，AR1220 ×2 + S5700 真机）：接口 IP 增 / 改 / 删（含 sub）、`shutdown`、`description`、静态路由、OSPF 进程 / area / network、ACL 规则与 `traffic-filter`、LoopBack / Vlanif、VLAN 创建删除、`port link-type`、`port default vlan`、trunk allow-pass、`stp mode`、`sysname`（提示符随之切回）。真机 e2e（`tests/e2e/reset_e2e.py`）在三道 Campus 题上做完这些改动后，每台设备与开练前快照逐行一致；放弃路径、服务重启后复位同样验证通过。未在真机上试过 BGP、RIP、DHCP、NAT、STP 端口参数。

- YunShan（2026-10-06 eNSP Pro 实测）：`config_reset` 直接可用，只需跳过 `!` 注释行（`Last configuration was updated / saved at` 每次都变）。真机 e2e 中学员额外改了 sysname、LoopBack、另一接口的 description 与 shutdown、静态路由，复位一轮 8 条命令全部撤销并复抓一致（命令见 §3）。替代验证（同日）补测 ENSP-LSW（S6700）：VLAN batch / vlan 视图描述 / access / trunk allow-pass / trunk pvid / shutdown / Vlanif / LoopBack / stp mode / 静态路由 / sysname 共 21 处改动，复位一轮 31 条命令与出厂配置一致；e2e 额外乱改（AR0 ACL 与黑洞路由、AR2 LoopBack 与接口描述 / shutdown、LSW0 sysname / VLAN / trunk / access / Vlanif / stp / 路由）复位各一轮撤销干净。

### 10.1 必须在 eNSP 界面上做的操作
网页能覆盖一次学习或考试中的终端操作、拓扑查看、故障注入、判定评分和配置复位。以下操作只能在 eNSP 界面完成：
| 操作 | 何时需要 | 处理方式 |
|---|---|---|
| 打开拓扑文件 | 换基础拓扑时 | 系统可通过文件关联让 eNSP 打开，但会弹「是否保存」，需人工点击 |
| 启动、停止设备 | 换基础拓扑或首次准备时 | 只能人工；系统按 GUID 核对是否到位 |
| 配置 PC、STA 等模拟终端 | 不需要 | 按 §6.2 用路由器扮演主机 |
| 增删连线、改拓扑 | 仅出题时 | 管理员在 eNSP 中完成 |
| 抓包 | 学习时可选 | 直接使用 eNSP，不纳入考试 |

风险：学员绕开网页、在 eNSP 自带的命令行窗口操作时，系统记录不到过程，过程分无法评定；结果分仍可从设备配置和状态判定。设备日志默认不记录执行的命令（已在 R1 上查看 `display logbuffer` 确认），能否通过基线配置开启命令记录待验证（§14）。

可选增强：后台服务与 eNSP 处于同一桌面会话，可用界面自动化（如 pywinauto）代为点击「是否保存」和「启动设备」。eNSP 版本固定，但受分辨率和窗口状态影响，需实测稳定性后再决定是否采用。

### 10.2 用外部虚拟设备替代 eNSP 中不可操作的设备（评估结论，暂不实施）
eNSP 对外的唯一通道是 **Cloud 设备**：把拓扑中的一个接口桥接到 Windows 的某块网卡，与该网卡同一二层网段的外部主机即可和 eNSP 设备直接通信。

| 方案 | 做法 | 结论 |
|---|---|---|
| A. 跨机接 Linux 上的 containerlab | Cloud 桥接 Windows 的 Wi-Fi 网卡，经局域网到 Linux 容器 | 不采用。Wi-Fi 通常不转发其他 MAC 的帧，二层桥接不可靠；只能走三层（需开 Windows 路由转发、两侧加静态路由），DHCP 等二层题目无法实现 |
| B. 在 eNSP 机器上跑轻量 Linux 主机 | VirtualBox 起 Alpine 小虚机接 Host-Only 网段，Cloud 也桥接到同一网卡 | **首选**。二层完整，可做 DHCP、ARP、DNS、HTTP；虚机串口开成 TCP 端口，网页终端按现有方式直接接入 |
| C. 整体迁到 EVE-NG / containerlab | 所有设备都在 EVE-NG/containerlab 中运行 | 长期方向。有 API 可自动起停、可抓包、主机真实；但 eNSP 的 AR/S（VRP5）没有可单独使用的镜像，NE40E/CE（VRPv8）缺乏正规授权渠道，且命令体系与 HCIA 所学的 VRP5 不同 |

- 在 Windows 上装 WSL2 + Docker 跑 containerlab：不与现有虚拟化冲突，但 WSL2 网络为 NAT 隔离，与 eNSP 打通二层困难，不推荐。
- 可替代：有线 PC、服务器（方案 B，能力强于 eNSP 自带 PC，系统可读取其状态用于判定）。不可替代：无线终端 STA（无线在 eNSP 内部模拟，外部设备无法接入 eNSP 的 AP）。帧中继交换机 FRSW 不考虑。
- 何时做：交换、路由排障题（VLAN、STP、OSPF、ACL、NAT）用 §6.2 的「路由器扮演主机」即可；需要「用户电脑上网异常」类应用层题目（DHCP 拿不到地址、DNS 解析失败、能 ping 通网关但打不开网页）时，再启动方案 B 的验证（§14）。
- 实施方案 B 时，外部主机作为新的设备类型接入 `DeviceConnector`，主机的启停归入 `LabProvider`；方案 C 则新增 EVE-NG/containerlab 的 `LabProvider` 与 `DeviceConnector` 实现，上层不变（§5）。

## 11. 学生档案

- **身份**：登录名 / 姓名 / 明文密码。
- **进度**：已完成场景、当前学习路径节点。
- **掌握度画像**：按 `knowledge_tags` 的熟练度，供 AI 自适应出题/难度调节。
- **历史记录**：每次练习/考试的场景、用时、得分、提示消耗、完整命令回放、最终配置。
- **错题本**：未通过/低分场景，供针对性复练。

## 12. 鉴权

本地明文密码，仅区分人员，不涉及保密；登录后载入对应档案。

**自动登录**（2026-10-07，里程碑 6.17；用户部署在一台单人使用的 Windows 笔记本上，要求跳过登录页直接用 admin）：环境变量 `AINET_AUTO_LOGIN=<登录名>`，缺省空 = 关闭。开启时启动确保该学员存在（不存在则创建，显示名同登录名，密码随机且不输出，只是占位；已存在的不改密码），日志只写「自动登录已开启：<登录名>」；没有有效会话的页面 / 接口 / 终端 WebSocket 请求一律当作该学员（`current_student_id` 回退），HTTP 响应补发会话 cookie（中间件，与 `api_login` 同一套 `SESSIONS`，共用一个 token）；`/login` 303 到课程页；`GET /api/me` 带 `auto_login`，前端隐藏「切换学员」「登出」，「我的档案」保留。**安全提示**：自动登录开启后，能访问 ainet 端口的任何人都以该账号身份操作（看档案、开练 / 考试、改接入设置、启停设备），没有任何口令。只适合单人使用的本机 / 局域网部署；不要在共享网络上开，也不要把端口转发到外网。它只能用环境变量开启，没有经接口或页面开启它的方式。

## 13. 数据模型（SQLite，初稿）

- `students(id, login, name, password_plain, created_at)`
- `knowledge_tags(id, name, category)`
- `topologies(id, name, ensp_ref, note)`  // ensp_ref 为 .topo 路径
- `scenarios(id, topology_id, required_devices_json, accessible_devices_json, symptom, inject_script, target_state, reference_path, scoring_points, tags_json, baseline_json, created_at)`  // 里程碑 3 补列：key（场景 JSON 的 id，唯一）、title、level、topology_ref、data_json（场景全文）、updated_at；inject_script / target_state 存 inject / checks 的 JSON。场景文件是单一事实源，表为镜像
- `exam_templates(id, name, config_json)`  // 计时/禁提示/权重/重试/回放 等；里程碑 6 补列 created_at、updated_at，config 见 §8
- `exams(id, student_id, template_id, template_name, config_json, status, started_at, deadline_at, ended_at, state_json, score_total, score_status)`  // 里程碑 6：考试会话；config_json 为开考时的模板快照，state_json 为各题状态（种子、顺序、提交记录、计时、复位结果），deadline_at 为绝对截止时间（含系统操作顺延）
- `attempts(id, student_id, scenario_id, mode, exam_template_id, started_at, ended_at, score_total, score_result, score_process, score_side, hints_used, passed)`  // 里程碑 6 补列 seed、param_values_json（参数化场景的种子与本次取值）、exam_id、exam_index（mode = exam 时关联考试与题号）  // 里程碑 5 补列：score_status（scoring / done / degraded / failed）、score_json（评分全文：三维分数与理由、AI 点评、标准思路、确定性信号、复位摘要、token 用量）；里程碑 4、5 集成补列 reset_json（结束时整机复位的结果：是否复位干净、无法撤销 / 不在范围内的配置行、各设备详情）；降级时 score_process / score_side 为 NULL、score_total = 结果分
- `attempt_configs(attempt_id, device, kind, port, config, created_at)`  // 里程碑 4：开练前每台设备的整机配置快照（kind=before），复位目标
- `attempt_logs(id, attempt_id, ts, command, output)`  // 命令回放；里程碑 3 补列 device（命令所在设备）；里程碑 5 起 output 存学员命令的设备输出摘要（截断，含其后到达的异步日志），隐藏事务不记录
- `student_mastery(student_id, tag_id, level, updated_at)`

### 10.3 远程启动设备（调研结论，2026-10-05）
- 其他 agent 的调研报告（Windows 上 `C:\Users\15111\xixi\ainet\ENSP-私有协议逆向研究报告.md`）确认：eNSP 启动路由器的底层动作是对 VBox 模板做链接克隆、把串口接到命名管道 `\\.\pipe\<设备ID>`、再无界面启动；停止为关机；关闭拓扑时删除克隆（与 §3 观察一致）。
- 报告中的原型需要替换 eNSP 后台服务（要 UAC 提权、出错会使 eNSP 卡死，且未实现设备间连线和交换机），**不采用**。
- 低风险候选：设备克隆仍在、只是被停止（如主机睡眠后）时，直接对该克隆执行 `VBoxManage startvm --type headless`，验证 console、设备间连线与 eNSP 界面状态是否正常（§14 第 11 项）。可行则做「一键恢复本题所需设备」。
- 已采取的预防措施：eNSP 主机已关闭睡眠（主机睡眠会停掉 VBox 设备）。
- eNSP Pro 后端（§10.4）有官方启停接口，ainet 拓扑图上直接「启动 / 停止」设备（练习 / 考试中不能停本题需要的设备）；eNSP 经典版仍只能人工启停。

### 10.4 eNSP Pro 实验后端（2026-10-06 实现，`backend/enspro.py`、`backend/connectors/enspro.py`）
- **配置**：`AINET_LAB_PROVIDER=enspro`、`AINET_ENSPRO_URL`（缺省 `https://ensp-pro.example:8443`；2026-10-07 起也只是缺省值，课程页「接入设置」改过以后以库里的 `lab:endpoint:enspro` 为准，§5.2）、`AINET_ENSPRO_SANDBOX`（当前沙箱 id，拓扑 = 这个沙箱；两者都只是缺省值，页面上切换后以库里记录为准，§5.2）、`AINET_ENSPRO_AUTO_RELOGIN`（缺省 0）与 `AINET_ENSPRO_RELOGIN_INTERVAL`（缺省 30 秒）；启动看门狗的 `AINET_ENSPRO_START_TIMEOUT` / `_START_RETRIES` / `_EARLY_CHECK` / `_PROBE_TIMEOUT` 2026-10-07 起也只是缺省值，课程页「接入设置」改过以后以库里的 `lab:watch:enspro` 为准（§5.2、§10.5）；设备 console 密码放 secrets.env 的 `ENSPRO_DEVICE_PASSWORD`（与 DeepSeek Key 同一套读取路径，不进代码 / 日志 / 提交）。
- **REST**：离线登录 `GET /` → `GET /api/login?service=<origin>`（跟随跳转），cookie jar 保存 `__Host-SESSION` 与 `csrfToken`，之后请求头带 `X-CSRF-TOKEN` / `csrfToken`；`{code, message, data}`，code 0 成功。会话被挤掉（`50070014` 在别处登录 / `50010005` 未登录）时：缺省不自动重登，页面状态栏提示并出现「重新登录 eNSP Pro」按钮（会挤掉别处的会话，有确认框）；开了自动重登则按最小间隔重登，避免与网页互相挤。服务退出时登出。沙箱详情缓存 2 秒。新建连线 `POST /service/ensp/link/create {leftDeviceId, leftIfId, rightDeviceId, rightIfId, linkTypeId:1, sandboxId}`（从前端 bundle 确认）。
- **拓扑转换**：`getOneSandbox` 的 devices / links → 前端原有的拓扑结构：设备 id = deviceId 字符串，`console` = deviceId；`devicePosition` 是 -1~1 的归一化中心坐标，乘 2500 成画布坐标（`anchor=center`）；kind 由 deviceType（superDeviceType / iconName）推导；接口名取 deviceIfs（按 serialId）；连线按接口 id 映射成接口名；设备 / 连线按 id 排序（接口返回顺序不固定）；状态 1、4 online，0 stopped，2、6 starting，3、5、17 error，ainet 发起启停后状态没变过来之前显示 starting / stopping；背景图形（graphicsVOList）与文本框（textBoxs）尽力转换（沙箱 2 里没有实例，字段名未验证）。`mtime` = 拓扑结构指纹，前端据此判断要不要重画。
- **console 连接器**（STOMP over WebSocket）：CONNECT → SUBSCRIBE `/user/topic/console/message|heartbeat/{sb}/{dev}` → SEND `/app/console/connect/…`；输入 SEND `/app/console/message/…`；应用层心跳每 30 秒；关闭 SEND `/app/console/close/…`。帧解析支持一条消息多帧、帧跨消息、心跳换行、content-length。**登录状态机**（成功后才交给 ConsoleHub）：连上发一次回车（服务端回放显示已停在密码提示时不发）；首次登录按提示设密码（输两遍）；`Password:` 只输一次密码，再出现 `Password:` 或错误即判定失败——记在 provider 里，之后对该设备的连接一律直接拒绝、不再输密码（状态栏与终端都给出原因），直到由 ainet 重新启动该设备或服务重启；登录后若问 `[Y/N]` 回答 N。交给 hub 之后设备又回到登录提示（空闲登出、学员 quit）：连接器从提示出现那一刻起吞掉学员输入，自己登录一次（同样只试一次）。会话失效时建连前先查 `getUserInfo`（被挤掉的会话 WebSocket 握手照样成功但收不到输出）。断线重连沿用 hub 的最小间隔。
- **设备启停**：`POST /api/lab/devices/start|stop|reboot {devices:[deviceId]}`（需登录，只有 eNSP Pro 开放；练习 / 考试进行中不能停止 / 重启本题需要的设备；停止时断开该设备的 console）；`GET /api/lab`；`POST /api/lab/relogin`。前端：拓扑图节点下方「▶ 启动 / ■ 停止」按钮，确认框，启动中显示「已用时 + 阶段」，启停期间 2 秒轮询一次。所有启动经启动看门狗（错开、停机间隔、卡住自动停止再启动、就绪探测，§10.5），状态多出「系统初始化中」「启动失败」。
- **终端主机（PC，2026-10-06 替代验证）**：`LabProvider.is_host / host_get / host_set`；eNSP Pro 实现用设备属性接口（§3），设完等 `ipv4.status` 回到 Ready；ConsoleHub 认 `PC>` 提示符，只读判定命令发出后无回显、又停在提示符上时重发一次（PC 命令行重启会吞输入）。练习流程对场景 `hosts` 的设备：快照 = 地址配置（`!ainet-host {json}`）、配置命令翻译成 `host_set`、复位 = 设回快照。
- **干净基线（应对自动保存 / 停机保存，2026-10-06）**：`app_settings` 键 `baseline:enspro:<沙箱名>:<设备>` 存每台设备的干净配置（PC 存地址配置）。练习 / 考试每题开始拍快照之前先与基线比对：一致 → `clean`；不一致 → 先复位到基线（`reset`，开练快照取复位后的配置），复位不干净 → 拒绝开始；没有基线且 `AINET_LAB_BASELINE_AUTO=1`（缺省）→ 记为基线（`captured`）。结果在练习状态的 `pre_reset`。`GET /api/lab/baseline` 查看、`POST /api/lab/baseline {devices?}` 手动重记（练习 / 考试中拒绝）。只对 eNSP Pro 启用（VRP5 不自动保存）。
- **其他（2026-10-06）**：`batchReboot` 返回 405 时 provider 退回「停止 → 等到已停止 → 启动」（返回 `via: stop+start`；2026-10-07 起接口的重启改由 PowerWatch 做，不再调 batchReboot，§10.5）；console 登录超时的报错带设备最后一行输出、登录步骤写日志；`Please Press ENTER.` 按最后一个非空行识别，提示一出现连接器就自己回车登录；AR6700 异步打印的 `Error: Failed to apply the traffic-filter` 按日志处理。
- **替代验证（2026-10-06，沙箱 `ainet_campus` id 3：LSW0=8、AR0=9、AR2=10、AR1=11、PC1=12、R1~R3=14~16，`tests/e2e/enspro_campus_e2e.py`）**：5 道 YunShan 题练习各 5/5、额外乱改 6/6、3 题考试 7/7、会话被挤掉与重新登录 6/6；结论与证据见 `docs/enspro-replacement.md`。
- **真机验证（2026-10-06，`tests/e2e/enspro_e2e.py`）**：basic 6/6（拓扑与状态、T1/T2 终端 display version / display ip interface brief、网页停止 T2 约 1 秒变已停止、再启动 108 秒回到运行中、重启后终端重新登录、无 JS 异常）；practice 5/5 与 practice-extra 5/5（YunShan 互联题开始约 8~12 秒 → 学员修复 → 自动判定通关 → 结束复位与开练前快照一致；extra 模式下学员额外改动由整机复位一轮撤销）。全部结束后 T1、T2 的 `display current-configuration` 与开工前存档逐行比对，只有 `!Last configuration was updated at` / `!Last configuration was saved at` 两行不同。

### 10.5 启动可靠性（2026-10-07 实现，`backend/labpower.py`，eNSP Pro）
根因与证据见 §14 第 21 项。目标：设备卡在启动中能自动恢复、失败时给出可读原因；「运行中」之后确认系统真正可用再开题；不用的设备自动停掉，省内存。
- **统一入口**：所有由 ainet 发起的启动都经 `PowerWatch`（`LabManager.power`）——拓扑图按钮 / `POST /api/lab/devices/start`、按题目准备环境、`POST /api/lab/devices/reboot`、看门狗的自动重启。重启一律是「停止 → 等 `restart_gap` 秒并确认接口已是已停止 → 启动」（返回 `via: stop+start, start_after_s`），不再调 `batchReboot`。
- **错开启动**：同一时刻只发一台的启动请求，相邻两次至少隔 `AINET_ENSPRO_START_STAGGER` 秒（缺省 5；0 = 一次批量）。第一台同步发出（会话失效等错误直接返回给调用方），其余由后台队列发出；排队中的设备状态为 starting、进度文字「排队启动」。根因分析没有发现并发启动是诱因（§14 第 21 项），错开只是为了降低瞬时负载、便于定位。
- **停机间隔**：同一台设备距 ainet 上次发出停止不到 `AINET_ENSPRO_RESTART_GAP` 秒（缺省 10）不发启动（进度文字「等上次停机清理完再启动」）。这只是保守的安全余量：2026-10-07 SSH 复现确认卡住与停机间隔无关（间隔 150 秒、约 3 分钟照样卡，5 秒也有正常的，§14 第 21 项），不能当作根治手段。
- **看门狗**：后台每 `AINET_ENSPRO_WATCH_POLL` 秒（缺省 5）读一次接口状态。启动超过 `AINET_ENSPRO_START_TIMEOUT` 秒（缺省 150；正常 LSW 55~70 秒、AR 110~125 秒）仍在状态 2 / 6，或接口报 3 / 5 / 17，就断开该设备的终端、自动「停止 → 启动」`AINET_ENSPRO_START_RETRIES` 次（缺省 3，2026-10-07 用户决定；按约 43% 卡住率，准备环境最终失败约 1/30）；仍不行标为 **start_failed**，原因写进拓扑的 `start_errors {deviceId: 原因}` 与进度文字（如「启动 150 秒仍停在「启动系统」（状态 6）；已自动停止再启动 1 次仍未就绪…」）。**早期卡死检查**（`AINET_ENSPRO_EARLY_CHECK`，缺省 40 秒，0 关闭）：设备在「启动系统」（状态 6）满 40 秒、80 秒时各下载一次设备日志（`GET /service/ensp/device/downloadLog?deviceId=N`，只读；zip 内 `vpnf_*.tar.gz` 是容器日志），容器日志里有 `systemctl_reboot.log`（`ignore systemctl reboot command`）= 已陷入板卡复位循环，不等 150 秒立即按卡住处理，原因写「设备系统启动早期服务异常退出（sfed 退出码 17）、请求重启被容器忽略，陷入板卡复位循环」（2026-10-07 对 11 份真机日志判定全部正确：4 份卡住、7 份正常）。**参数按环境配置**（2026-10-07，里程碑 6.16，§5.2「接入设置」）：启动超时、自动重启次数、早期卡死检查、就绪探测上限可在课程页 / 接口里按环境改，上面的环境变量只是缺省值。**超时误判的事后恢复**：最后一轮判卡住标 start_failed 时不停设备，看门狗继续跟踪；之后接口报运行中，照样进 initializing、就绪探测通过即恢复为 online（`start_errors` 里不再有它，事件写「此前判为启动失败，设备随后自行启动完成」）；对这种已在运行的 start_failed 设备点「启动」不再停止重启，只等就绪探测（返回 `recovering`）。**没有做「超时前延长等待」**：接口在「启动系统」（状态 6）期间没有可靠的推进信号——`boot_failure` 拿不到日志与没有复位循环都返回空，不能据此判断设备健康；`progress` 字段只记录过正常启动的序列，卡住时怎么变没有真机记录——误延长会让真卡住的设备多等，所以靠按环境调大启动超时解决慢主机的问题。对 start_failed 的设备再点「启动」= 先停止再启动、计数清零。设备在别处被停掉（接口持续已停止超过 30 秒）不再跟踪。
- **就绪探测**：接口报运行中（1 / 4）后，设备状态先是 **initializing**（系统初始化中），看门狗每 `AINET_ENSPRO_PROBE_INTERVAL` 秒（缺省 10）经 ConsoleHub 隐藏事务（kind=check，只读）执行 `display current-configuration | include sysname`，输出里出现 busy / building configuration 等字样 = 仍在初始化；不 busy 才算就绪（状态回到 online）。选这条的实测依据（2026-10-07，T2 冷启动，接口报运行中后每 6 秒试三种）：读配置在 0~27 秒报 `Error: The system is busy in building configuration. Please wait for a moment...`、34 秒起正常；改配置（接口 description 设了立即撤销）在 21 秒报 `Info: The forward engine is not ready.`、27 秒报 `Error: The system is busy in building system configurations…`、35 秒起正常——两者 busy 窗口一致；`system-view` / `quit` 全程不报 busy，不能用作探测。学员正在输入、连不上 console 时本轮不算数。`AINET_ENSPRO_PROBE_TIMEOUT` 秒（缺省 300）内一直没就绪按卡住处理（自动停止再启动 / start_failed）。隐藏事务的输出不进学员终端、不产生命令记录（§5.1）。`AINET_ENSPRO_PROBE=0` 关闭。
- **状态与接口**：设备状态新增 `initializing`、`start_failed`（`/api/topology/current` 的 `devices`、`topology.devices[].status`，`progress` 进度文字，`start_errors`；`running` 只数 online；消息里注明「N 台系统初始化中」「启动失败：…」）。initializing 的设备可以开终端（console 已可用，看启动过程）。开练 / 开考的就绪判断（`scenarios.readiness`）：有 initializing → 409「设备 X 系统初始化中（initializing：刚启动的设备改配置会报 busy），请稍候再开始」（课程页据 detail 里的 busy / initializing 自动重试）；有 start_failed → 409 带失败原因。`GET /api/lab` 新增 `source`、`watch {参数, devices, events}`、`idle_stop`、`stop_old`、`last_stopped_old`。
- **按题目准备环境**：启动走 PowerWatch，阶段 `queued → switch / sandbox → start → wait →（卡住时）restart → probe → done | error`；有设备 start_failed 时任务失败（504，带原因）；等待上限按当前环境的看门狗参数推算（错开 +（启动超时 + 60）×（自动重启次数 + 1）+ 探测超时 + 60 秒；如启动超时 300、重启 3 次、探测 300 时约 30 分钟），浮层的 `estimate.max_s` 据此给剩余；「预计还需」仍用经验值（ainet 没有记录历史冷启动用时，不做统计）。任务新增 `stopped_old`（切换时停掉的旧沙箱设备，列表）与 `notes`（各设备进度文字）。原来「180 秒未就绪停止再启动」由看门狗取代。
- **切换时停掉旧沙箱的设备**（`AINET_ENSPRO_STOP_OLD`，缺省 1）：显式切换沙箱 / 环境、按题目准备环境切到新沙箱时，在旧后端退出登录之前停掉旧沙箱里运行着的设备（状态 online / starting / error），切换接口返回 `stopped_old {ok, sandbox, sandbox_name, devices[, error]}`（停不掉只记录，不影响切换）。理由：已定为单人使用，旧沙箱的设备没人用却各占约 2.4GB（§3 资源）；**eNSP Pro 停机时会保存配置**（§3 配置持久化），所以只能在复位干净之后停——切换本来就要求没有练习 / 考试进行中（拿着切换锁、考试锁、练习锁检查），上一题结束时已整机复位，停机保存的就是干净配置。停机时刻记进 `stopped_at`，切回来时同样遵守停机间隔。
- **空闲自动停机**（`AINET_ENSPRO_IDLE_STOP_MIN`，缺省 30 分钟，0 关闭）：活动 = 终端输入、提交的命令、命令输出（hub 事件，不含异步日志与空闲登出后的自动重登）、练习 / 考试进行中、Agent 执行命令、启停、准备环境、切换；有设备在排队 / 启动 / 初始化时也不停。超过 N 分钟无活动，停掉当前沙箱里运行着的设备（经 PowerWatch，记停机时刻）。停之前提前 `AINET_ENSPRO_IDLE_WARN` 秒（缺省 60）给打开着的终端发通知（`ConsoleHub.notify_all`，不经设备、不进回放），终端里按任意键即取消；停机时再发一条并断开这些终端。练习 / 考试进行中绝不停（停机会保存未复位的配置）。
- **服务重启**：看门狗状态（未完成的启动、停机时刻）存 `app_settings` 的 `lab:power`，启动时只恢复当前环境与沙箱的记录：排队中的照样发出，启动中 / 初始化中的接着计时、接着探测，启动失败的照样显示失败；就绪的不留记录，所以不会留下错误状态。
- **AINET_LAB_FORCE=1**：启动时以 `AINET_LAB_PROVIDER` / `AINET_ENSPRO_SANDBOX` 为准（没设沙箱时沿用库里的），并写回库；启动日志写明当前环境来自「数据库记录」「环境变量（AINET_LAB_FORCE=1）」还是「环境变量缺省值（库里还没有记录）」。

## 14. 待验证技术点（开发前 spike）

1. ~~配置级快速复位在 VRP 上的可靠方式（§10）~~：**已解决**（2026-10-04）。AR1220 V200R003 没有 `configuration replace` / `rollback configuration` / `commit` / `load`，S5700 只有 `clear configuration interface`，所以采用「开练前 `display current-configuration` 快照 + 按配置层级差异生成撤销命令 + 复抓比对直到一致」（§10）。实测要点：`undo traffic-filter inbound acl 3000`、`undo rule 10 deny icmp` 报 Too many parameters，只能写 `undo traffic-filter inbound`、`undo rule 10`；`undo area` 前要先删该 area 的 network；`undo ospf`、`undo vlan batch` 要 `[Y/N]` 确认；整行 `undo vlan batch` 会删掉快照里本来就有的 VLAN，须按集合比较；S5700 的 `DS/4/DATASYNC_CFGCHANGE` 日志不带 `%%`、打印后不重印提示符、可能插在 `---- More ----` 之后。
2. ~~同一 console 端口的并发会话数（系统代理 + 是否允许旁路观察）~~：**已确认**（2026-10-04）多个连接可同时连上同一端口，且**共享输入输出**（像共用串口，§3）。处理方式：ConsoleHub（§5.1）每个端口只保持一条设备连接，多个标签订阅共享；系统命令走隐藏事务（暂停转发、学员输入暂存、日志补发），学员看不到；关标签只退订、不断开，空闲 10 分钟后才关，重连有最小间隔——学员关掉标签又立刻重开时不再新建连接，避开「断开后几分钟内重连收不到输出」的现象（该现象本身仍存在，服务重启后首次连接可能遇到）。
3. console 裸 telnet 的分屏/提示符识别稳健性（PoC 已有基础版）。
4. 多场景下 eNSP 设备与拓扑的半自动上电方案（候选：界面自动化，见 §10.1）。
5. 重新分配 GUID 后的 `.topo` 能否被 eNSP 正常打开并启动设备（§6.2 第 4 条）。
6. 能否通过基线配置让 VRP 记录执行过的命令，用于识别学员绕开网页的操作（§10.1）。
7. ~~服务以 `15111` 用户运行时 `VBoxManage list runningvms` 能否列出 eNSP 虚机~~：已确认可以（2026-10-04，start.bat 启动的服务检测方法为 vboxmanage）。
8. 方案 B（§10.2）验证，需要应用层题目时再做：① eNSP Cloud 桥接到 Host-Only 网卡，与同网段 Alpine 虚机互相 ping 通；② 多块 Host-Only 网卡各对应一个独立网段，能否构成多个互不相通的网段；③ 新增网卡是否影响 eNSP 自身运行（VirtualBox 兼容层脆弱，必须先测）。
9. eNSP_Client 崩溃原因（2026-10-04 03:28，0xc0000005，出错模块为 eNSP_Client.exe 自身）：当时正每 5 秒对 35 个 console 端口做连接探测，疑似诱因但未证实，已取消定时探测；崩溃后虚机与交换机进程会残留运行，需人工清理。
10. ~~旧格式 `.topo`（如 Campus_Network）的接口序号到真机接口名的映射~~：**已解决**（2026-10-04）。序号按真机接口顺序（`display interface brief`）解释：AR1220 = GE0/0/0-1 + Ethernet0/0/0-7，S5700 = GE0/0/1-24；`topology._interfaces` 对这两个型号的主板直接用真机排列（文件主板接口总数一致时），其余型号仍按文件推断。锚点见 §3（AR0–AR2、AR0–LSW0 两端配地址 ping 通，各口物理状态吻合）；2-1 OSPF 的 R1 idx0 = GE0/0/0 不变。Campus 拓扑图的接口标注已与真机一致（真机 e2e 在页面上核对）。

11. 设备克隆仍在时，外部 `VBoxManage startvm` 能否恢复设备的 console、设备间连线和 eNSP 界面状态（§10.3）。须确认无人在用设备后再测。
12. PC/Server 进程（`eNSP_PCClient.exe`）能否从外部读取状态或执行 ping，若可行可修订 §6.2 第 2 条「用路由器扮演 PC」。
13. eNSP Pro：读配置 `POST /service/ensp/configuration/getConfigurations`、命令日志 `POST /service/ensp/cmd/log/list` 的参数（按猜的 body 调用返回 HTTP 405，bundle 里没找到调用处）；命令日志若可用，可补上「学员绕开网页、在 eNSP Pro 自带终端里操作」的过程记录（§10.1 风险）。
14. ~~eNSP Pro：其他 YunShan 设备类型是否两阶段生效；交换机的 VLAN 等撤销写法~~：**ENSP-LSW 已验证**（2026-10-06）：S6700 单阶段生效，VLAN / access / trunk / pvid / Vlanif / stp mode 的写法与撤销实测可用、整机复位干净（§3、§10）。仍未验证：ENSP-CE 等其他型号是否两阶段、Eth-Trunk、STP 端口参数。
15. ~~eNSP Pro：`configuration file auto-save` 会自动保存运行配置~~：**已解决**（2026-10-06）。实测改动后 5 分钟定时保存、停机必保存（关掉自动保存也挡不住）、行为不稳定，所以不关自动保存，改为每题开始前与干净基线比对、不一致先复位（§10.4）；真机上抓到并复位了一台带旧配置启动的 R3。
16. ~~eNSP Pro：空闲登出后自动重登、会话被挤掉后的提示与重新登录~~：**已在真机验证**（2026-10-06）。console 空闲约 5 分钟登出，连接器按 `Please Press ENTER.` 立即自动重登；第二个登录会话挤掉 ainet 后页面 1.5~4 秒内提示并出现「重新登录」按钮，重新登录后终端恢复；自动重登模式按间隔重登；全新设备首次登录设密码 7 台均成功。
17. eNSP Pro：沙箱背景图形（graphicsVOList）与文本框（textBoxs）的字段格式（沙箱 2 里没有实例；2026-10-06 沙箱列表接口里看到 demo 沙箱 1 的实例：图形 `{type:2, positionX/Y 归一化, width/height 像素, color, isFill, boardWidth}`、文本框 `{positionX/Y, content, fontColor, backgroundColor, fontSize}`，`_shapes` 把 width/height 当归一化值乘 2500，尚未按实例修正）。~~切换沙箱要改 `AINET_ENSPRO_SANDBOX` 并重启服务~~：已改为运行时切换（§5.2）。
18. ~~eNSP Pro：考试流程未在真机上跑~~：**已验证**（2026-10-06）：3 题 YunShan 考试真机 + 浏览器 e2e 7/7（开考、提交、未通过重试、下一题、交卷二次确认、成绩单、各题复位成功）。
19. eNSP Pro：EXTERNAL-HUB 启动后停在「分配虚机」（状态 2）不就绪，原因未查（可能需要嵌套虚拟化或资源不足）；目前共享网段用交换机 VLAN 1 代替。
20. eNSP Pro：多台同时冷启动后 console 可能几分钟无输出（接口状态已是运行中），ainet 只能报错让用户稍后重试；能否从接口得知 console 就绪待查。（2026-10-07：接口查不到；改为就绪探测——接口报运行中后经 console 试探，通过前设备为 initializing，300 秒仍不通过按卡住处理，§10.5。）
21. ~~eNSP Pro：设备启动偶发一直停在状态 6（启动系统）不就绪~~：**机制已查清；已排除「上一实例在宿主上的残留」与停机间隔；sfed 退出码 17 指向的资源仍未确定（判断为镜像内部启动竞态）；由启动看门狗兜底**（2026-10-07，§10.5）。
    - **现象**：只有 LSW0（ENSP-LSW，镜像 vpnf_tor_s6730hi，V600R025C10SPC500B679）会卡，T1 / T2 / AR0 等 ENSP-AR 没卡过。卡住后接口一直是 6（`progress` 停在 49），不会自己恢复。卡住率：2026-10-07 上午经接口 14 次卡 7 次；下午 SSH 复现 14 次卡 6 次（约 43%；12 次带完整快照，另 2 次手动启动都正常）。
    - **证据一（设备日志接口，不进虚机）**：`GET /service/ensp/device/downloadLog?deviceId=N` 返回 zip（`op_*.log` 是 vpnf 建容器过程，`vpnf_*.tar.gz` 是容器日志）。卡住与正常两种情况下建容器、加载配置、`vPNF Creating process succeed` 完全相同；卡住时容器内 `sfed` 以退出码 17 退出（`service_failure_proc.log`：`[sfed]=== exit, service result(exit-code), exit code(exited), exit status(17) ===` → `reboot board(0x80060301)`），`systemctl_reboot.log`：`ignore systemctl reboot command`，之后 `vrpv8_startup` 每 15 秒以原因 0x101000b 请求复位，因为 `/dev/shm/sispreboot` 已存在每次返回 300（`boot.log`）。4 份卡住日志特征一致，7 份正常日志都没有。
    - **证据二（2026-10-07 下午 SSH 只读复现，`root@192.168.56.101` 免密；脚本与 13 轮快照在 scratchpad `real-verify/`）**：反复启停 LSW0（单独启动，没有别的设备同时启动），每轮在启动前、启动后 6~32 秒每 2~6 秒、停止后各拍一次只读快照（宿主 `/dev` 与 `/dev/shm`、`/tmp`、`/dev/hugepages` 与 HugePages、`/var/run/docker/netns`、veth / 网桥、loop、device-mapper、sctp / tipc 引用、残留进程、dmesg；容器内 `/dev/shm` 文件名、SysV IPC（`nsenter -i ipcs`，含创建进程）、`/dev/mqueue`、容器日志、`sfe.log`）。
      - 容器是 vpnf 按 `/opt/vpnf/configs/vpnf_container_lsw_x86_64.json` 建的特权容器：`network=none`（vpnf 自建 veth / 网桥）、`IpcMode=shareable`（私有 IPC 命名空间）、`shm_size=1G`（**容器私有的 /dev/shm**）、`auto_remove`、`cap_drop: CAP_LINUX_IMMUTABLE, CAP_SYS_BOOT`，并把宿主 `/dev`（rbind，含宿主上的 0 字节文件 `KLFE0` `board_mng` `kbox` `patch_check` 等）和 `/tmp`（挂到 `/data1`）挂进去。停机后容器连同 `/root/workspace/containers/<实例>` 一起删除。
      - 时间线：启动请求返回后约 14 秒容器创建；卡住时 `sfed` 在容器内约 11 秒启动、1 秒内以 17 退出——`sfe.log` 停在 `RTF_COMP_CONSTRUCT_STAGE3 has completed!`（正常时接着是 `SFE_DrvCtrlIfTask, the drv pnmp is not open, the task quit.`），`service_failure_proc.log` 里没有 sfed 的 `oper type : start`（`Type=notify`，没走到 READY）；正常时 sfed 在容器内约 13~18 秒 READY。卡住特征文件 `systemctl_reboot.log` 出现在启动请求后 28~34 秒（所以早期检查定在 40 秒合适，不必提前）。
      - **`/dev/shm/sispreboot` 不是残留**：它在容器私有的 tmpfs 里，是同一次启动中 sfed 的故障策略（`FAULT_POLICY=REBOOT`、`FAULT_REBOOT_REASON=0x80060301`）调 `SISP_RebootOS` 时写下的「重启待执行」标记（`boot.log`：`sfed call SISP_RebootOS for reason 0x80060301` → 之后 `rebootcode:0x101000b, but file /dev/shm/sispreboot already exist … result: 300`）。
      - **为什么不会自己恢复**：已证实——`/var/log/messages` 里 vpnf_daemon 从没收到过复位消息（`Got reset code` 0 条），且 vpnf 只对一组固定原因码执行 `vpnf reboot --device`（`/opt/vpnf/scripts/definition.py`：0x6010004、0x6010017、0x2010171、0x1010010、0x101000e、0x2010129 与堆叠类），0x80060301 / 0x101000b 都不在其中，没人重建容器；推断（未证实）——容器 `cap_drop CAP_SYS_BOOT` 与「容器内 reboot 被忽略」一致。
    - **排除**：
      - **与上一实例的宿主残留无关**：卡住前的停机间隔 10、15、10、约 180、150、10 秒，正常的 30、20、90、5、40、约 80 秒；启动前快照里上面列的宿主状态，卡住 / 正常两组没有任何一项能区分。前 4 个样本里「上一实例的 docker netns 文件还在 ↔ 卡住」看似相关，后续样本推翻（无残留也卡、有残留也不卡；停机后那个文件已不是 nsfs，只是 docker 待回收的空文件）。容器自己的 /dev/shm 与 IPC 命名空间每次全新。
      - 与资源、并发、其他沙箱无关（上午结论：卡住时虚机 CPU 1~10%、内存 5~30%；T1、T2 全停时照样卡）。
      - 与容器配置无关：同时运行的 LSW0 与 AR0 容器在复位相关项上相同（`REBOOT_ON_EXCEPTION=0`、`cap_drop` 都是 `CAP_LINUX_IMMUTABLE, CAP_SYS_BOOT`），AR 不卡，差别在镜像本身。
      - 容器内也找不到稳定的区分特征：服务启动顺序到 `pssp_kms_master` 一致，`sfe.log` 到 STAGE3 一致；「启动更快」「SysV shm id 有空洞」都试过，各有反例。
    - **结论与置信度**：机制（sfed 启动 1 秒内以 17 退出 → 故障策略请求重启 → 容器内重启不生效、vpnf 不处理这类原因码 → 板卡复位死循环）**高**；「不是宿主残留、与停机间隔无关，预先等待或启动前检查都避免不了」**中高**（14 + 11 个样本）；17 指向什么资源**未确定**——退出码 17 可能是 errno EEXIST，也可能是 DOPRA / RTF 自己的错误码；没有 strace / bpftrace / perf，只读手段到此为止。判断为 LSW 镜像启动早期 sfed 与同时启动的服务之间的竞态（镜像缺陷），**低**。
    - **ainet 侧**：找不到不改虚机的预防办法（没有任何启动前条件能预测卡住，停机间隔无效），维持「发现后自动重启」：看门狗（§10.5）、40 秒 / 80 秒的早期检查。按约 43% 的卡住率，`AINET_ENSPRO_START_RETRIES=3`（共 4 次，2026-10-07 用户定为缺省）时一次准备环境最终 start_failed 的概率约 0.43⁴ ≈ 1/30；对 start_failed 的设备再点一次「开始练习 / 启动」会先停后启、计数清零（真机 e2e 就这样重试）。线索（样本太少、不显著）：课程页真机联调里 LSW0 都是和 AR0 错开 5 秒一起启动的，3 次都没卡（按 43% 算全不卡的概率约 0.19），而 SSH 复现时 LSW0 是单独启动的；还没和 2026-10-06 卡住的那几次（当时是否与别的设备一起启动）核对，值得用更多样本验证「与别的设备同时启动是否更少卡」，现在不作为结论。
    - **对虚机的建议（都要改虚机，未做，由用户决定）**：
      1. **定位 17 的来源（诊断）**：在一次卡住的启动期间用内核 ftrace 抓返回 -17 的系统调用（只看、不改业务文件，但会改内核跟踪设置）：`cd /sys/kernel/tracing && echo 'ret == -17' > events/raw_syscalls/sys_exit/filter && echo 1 > events/raw_syscalls/sys_exit/enable && timeout 60 cat trace_pipe | grep -a sfed > /root/sfed_eexist.txt; echo 0 > events/raw_syscalls/sys_exit/enable; echo 0 > events/raw_syscalls/sys_exit/filter`（`id` 是系统调用号，x86_64：2 open、257 openat、83 mkdir、29 shmget、64 semget、68 msgget、240 mq_open）。风险：跟踪期间全机系统调用有额外开销，忘了关会一直开着；或临时装 strace（`dnf install strace`，改了系统软件包）。
      2. **让卡住的实例自动恢复（缓解）**：vpnf 收到这类复位请求时重建容器（例如把 0x80060301、0x101000b 加进 vpnf_daemon 处理的原因码，并让容器把复位请求发给 `/dev/vpnf_daemon_socket_file`）。属于 eNSP Pro 自身脚本（`/opt/vpnf/scripts/`），改前备份，升级会被覆盖，可能影响其他设备类型的复位行为，不建议自行改。
      3. 把以上证据反馈给 eNSP Pro（ENSP-LSW 镜像 V600R025C10SPC500B679）。

22. ~~eNSP Pro：PC 的 DNS 属性名未在真机核实~~：**已核实**（2026-10-07）为 `ipv4.config.dns1`（首选）/ `ipv4.config.dns2`（备用），`enspro.HOST_DNS_ATTR` 已改（§6.2、§16.4）。证据：虚机里 PC-CLIENT 的设备模板（`/opt/ensp_conn_service/crd/standalone-pc-sta-temp.yaml`，boardcard `ensp-pc-iface` 的 `ipv4` 组）列出 `ipv4.type / ipv4.config.dns1 / dns2 / ip / ipmask / gateway`，eNSP Pro 前端 PC 表单也是 `dns1`、`dns2` 两栏；真机 `updateAttrs` 设 `dns1=8.8.4.4` 后 `refreshAttrs` 读回一致、PC 容器 `/etc/resolv.conf` 出现 `nameserver 8.8.4.4`（`/var/vpnf/attr/<设备>/ipv4.prop` 同步写入），设 `""` 即清空；从没设过 DNS 时 `refreshAttrs` 里没有这个键。

23. 模拟环境作为独立节点（2026-10-07 用户定）：ainet 已能在运行时改各环境的接入地址（§5.2「接入设置」），但 eNSP 经典版的运行设备 / 最近拓扑检测（`ensp_runtime.py`：VBoxManage、VBox.log 锁、flash.dat 锁、eNSP_Switch 进程、topo.his）只能在 eNSP 主机本机做；ainet 不在 eNSP 主机上时只能靠 `AINET_ENSP_CLIENT=1` + `AINET_ENSP_RUNNING_IDS` 之类的覆盖。待定：在 eNSP 主机上跑一个只读「节点代理」（报告运行中设备 GUID、交换机名、topo.his、eNSP_Client 是否在运行），ainet 经它获取运行状态；代理的鉴权、端口与部署方式未定，**未实现**。

## 15. 开发里程碑（建议顺序）

1. [x] DeviceConnector(eNSP) + Web 终端打通（xterm.js ↔ console 2000+）。
2. [x] SQLite 模型 + 明文登录 + 档案载入。
2.5. [x] 拓扑图展示（解析 eNSP .topo：设备、连线、接口名、注释；端口白名单随拓扑变化）+ 多设备终端（标签页、左右分屏、可拖动分栏、点拓扑节点开终端）。
2.6. [x] 拓扑图补画背景图形与注释样式（白底网格，同 eNSP）；按运行中设备的 GUID 自动确定当前拓扑，去掉网页上的拓扑选择；设备状态分为运行中、已停止、不适用（不探测 console 端口）。
3. [x] 场景库数据结构 + 一个手工场景跑通练习闭环（含目标自动判定）。ConsoleHub 共享 console 连接 + 隐藏事务；`scenarios/*.json` 场景库与校验；练习 API（开始注入并确认故障 → 学员排障 → 自动判定通关 → 结束还原）与工作台「练习」页签。2026-10-04 在 Campus_Network（AR0、AR2）上真机跑通通关与放弃两条路径（`backend/tests/e2e/practice_e2e.py`）。学员额外的配置更改由第 4 项整机复位撤销。
4. [x] 复位机制 spike 并落地（配置级 + 兜底重载）。spike 结论见 §10 / §14 第 1 项：VRP 无整份替换命令，采用快照差异回滚（`config_reset.py`）；开练前快照存 db，结束时场景 restore 后整机复位并比对，撤不掉的列出并提示在 eNSP 重载拓扑（兜底为人工操作，§10.1）。2026-10-04 在 Campus_Network（AR0、AR2、LSW0）上真机验证：三道新题（接入 VLAN 配错、回程静态路由下一跳错、ACL 误拦截）通关 / 放弃路径，学员额外改动（含 sysname）全部撤销、与开练前快照逐行一致（`tests/e2e/reset_e2e.py`）。拓扑图接口映射一并修正（§14 第 10 项）。2026-10-04 与里程碑 5 合并后复跑 `reset_e2e.py`：9 pass / 0 fail。
5. [x] AI 编排(DeepSeek V4 Flash)：操作分析 + 评分 + 分级提示。ConsoleHub 采集学员命令输出写入 attempt_logs；`backend/ai.py`（客户端、重试、错误分类、稳健 JSON 解析、Key 脱敏）、`prompts.py`、`grading.py`（结果分确定性 + AI 过程 / 副作用分，异步，降级）、`hints.py`（AI 三级提示 + 泄露过滤 + 静态回退）；结果 / 重新评分 API；前端评分卡、提示来源标注、档案总分。2026-10-04 用 `backend/tests/manual/ai_smoke.py` 实测真实 DeepSeek 评分与三级提示；2026-10-04 与里程碑 4 集成后真机联调（`backend/tests/e2e/ai_e2e.py`，Campus_Network 的 AR0 / LSW0 / AR2，真实 DeepSeek）：ACL 题第 1、2 级提示均由 AI 生成且无泄露 → 额外改动（含 sysname）与排障 → 修复自动通关 → 复位逐行一致 → 评分卡（89.2 分；hub 修复后复跑 P1~P6 全过，88.9 分；评分全文含复位结果）→ 档案页显示总分；回程路由题盲目试错后放弃得 2.4 分，点评指出零查看、shutdown、删正确路由等问题，复位逐行一致。最终代码上复跑 `practice_e2e.py` 11 pass / 0 fail；ACL 题的 C / C2 与 AI e2e 的 P 需要在 eNSP 中重启 AR2 后再复跑（§3 traffic-filter 资源问题）。
6. [x] 考试模式 + 可配置模板 + 命令回放。吸收 ensp-coach：学员点「我修好了」后统一验收（与自动判定并存，考试默认关闭自动判定）；**参数化故障 + 随机种子**（同题每次注入值不同、可复现，防背答案）。2026-10-05 完成（**真机联调待做**）：`params.py`（参数声明、按种子展开、抽样 + 边界 + 碰撞校验）与 4 道 Campus 题参数化；`exam.py`（模板、状态机、服务端计时与重启恢复、提交验收、提示开关与扣分、答案保护、加权汇总、成绩单、命令回放）；工作台「考试」页签、成绩单页、模板管理页、档案页考试记录。pytest 全部通过；模拟设备（假 VRP 网络模型）+ 无头浏览器 e2e `tests/e2e/exam_fake_e2e.py` 10 pass / 0 fail；真实 DeepSeek 冒烟确认考试评分提示词正常。真机 e2e 脚本 `tests/e2e/exam_e2e.py` 已备好，未在真机上跑。
6.5. [x] 第二种实验后端 eNSP Pro + YunShan 适配（2026-10-06）。`lab.py` 实验后端抽象（eNSP 经典版行为不变）与统一设备标识 `console`；`enspro.py`（REST 登录与单会话冲突、沙箱转拓扑、启停、console 登录失败记录）与 `connectors/enspro.py`（STOMP console、心跳、登录状态机、空闲登出后自动重登）；hub / 复位的 YunShan 适配（§5.1、§10）；场景 `platform` 字段与 YunShan 题 `enspro-t1-t2-link`；前端启停按钮与重新登录。假服务测试（REST MockTransport + STOMP 服务 + YunShan 假设备）与真机 e2e（§10.4）通过。
6.6. [x] 工作台布局重构 + Agent 面板（2026-10-06，§16）。按已确认的布局稿 `docs/design/ui-layout.html`：顶栏（环境状态、模式切换，练习 / 考试进行中锁定）、练习任务条与红色考试条（原左栏页签的功能全部迁入）、拓扑与右侧面板可折叠成 48px 图标栏并可拖宽、窄屏抽屉；右侧 Agent 面板（`backend/agent.py`、`frontend/agent.js`）：SSE 流式、环境快照、只读命令工具、练习导师模式（泄露按第 3 级提示扣分）、考试 403、对话落库并进评分。pytest 全部通过；模拟设备 + 无头浏览器 `tests/e2e/ui_e2e.py`（真 DeepSeek）13 pass / 0 fail，`exam_fake_e2e.py` 10 pass，`practice_fake_e2e.py` 与基线相同（步骤 5 / G 依赖真机输出格式）。
6.7. [x] eNSP Pro 替代验证与题库迁移（2026-10-06，`docs/enspro-replacement.md`）。新建沙箱 `ainet_campus`（AR0 / AR1 / AR2 / LSW0 / PC1 / R1~R3，经接口建设备与连线、由 ainet 启动）；真机摸清 S6700 交换机、PC-CLIENT、自动保存与停机保存、空闲登出、会话冲突、资源（§3）；实现 PC 终端主机（场景 `hosts`、设备属性接口）、干净基线（每题开始前比对、不一致先复位）、重启 405 退回停止 + 启动、`Please Press ENTER` 立即重登、AR6700 异步 traffic-filter 报错按日志处理、PC 改地址后重发判定命令；Campus 4 题与 OSPF 题的 YunShan 版（`*-ys.json`）。真机 + 浏览器 e2e（`tests/e2e/enspro_campus_e2e.py`）：5 题练习各 5/5、额外乱改 6/6、考试 7/7、会话挤掉与重新登录 6/6。
6.8. [x] 模拟环境注册与运行时切换（2026-10-06，§5.2）。`lab.REGISTRY`（eNSP / eNSP Pro）+ 快速探测（超时、缓存、不登录、不连 console）；`labenv.LabManager` 实验上下文（切换串行加锁、练习 / 考试进行中 409、断开全部 console、退出旧登录、清缓存、`app_settings` 持久化与重启恢复）；`GET /api/lab/environments`、`POST /api/lab/environment`、`GET /api/lab/sandboxes`（eNSP Pro `getByPage`）、按题目准备环境 `POST|GET /api/lab/prepare`（题目 topology = 沙箱名，切沙箱并启动本题设备）；题目与考试模板按平台过滤、开考平台不匹配拦截；Agent 跟随切换；前端独立模块 `labswitch.js`（顶栏过渡挂载，档案等页面只读）。pytest 与真机 + 无头浏览器 e2e（`tests/e2e/labswitch_e2e.py`）见 README。
6.9. [x] 课程选择页、操作页单层顶栏、PC 设置气泡（2026-10-07，§16.3 / §16.4）。课程页四列（环境 → 章节 → 课题 → 预览；环境在线 / 离线、按题目准备环境的进度、经典版「请在 eNSP 中打开拓扑」、busy 自动重试、多题考试模板入口、localStorage）；`scenarios/chapters.json` 与场景课程字段（校验、不泄露答案）；单题考试 `POST /api/exam/start_single`（复用考试引擎，开题失败整场回滚）；操作页单层顶栏（题名气泡、学员菜单、模式切换规则、退出到课程，撤掉顶栏环境选择器）；PC 设置气泡 `GET|POST /api/lab/devices/{console}/host`（练习 / 考试中记操作记录、随复位还原）；出题规则允许 eNSP Pro 的 PC 地址故障（§6.2）。pytest 与模拟环境的无头浏览器 e2e（`tests/e2e/course_e2e.py`，假 eNSP Pro + 假 VRP）见 README；真机 e2e 脚本同步了选择器，未在真机上重跑。
6.10. [x] 设备启动可靠性（2026-10-07，§10.5、§14 第 21 项）。根因真机定位（LSW 镜像启动早期 sfed 退出码 17 → 容器内重启被忽略 → 板卡复位死循环，证据来自 eNSP Pro 设备日志接口）；`labpower.py`：启动看门狗（所有启动经它，超时 / 错误状态 / 设备日志里的复位循环 → 自动停止再启动，仍失败 start_failed + 原因，状态存库、服务重启后接着跟踪）、错开启动与停机间隔、就绪探测（initializing，隐藏事务，开练 409 initializing / busy，准备环境 probe 阶段）、切换沙箱自动停掉旧沙箱设备（`AINET_ENSPRO_STOP_OLD`）、空闲自动停机（`AINET_ENSPRO_IDLE_STOP_MIN`，终端预告）、`AINET_LAB_FORCE`。pytest 与真机 e2e（`tests/e2e/reliability_e2e.py`）见 README。
6.11. [x] 真机联调：课程页 + 启动可靠性（2026-10-07，本机 eNSP Pro）。真机 + 无头浏览器 e2e `tests/e2e/course_real_e2e.py`：全流程 12 pass / 0 fail（登录进课程页、环境在线 / 离线、T1/T2 题开始练习的准备进度与旧沙箱 PC1 自动停机、单层顶栏与题名气泡、T2 终端、修复 → 自动通关 → 退出到课程 → reset_ok、卡片「已通过」、单题考试到成绩单、VLAN 题切到沙箱 3 并停掉 T1/T2、PC 设置气泡改 IP / 掩码 / 网关 / DNS、PC1 ifconfig 看到新地址与 DNS、LSW0 修复通关后复位与 PC 地址还原、档案平台标签、三个沙箱停机、无 JS 异常）；只跑 VLAN 题的模式又跑 两次，各 8 pass / 0 fail；三次运行里 LSW0 都没卡（接口 55~65 秒报运行中、就绪探测再 40~60 秒），课程流程里没能自然碰到看门狗的自动重启——这条路径由 reliability_e2e.py 的 stuck 模式（2026-10-07 上午 3/0）、test_labpower.py 与 §14 第 21 项的 14 次 SSH 复现覆盖。联调发现并修复：PC 的 DNS 属性名（`ipv4.config.dns1`，§14 第 22 项）与「没设过 DNS」按空处理（否则复位撤不掉学员改的 DNS）；已通关的练习「退出到课程」确认框误写「按放弃记录」；题名去掉「（YunShan）」后缀、档案 / 成绩单 / 模板管理用平台标签区分同名题。LSW 卡住经 SSH 只读复现定位（§14 第 21 项）：不是宿主残留、与停机间隔无关，ainet 侧无预防手段，维持看门狗。pytest 与运行结果见 README。
6.12. [x] 接入设置（2026-10-07，§5.2「接入设置」、§14 第 23 项）。每个环境的接入地址可在运行时配置（`app_settings` 的 `lab:endpoint:<环境>`，优先级与 `lab:env` 一致：库里的记录优先，`AINET_LAB_FORCE=1` 时环境变量优先）；`GET|POST /api/lab/environments/{key}/endpoint`、`POST …/endpoint/test`，练习 / 考试进行中修改 409，改当前环境时断开终端并重建后端；凭据只报有无（`secrets`）；离线原因分类 `reason_code`（unreachable / tls / auth / missing_secret / other）+ 中文说明，课程页环境列显示原因与「⚙ 接入设置」气泡；eNSP console 主机不是本机时 `runtime_local: false`。pytest（`tests/test_lab_endpoint.py`，全部本机回环 / TEST-NET 地址、假密码）见 README；没有连 Windows 部署机和 eNSP Pro 虚机验证。
6.13. [x] 课程页准备浮层（2026-10-07，§16.3「准备浮层」、§5.2 按题目准备环境的新字段）。遮罩 + 居中卡片、步骤条（跳过的标已跳过）、只增不减的整体进度条与「预计还需」、设备状态卡片、自动重启警示（第 n 次 / 最多 N 次）、轮播提示与本题工单、错误态（后端原因 + 重试 / 返回课程）、完成态停留后进入操作页；经典版同一套浮层（只有切换环境 → 核对设备 → 完成）。`GET /api/lab/prepare` 加 `steps / device_info / progress / estimate / env_label / power / sandbox_name / retries`（向后兼容）。pytest（`test_labenv.py` 三个新用例）与假环境浏览器 e2e（`tests/e2e/prepare_overlay_e2e.py`，假 eNSP Pro 上模拟 T2 启动卡住）见 README；没有在真机上跑。
6.14. [x] eNSP Pro 题库规划与草稿题（2026-10-07，离线完成，`docs/question-bank-plan.md`）。按 L01~L12 每章 2~3 道规划 YunShan 题（故障、设备、注入、判定、复位、所需设备类型），标出现有沙箱 ainet_test / ainet_campus 能做的与需新建的沙箱（ainet_l2 做 STP、ainet_fw 做防火墙、ainet_svc 做动态 DNS；ainet_campus 加一条 AR0—AR1 链路做浮动静态路由）；实写 17 道草稿题（ainet_test 5 道：L01×2、L02、L03、L12；ainet_campus 12 道：L01、L02 PC 网关、L04 单臂路由 Trunk、L05×2、L06、L07 DHCP×2、L08 静态域名、L09 Easy IP / 静态 NAT、L12），全部 `draft: true`；场景 `draft` 字段与缺省隐藏（`AINET_SHOW_DRAFTS`，§6）。假网络模型支持接口 shutdown，ainet_test 的 5 道在假 eNSP Pro 上跑通注入 → 确认故障 → 经终端修复 → 判定 → 复位（`tests/test_question_bank_fake.py`）；ainet_campus 的 12 道只过加载校验。**全部未真机验证**，需真机确认的写法（子接口、OSPF 定时器、DHCP、ip host、NAT 等）见规划 §6，验证步骤见 §7。pytest 全量 559 passed, 1 skipped（基线 549 passed, 1 skipped）；没有连 Windows 部署机和 eNSP Pro 虚机。
6.15. [x] 题库扩充草稿题真机验证（2026-10-07，本机 eNSP Pro，`docs/question-bank-verify.md`）。17 道草稿题逐题跑真机 + 无头浏览器 e2e（`tests/e2e/enspro_campus_e2e.py practice <id>`：开练前与手动记录的干净基线比对 → 注入并确认故障 → OSPF / DHCP 题等 90 秒后再判一次应不通过 → 经终端（PC 题经 PC 设置接口）按正确方式修复 → 自动判定通关 → 结束复位与开练前快照一致），各 7/7，全部去掉 `draft` 上线（YunShan 题 6 → 23 道）。按真机修正 4 道：两道 DHCP 题 restore 去掉 `undo dhcp enable`（出厂配置），两道 NAT 题改为 `nat-policy` / `nat server` + `nat enable`。脚本补齐 17 道的修复步骤、设备号按当前拓扑取（ainet_test 的题也能跑）、`FAULT_WAIT` / `RETRIGGER`、`AINET_E2E_REQUIRE_BASELINE` / `AINET_E2E_LOG`；`tests/test_scenarios.py` 改为题库清单 `BANK_*` − `VERIFIED` = `DRAFTS_*`。开工时 AR0、AR2 各有一条 `campus-ar0-ar2-link-ys` 的残留地址（停在故障中被自动保存），按该题 restore 撤销后与 AR1 / R1~R3 / LSW0 / T1 / T2 的出厂配置一起手动记了基线；AR1 是出厂配置。
6.16. [x] 启动看门狗参数按环境配置（2026-10-07，§5.2「接入设置」、§10.4、§10.5）。起因：ainet 部署在多台主机上、各接不同的 eNSP Pro，一台 Windows 笔记本上 T1 / T2 冷启动 187~202 秒，全局 150 秒启动超时让看门狗误判卡住、自动重启 3 次后标 start_failed（那台主机已先用环境变量调到 300）。`lab.WatchParam` / `LabEnv.watch`：eNSP Pro 的 `start_timeout`（60~1800 秒）、`start_retries`（0~10）、`early_check`（0 或 10~600 秒且小于启动超时）、`probe_timeout`（30~1800 秒）存 `app_settings` 的 `lab:watch:<环境>`，优先级同接入地址（库里的记录 > 环境变量 > 内置缺省，`AINET_LAB_FORCE=1` 时环境变量优先并写回库）；`GET|POST /api/lab/environments/{key}/watchdog`（422 / 409 / 经典版 400），环境列表与接入设置带 `watchdog`；`PowerWatch.configure()` 让修改对当前环境立即生效（进行中的启动按新值继续），切换环境时换参数；准备环境的等待上限与「最多 N 次」随该环境的参数。课程页「⚙ 接入设置」加「启动超时（秒）」「卡住自动重启次数」。超时误判的 start_failed 设备事后恢复为 online（原有逻辑已支持，本次补测试、事件说明，并让对已在运行的 start_failed 设备点「启动」不再停止重启）；**没有做**「超时前延长等待」（判断依据不足，见 §10.5）。pytest 全量 589 passed, 1 skipped（基线 559 passed, 1 skipped；新增 `tests/test_lab_watchdog.py` 30 个）；没有连真机与 Windows 部署机。
6.17. [x] 单人部署：自动登录 + 同时运行设备上限（2026-10-07，§12「自动登录」、§5.2「接入设置」）。起因：ainet 部署在一台性能有限、单人使用、只在局域网用的 Windows 笔记本上，用户要求跳过学员登录直接用 admin，并隐藏开不了的题（这台机器的 eNSP Pro 最多稳定同时运行 3 台路由器 / 交换机，PC 另算）。`AINET_AUTO_LOGIN=<登录名>`（缺省关闭；学员不存在自动创建、随机密码不输出；无有效会话的请求自动当作该学员并补发 cookie，`/login` 跳课程页，`/api/me.auto_login` 让前端隐藏「切换学员」「登出」；能访问端口的任何人都以该账号操作，只适合单人本机 / 局域网，不提供经接口开启的方式）。`lab:watch:<环境>` 新增 `max_devices`（0~64，0 不限，`AINET_ENSPRO_MAX_DEVICES`，来源 / 优先级同看门狗参数），课程页「⚙ 接入设置」加「同时运行设备上限（不含 PC，0 不限）」；`scenarios.device_load` 计数（PC / Cloud 不计，见过沙箱时按设备类型，否则按 hosts 与名字）；`GET /api/scenarios` 隐藏超上限的题并给 `capacity_hidden / capacity_hidden_chapters / max_devices`，课程页提示「另有 N 道题需要更多设备，本机已隐藏」；开练 / 单题考试 / 模板考试 / 准备环境超上限 409；准备环境时按整个 eNSP Pro 计数（本题 + 本沙箱非本题 + 其他沙箱在运行的非 PC 设备，2026-10-08 按真机发现补上其他沙箱），超上限先停非本题设备（`stopped_other` / `stopped_extra`）。上限 3 时隐藏 NAT 2 道、OSPF 3 道。pytest 全量 599 passed, 1 skipped（基线 589 passed, 1 skipped；新增 `tests/test_capacity_autologin.py` 10 个）；没有连真机与 Windows 部署机，课程页改动只做了 `node --check`。
6.18. [x] AI 接口设置（2026-10-08，§4「AI 接口设置」）。学员菜单「AI 设置」（课程页与操作页，考试中不显示）对话框：API URL、API Key（password，只写不读）、模型（可选）、「测试连接」「保存」「清除 Key」。URL / 模型存 `app_settings`，Key 原子写入 secrets 文件（保留其他行），库里只记来源；优先级页面 > 环境变量 > secrets 文件，`AINET_LAB_FORCE=1` 时环境变量优先；客户端按（Key, URL, 模型）缓存，保存后立即生效；接口只回显有无与掩码。`GET|POST /api/ai/settings`、`POST /api/ai/settings/test`。pytest 全量 605 passed, 1 skipped（新增 `tests/test_ai_settings.py` 6 个，考试防泄露用例覆盖新接口）；测试连接只用 MockTransport，没有发真实外网请求，对话框只做了 `node --check`。
6.19. [x] 课程页添加模拟环境（多台 eNSP Pro，2026-10-08，§5.2「页面上添加环境实例」）。环境列「+」→ 对话框（类型 / 显示名 / 地址 / 设备 console 密码 / 启动超时 / 设备上限 / 测试连接 / 添加）；实例存 `lab:envs`（key `enspro-N`，编号不复用），启动最先注册；每个实例独立的地址、看门狗参数与设备上限、沙箱记录、干净基线、设备密码 `ENSPRO_DEVICE_PASSWORD__<KEY>`（页面上只写不读，写 secrets 文件对应行）；内置 enspro 与已有库键完全兼容。YunShan 题对所有实例可见，准备环境 / 设备上限按所浏览或指定的实例（`prepare.environment`）。`POST /api/lab/environments`、`POST …/probe`、`PATCH|POST|DELETE /api/lab/environments/{key}`；删除只限用户添加的，当前环境 / 进行中 409，清理设置、基线与密码行。pytest 全量 614 passed, 1 skipped（基线 607 passed, 1 skipped；新增 `tests/test_lab_instances.py` 7 个，考试防泄露用例覆盖新接口）；前端只做了 `node --check`。
6.20. [x] 自动识别 https（2026-10-08，§5.2「自动识别 https」）。起因：添加 eNSP Pro 时地址写成 http://…:33000，而该端口只有 HTTPS，系统没提示、连不上。eNSP Pro 地址可不写协议（只有主机名补 :8443）；添加 / 添加前测试连接 / 改地址 / 测试连接遇到 http:// 或没写协议时先试 TLS 握手（不登录），通就存 https:// 并返回 normalized_from 与「已自动改为 https」，明文 HTTP 有响应就保留 http 并警告，都不通按输入；已存的 http 地址不自动迁移，⚙ 打开时发现 https 可用提示「建议改为 https」一键改。测试 `test_lab_scheme.py`。
7. AI 生成场景 + 规则校验器。题库路线图采用 ensp-lab 课程地图 L01~L12（链路中断 → IP/网关 → VLAN → 跨 VLAN 路由 → OSPF 邻居 → 静态路由 → ACL → DHCP → DNS → NAT → STP 环路 → 防火墙）；现有 Campus 题约覆盖 L02/L03/L06/L07。
8. 掌握度画像 + 错题本 + 自适应出题。
8.5. 吸收 ensp-coach 的评分与运维增强：**根因指纹**（修复后配置与期望修复 `expected_fix` 比对，识别绕路修复，如用静态路由掩盖 OSPF 故障）；**回归检查**（修好 A 不能弄坏 B，场景声明回归用例）；**推荐命令命中率**作为过程分的确定性信号；**资源检查**（开题前按设备型号估算内存，不足则提示不可开题）。
9. 外部 Linux 主机接入（§10.2 方案 B，需要应用层题目时）。
10. LabProvider/DeviceConnector 的 EVE-NG / containerlab 实现（长期，§10.2 方案 C）。

## 16. 工作台布局与 Agent 面板（2026-10-06 实现）

### 16.1 布局（以 `docs/design/ui-layout.html` 为准）
- **顶栏**（2026-10-07 起为**单层**，56px，布局稿 ⑥，详见 §16.4）：logo、模式「实验 / 练习 / 考试」、「模式标签 + 题名 ▾」、计时 / 判定 / 操作按钮、学员名 ▾、「退出到课程」。原来的两层（顶栏 + 任务条 / 考试条）合并成这一行；环境与拓扑收进题名气泡；「档案」从模式里移到学员菜单。顶栏下方的通知条照旧放通关横幅、操作消息、还原失败、开题失败的「重新开始本题 / 跳过本题」、交卷二次确认条。
- ~~**任务条**~~（2026-10-07 并入单层顶栏）：练习的用时、判定进度（只显示「通过 x/y」，不暴露判定命令）、立即检查、结束练习，考试的剩余总时间、题号进度、本题计时、提示（模板允许时）、提交本题、下一题、交卷都在顶栏同一行；考试的工单（工单、可访问设备按钮、提交记录、已用提示）在题名气泡里。
- **拓扑面板**（左，缺省 300px）：可折叠成 48px 图标栏、可拖宽；保留拓扑绘制、eNSP Pro 启停按钮；考试不可看拓扑时画布打码，只列出本题可登录的设备按钮。
- **终端**（中）：标签页、分屏不变；考试作答中没有右侧面板，终端加宽。
- **右侧面板**（缺省 380px）：可收起成 48px 图标栏、可拖宽；收起（或不在 Agent 页签、窄屏抽屉关着）时有新回复，图标上显示圆点。自由实验只有 Agent；练习有「工单详情」（未开始时是场景列表，结束后是总结与评分卡）和「Agent 导师」两个页签，三级提示按钮在导师页签底部（3 级标「扣分」，取到的提示同时进对话流和工单详情）；考试作答中整个面板不出现（未开考 / 已交卷时显示考试列表 / 已交卷）。
- 窄屏（< 1100px）：右侧面板改成抽屉，图标栏常驻，点遮罩或 Esc 关闭。
- 面板宽度、折叠状态、上次的模式存 localStorage（`ainet.ui.*`，读写 try/catch，不可用时用默认值）；`?mode=lab|practice|exam` 可指定模式（独立页面顶栏的链接用）。
- 成绩单、命令回放、档案、模板管理仍是独立页面，改用同一套顶栏（`frontend/page.js`），顶栏只读显示当前模拟环境与沙箱，导航为「课程 / 操作页 / 档案」。无 CDN、无新前端库、URL 一律相对路径（支持 `/ainet` 子路径）。

### 16.2 Agent 面板
- **接口**：`POST /api/agent/chat {message, history}`（需登录，`text/event-stream`）。事件：`meta`（服务端判定的场景）、`delta`（文字增量）、`tool`（执行了一条只读命令：设备、命令、ok / busy / error / rejected、输出）、`penalty`（导师回复判定泄露，已扣分）、`error`（友好提示）、`done`。响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`（经 nginx 子路径时不缓冲）。`GET /api/agent/history`：当前场景的对话（刷新页面后恢复）。请求里的历史只接受 user / assistant 纯文本，限条数和长度。同一学员同时只处理一个问题（429）。
- **场景只由服务端判定**：考试进行中（考试会话存在或当前题是考试题）→ 403（在开始流式输出之前判断，`test_exam` 的答案保护遍历与 `test_agent` 覆盖）；练习进行中 → 导师模式；否则自由问答。
- **环境快照**（每次请求现组装，作为第二条系统消息）：实验后端与平台、当前拓扑（设备 / 型号 / 状态 / 是否可执行只读命令、连线）、学员最近 15 条终端命令与输出摘要（`TermLog` 监听 ConsoleHub 的 command / output 事件，单条输出截 600 字、总量截 5000 字）、练习工单。工单只取 `scenarios.public()` 的面向学员字段（标题、症状、可登录设备、知识点、难度）加进度（是否通关、已用提示级数、最近判定 x/y），再按 `FORBIDDEN_KEYS`（inject、restore、checks、answer、root_cause、expected_fix、solution、baseline、hints、reference_path、scoring_points、params、param_values、seed）兜底过滤；测试遍历全部场景（含多个种子）确认发给模型的内容里没有这些字段，也没有注入 / 修复 / 判定命令、提示原文、参考路径与评分要点。
- **只读命令工具**：DeepSeek function calling（`run_readonly(device, command)`，流式 + 工具已实测支持，关闭思考）。白名单：只认完整的 `display ...` / `ping ...`（不认缩写），ASCII 可见字符、长度 ≤ 120，禁止 `;`（含全角）、换行、Tab 与其他控制字符、`?`、`>` 等；管道只允许 `display ... | include / exclude / begin`；ping 没写 `-c` 时补 `-c 3`，`-c` ≤ 5；`display diagnostic-information` 这类超长输出拒绝。执行走 `ConsoleHub.run_hidden(kind="check")`：学员终端看不到，不产生 command 事件（不进学员命令采集与 TermLog）；学员输入半行、停在分页 / [Y/N] 时 hub 返回 busy，如实告诉模型和学员，不排队重试。设备按名字（不分大小写）解析，必须在线；练习中只能查本题开放的设备。每次提问最多执行 3 条，超出的工具调用返回「达到上限」，最后一轮不再提供工具。前端显示「已在 X 执行（只读）：命令」，可展开看输出。
- **导师模式（练习）**：系统提示要求只引导、不给答案（不指出故障设备与配置、不给错误值的正确写法和修复命令），优先让学员自己执行查看命令。最终回复用 `hints.leaks(text, hints.banned_terms(scenario))`（与第 1、2 级提示同一套）检查；命中即按第 3 级提示记一次扣分：`hints_used` 至少记到 3（写库并持久化，练习已结束则不记）、`attempts.agent_penalties` + 1，对话里显示扣分提示。提问本身不扣分；自由实验不做泄露检查。
- **记录与评分**：`agent_messages` 表（学员、练习记录 attempt_id（自由实验为空）、场景、角色、内容、执行过的命令、是否判定泄露），新表 `CREATE TABLE IF NOT EXISTS`、新列按 `PRAGMA table_info` 补，幂等。评分（`grading.py`）把该次练习的导师对话摘要（学员提问原文、导师回复摘要、替学员查看过的命令、泄露标记）交给 AI 作为过程分参考，`score_json.agent` 记提问数与扣分次数。
- **模型与失败处理**：沿用 `ai.py` 的 DeepSeek 配置（deepseek-flash，Key 读取顺序同 §4 / README），新增 `chat_stream()`。两块之间超过 `AINET_AI_AGENT_TIMEOUT`（30 秒）或整轮超过 `AINET_AI_AGENT_DEADLINE`（120 秒）算超时；无 Key、超时、限流、网络等错误都以 `error` 事件给出中文提示（前端可「重试」），不抛异常；前端另有 150 秒的兜底超时，可随时「停止」。`AINET_AI_AGENT=0` 关闭。
- **已知局限**：泄露检查是字面匹配（与提示同一套），模型引用 display 里看到的注入值（如错误的地址）也会被判为泄露；缩写形式的命令匹配不到。泄露判定在回复流完之后做，学员已经看到了内容（所以记扣分）。终端最近命令只记服务进程启动以来的（内存，不落库）。

### 16.3 课程选择页（2026-10-07 实现，布局稿 `docs/design/ui-layout.html` ⑤，`frontend/courses.html` / `courses.js`）
- **路由**：登录后默认进课程页 `/courses`（未登录跳登录页），选好课题再进操作页 `/?mode=lab|practice|exam&s=<场景 id>`。单人使用：任何已登录用户都能切环境（§2、§5.2）。
- **四列**（从左到右）：
  - **模拟环境**：打开页面时 `GET /api/lab/environments` 检测全部环境；在线的可点，离线的灰色、不可点，下方写「（离线）」（悬停看原因）；顶栏「↻ 重新检测环境」带 `refresh=1`。点环境只是浏览：`GET /api/scenarios?env=<key>` 取题，**不触发切换**。
  - **章节**：`scenarios/chapters.json` 从易到难，每章显示题数；没有题的灰色、标「待补充」。最下方另有一项「考试」：列出题目全部属于当前浏览环境平台的多题考试模板（按模板的 `platforms` 过滤，不用对当前环境算的 `platform_ok`）。
  - **课题**：题名、一句话症状、难度、预计用时、完成状态（档案：已通过 / 未通过 / 未开始；进行中考试的题不算）。
  - **预览**：标题、章节与难度、简介（工单）、知识点标签、拓扑预览（当前环境已加载同一拓扑时画全图，本题设备橙框、未启动虚线；否则只示意本题设备）、将要准备的环境说明；底部「自由实验」「开始练习」「单题考试」（模板为「开始考试」）。
- **进入**：eNSP Pro（能启停设备的环境）调 `POST /api/lab/prepare` 并轮询，进度显示在**准备浮层**（2026-10-07 起，见下条），完成后进操作页对应模式；eNSP 经典版不能远程打开拓扑：所需拓扑没在运行时提示「请在 eNSP 中打开 xxx 拓扑并启动设备」，每 5 秒重新检测，`/api/scenarios` 报告本题就绪才放行（prepare 任务对经典版不核对拓扑名）。`practice/start`、`exam/start_single`、`exam/start` 返回 409 且 detail 含 busy（设备刚冷启动）时每 20 秒自动重试、最多 5 次、显示倒计时；用户可以取消（准备任务在后台继续完成）。多题考试模板先依次准备每道题的环境再开考。
- **准备浮层**（2026-10-07，里程碑 6.13；用户反馈 eNSP Pro 冷启动 1~4 分钟、卡住还会自动重启，原来一行小字不够醒目）：点进入后整页遮罩 + 居中卡片（`role=dialog`、`aria-modal`，背景 `inert`，焦点移入卡片）。标题写在做什么（「正在准备练习 / 实验 / 考试环境」）+ 题名 · 环境 · 沙箱；**步骤条**按任务的 `steps`（切换环境 → 查找沙箱 → 启动设备 → 等待运行 → 确认就绪 → 完成；经典版只有切换环境 → 核对设备 → 完成），进行中的脉冲、已完成打勾、跳过的虚线圈「已跳过」、出错红色，restart 阶段「等待运行」变警示色「自动重启」；**整体进度条**（`role=progressbar`、`aria-valuenow`）取任务 `progress` 只增不减、每秒在其上缓慢推进（最多 +4%，完成前不满格），最后 5% 留给开练 / 开考，旁边是已用时间与「预计还需约 N 分钟」（`estimate` 的区间，重启时注明「含自动重启」）；**设备卡片**（图标 + 颜色 + 文字：未启动 / 启动中 / 系统初始化 / 就绪 / 重启中 / 启动失败，状态变化有颜色过渡与轻微弹动）；卡住时警示框「XX 启动卡住，正在自动重启（第 n 次，最多 N 次）」；**引导提示**每 7 秒轮播（首次启动约 2~3 分钟可先读工单、勿在 eNSP Pro 网页端登录、卡住自动重启最多 N 次、busy 的原因、可取消）；等待期间展示本题工单。出错：浮层内显示后端原因原文 +「重试」「返回课程」；完成：「环境已就绪」停留约 0.8 秒后自动进入操作页（开练 / 开考成功之后才算完成，busy 重试的倒计时也在浮层里）。经典版「请在 eNSP 中打开拓扑」是等待态：「核对设备」保持进行中、进度条改为来回滑动的不确定样式、只有「取消」。阶段变化经 `aria-live` 播报（不播报计时）；`prefers-reduced-motion` 时关闭全部动画与过渡；颜色只用 `:root` 变量；390px 宽卡片在视口内（两侧 16px）、无横向滚动。
- 环境、章节、课题的选择存 `localStorage`（`ainet.course.*`，读写 try/catch）。有进行中的练习 / 考试时顶部提示并给「回到操作页」，进入按钮禁用。
- **单题考试**（`POST /api/exam/start_single {scenario_id}`）：本题的考试，默认规则——计时 15 分钟、无 Agent、无提示、每题可重试 1 次、拓扑可见、交卷后出成绩单与命令回放。复用考试引擎（`ExamManager._start_exam`），用临时单题模板（不进模板表，`exams.template_id` 为空，名称「单题考试 · 题名」，状态里 `single: true`）。引擎扩展：题目没能开始（设备 busy、故障没生效等）时**整场回滚**（删掉考试记录、不计时）并 409，便于前端按 busy 重试；模板考试行为不变（开题失败留在 pending，可重试 / 跳过）。有练习进行中 409。

### 16.4 操作页单层顶栏与 PC 设置气泡（2026-10-07 实现，布局稿 ⑥）
- **单层顶栏**（依次）：`ainet`（回课程页）、模式（实验 / 练习 / 考试）、「模式标签 + 题名 ▾」、计时 / 判定 / 操作按钮、学员名 ▾、「退出到课程」。窄屏（< 1000px）换行，按钮都在视口内。
- **题名气泡**：模拟环境（含平台）、拓扑（沙箱）与运行台数、本题设备、章节与难度、工单摘要，「↻ 刷新设备状态」「查看完整工单」（练习中打开右侧「工单详情」，实验中在气泡里展开全文）。环境和拓扑不再常驻显示（题名按钮上只留状态点；eNSP Pro 会话被挤掉时顶栏出现「重新登录 eNSP Pro」）。没有课题时题名为「自由实验」。考试时题名处显示「第 n/N 题 ▾」、顶栏变红，气泡只显示工单（题号、工单、可访问设备、提交记录、已用提示），模板隐藏拓扑时不显示环境和拓扑。
- **学员菜单**：我的档案、切换学员、登出；顶栏不再放「档案」模式。
- **模式切换规则**（同一道题）：实验 ↔ 练习可以切——实验 → 练习直接开始本题练习，练习 → 实验等于放弃本次练习，要确认，结束并复位（按放弃记录；复位暂不可执行时留在练习并提示）；实验 / 练习 → 考试（本题的单题考试）要确认，开始计时（练习中先按放弃结束）；考试中不能切到其他模式，只能交卷。后端约束：考试进行中开练习 / 用练习接口 / 再开考 / 切环境一律 409，练习进行中开考 409。
- **退出到课程**：操作页内不能换题；换题、换环境、换拓扑都要退出到课程。练习中退出要确认（说明会结束练习、复位、按放弃记录）；考试中不可用。
- **PC 设置气泡**：点拓扑中的 PC 弹出（路由器、交换机不变：点击打开终端）。可设 IP、掩码、网关、DNS，点「应用」生效；显示设备状态，带「打开终端」。应用后等 PC 终端恢复（eNSP Pro `ipv4.status` 回到 Ready）再提示「已生效」，期间提示稍候。接口 `GET|POST /api/lab/devices/{console}/host`，复用 `LabProvider.host_get / host_set`；校验 IP / 掩码 / 网关 / DNS（422），不是 PC 400、不在拓扑 404、没运行 409，eNSP 经典版 400——前端只显示「请在 eNSP 软件中双击 PC 设置」，输入框禁用。
  - 练习和考试中允许修改，只能改本题开放的设备（403）；与开始 / 结束串行（`PracticeManager.set_host`）。每次修改写进操作记录（`attempt_logs`）：伪命令「[PC 配置] PC1 ip=… mask=… gw=… dns=…」，输出「已生效：…」或「设置失败：…」，命令回放、评分可见；开着自动判定时改完排一次判定。
  - 结束时 PC 地址随复位还原：场景 `hosts` 与拓扑里是 PC 的本题设备开练时按地址配置拍快照（§10.4），复位设回快照；还没有快照的开放 PC 在第一次修改前补拍（端口与快照落库，服务重启后照样还原）。
  - DNS：`enspro.HOST_DNS_ATTR = "ipv4.config.dns1"`（2026-10-07 真机核实，§14 第 22 项；ainet 只管首选 DNS，`dns2` 不动）。PC 从没设过 DNS 时 `refreshAttrs` 里没有这个键，按空处理：`host_cfg` 总是带 `dns`，开练快照因此总有 dns，学员改了 DNS 结束时能设回去（设 `dns1=""` 真机实测可清空）；比对时只有一边没有 dns 键（旧快照、旧干净基线）就不比 DNS。
