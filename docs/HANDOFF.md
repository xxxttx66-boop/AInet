# ainet 开发交接文档

> 更新：2026-10-11，基线 master `0088d51`（里程碑 6.20）。需求全文见 [`spec.md`](../spec.md)，运行与测试细节见 [`README.md`](../README.md)。
> 本文只做导航和状态交接，细节以 spec / README / 代码为准。

## 1. 项目一句话

局域网运维实操练习 / 考试系统：FastAPI + SQLite + 静态前端，接 eNSP（经典版，VRP5）或 eNSP Pro（YunShan OS）的真实设备 console，注入故障 → 学员排障 → 自动判定 → 整机复位 → AI 评分与提示。

## 2. 部署与开发机现状

| 机器 | 角色 | 目录 / 地址 | 运行方式 | 接的环境 |
|---|---|---|---|---|
| fex1（Linux，`xixi-FAEX1`） | 主开发机，git 仓库所在 | `/home/xixi/Git/ainet` | 开发时手动起 uvicorn；用户服务 `ainet-enspro-forward.service` 把本机 KVM eNSP Pro 转发出去 | 本机 KVM 虚机 `ensppro`（192.168.56.101:8443，仅本机可达） |
| Windows 72（192.168.31.72） | 部署 + eNSP 经典版执行机 | `C:\Users\xixi\ainet` | 计划任务 `ainet`，`run_ainet.bat`（`ENSP_HOST=127.0.0.1`）；重启 = `schtasks /end` 后 `/run` | 库里 `lab:env=enspro`，地址 https://192.168.31.69:18443（经 fex1 转发） |
| 笔记本（`ssh -p 40022 xTTx@47.105.42.104`） | 单人部署，只在局域网用，**不对外暴露** | `D:\AINET` | 计划任务 `ainet`（SYSTEM，开机启动），`run_ainet.bat`；停止用 `stop_ainet.ps1`（`schtasks /end` 杀不掉 python） | 本机 eNSP Pro https://172.16.133.138:8443；另加了实例 `enspro-2` = https://47.105.42.104:33000 |

- 72 和笔记本都不是 git 仓库，部署方式是按 master 拷文件，部署后用文件哈希（`main.py` / `lab.py`）对照 master 确认版本。
- 笔记本：`AINET_AUTO_LOGIN=admin`、`AINET_ENSPRO_MAX_DEVICES=3`（该机 eNSP Pro 只能同时稳定跑 3 台 AR/LSW，冷启动 190~330 秒，启动超时 300）。上限 3 时隐藏 NAT 2 道、OSPF 3 道。
- 33000 端口是 fex1 KVM eNSP Pro 的转发，72 和笔记本的 `enspro-2` 同时用它会互相挤会话（eNSP Pro 同账号单会话）。
- pip 在笔记本上直连会 SSL EOF，用清华源 `https://pypi.tuna.tsinghua.edu.cn/simple`。

## 3. 代码导航

后端 `backend/`：

- `main.py`：全部路由；自动登录中间件；环境 / 接入 / 看门狗 / AI 设置接口。
- `lab.py`：环境注册表 `REGISTRY` / `LabEnv`、`ENV_TYPES`、地址规范化与 https 探测（`normalize_endpoint` / `detect_scheme`）、看门狗参数、用户添加的实例（`lab:envs`，`load_custom_envs`）、密钥读取（`read_secret` / `device_password`）。
- `labenv.py`：`LabManager` 运行时上下文——切换环境、接入地址读写与测试、添加 / 编辑 / 删除实例、按题目准备环境（进度、步骤、设备卡片、超上限停多余设备）。
- `labpower.py`：eNSP Pro 启动看门狗 `PowerWatch`（超时 / 复位循环 → 自动重启 → start_failed）、就绪探测、空闲停机。
- `enspro.py` + `connectors/enspro.py`：eNSP Pro REST 客户端与 STOMP console；`connectors/ensp.py`：经典版 telnet console。
- `console_hub.py`：每个 console 一条共享连接 + 隐藏事务。
- `practice.py` / `exam.py` / `config_reset.py`：练习、考试、配置快照与复位。
- `scenarios.py` / `params.py`：题库加载校验、参数化、`device_load`（设备上限计数）、`draft`。
- `ai.py` / `grading.py` / `hints.py` / `agent.py` / `prompts.py`：DeepSeek 客户端、评分、分级提示、Agent 面板。

前端 `frontend/`：`courses.html` + `courses.js`（课程页：环境列「+」、⚙ 接入设置气泡、准备浮层）、`index.html` + `app.js`（操作页）、`labswitch.js`、`aiset.js`（AI 设置对话框）、`exam*.html` / `exam.js`、`agent.js`。

题库 `scenarios/*.json`（`chapters.json` 为章节）；YunShan 题 23 道，全部真机验证过（`docs/question-bank-verify.md`）。

## 4. 关键约定

- **设置优先级**：库（`app_settings`）> 环境变量 > 内置缺省；`AINET_LAB_FORCE=1` 时环境变量优先并写回库。
- **库键**：`lab:env`、`lab:envs`（用户添加的实例，key `enspro-N`，编号不复用）、`lab:endpoint:<key>`、`lab:watch:<key>`（start_timeout / start_retries / early_check / probe_timeout / max_devices）、`lab:sandbox:<key>`、`baseline:<key>:<sb>:<dev>`、`ai:base_url`、`ai:model`。
- **密钥**只在 secrets 文件（`backend/data/secrets.env`，或 `AINET_SECRETS_FILE`）：`ENSPRO_DEVICE_PASSWORD`（内置 enspro）、`ENSPRO_DEVICE_PASSWORD__<KEY>`（各实例）、`DEEPSEEK_API_KEY`。接口只回显有无，不回显值；不得写进代码、日志、提交、文档、聊天记录。
- eNSP Pro：同账号单会话（50010005 = 会话被挤 / 过期）；新设备首次登录 console 要设密码，由 ainet 用 `ENSPRO_DEVICE_PASSWORD` 自动完成；设备类型 id 各安装一致（ENSP-AR 25、ENSP-LSW 111、PC-CLIENT 26）；设备会自动保存，靠「干净基线」兜底。
- 环境原则上是独立网络节点（局域网或其他网络），地址、凭据都要可配置，不要写死 192.168.56.x。
- 草稿题 `"draft": true`，缺省隐藏（`AINET_SHOW_DRAFTS=1` 显示）。

## 5. 测试

- 单元 / 集成：`cd backend && .venv/bin/python -m pytest tests -q`（Windows：`.venv\Scripts\python -m pytest tests -q`）。全部用假服务，不连真机、不发外网请求。
- 假环境 e2e（无头浏览器）：`tests/e2e/*_fake_e2e.py`、`course_e2e.py`、`prepare_overlay_e2e.py`。
- 真机 e2e：`tests/e2e/enspro_campus_e2e.py practice <题 id>` 等，会启停真实设备、占用 eNSP Pro 会话，跑之前先确认没有进行中的练习 / 考试。
- 只认输出里的 `N passed / N failed`。

## 6. 未完成 / 已知问题

缺陷：

1. `/api/lab/relogin` 返回 ok，但会话被挤（50010005）后实际没恢复。
2. console 自动重登后学员输入的第一个字符被吞。
3. 设备处于 start_failed 时，准备环境立即 504，没有先尝试恢复。
4. 看门狗 / 空闲停机只跟踪当前环境；停机间隔按沙箱 id 记，多实例间会串。
5. 多题考试只在开考时检查设备上限。
6. eNSP 经典版没有提示符探测，设备还在启动（输出 `####`）时开练会失败。

未做的功能：

- 远程 eNSP 节点代理（经典版跑在另一台机器时的运行时探测）。
- L10 STP、L11 防火墙需要新沙箱（`ainet_l2`、`ainet_fw`）和题目；L03 / L04 / L06 / L07 / L08 / L12 缺第 3 道题。
- 里程碑 7 以后：AI 生成场景 + 校验器、掌握度画像 / 错题本、根因指纹与回归检查、EVE-NG / containerlab。

未验证：

- 自动登录、AI 设置对话框、「+」添加环境三处新界面只做了 `node --check`，没有浏览器实测。
- 笔记本上的 DNS 题未真机验证。

仓库：

- 没有远端，从未 push；git 历史里有早期提交带过明文密码，清理历史（reflog / gc）未获授权，push 前必须先处理。

## 7. 操作规矩（必须遵守）

- 高危操作（git push、rm -rf、删除 worktree / 分支）先逐字列命令，等用户明确回复「确认 xxx」。
- 动设备、部署、重启服务前先查有没有进行中的练习 / 考试。
- eNSP Pro 虚机只做只读诊断，不改系统和服务配置；登录会挤掉别人的会话，事先告知用户。
- 设备 console 密码只试一次，不猜。
- 笔记本只在局域网用，不做端口暴露。
- 多文件改动在隔离 worktree 里做，用户确认后再合并到 master。
