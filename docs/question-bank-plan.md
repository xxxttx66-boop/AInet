# eNSP Pro（YunShan OS）题库规划（2026-10-07 草案）

> 状态：离线规划，17 道新题先以草稿（`"draft": true`，课程页缺省不列出，见 §3）提交。
> **2026-10-07 已在本机 eNSP Pro 逐题真机验证，17 道全部通过并去掉 `draft` 上线**（进度与逐题记录见
> [`question-bank-verify.md`](question-bank-verify.md)）。其中 4 道按真机输出修正后通过：两道 DHCP 题的 restore 去掉
> `undo dhcp enable`（出厂配置），两道 NAT 题改成 AR6700 的 nat-policy / nat server 写法（§4 L09）。
> §6 的「需真机确认」各条已写上实测结论（「→ 实测」）。

> 当前 AInet 对学员统一显示设备型号名：`ainet_test` 的 ENSP-AR（AR6700）节点在原沙箱 API 中叫 T1/T2，页面映射为 AR1/AR2；设备 ID、端口、连接及后台 console 绑定不变。其他沙箱按实际拓扑名称显示。

## 1. 现状与目标

**现状**（2026-10-07，基线 master 6c6c792）：eNSP Pro 有 6 道已上线题，全部在真机验证过。

| 章节 | 已上线题 | 沙箱 |
|---|---|---|
| L02 IP 编址 | `enspro-t1-t2-link`、`campus-ar0-ar2-link-ys` | ainet_test / ainet_campus |
| L03 静态路由 | `campus-ar2-return-route-ys` | ainet_campus |
| L04 VLAN | `campus-lsw0-access-vlan-ys` | ainet_campus |
| L05 OSPF | `ospf-r1-shutdown-ys` | ainet_campus |
| L06 ACL | `campus-ar2-acl-block-ys` | ainet_campus |

L01 以及 L07~L12 在两个环境下都还没有题。

**目标**：每章 2~3 道，从易到难。现有两个沙箱能做的先写成草稿 JSON，做不了的写清楚需要什么样的新沙箱。

**本轮结果**：新增 17 道题（先为草稿，2026-10-07 真机验证后全部上线）（ainet_test 5 道、ainet_campus 12 道），覆盖 L01~L09 和 L12。L10 STP、L11 防火墙需要新建沙箱，只做了规划。L08 的动态 DNS、L07 的 PC 当 DHCP 客户端也只做了规划：前者缺 DNS 服务器，后者需要扩展 ainet。

**每章题数**（已上线 + 草稿 + 仅规划；2026-10-07 真机验证后 17 道草稿题都计入已上线）：

| 章节 | 已上线 | 草稿 JSON | 仅规划 |
|---|---|---|---|
| L01 链路与接口 | 3 | 0 | 0 |
| L02 IP 编址 | 4 | 0 | 0 |
| L03 静态路由 | 2 | 0 | 1（浮动静态，需加一条链路） |
| L04 VLAN | 2 | 0 | 1 |
| L05 OSPF | 3 | 0 | 0 |
| L06 ACL | 2 | 0 | 1 |
| L07 DHCP | 2 | 0 | 2（中继；PC 当客户端） |
| L08 DNS | 1 | 0 | 1（动态 DNS，需新沙箱或新设备） |
| L09 NAT | 2 | 0 | 0 |
| L10 STP | 0 | 0 | 3（新沙箱 ainet_l2） |
| L11 防火墙 | 0 | 0 | 3（新沙箱 ainet_fw） |
| L12 综合排障 | 2 | 0 | 1 |

## 2. 沙箱清单

两个现有沙箱的设备和连线，取自现有题目的 `note` 字段与 `docs/enspro-replacement.md` §1。

### 2.1 ainet_test（沙箱 id 2）

| 设备 | 类型 | 连线 |
|---|---|---|
| AR1 | ENSP-AR（AR6700，YunShan OS 1.25.1.1 / V600R025C10SPC100） | AR1 GE0/0/1 — AR2 GE0/0/1 |
| AR2 | ENSP-AR（同上） | 同上 |

- 只有一条链路，适合做两台路由器之间的题：接口、地址、掩码、静态路由，以及这几类叠加的多处故障。
- **需真机确认**：
  - 沙箱里除 AR1、AR2 外是否还有别的设备。课程 e2e 里出现的 PC1（deviceId 12）只存在于假环境。
  - AR2 的 GE0/0/2 是否存在、在未接线时是否显示物理 down。`enspro-t2-ip-wrong-port` 依赖这一点。

### 2.2 ainet_campus（沙箱 id 3）

| 设备 | 类型 | 连线 |
|---|---|---|
| AR0 | ENSP-AR | GE0/0/0 — LSW0 GE1/0/1；GE0/0/1 — AR2 GE0/0/0 |
| AR2 | ENSP-AR | GE0/0/0 — AR0；GE0/0/2 — AR1 GE0/0/0 |
| AR1 | ENSP-AR | GE0/0/0 — AR2 GE0/0/2（现有题都没用到） |
| LSW0 | ENSP-LSW（S6700，V600R025C10SPC500） | GE1/0/1 — AR0；GE1/0/2 — PC1；GE1/0/3~5 — R1~R3 |
| PC1 | PC-CLIENT | GE1/0/0 — LSW0 GE1/0/2 |
| R1 / R2 / R3 | ENSP-AR | GE0/0/0 — LSW0 GE1/0/3 / 4 / 5 |

- LSW0 在缺省配置下所有端口都在 VLAN 1。所以不给 LSW0 下发配置时，AR0 GE0/0/0、PC1、R1~R3 的 GE0/0/0 处在同一个二层网段。`ospf-r1-shutdown-ys` 在真机上验证过这一点。
- 本轮新题按角色使用这些设备：
  - AR1：运营商（ISP），用在 L09 的 NAT 题；
  - R1：DHCP 客户端，用在 L07 的 DHCP 题；
  - R1 / R2：DNS 客户端和文件服务器，用在 L08。
- **需真机确认**：
  - AR1 除 GE0/0/0 以外的接口有没有接线。按现有资料没有。
  - LSW0 的 GE1/0/6~10 有没有接线。按现有资料没有。
  - 同时开启的设备数。8 台全开时虚机内存约 21/32GB。每道新题最多用 4 台设备（NAT 题用 AR0、AR1、AR2、LSW0）。

### 2.3 现有沙箱做不了的

| 需求 | 原因 | 去向 |
|---|---|---|
| STP 环路 | 只有一台交换机，没有冗余二层链路 | 新沙箱 ainet_l2（§5.1） |
| 防火墙 | 没有 USG 类设备 | 新沙箱 ainet_fw（§5.2） |
| 动态 DNS | 没有能当 DNS 服务器的设备 | 新沙箱 ainet_svc，或先查 eNSP Pro 的设备类型（§5.3） |
| PC 当 DHCP 客户端 | ainet 的主机命令只能配静态地址（`scenarios.HOST_COMMANDS`） | 扩展 ainet（§4 L07 第 4 题） |
| 浮动静态路由 / 主备链路 | 两个沙箱里两点之间都只有一条路径 | 在 ainet_campus 里加一条 AR0—AR1 链路（§5.4） |

## 3. draft（未真机验证）机制

**场景字段**：`"draft": true`。可选，只能是布尔值。
- 加载时校验（`backend/scenarios.py:280`），规范化后带出（`backend/scenarios.py:369`）；
- `scenarios.public()` 带出 `draft`（`backend/scenarios.py:544`）。

**列表**：`GET /api/scenarios` 缺省不列草稿题（`backend/main.py:623-627`）。课程页和操作页都调这个接口，所以两边都看不到。
- 例外：正在进行的那道照样列出。真机验证时按 id 开始一道草稿题，操作页仍然要按 `?all=1` 找到它的工单。
- 「其他平台题数」`other_platform` 也不把隐藏的草稿题算进去。
- 响应另带 `show_drafts`，以及 `drafts_hidden`（隐藏了几道）。

**开关**：
- 环境变量 `AINET_SHOW_DRAFTS=1`（`backend/config.py:139-141`）打开后，草稿题照常列出；
- 打开时课程卡片标「草稿 · 未验证」（`frontend/courses.js:324`）；
- 按 id 开始练习、单题考试不受 draft 限制，真机验证要靠这一点。

**测试**：
- `backend/tests/test_courses.py:147-206`：字段校验、缺省隐藏、开关显示、进行中照列、`other_platform` 计数；
- `backend/tests/test_scenarios.py`：题库清单 `BANK_TEST` / `BANK_CAMPUS`，真机验证通过的记进 `VERIFIED`；要求 `DRAFTS_*`（= 清单 − `VERIFIED`）正好是带 draft 的题，且只有它们的 note 写「未真机验证」。

**已知局限**：考试模板管理页（`/exam/templates`）仍然列出全部题目，包括草稿题。那是出题人用的页面，选了草稿题就是有意为之。

## 4. 分章规划

图例：
- **状态**：「已上线」= 真机验证过；「草稿」= 已写 JSON、`draft: true`；「规划」= 只有本文描述。
- 本节的命令写法是出题时的推断；与真机不符的地方已改题，并在各条后面用「→ 实测」注明（汇总见 §6）。
- **假 e2e**：在假 eNSP Pro（`backend/tests/test_question_bank_fake.py`）上跑通了注入 → 确认故障 → 经终端修复 → 判定 → 复位。

命令统一按 YunShan 写：
- 接口用缩写，路由器 GE0/0/x、交换机 GE1/0/x；
- 配置立即生效，不用 commit；
- 场景里不写 quit，回顶层用 `return` + `system-view`；
- 设备配置开头的 `!` 注释行、`---- More ----` 分页由 hub 和复位逻辑处理，题目不用管；
- 复位分两层：场景的 `restore` 加整机快照复位（spec §10），前者只撤销场景自己的改动。

### L01 链路与接口

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | AR1 与 AR2 链路中断（接口状态）`enspro-t2-port-shutdown` | 已上线（2026-10-07 真机） | ainet_test：AR1、AR2（ENSP-AR） | 1 |
| 2 | 新接入的 AR2 不通（地址配错接口）`enspro-t2-ip-wrong-port` | 已上线（2026-10-07 真机） | ainet_test：AR1、AR2 | 2 |
| 3 | 网关 ping 不通交换机（端口状态）`campus-lsw0-port-shutdown-ys` | 已上线（2026-10-07 真机） | ainet_campus：AR0、LSW0、PC1（AR + LSW + PC） | 2 |

1. **故障**：AR2 的 GE0/0/1 被 shutdown，AR2 开放终端，学员能修。spec §6.2 第 3 条说「关闭对端接口并且不开放对端」，那样学员修不了，所以本题让故障端开放。
   - 注入：`AR2: interface GE0/0/1 / shutdown`。
   - 判定：AR2 `display ip interface brief` 里 GE0/0/1 为 up/up；AR1 能 `ping -c 2 10.0.12.2`。
   - 复位：restore 撤销两端地址、对 AR2 执行 undo shutdown；之后整机复位。
   - **需真机确认**：对端关闭后，本端 GE0/0/1 是否也显示 down。
2. **故障**：互联地址被配到没接线的 GE0/0/2 上。
   - 注入：`AR2: GE0/0/1 undo ip address；GE0/0/2 ip address 10.0.12.2 24`。
   - 判定：AR1 能 ping 通 10.0.12.2。
   - 复位：撤销两个接口的地址。
3. **故障**：LSW0 的上联口 GE1/0/1 被关闭。PC1 到交换机通、到网关不通，借这个现象教学员分段判断。
   - 注入：`LSW0: interface GE1/0/1 / shutdown`。
   - 判定：AR0 能 ping 通 10.0.10.254；PC1 能 ping 通 10.0.10.1。
   - 复位：基线沿用 `campus-lsw0-access-vlan-ys`，restore 多一条 undo shutdown。

### L02 IP 编址

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | AR1 与 AR2 互联不通 `enspro-t1-t2-link` | 已上线 | ainet_test | 1 |
| 2 | AR0 与 AR2 互联不通 `campus-ar0-ar2-link-ys` | 已上线 | ainet_campus | 1 |
| 3 | 用户电脑访问服务器不通（网关）`campus-pc1-gateway-ys` | 已上线（2026-10-07 真机） | ainet_campus：AR0、AR2、LSW0、PC1 | 1 |
| 4 | AR1 ping 不通 AR2（两端掩码不一致）`enspro-t2-mask-mismatch` | 已上线（2026-10-07 真机） | ainet_test：AR1、AR2 | 2 |

3. **故障**：PC1 的网关被设成 10.0.10.100，网段里没有这个地址。同网段能通、跨网段不通。spec §6.2 允许在 eNSP Pro 上出 PC 地址类故障，学员用「PC 设置」气泡修。
   - 注入：`PC1: ip gateway 10.0.10.100`（主机命令，由 ainet 翻译成设备属性接口）。
   - 判定：PC1 能 `ping -c 2 192.168.2.1`。
   - 复位：restore 撤销 AR0、AR2 的地址和路由，以及 PC1 的地址；开练快照按地址配置拍。
   - LSW0 不下发配置，用缺省 VLAN 1。
4. **故障**：AR2 一端是 10.0.12.130/25，AR1 的 10.0.12.1 不在这个网段里。请求能到 AR2，AR2 回不了包。
   - 注入：`AR2: interface GE0/0/1 / ip address 10.0.12.130 25`（重配地址时覆盖原地址）。
   - 判定：AR1 能 ping 通 10.0.12.130。

### L03 静态路由

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | AR1 访问 AR2 的业务地址不通（静态路由）`enspro-t1-static-route-dest` | 已上线（2026-10-07 真机） | ainet_test：AR1、AR2 | 2 |
| 2 | 管理网访问服务器网段不通（回程下一跳）`campus-ar2-return-route-ys` | 已上线 | ainet_campus | 3 |
| 3 | 主链路断了没有切到备份（浮动静态路由） | 规划 | ainet_campus 加 AR0 GE0/0/2 — AR1 GE0/0/1 | 4 |

1. 两台路由器的 LoopBack0 是 1.1.1.1 和 2.2.2.2，用静态路由互通。
   - 注入：把 AR1 去 2.2.2.2 的路由目的写成 22.2.2.2（`undo ip route-static 2.2.2.2 32 10.0.12.2`、`ip route-static 22.2.2.2 32 10.0.12.2`）。
   - 判定：AR1 `ping -c 2 -a 1.1.1.1 2.2.2.2`。
   - 复位：撤销两边的路由、LoopBack0 和地址。
   - **需真机确认**：YunShan 上 `ping -c … -a …` 的参数顺序。
3. **规划**：服务器网段 192.168.2.0/24 走两条路径：主路径 AR0 → AR2，备份路径 AR0 → AR1 → AR2，备份静态路由 `preference 100`。
   - 注入：备份路由写成和主路由相同的优先级（形成等价负载分担），或写错备份下一跳；再关闭主链路的 AR2 一端（AR2 不开放终端）。
   - 判定：从 LSW0 能 ping 通服务器网段。
   - 前提：要先在 ainet_campus 里新建这条链路（§5.4）。

### L04 VLAN

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | 交换机管理地址 ping 不通（接入 VLAN）`campus-lsw0-access-vlan-ys` | 已上线 | ainet_campus | 2 |
| 2 | VLAN 20 用户上不了网关（单臂路由 Trunk）`campus-ar0-dot1q-trunk-ys` | 已上线（2026-10-07 真机） | ainet_campus：AR0、LSW0、PC1 | 3 |
| 3 | 跨 VLAN 不通（交换机 Vlanif 网关） | 规划 | ainet_campus：LSW0、R1、R2 | 3 |

2. **配置**：AR0 的两个子接口做单臂路由，GE0/0/0.10 和 GE0/0/0.20 分别用 `dot1q termination vid 10/20`。LSW0 的 GE1/0/1 是 Trunk，放行 10、20；PC1 在 VLAN 20。
   - 注入：`LSW0: interface GE1/0/1 / undo port trunk allow-pass vlan 20`。
   - 判定：PC1 能 ping 通 10.0.20.1 和 10.0.10.254。
   - 复位：删两个子接口、撤销 trunk / VLAN / Vlanif / 路由，撤销 PC1 地址。
   - **需真机确认**：AR6700 的子接口写法；是否还要 `arp broadcast enable`（VRP5 需要）；`undo interface GE0/0/0.10` 能否直接删掉子接口。
3. **规划**：LSW0 用 Vlanif10、Vlanif20 做两个 VLAN 的网关。R1 在 GE1/0/3（VLAN 10），R2 在 GE1/0/4（VLAN 20），两台各配一条默认路由指向自己的 Vlanif。
   - 故障：Vlanif20 的地址配到了 Vlanif30 上，或者 GE1/0/4 的 PVID 和 Vlanif 对不上。
   - 判定：R1 能 ping 通 R2。

### L05 OSPF

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | OSPF 邻居全部断开（R1）`ospf-r1-shutdown-ys` | 已上线 | ainet_campus：R1~R3、LSW0 | 2 |
| 2 | R3 的 OSPF 邻居建不起来（区域）`ospf-r3-area-mismatch-ys` | 已上线（2026-10-07 真机） | 同上 | 2 |
| 3 | R2 与 R1 建不起 OSPF 邻居（定时器）`ospf-r2-hello-mismatch-ys` | 已上线（2026-10-07 真机） | 同上 | 3 |

- 共同基线沿用已上线的 `ospf-r1-shutdown-ys`：R1~R3 经 LSW0 的 VLAN 1 在 10.0.123.0/24，LoopBack0 宣告进 area 0。restore 用 `undo ospf 1`，会问 [Y/N]，由 hub 自动回答。
- 2 的注入：R3 的 `network 10.0.123.0 0.0.0.255` 从 area 0 挪到 area 1，中间用 `return` + `system-view` 回到顶层。
- 3 的注入：`R2: interface GE0/0/0 / ospf timer hello 20`。
- 判定（两题相同）：R1 `display ospf peer brief` 里 `2.2.2.2 … Full` 和 `3.3.3.3 … Full` 都要有，正则依赖 Neighbor id 和 State 两列相邻，**需真机确认**。
- **注意**：baseline 和 inject 连续执行，注入后确认故障时邻居本来就还没建立，所以「故障已生效」的确认总会通过，证明不了注入有效。真机验证时要等约 1 分钟，确认邻居仍然不是 Full。

### L06 ACL

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | 管理网 ping 不通服务器（ACL 误拦截）`campus-ar2-acl-block-ys` | 已上线 | ainet_campus | 3 |
| 2 | 禁 Telnet 的策略把 ping 也挡了（ACL）`campus-ar2-acl-telnet-ys` | 已上线（2026-10-07 真机） | ainet_campus：AR0、AR2、LSW0 | 3 |
| 3 | ACL 应用方向错误（outbound / inbound） | 规划 | 同上 | 3 |

2. **故障**：本意是只禁管理网 Telnet 服务器，rule 5 却写成了 `deny ip`。
   - 判定：LSW0 能 ping 通；`display acl all` 里还有 `deny tcp … destination-port eq telnet|23`；接口上的 traffic-filter 还在。
   - **需真机确认**：`destination-port eq telnet` 的写法，以及 display 里显示成 telnet 还是 23。正则两种都认。
3. **规划**：禁访客网段访问服务器的 ACL 被挂在 AR2 GE0/0/0 的 outbound 方向（应为 inbound），结果访客照样能访问。
   - 判定：要能证明访客被禁。需要一个访客源地址，例如在 AR0 上配 LoopBack 10.0.20.1，用 `ping -a 10.0.20.1` 当访客。
   - 判定还要求管理网仍然能访问。

### L07 DHCP

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | 终端拿不到地址（DHCP 服务未生效）`campus-ar0-dhcp-select-ys` | 已上线（2026-10-07 真机，修正后通过） | ainet_campus：AR0（服务器）、R1（客户端）、LSW0 | 2 |
| 2 | 终端拿不到地址（地址池无可用地址）`campus-ar0-dhcp-exhausted-ys` | 已上线（2026-10-07 真机，修正后通过） | 同上 | 3 |
| 3 | 跨网段拿不到地址（DHCP 中继） | 规划 | ainet_campus：AR2（服务器）、AR0（中继）、R1、LSW0 | 4 |
| 4 | PC 拿不到地址（PC 当 DHCP 客户端） | 规划，要扩展 ainet | ainet_campus：AR0、LSW0、PC1 | 2 |

- 客户端用路由器 R1（`ip address dhcp-alloc`），这是 spec §6.2 第 2 条「用路由器扮演 PC」。
- AR0 用全局地址池 campus：
  - `dhcp enable`；
  - `ip pool campus` / `gateway-list 10.0.10.1` / `network 10.0.10.0 mask 255.255.255.0` / `excluded-ip-address 10.0.10.2 10.0.10.9`；
  - 接口 `dhcp select global`。
- → 实测：`dhcp enable` 是 AR6700 的**出厂配置**，restore 不能 `undo dhcp enable`（会被整机复位补回），已从两题的 restore 删掉。
  服务器修好后客户端不用重新 dhcp-alloc，约 20~35 秒自动拿到地址。
- **R1 的 dhcp-alloc 放在 inject 最后**：required 顺序是 AR0、R1、LSW0，inject 按这个顺序执行。放进 baseline 的话，R1 可能在注入之前就拿到地址，故障不生效。
- 1 的注入：`AR0: interface GE0/0/0 / undo dhcp select global`。
- 2 的注入：`AR0: ip pool campus / excluded-ip-address 10.0.10.10 10.0.10.254`。
- 判定：R1 `display ip interface brief` 里 GE0/0/0 拿到 10.0.10.x/24（第 2 题要求 ≥ 10.0.10.10）；R1 能 ping 通网关；第 2 题还要求保留段那一条仍在。
- **需真机确认**：
  - YunShan 的 DHCP 全套写法；
  - `undo ip pool`、`undo dhcp enable` 是否问 [Y/N]；
  - 服务器修好后，客户端要多久才重新获取。如果太久，提示里让学员在 R1 上重新 dhcp-alloc。
- **注意**：和 OSPF 题一样，注入后确认故障时 R1 还来不及拿到地址，所以确认总会通过，证明不了注入有效。真机验证时要等约 1 分钟，确认 R1 仍然没有地址。
3. **规划**：AR2 上配地址池，网关是 10.0.10.1。AR0 的 GE0/0/0 做中继：`dhcp select relay` + `dhcp relay server-ip 10.0.12.2`，VRP5 写法，YunShan **需真机确认**。
   - 故障：server-ip 写错，或者 AR2 没有回 10.0.10.0/24 的路由。
4. **规划**：ainet 要加一条主机命令 `ip dhcp`，翻译成设备属性 `ipv4.type=dhcp`（spec §6.2 记录的属性名）。
   - 复位：按地址配置快照还原，dhcp 改回 static。
   - 判定：PC1 能 ping 通网关。
   - 改动范围：`HOST_COMMANDS`、`apply_host_commands`、`enspro.host_set`、主机快照格式，另补测试。本轮不做。

### L08 DNS

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | 按名字访问文件服务器不通（静态域名）`campus-r1-dns-host-ys` | 已上线（2026-10-07 真机） | ainet_campus：R1、R2、LSW0 | 1 |
| 2 | 按名字访问不通（DNS 服务器地址配错） | 规划 | 新沙箱 ainet_svc，或 ainet_campus + DNS 服务器 | 3 |

1. **故障**：R1 的静态主机表把 files.ainet.lab 指向了 10.0.123.20（应为 10.0.123.2）。
   - 注入：`undo ip host files.ainet.lab 10.0.123.2`、`ip host files.ainet.lab 10.0.123.20`。
   - 判定：R1 能 `ping -c 2 files.ainet.lab`。
   - **需真机确认**：YunShan 的 `ip host` / `display ip host` 写法，以及 ping 能不能直接用静态主机名。
2. **规划**：R1 上配 `dns resolve` + `dns server <IP>`，或者把 PC1 的 dns1 指向 DNS 服务器（ainet 已支持 dns1，见 spec §16.4）。故障是 DNS 服务器地址配错。
   - 现有沙箱没有能当 DNS 服务器的设备，要先查 eNSP Pro 的设备类型（§5.3）。

### L09 NAT

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | 管理网访问外网不通（Easy IP）`campus-ar2-easyip-acl-ys` | 已上线（2026-10-07 真机，修正后通过） | ainet_campus：AR0、AR1（运营商）、AR2（出口）、LSW0 | 3 |
| 2 | 外网访问不到发布的内网设备（静态 NAT）`campus-ar2-nat-static-ys` | 已上线（2026-10-07 真机，修正后通过） | 同上 | 4 |

- 共同拓扑：
  - AR2 的 GE0/0/2 = 198.51.100.2/24 接运营商 AR1（198.51.100.1，LoopBack0 = 203.0.113.1/32）；
  - AR1 没有去内网的路由，也不开放终端；
  - AR0 的默认路由指向 AR2，AR2 的默认路由指向 AR1；
  - 地址都用文档保留网段。
1. **配置**：~~AR2 的 GE0/0/2 上配 `nat outbound 2000`~~ → 实测 AR6700 接口下没有 `nat outbound`，改为：GE0/0/2 `nat enable`，
   `nat-policy` → `rule name easyip` → `source-address 10.0.10.0 24`、`egress-interface GE0/0/2`、`action source-nat easy-ip`。
   - 注入：规则先加 `source-address 10.0.20.0 24`，再 `undo source-address 10.0.10.0 24`（删掉最后一个源地址会问 [Y/N]）。
   - 判定：LSW0 能 ping 通 203.0.113.1，且 `display nat-policy rule name easyip` 里仍是 `action source-nat easy-ip`。
2. **配置**：~~GE0/0/2 上 `nat static global …`~~ → 实测改为系统视图 `nat server mgmt global 198.51.100.10 inside 10.0.10.254`
   （GE0/0/2 同样要 `nat enable`），把交换机管理地址发布出去。
   - 注入：`undo nat server name mgmt` 后以 inside 10.0.10.253 重配（同名不能直接覆盖）。
   - 判定：AR1 能 ping 通 198.51.100.10（AR1 不开放终端，判定照常能跑），且 `display nat server` 的 inside 地址为 10.0.10.254。
- **需真机确认**：
  - AR6700（YunShan）的 NAT 写法，以及要不要 license 或 nat instance；
  - `display nat outbound`、`display nat static` 命令；
  - 静态 NAT 的公网地址和出接口同网段时，AR2 会不会代答 ARP。
- **AR1 第一次被题目使用**：自动保存实验时 AR1 留过 LoopBack99 等标记（`docs/enspro-replacement.md` §3.3）。验证前先和出厂配置比对，确认干净后用 `POST /api/lab/baseline` 手动记基线。否则自动记录会把旧配置当成干净基线，比如残留一条回内网的路由，Easy IP 的故障就不会生效。

### L10 STP（新沙箱 ainet_l2，§5.1）

| # | 题目 | 状态 | 设备 | 难度 |
|---|---|---|---|---|
| 1 | 流量绕远路（根桥被抢） | 规划 | LSW1~LSW3（ENSP-LSW）+ PC1、PC2 | 2 |
| 2 | 交换机互联口被错误关闭（边缘端口 + BPDU 保护） | 规划 | 同上 | 3 |
| 3 | 部分 VLAN 不通（MSTP 域配置不一致） | 规划 | 同上 | 4 |

- **基线**：
  - 三台都配 `stp mode rstp`（S6700 已验证）；
  - LSW1 配 `stp root primary`，LSW2 配 `stp root secondary`；
  - 互联口是 Trunk，放行 10、20；PC1 在 LSW2 的 VLAN 10，PC2 在 LSW3 的 VLAN 10。
1. **注入**：`LSW3: stp priority 0`，根桥被抢到 LSW3。
   - 判定：LSW1 `display stp brief` 里两个互联口都是 DESI（根桥上所有端口都是指定端口），且 PC1 能 ping 通 PC2。
   - 复位：`undo stp priority`。
2. **注入**：LSW2 的上联口配了 `stp edged-port enable`，全局开了 `stp bpdu-protection`，端口收到 BPDU 后被 error-down。
   - 判定：互联口 up，且 PC1 能 ping 通 PC2。
   - 修复：`undo stp edged-port`，再 `shutdown` / `undo shutdown`，或配 error-down 自动恢复。
3. **注入**：MSTP 模式下，LSW3 的 `stp region-configuration` 里 region-name 或 VLAN 映射与另外两台不一致，被当成别的域，阻塞端口挡住了 VLAN 20。
   - 判定：VLAN 20 的两台主机能互通。
- **风险**：不出「关闭 STP 造成广播风暴」这类题。风暴会把 eNSP Pro 虚机的 CPU 打满，影响整个沙箱。
- **需真机确认**：S6700 的 STP 端口参数还没测过（spec §14 第 14 项），包括 edged-port、bpdu-protection、MSTP region、`display stp brief` 的格式。

### L11 防火墙（新沙箱 ainet_fw，§5.2）

| # | 题目 | 状态 | 设备 | 难度 |
|---|---|---|---|---|
| 1 | 内网访问外网不通（接口没加入安全区域） | 规划 | FW1（USG 类）+ R1（trust）、R2（untrust） | 2 |
| 2 | 内网访问外网不通（安全策略方向写反） | 规划 | 同上 | 3 |
| 3 | 外网能访问 DMZ 服务器、内网反而不能（策略顺序） | 规划 | 同上 + R3（dmz） | 4 |

- **基线**（按 USG6000 系列写法，YunShan / eNSP Pro 上的 USG 型号**需真机确认**）：
  - `firewall zone trust` / `add interface GE1/0/1`，`firewall zone untrust` / `add interface GE1/0/2`；
  - `security-policy` / `rule name trust_out` / `source-zone trust` / `destination-zone untrust` / `action permit`；
  - 互联口配 `service-manage ping permit`，方便学员 ping 防火墙。
1. **注入**：`firewall zone untrust` / `undo add interface GE1/0/2`。
2. **注入**：把 rule 的 source-zone 和 destination-zone 对调。
3. **注入**：插一条范围更大的 deny，用 `rule move` 放到 permit 之前。
- **判定**：都由 R1 / R2 / R3 之间 ping 来判，必要时再看 `display security-policy rule all`。
- **需真机确认**：
  - eNSP Pro 有哪些防火墙类型。`device-type/all` 里列了 USG 等，标称 1.5 CPU、2.6GB；
  - 防火墙的 console 登录、首次改密码，以及是否要先 `undo shutdown` 接口；
  - 是否两阶段提交（hub 已兼容 `[~` / `[*`）；
  - 复位：`undo security-policy` 之类会不会问 [Y/N]，整机快照复位在防火墙的配置结构上能不能用（config_reset 只在 AR / LSW 上验证过）。

### L12 综合排障

| # | 题目（文件） | 状态 | 沙箱 / 设备 | 难度 |
|---|---|---|---|---|
| 1 | AR1 访问 AR2 业务地址不通（多处故障）`enspro-t1-t2-two-faults` | 已上线（2026-10-07 真机） | ainet_test：AR1、AR2 | 3 |
| 2 | 管理网访问服务器不通（多处故障）`campus-mgmt-two-faults-ys` | 已上线（2026-10-07 真机） | ainet_campus：AR0、AR2、LSW0 | 4 |
| 3 | OSPF + ACL 叠加 | 规划 | ainet_campus：R1~R3、LSW0 | 5 |

1. 两处故障：AR2 的互联地址网段错，AR1 去 2.2.2.2 的下一跳是不存在的 10.0.12.3。
   - 两条判定分层：直连能 ping 通、源为 1.1.1.1 能 ping 通 2.2.2.2。
2. 两处故障：LSW0 GE1/0/1 的接入 VLAN 被改成 30，AR2 回程下一跳被改成 10.0.12.3。两种写法各自都在真机上验证过，组合在一起没有。
   - 两条判定：LSW0 能 ping 通网关、能 ping 通服务器。
3. **规划**：R2 的 Hello 定时器被改，同时 R1 上有一条 ACL 挡住了 3.3.3.3 发往 1.1.1.1 的流量。
   - 判定：邻居 Full，且 R1 能 `ping -a 1.1.1.1 3.3.3.3`。

## 5. 新建沙箱建议

新沙箱都按 `docs/enspro-replacement.md` 的做法经 REST 接口新建（`link/create` 已确认），建好后：
1. 首次启动由 ainet 设 console 密码；
2. 确认设备是出厂配置，再用 `POST /api/lab/baseline` 记录干净基线；
3. 题目的 `topology` 写沙箱名。

### 5.1 ainet_l2（L10 STP）

```
        LSW1 (根桥候选)
       GE1/0/1   GE1/0/2
        /             \
  GE1/0/1             GE1/0/1
   LSW2 ---GE1/0/2--GE1/0/2--- LSW3
   GE1/0/3                     GE1/0/3
     |                           |
    PC1                         PC2
```

- **设备**：3 × ENSP-LSW（S6700）、2 × PC-CLIENT，约 3 × 1.7GB + 2 × 0.25GB ≈ 5.6GB。
- 可选：LSW1 的 GE1/0/3 接一台 ENSP-AR 当网关，做跨 VLAN 和 STP 的综合题。
- **风险**：spec §14 第 21 项，LSW 镜像启动卡住率约 43%。三台交换机同时冷启动时，依赖看门狗自动重启，准备时间会明显变长。建议错开启动（`labpower` 已实现），并在课程页提示预计时间。

### 5.2 ainet_fw（L11 防火墙）

```
  R1 (trust, 10.1.1.0/24) --GE1/0/1-- FW1 --GE1/0/2-- R2 (untrust, 198.51.100.0/24)
                                       |
                                    GE1/0/3
                                       |
                                  R3 (dmz, 172.16.1.0/24)
```

- **设备**：1 × 防火墙（USG 类，约 2.6GB）、3 × ENSP-AR，约 7.7GB。
- **先做的验证**：
  1. 防火墙能否启动；
  2. console 登录流程；
  3. 命令是一阶段还是两阶段生效；
  4. 整机快照复位能否用在防火墙配置上。

  验证通过前，不写题目 JSON。

### 5.3 ainet_svc（L08 动态 DNS、L07 PC 当 DHCP 客户端）

- 先查 `GET /service/ensp/device-type/all`，确认 eNSP Pro 有没有 Server、DNS、Linux 主机一类的设备类型。
  - 有：建 AR0 + LSW0 + PC1 + DNS 服务器；
  - 没有：L08 只出静态主机表的题。另一个方向是 spec §10.2 的方案 B，外接 Linux 主机，属于里程碑 9。
- L07 的「PC 当 DHCP 客户端」不需要新沙箱，在 ainet_campus 上扩展 ainet 的主机命令即可（§4 L07 第 4 题）。

### 5.4 ainet_campus 增加一条链路（L03 浮动静态路由）

- 用 `link/create` 新建 AR0 GE0/0/2 — AR1 GE0/0/1。现有题目都没用到这两个口。
- 加链路会改变沙箱的拓扑指纹，现有题不受影响。
- 加完重记 AR0、AR1 的干净基线。

## 6. 需要真机确认的点（汇总）

1. **沙箱现状**：
   - ainet_test 是否只有 AR1、AR2；
   - AR2 的 GE0/0/2 未接线时是否 down；
   - ainet_campus 里 AR1 其他口、LSW0 GE1/0/6~10 是否空闲。
   - → 实测：ainet_test 只有 AR1、AR2；AR2 GE0/0/2 未接线显示 down/down；ainet_campus 的 AR1 只接 GE0/0/0，LSW0 GE1/0/6~10 空闲。
2. **接口**：对端 shutdown 后本端是否显示 down；`display interface brief` 里 `*down` 的写法。
   - → 实测：对端 shutdown 后本端 down/down（虚拟链路传递）；`display interface brief` 与 `display ip interface brief` 里管理关闭都显示 `*down`。
3. **地址**：接口重配同一地址、不同掩码时直接覆盖；`display ip interface brief` 掩码列的写法。
   - → 实测：重配同一地址、不同掩码直接覆盖；掩码列显示为 `10.0.12.130/25`。
4. **命令参数**：
   - `ping -c 2 -a <源> <目的>` 的参数顺序；
   - `display current-configuration | include …` 是否可用；
   - `display current-configuration configuration ospf` 是否可用（只出现在提示里）。
   - → 实测：`ping -c 2 -a <源> <目的>` 可用；`display current-configuration | include …` 可用；`… configuration ospf` 可用（`configuration nat-policy` 不可用）。
5. **子接口**：`interface GE0/0/0.10` + `dot1q termination vid 10` 的写法，是否要 `arp broadcast enable`，`undo interface GE0/0/0.10`。
   - → 实测：写法可用，**不需要** `arp broadcast enable`；`undo interface GE0/0/0.10` 直接删除子接口。
6. **OSPF**：
   - 接口 `ospf timer hello` / `undo ospf timer hello` 的写法，改 Hello 后 Dead 是否随之变化；
   - `undo network` 的写法；
   - `display ospf peer brief` 里 Neighbor id 与 State 两列是否相邻；
   - 注入后约 1 分钟邻居仍不是 Full（确认故障真的生效）。
   - → 实测：`ospf timer hello 20` / `undo ospf timer hello` 可用，Dead 随之变 80；`undo network` 同 VRP5；peer brief 列为 Area Id / Interface / Neighbor id / State，判定正则可用；两道题注入 90 秒后邻居仍不 Full。
7. **ACL**：`destination-port eq telnet` 的写法与显示；基本 ACL `rule … permit source …`。
   - → 实测：`destination-port eq telnet` 可用，`display acl all` 显示为 telnet（不是 23），行尾带 `(N times matched)`。基本 ACL `rule … permit source …` 已没有题用到（Easy IP 题改用 nat-policy 的 source-address），仍未实测。
8. **DHCP**：
   - 全局地址池全套写法、`excluded-ip-address` 多条并存与 undo；
   - 客户端 `ip address dhcp-alloc`；
   - `undo ip pool` / `undo dhcp enable` 是否问 [Y/N]；
   - 客户端重新获取地址要多久；
  - 注入后约 1 分钟 R1 仍然没有地址（确认故障真的生效）。
   - → 实测：全局地址池全套写法、`excluded-ip-address` 多条并存与按完整范围 undo、`ip address dhcp-alloc` 都可用；`dhcp enable` 是出厂配置（restore 不 undo）；`undo ip pool campus` 执行正常（是否提示确认没有单独观察，hub 会自动回答）；客户端约 20~35 秒自动重新获取；注入 90 秒后 R1 仍无地址。
9. **DNS**：`ip host` / `undo ip host` / `display ip host` 的写法，ping 能否直接用静态主机名。
   - → 实测：`ip host` / `undo ip host <名> <址>` / `display ip host` 可用，ping 能直接用静态主机名；改主机表后紧接着那次 ping 可能还解析到旧地址，几秒后正常。
10. **NAT**：
    - `nat outbound <ACL>`、`nat static global … inside …` 能否直接用；
    - `display nat outbound` / `display nat static`；
    - 静态 NAT 公网地址的 ARP 代答。
    - → 实测：接口下**没有** `nat outbound` / `nat static`；改用 `nat-policy`（Easy IP）与 `nat server`（静态），出接口必须 `nat enable`；不需要 license、不需要安全区域；查看用 `display nat-policy rule all|name <名>`、`display nat server`、`display session all`（没有 `display nat outbound|static`）；同网段公网地址 AR2 代答 ARP；改完 NAT 规则几秒后才生效。
11. **STP / 防火墙**：见 §4 L10、L11。
12. **AR1 的干净基线**：AR1 曾留过实验标记，NAT 题验证前先确认出厂配置、手动记基线（§4 L09）。
    - → 实测：AR1 是出厂配置（没有 LoopBack99 等旧标记），已手动记基线；另外开工时 AR0、AR2 各有一条 `campus-ar0-ar2-link-ys` 的残留地址，已按该题 restore 撤销后再记基线。
13. **资源**：NAT 题同时开 4 台 AR / LSW，L10 新沙箱 3 台 LSW 冷启动（启动卡住率）。
    - → 实测：ainet_campus 8 台全开跑完 12 道题无资源问题（R3 冷启动卡住一次，看门狗自动重启后正常）；切 ainet_test 前先停掉 campus 全部设备。

## 7. 真机验证步骤（每道草稿题；2026-10-07 已按此跑完 17 道）

1. 确认 eNSP Pro 没有别处在用。单会话，ainet 一登录就会挤掉 Windows 上的 ainet。
2. 用 `AINET_SHOW_DRAFTS=1` 启动 ainet，课程页上会出现带「草稿 · 未验证」标记的题。第一次用到的设备（如 AR1）先确认是出厂配置，再用 `POST /api/lab/baseline` 记基线。
3. 按题跑：

   ```
   python tests/e2e/enspro_campus_e2e.py practice <场景 id>
   python tests/e2e/enspro_campus_e2e.py practice-extra <场景 id>
   ```

   覆盖：开始（故障确认）→ 修复 → 自动判定 → 结束复位 `reset_ok`。
   - 脚本的修复步骤按题写在 `fixes()` 里（2026-10-07 已补齐这 17 道；PC 地址题走 PC 设置气泡的同一接口）；
   - 设备号按当前拓扑取，ainet_test 的题也用这个脚本（先经 `POST /api/lab/prepare` 切到 ainet_test）；
   - OSPF / DHCP 题开始后先等 90 秒（`FAULT_WAIT`）、看一眼现象，再判一次应不通过（步骤 2c），排除「还没收敛」造成的假故障；
   - `AINET_E2E_REQUIRE_BASELINE=1`：开练时每台设备都要与手动记录的干净基线比对过（clean / reset）；
   - `AINET_E2E_LOG=<文件>`：把学员每条命令的完整回显记下来，核对真机输出格式。
4. 再看「需真机确认」的点，对照真机输出修正命令写法或判定正则，并在题目 `note` 里写上实测结论。
5. 通过后：
   - 删掉题目里的 `"draft": true`；
   - 把它的 id 加进 `backend/tests/test_scenarios.py` 的 `VERIFIED`（`DRAFTS_TEST` / `DRAFTS_CAMPUS` 由清单减去 `VERIFIED` 得到）；
   - 更新本文 §4 的状态列和 spec 里程碑。
