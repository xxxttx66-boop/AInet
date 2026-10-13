# eNSP Pro 能否替代 eNSP 经典版：验证结论（2026-10-06）

> 结论先行：**在练习 / 考试这条主线上，eNSP Pro 可以替代 eNSP 经典版**。迁移后的 5 道 YunShan 题（4 道 Campus + OSPF）在真机 +
> 无头浏览器上跑通了练习、额外乱改后的整机复位和一场 3 题考试。需要接受的差异有四点：YunShan 与 VRP5 命令体系不同；
> PC 只能当观察点；EXTERNAL-HUB 起不来；同一账号只能有一个会话。
> 另外，设备会自动保存、停机时也保存，这是新出现的风险，已用「干净基线」解决。
> 建议新部署默认用 eNSP Pro 后端，经典版保留给 VRP5 教材一致性和没有 KVM 主机的场合。

## 1. 验证环境

| 项目 | 内容 |
|---|---|
| eNSP Pro | 本机（Linux 开发机，32 核 / 124GB）KVM 虚机 `ensppro`：16 vCPU、32GB，`https://192.168.56.101:8443`，离线账号 |
| 设备系统 | ENSP-AR = AR6700，YunShan OS 1.25.1.1（V600R025C10SPC100）；ENSP-LSW = S6700，V600R025C10SPC500；PC-CLIENT = 容器（受限 bash） |
| 沙箱 | `ainet_campus`（id 3），经 REST 接口新建，设备由 ainet 的启停接口启动 |
| ainet | worktree `agent-a11bf22edf55fd7d7`，`AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=3`，AI 评分 / 提示关闭（只看流程） |
| 证据 | scratchpad `enspro-replace/`：`server*.log`、`e2e_*.log`、`boot_*.log`、`res_*.txt`、截图 `ys_*.png` |

沙箱 `ainet_campus` 与 Campus_Network 的对应：

| 设备 | 类型 | 连线 | 与原拓扑的差异 |
|---|---|---|---|
| AR0 / AR2 | ENSP-AR | AR0 GE0/0/1 — AR2 GE0/0/0；AR0 GE0/0/0 — LSW0 GE1/0/1 | 与原拓扑一致（接口名改为 YunShan 缩写） |
| AR1 | ENSP-AR | AR2 GE0/0/2 — AR1 GE0/0/0 | 原拓扑是 AR2 Ethernet0/0/0 — AR1 Ethernet0/0/0，ENSP-AR 没有 Ethernet 口，改用 GE |
| LSW0 | ENSP-LSW（S6700） | GE1/0/1 — AR0，GE1/0/2 — PC1，GE1/0/3~5 — R1~R3 | 原为 S5700（GE0/0/x），现为 S6700（GE1/0/x） |
| PC1 | PC-CLIENT | GE1/0/0 — LSW0 GE1/0/2 | 原拓扑的 PC 挂在 LSW3~6 下；只保留一台，接在 LSW0 |
| R1~R3 | ENSP-AR | GE0/0/0 — LSW0 GE1/0/3~5（VLAN 1） | 原 2-1 OSPF 拓扑三台经 Cloud 共享网段；EXTERNAL-HUB 起不来，用 LSW0 的 VLAN 1 代替 |

原 Campus_Network 共 42 台设备（AR×8、S5700×28、PC×8），4 道 Campus 题只用到 AR0、AR2、LSW0。按每台约 2.4GB 计算，整套搬过来要 100GB 以上，所以只建了题目用到的部分，再加上 AR1、PC1 和 OSPF 的三台路由器。

## 2. 逐项结论

| # | 能力 | 结论 | 证据摘要 |
|---|---|---|---|
| 1 | 建拓扑、启停设备 | **完全替代（优于经典版）** | 沙箱、设备、连线都能经接口建好；ainet 页面和接口都能启停设备。经典版只能人工操作。`batchReboot` 返回 HTTP 405，ainet 自动改成「停止 → 启动」 |
| 2 | 路由器（AR6700） | **完全替代**（命令体系是 YunShan） | 5 道题的 baseline、inject、restore、判定都在真机上跑通；接口只认 `GE0/0/x`，写 `GigabitEthernet0/0/0` 报 Wrong parameter |
| 3 | 交换机（S6700） | **完全替代**（已覆盖题目用到的功能） | 单阶段生效；VLAN、access、trunk、pvid、Vlanif、shutdown、stp mode、静态路由、sysname 都能整机复位（1 轮 31 条命令）。Eth-Trunk、STP 端口参数未测 |
| 4 | PC 终端 | **部分替代**（作为观察点比经典版强） | 有 `PC>` 命令行，能 ping，系统可以拿它做判定；但命令行不能改地址，地址只能由 ainet 经接口配置。所以出不了「PC 地址 / 网关配错、学员在 PC 上修」的题 |
| 5 | Hub / Cloud | **不能替代**（Hub） | EXTERNAL-HUB 启动后一直停在「分配虚机」（状态 2），超过 17 分钟，已删除；共享网段改用交换机 VLAN 1。Cloud 未测 |
| 6 | 配置复位（快照差异回滚） | **完全替代** | AR 和 LSW 上场景改动加额外乱改都一轮撤销，与开练前快照逐行一致（e2e 的 reset_ok） |
| 7 | 自动保存 | **新风险，已解决** | AR 改动后 5 分钟自动保存；有未保存改动时停机会保存（LSW 也会）；关掉自动保存也挡不住停机保存，保存行为还不稳定。对策是干净基线（§4），真机上抓到并复位了一台带旧配置启动的 R3 |
| 8 | 练习流程 | **完全替代** | 5 道 YunShan 题的浏览器 e2e 每道 5/5，额外乱改测试 6/6；最终代码上整套回归（5 题 + 额外乱改 + 考试 + 会话）一轮全部通过，前一轮暴露的 traffic-filter 异步报错已修复、冷启动 console 滞后已记录 |
| 9 | 考试流程 | **完全替代** | 3 题考试 e2e 7/7：开考、作答、提交、未通过后重试、下一题、交卷二次确认、成绩单、各题复位成功 |
| 10 | 登录与会话 | **完全替代**（有单会话限制） | 新设备首次登录设密码（7 台）；空闲 5 分钟登出后自动重登；会话被挤掉时页面提示，重新登录后终端恢复；自动重登模式实测可用 |
| 11 | 资源与时间 | **可接受** | 8 台全开时虚机内存 21.1/32GB、CPU 9~21%；8 台一起冷启动 207 秒（经典版在 Windows 上单台 AR 3~5 分钟） |
| 12 | 教材一致性（VRP5） | **不能替代** | HCIA 教材按 VRP5 讲：接口全称、display 输出格式、S5700 / AR1220 的行为都不同。需要与教材逐字一致时仍用经典版 |
| 13 | 学员绕开 ainet 的操作 | **与经典版相同（看不到）** | 学员在 eNSP Pro 网页终端里的操作 ainet 记录不到，而且登录网页会把 ainet 挤掉 |

## 3. 实测细节（第 2~8 项）

### 3.1 交换机 LSW（S6700 V600R025C10SPC500）

- **提示符和视图**：`<HUAWEI>`、`[HUAWEI]`、`[HUAWEI-GE1/0/1]`、`[HUAWEI-Vlanif10]`，与 AR 相同。
- **生效方式**：单阶段生效，不出现 `[~` / `[*`；`commit` 能敲，但什么也不做；`return` 不问确认。
- **接口名**：只认缩写 `GE1/0/1~10`。`interface GigabitEthernet1/0/2` 报 `Error: Wrong parameter found at '^' position.`，AR 上写 `GigabitEthernet0/0/0` 也一样，所以 YunShan 题统一写缩写。
- **端口缺省值**：link-type 为 `desirable`，PVID 1，Trunk VLAN List 为 `1-4094`。这些缺省值不显示在配置里，`undo port link-type` 会回到缺省。
- **配置写法与 undo 写法**（全部实测）：
  - `vlan batch 10 20`，撤销用 `undo vlan batch …`，会问 `[Y/N]`，同时删掉 `vlan N` 视图里的配置；
  - `port link-type access|trunk`，撤销用 `undo port link-type`；
  - `port default vlan N`，撤销用 `undo port default vlan`；
  - `port trunk allow-pass vlan …`，撤销用 `undo port trunk allow-pass vlan …`；
  - `port trunk pvid vlan N`，撤销用 `undo port trunk pvid vlan N`；
  - `interface Vlanif10` + `ip address 10.0.10.254 24`，撤销用 `undo interface Vlanif10`；
  - `ip route-static 0.0.0.0 0 …`：会提示 Warning，但不问 Y/N；
  - `stp mode rstp`，撤销用 `undo stp mode`。
- **`display vlan`**：先是表头和图例（U/D/TG/UT…），然后 `VID  Ports` 段，例如 `10  UT:GE1/0/1(U)` / `TG:GE1/0/2(U)`；最后是 `VID Type Status Property …` 段。
- **`display port vlan`**：每行 `Port  Link Type  PVID  Trunk VLAN List`，例如 `GE1/0/1  access  10  --`、`GE1/0/2  trunk  1  1 10 20`。
- **分页**：`  ---- More ----`，用 `ESC[16D` 擦除，空格翻页，与 AR 相同，现有处理已覆盖。
- **自动保存**：配置里没有 `configuration file auto-save`，也没有保存过的配置文件（`display saved-configuration` 显示文件不存在），但停机时照样保存，见 §3.3。
- **复位**：`reset_probe.py` 在 LSW0 上做了 VLAN、trunk、access、pvid、shutdown、Vlanif、LoopBack、stp、静态路由、sysname 一共 21 处改动，复位一轮执行 31 条命令，结果与出厂配置一致（`reset_lsw1.log`）。e2e 的额外乱改测试也在 LSW0 上复位干净，终端提示符改回 `<HUAWEI>`。
- **已修改的代码**：
  - hub：PC 提示符；
  - config_reset：没有改动，原逻辑直接可用；
  - 判定正则：PC 的 ping 用 `\b[12] received\b`；
  - 假设备：`YunShanSwitch` 拒绝长接口名，按配置计算 `display port vlan`，删 VLAN 时连带删掉 vlan 视图。

### 3.2 PC（PC-CLIENT）

- **console**：没有登录，提示符是 `PC> `。`help` 列出 ping、traceroute、arp、ifconfig、ssh、curl、watch，没有 ipconfig。traceroute 是全屏刷新的程序（类似 mtr），不适合做判定。
- **ping**：Linux iputils 格式，例如 `2 packets transmitted, 2 received, 0% packet loss`。`ifconfig` 显示 IPv4 地址、掩码、网关、MAC。
- **配地址**：前端用的是设备属性接口，取自 bundle：
  - 读：`GET /service/ensp/device/property/refreshAttrs?deviceId=N`；
  - 写：`POST /service/ensp/device/property/updateAttrs?deviceId=N`，body 为 `{"ipv4.type":"static","ipv4.config.ip":…,"ipv4.config.ipmask":…,"ipv4.config.gateway":…}`。
  
  设备运行时立即生效，`ifconfig` 能看到。改完后 `ipv4.status` 先是 `PendingUpdate`，**这期间 PC 的命令行会重启，吞掉当时输入的命令**。这曾让考试第 3 题的故障确认超时，已修复：设完地址后等状态回到 Ready，只读判定命令没有回显、又停在提示符上时重发一次。
- **ainet 的支持**：
  - 场景字段 `hosts`；
  - 主机命令 `ip address <IP> <掩码>`、`ip gateway <IP>`、`undo ip address`、`undo ip gateway`，由 ainet 翻译成接口调用；
  - 快照、复位、干净基线对主机都按地址配置处理；
  - 判定在 PC 上只允许 ping。
  
  VLAN 题的 YunShan 版加了 PC1，额外判定「PC1 能 ping 通网关」。

### 3.3 自动保存与配置持久化

| 实测 | 结果 |
|---|---|
| AR 改配置后 | 5 分钟后自动保存，例如 09:24:08 改、09:29:08 存，09:53:03 改、09:58:04 存。原 spec 记的「约 2 分钟」不准 |
| 停止设备 | 有未保存的改动时，停机会以 `by _SYSTEM_` 保存：AR1 09:33:30、R3 09:39:43，LSW0 10:50:25（交换机配置里没有 auto-save，照样保存）；没有改动时不保存，例如 AR1 停在 09:58 的保存 |
| 启动设备 | 从 `flash:/1.cfg` 启动（`display startup`），AR1 的标记 LoopBack99 在停止、启动后仍在 |
| `undo configuration file auto-save` | 能挡住定时保存（R3 改动 6.5 分钟后仍没保存），**挡不住停机保存**（R3 停机时保存，标记仍在） |
| 不稳定的例子 | R3 以「自动保存关闭」启动，运行中重新打开自动保存并撤掉标记，之后 55 分钟既没有定时保存，8 台一起停机时也没有保存。再次启动后带回了旧标记和「自动保存关闭」 |
| 重启接口 | `batchReboot` 返回 HTTP 405（前端有菜单，但接口没开放），ainet 改成「停止 → 等到已停止 → 启动」 |

**方案（已实现）**：开练时关掉自动保存不可靠，因为停机照样保存，而且保存行为不稳定。ainet 采用「练习、考试每题开始前与快照（干净基线）比对，不一致就复位」：

- 每台设备在 `app_settings` 里记一份干净基线（键为 `baseline:enspro:<沙箱名>:<设备>`）：
  - 网络设备存整机配置；
  - PC 存地址配置。
- 每题开始拍快照前，先读当前配置，与基线比对：
  - 一致，记为 `clean`；
  - 不一致，先复位到基线，记为 `reset`，开练快照取复位后的配置；
  - 复位不干净，拒绝开始，并提示停止、重新启动设备或重新记录基线；
  - 还没有基线（`AINET_LAB_BASELINE_AUTO=1`，缺省），把这次读到的配置记为基线，记为 `captured`。**注意：自动记录会把设备当时的配置当成干净的**，如果设备恰好带着旧改动启动（如 R3），旧改动会被记进基线；生产环境应先确认设备干净（例如与出厂配置比对），再用 `POST /api/lab/baseline` 手动记录，或把 `AINET_LAB_BASELINE_AUTO` 设为 0。
- 接口：
  - `GET /api/lab/baseline` 查看各设备的基线状态；
  - `POST /api/lab/baseline {devices?}` 手动重记，练习、考试进行中拒绝。
- 只对 eNSP Pro 后端启用。经典版的 VRP5 不自动保存，行为不变。
- 真机证据：
  - 在 AR0 上手工留下改动（接口地址、描述、LoopBack55）后开题，`pre_reset` 显示 AR0 `reset`，一轮 6 条命令；
  - 8 台冷启动后，R3 带回旧配置，下一次开 OSPF 题时 `pre_reset` 显示 R3 `reset`（differences 2），复位后题目正常完成。

### 3.4 登录与会话（第 7 项）

- **首次登录设密码**：7 台新设备都由 ainet 的连接器完成，日志为 `enter → set-password → confirm-password → enter → prompt`（server2.log）。
- **空闲登出**：真机 console 空闲约 5 分钟会登出，输出 `User interface con0 is available` + 空行 + `Please Press ENTER.` + 两个空行。
  - 旧逻辑只看最后一行（为空），检测不到，要等学员按回车才重登；而且学员当时敲的命令会被吞掉。
  - 改为按最后一个非空行识别：连接器立即自己回车、输一次密码。
  - 真机日志：R2 17:39:30 登录，17:44:33 自动重登；R3 17:42:05 登录，17:47:21 自动重登。
- **会话被挤掉**（e2e session 6/6）：
  1. 脚本另开一个登录会话，1.5~4 秒后页面状态栏显示「该账号在其它地方被重新登录（code 50070014）」，并出现「重新登录 eNSP Pro」按钮。
  2. eNSP Pro 会关掉被挤会话的 console WebSocket，终端标签显示「连接已断开」。
  3. 缺省不自动重登，别处的会话不受影响。
  4. 点按钮后页面恢复，别处的会话被挤掉；重开终端后 display clock 正常。
- **自动重登模式**（`AINET_ENSPRO_AUTO_RELOGIN=1`，间隔 15 秒）：被挤后倒计时，15.1 秒后自动重登并恢复，对方会话被挤掉（autorelogin.log）。
- **刚启动的设备**：8 台一起冷启动后，接口状态已经是「运行中」，但部分设备的 console 有几分钟完全没有输出（AR0 约 5 分钟，AR2、LSW0 约 11 分钟）。这期间开题会因「console 登录超时，设备没有任何输出」失败，稍后重试即可，提示已写明。单台重启时 1 分钟内就能登录。

### 3.5 资源（第 8 项）

| 场景 | 数据 |
|---|---|
| 设备类型标称（接口 `device-type/all`） | AR / LSW / CE：1 CPU、1.7GB；USG 等防火墙：1.5 CPU、2.6GB；PC：0.2 CPU、0.25GB；EXTERNAL-HUB：1 CPU、1.6GB |
| 首次启动 9 台（7 AR + LSW + PC + HUB 同时） | PC 33 秒；LSW 63 秒；AR 134~193 秒；HUB 一直不就绪 |
| 单台 AR 重启（停止 → 启动） | 102 秒、109 秒 |
| 8 台一起冷启动（全停后同时启动） | LSW 88 秒；AR 128~207 秒；PC 136 秒；**全部就绪 207 秒**；宿主机 qemu 进程峰值约 10.6 核，宿主机负载峰值约 9 |
| 8 台全开、空闲 | 虚机 CPU 9~21%，内存 21.1 / 32.1GB（72%），磁盘 6%；宿主机 qemu 约 5.8 核（top 580%） |
| 全部停止 | 虚机 CPU 1%，内存 6%（约 1.9GB） |
| 练习耗时（真机 e2e） | 开始 10~15 秒（2~3 台设备），OSPF 题 20~30 秒（4 台）；结束复位 2~14 秒；开考 8~17 秒，考试换题 14~18 秒，交卷 5 秒 |

按以上数据，每台设备平均约 2.4GB。32GB 的虚机同时开 10~12 台路由器 / 交换机比较稳妥。

## 4. 题库迁移

| YunShan 题（`scenarios/*-ys.json`） | 原题（保留） | 改动 | 真机 e2e |
|---|---|---|---|
| `campus-ar0-ar2-link-ys` | campus-ar0-ar2-link | 接口缩写；参数同原题 | 5/5 |
| `campus-lsw0-access-vlan-ys` | campus-lsw0-access-vlan | 交换机接口 GE1/0/x；加 PC1（hosts）与第二条判定；提示与评分要点按「Vlanif10 仍 up」改写 | 5/5 |
| `campus-ar2-return-route-ys` | campus-ar2-return-route | 接口缩写；参数同原题 | 5/5 |
| `campus-ar2-acl-block-ys` | campus-ar2-acl-block | 接口缩写；参数同原题；ACL、traffic-filter 写法不变 | 5/5；额外乱改 6/6 |
| `ospf-r1-shutdown-ys` | ospf-2-1-r1-shutdown | 三台路由器经 LSW0 的 VLAN 1 组成共享网段；OSPF 配置改由 baseline 下发、restore 撤销；LSW0 列入 required 但不开放终端 | 5/5 |

所有题目都是 `platform: yunshan`、`topology: ainet_campus`。e2e 由服务随机取种子，参数化与原题相同。

考试 e2e 用 3 题模板，固定种子 777 / 4242 / 9：
- 开考 8~17 秒；
- 第 1 题一次通过；
- 第 2 题先提交未通过，修复后重试通过；
- 第 3 题含 PC1 判定，一次通过；
- 交卷需二次确认，成绩单 3 题都通过、各题 `reset_ok`；
- AI 评分关闭，只给结果分。

## 5. 期间修复的问题（都有单元测试）

1. **空闲登出后不自动重登**：真机输出的最后一行为空，旧逻辑识别不到，改按最后一个非空行识别。
2. **复位偶发超时**：AR6700 删 ACL 或 traffic-filter 后几秒，会在提示符之后异步打印 `Error: Failed to apply the traffic-filter …`，而且不重印提示符，hub 一直等不到提示符而超时。现在把它按日志处理。
3. **PC 吞命令**：改地址后 PC 命令行重启、吞掉输入。现在设完地址等状态回到 Ready，只读命令没有回显时重发一次。
4. **重启接口 405**：退回「停止 → 启动」。
5. **诊断日志**：
   - console 登录超时时，记下设备最后一行输出；
   - 登录步骤写进日志；
   - hub 命令超时、回不到用户视图时，记下最后输出。

## 6. 迁移建议

1. **默认后端：建议切到 eNSP Pro**，在新部署的配置里设 `AINET_LAB_PROVIDER=enspro`、`AINET_ENSPRO_SANDBOX=<ainet_campus 的 id>`。理由如下：
   - ainet 能自己启停设备、建拓扑；
   - console 稳定，不存在经典版「断开后几分钟收不到输出」「频繁连接导致崩溃」的问题；
   - 冷启动更快；
   - 题库已经迁好，练习、考试全链路在真机上验证过。
   
   代码里的缺省值暂时保留 `ensp`：Windows 上 `start.bat` 的现有部署不受影响，等部署形态确定后再改。
2. **前提条件**：
   - ainet 与 eNSP Pro 在同一台有 KVM 的主机上（eNSP Pro 只监听 host-only 网段）；
   - eNSP Pro 账号由 ainet 独占，教师不要同时登录网页，要看的话先在 ainet 上停用，或开自动重登，但会互相挤；
   - 新建沙箱、确认设备是出厂干净配置之后，用 `POST /api/lab/baseline` 记基线（第一次开题自动记录会把当时的配置当成干净的，不可靠）；本次验证的基线只在临时库里，生产库需要重新记；
   - 冷启动后等几分钟再开题。
3. **旧版 eNSP 保留的作用**：
   - VRP5 题库，与 HCIA 教材命令和输出一致；
   - 没有 KVM 主机、只能在 Windows 单机部署时作为后备；
   - 大拓扑练习，例如 42 台的 Campus，在 eNSP Pro 上约要 100GB 内存；
   - VRP5 行为的对照与回归。
   
   两个后端的题目用 `platform` 区分，互不影响。
4. **后续**：
   - 补测交换机的 Eth-Trunk、STP 端口参数、DHCP，以及 USG 等防火墙设备；
   - 查清 EXTERNAL-HUB 起不来的原因；
   - 设计以 PC 作为观察点的应用层题目（DHCP、DNS、HTTP）；
   - 切换沙箱目前要重启服务；
   - eNSP Pro 读配置、命令日志接口的参数仍未确认。
