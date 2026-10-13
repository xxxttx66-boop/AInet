# eNSP Pro 草稿题真机验证进度（2026-10-07）

基线 master 7d01971；本机 KVM eNSP Pro（https://192.168.56.101:8443），本地 ainet 只绑 127.0.0.1、独立临时数据库
（`AINET_SHOW_DRAFTS=1`、`AINET_LAB_BASELINE_AUTO=0`、`AINET_AI_SCORING=0`），Windows 上的 ainet 已停。
脚本：`backend/tests/e2e/enspro_campus_e2e.py practice <场景 id>`。

状态：pending / pass / fixed-pass（按真机修正题目后通过）/ fail / skipped。中断后从本表续做，不重做已完成项。

## 沙箱现状（只读核对）

- ainet_campus（id 3）：8 台设备 LSW0(8) AR0(9) AR2(10) AR1(11) PC1(12) R1(14) R2(15) R3(16)；连线与规划文档 §2.2 一致：
  AR1 只接 GE0/0/0，LSW0 只用 GE1/0/1~5（GE1/0/6~10 空闲）。
- ainet_test（id 2）：只有 T1、T2 两台（PC1 只存在于假环境）。
- AR1、R1、R2、R3、LSW0 是出厂配置（AR1 没有 LoopBack99 之类的旧实验标记，与其他 AR 逐行相同，只差 snmp engine-id、
  console 密码密文与时间戳）；PC1 没有地址。
- 开工时 AR0、AR2 在运行，配置里各有一条题目残留：AR0 GE0/0/1 `10.0.14.1/24`、AR2 GE0/0/0 `10.0.30.2/24`
  （与 `campus-ar0-ar2-link-ys` link=14、wrong=30 停在故障中、被自动保存的状态一致）。其余部分 AR0 与 AR2 逐行相同。
  这是 ainet 自己的题目残留（不是别的实验），按该题 restore 的写法在两台上各执行一次 `undo ip address` 撤销，
  之后 AR0、AR2 与 AR1 出厂配置一致；再用 `POST /api/lab/baseline` 给 AR0、AR1、AR2、LSW0、PC1、R1、R2 记干净基线
  （R3 冷启动卡住被看门狗重启，起来后核对再记）。脚本设 `AINET_E2E_REQUIRE_BASELINE=1`：开练时每台设备都要与基线比对过。

## 逐题

| # | 题目文件 | 章节 | 状态 | 耗时 | 要点 |
|---|---|---|---|---|---|
| 1 | campus-lsw0-port-shutdown-ys | L01 | pass | 开始 17s、全程约 1 分钟 | 7 pass / 0 fail；对端 shutdown 后 AR0 GE0/0/0 down/down；修复后 PC1 立即通、AR0 第一轮 ping 还不通，3s 后判定通过；复位 0 轮（restore 已撤销干净） |
| 2 | campus-pc1-gateway-ys | L02 | pass | 开始 21s、全程约 1 分钟 | 7 pass / 0 fail；PC 设置气泡（同一接口）改回网关后 12s 自动判定通过；复位 0 轮 |
| 3 | campus-ar0-dot1q-trunk-ys | L04 | pass | 开始 9s、全程约 1 分钟 | 7 pass / 0 fail；修复后 9s 自动判定通过；复位 0 轮 |
| 4 | ospf-r3-area-mismatch-ys | L05 | pass | 开始 24s + 等 90s、全程约 2.5 分钟 | 7 pass / 0 fail；等 90s 后判定 1/2（R1 只有 2.2.2.2 Full，R3 无邻居）；修复后 6s 自动判定通过；复位 0 轮 |
| 5 | ospf-r2-hello-mismatch-ys | L05 | pass | 开始 9s + 等 90s、全程约 2.5 分钟 | 7 pass / 0 fail；等 90s 后判定 1/2（R1 只有 3.3.3.3 Full）；修复后 18s 自动判定通过；复位 0 轮 |
| 6 | campus-ar2-acl-telnet-ys | L06 | pass | 开始 19s、全程约 1 分钟 | 7 pass / 0 fail；注入后判定 1/3；修复后 6s 自动判定通过；复位 0 轮 |
| 7 | campus-ar0-dhcp-select-ys | L07 | fixed-pass | 开始 15s + 等 90s、全程约 2.5 分钟（跑了两次） | 7 pass / 0 fail；第一次跑 AR0 整机复位补回 dhcp enable（restore 里的 undo dhcp enable 删了出厂配置），去掉这条后重跑复位 0 轮；等 90s 后 R1 仍无地址；修复后 R1 自动重新获取，35s 判定通过 |
| 8 | campus-ar0-dhcp-exhausted-ys | L07 | fixed-pass | 开始 10s + 等 90s、全程约 4 分钟 | 7 pass / 0 fail（restore 同第 7 题去掉 undo dhcp enable）；等 90s 后 R1 仍无地址、判定 0/3；修复后 19s 判定通过；复位 0 轮。脚本小插曲：AR0 console 空闲登出后自动重登，第一条命令吞了首字母，不影响修复 |
| 9 | campus-r1-dns-host-ys | L08 | pass | 开始 16s、全程约 1 分钟 | 7 pass / 0 fail；修复后学员紧接着的 ping 仍解析到旧地址 10.0.123.20，3s 后自动判定通过；复位 0 轮 |
| 10 | campus-ar2-easyip-acl-ys | L09 | fixed-pass | 第一次开始失败（约 3 分钟）；手工试写法约 20 分钟；改写后全程约 1 分钟 | 原写法 `nat outbound 2000` 在 AR6700 接口下报 Unrecognized command，开始即失败并已还原。手工试出 nat-policy + 接口 `nat enable` 的写法（试验配置事后逐台撤销、与基线比对无残留），改写 baseline / inject / checks / restore / solution / 提示；重跑 7 pass / 0 fail，修复后紧接着的 ping 还不通，约 16s 后自动判定通过；复位 0 轮 |
| 11 | campus-ar2-nat-static-ys | L09 | fixed-pass | 开始 14s、全程约 1 分钟 | 原写法 `nat static global … inside …` 在 AR6700 接口下不存在，改为系统视图 `nat server mgmt global 198.51.100.10 inside 10.0.10.254` + GE0/0/2 `nat enable`，判定加一条 `display nat server` 的 inside 地址；7 pass / 0 fail，修复后 9s 判定通过；复位 0 轮 |
| 12 | campus-mgmt-two-faults-ys | L12 | pass | 开始 23s、全程约 1 分钟 | 7 pass / 0 fail；注入后判定 0/2；修好二层后网关通、服务器仍不通（分层成立）；修好回程后判定通过；复位 0 轮 |
| 13 | enspro-t2-port-shutdown | L01 | pass | 开始 15s、全程约 1 分钟 | 7 pass / 0 fail；对端关闭后 T1 GE0/0/1 down/down，T2 该口 *down；修复后 3s 判定通过；复位 0 轮。开题前先停掉 ainet_campus 全部设备，再经 POST /api/lab/prepare 切到 ainet_test 并启动 T1/T2；两台核对为出厂配置后手动记基线 |
| 14 | enspro-t2-ip-wrong-port | L01 | pass | 开始 8s、全程约 1 分钟 | 7 pass / 0 fail；注入后 T2 GE0/0/1 unassigned up/down、GE0/0/2 10.0.12.2/24 down/down；修复后 3s 判定通过；复位 0 轮 |
| 15 | enspro-t2-mask-mismatch | L02 | pass | 开始 8s、全程约 1 分钟 | 7 pass / 0 fail；注入后 T2 显示 10.0.12.130/25 up/up、T1 ping 不通；修复后 3s 判定通过；复位 0 轮 |
| 16 | enspro-t1-static-route-dest | L03 | pass | 开始 10s、全程约 1 分钟 | 7 pass / 0 fail；修复后 6s 判定通过；复位 0 轮 |
| 17 | enspro-t1-t2-two-faults | L12 | pass | 开始 14s、全程约 1 分钟 | 7 pass / 0 fail；注入后判定 0/2；两处修完 6s 判定通过；复位 0 轮 |

## 真机实测结论

（逐条补充，同步到 `docs/question-bank-plan.md` §6）

- 接口（§6.2）：对端 shutdown 后本端显示 down/down（eNSP Pro 虚拟链路传递对端关闭）；`display interface brief` 里管理关闭显示 `*down`；
  `display ip interface brief` 地址列为 `10.0.10.1/24`。
- PC 地址故障（§4 L02-3）：网关不可达时 PC-CLIENT 的 ping 输出 Destination Host Unreachable 与「0 received, +2 errors」，判定正则 `\b[12] received\b` 不受影响；改地址后 PC 命令行重启，第一次 ping 可能因 ARP 不通。
- 子接口（§6.5）：`interface GE0/0/0.10` + `dot1q termination vid 10` 可用，**不需要** `arp broadcast enable`；`undo interface GE0/0/0.10` 直接删除子接口；主接口不配地址时显示 unassigned up/down。
- OSPF（§6.6）：`display ospf peer brief` 列为 Area Id / Interface / Neighbor id / State，Neighbor id 与 State 相邻，`\b3\.3\.3\.3\s+Full\b` 可用；`undo network` 同 VRP5；`display current-configuration configuration ospf` 可用（区域显示成 area 0.0.0.1）；区域不一致注入 90s 后邻居仍不建立。
- OSPF 定时器（§6.6）：接口视图 `ospf timer hello 20` / `undo ospf timer hello` 可用；改 Hello 后 Dead 随之变成 80（`display ospf interface GE0/0/0` 的 Timers 行）；注入 90s 后 R1 与 R2 仍无邻居。
- ACL（§6.7）：`rule 5 deny tcp … destination-port eq telnet` 可用，`display acl all` 显示为 `destination-port eq telnet`（不是 23），行尾带 `(N times matched)`；高级 ACL 视图提示符 `[HUAWEI-acl4-advance-3001]`。
- DHCP（§6.8）：`dhcp enable` 是 AR6700 出厂配置，两道 DHCP 题的 restore 去掉 `undo dhcp enable`；全局地址池全套写法与 `ip address dhcp-alloc` 可用；`undo ip pool campus` 执行正常（是否提示确认没有单独观察，hub 会自动回答）；服务器修好后客户端不用重新 dhcp-alloc，约 20~35s 自动拿到地址；注入 90s 后 R1 仍是 unassigned。
- DHCP 保留段（§6.8）：`excluded-ip-address` 两条范围可并存，`undo excluded-ip-address <起> <止>` 写完整范围只撤销那一条。
- 静态域名（§6.9）：`ip host` / `undo ip host <名字> <地址>` / `display ip host`（列 Host / Age / Flag / IP Address，Flag 为 Static）可用；ping 能直接用静态主机名，首行显示解析结果；改主机表后紧接着的 ping 可能还解析到旧地址，几秒后正常。
- NAT（§6.10）：AR6700 接口下**没有** `nat outbound` / `nat static`（接口视图 nat 只有 enable、gratuitous-arp）。源 NAT 用 `nat-policy` → `rule name <名>` → `source-address 10.0.10.0 24` / `egress-interface GE0/0/2` / `action source-nat easy-ip`；一对一发布用系统视图 `nat server <名> global <公网> inside <内网>`；两者都要出接口 `nat enable`，否则规则不命中。不需要 license，不需要把接口加入安全区域（缺省 security-policy permit）。查看：`display nat-policy rule all`（HITS）、`display nat-policy rule name <名>`、`display nat server`、`display session all`；没有 `display nat outbound` / `display nat static`。删掉规则里最后一个 source-address 会问 [Y/N]；改完规则几秒后才生效。
- 静态 NAT（§6.10）：公网地址与出接口同网段时 AR2 代答 ARP（AR1 直接 ping 通 198.51.100.10）；同名 nat server 不能直接覆盖（报 already exists），要先 `undo nat server name <名>`；`display nat server` 里 `inside-start-addr : 10.0.10.254`。
- ainet_test 只有 T1、T2（§2.1）；未接线的 GE0/0/2 显示 down/down；`display ip interface brief` 里管理关闭的接口物理列也显示 `*down`。
- 地址（§6.3）：接口重配同一地址、不同掩码直接覆盖，不报冲突；`display ip interface brief` 显示为 `10.0.12.130/25`。
- 命令参数（§6.4）：`ping -c 2 -a <源> <目的>` 可用；`display current-configuration | include …` 可用；`display current-configuration configuration ospf` 可用（`configuration nat-policy` 不可用）。

## 收尾与遗留

- 17 道全部通过并去掉 draft（13 pass、4 fixed-pass、0 fail、0 skipped）。全部完成后停掉了 ainet_campus 8 台与 ainet_test 2 台设备
  （其中 AR0、AR2 开工前就在运行），eNSP Pro 三个沙箱运行数都是 0；本地 ainet 已关闭并登出。停机前 T1、T2 与出厂配置逐行比对无差异，
  campus 各设备最后一题结束时复位与基线一致。
- 题目之外动过设备的操作：AR0 / AR2 残留地址各一次 `undo ip address`；NAT 写法手工试验动过 AR0、AR1、AR2、LSW0，试完逐台撤销，
  再与出厂配置比对，LSW0 漏掉的 `vlan batch 10` 手动删除，最终无差异（之后两道 NAT 题开练时基线比对也是 clean）。
- Windows 上的 ainet：AR0 / AR2 的残留看起来是一次停在故障中的 `campus-ar0-ar2-link-ys` 练习，那边的库里可能还记着这道进行中的练习，
  重启后会恢复它，建议先结束；那边的库也还没有 AR1 的基线（AR1 现在是出厂配置，自动记录是安全的）。
- 仍未实测：基本 ACL `rule … permit source …`（Easy IP 题改用 nat-policy 后没有题用到）；`undo ip pool` 是否提示确认（hub 自动回答，没有单独观察）。
- 脚本小问题：设备 console 空闲登出后自动重登期间，第一条命令可能被吞掉首字母（第 8 题出现一次，不影响结果）。
