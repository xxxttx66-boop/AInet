# AInet 开发交接

> 更新：2026-10-14。GitHub 公开导入基线 master `6c2006d`；本轮改动在 `codex/project-audit` worktree。
> 需求见 [spec.md](../spec.md)，启动与接口说明见 [README.md](../README.md)。

## 1. 项目与启动

AInet 是单人使用的网络运维实操练习 / 考试系统：FastAPI + SQLite + 静态前端，连接 eNSP 或 eNSP Pro，完成故障注入、排障、判定、复位和评分。

- Windows 前台：`启动服务.bat` / `start.bat`；后台：`run_ainet.bat` / `start_silent.bat`。
- 命令行：在 backend 下运行 `python -m launcher`。默认仅 `127.0.0.1:8000`，不默认开启免登录。
- 本机部署配置：复制 `backend/local.env.example` 为 `backend/data/local.env`。地址、启动超时、设备上限、绑定网卡均由部署者填写，进程环境变量优先。
- 设备密码与 AI Key 仍存 `backend/data/secrets.env` 或 `AINET_SECRETS_FILE`。公开源码、日志和报告只显示配置是否存在。
- 局域网免登录需要同时显式设置绑定地址、`AINET_AUTO_LOGIN` 和 `AINET_ALLOW_LAN_AUTO_LOGIN=1`；能访问端口的人都能操作设备。
- 本项目当前 GitHub 仓库为公开仓库。真实机器地址、SSH 登录标识与私有部署记录应保存在 Git 忽略的本机文件中。
- 停止脚本按当前 checkout 的虚拟环境路径和启动命令筛选进程，不按端口任意终止其他服务。

## 2. 代码导航

- `backend/main.py`：页面与 API、登录、lifespan。
- `backend/lab.py`：环境注册、地址、凭据与看门狗参数。
- `backend/labenv.py`：环境切换、准备设备、运行上下文协调；`labcontext.py`：每个环境 / 沙箱的独立后台上下文。
- `backend/labpower.py`：启动看门狗、排队、就绪探测、空闲停机。
- `backend/enspro.py`、`backend/connectors/`：REST 会话与设备 console。
- `backend/console_hub.py`：共享 console、隐藏事务和启动提示符检查。
- `backend/practice.py`、`exam.py`、`config_reset.py`：练习、考试、快照与还原。
- `backend/scenarios.py`、`params.py`：题库校验与参数化。
- `backend/ai.py`、`agent.py`、`grading.py`、`hints.py`：AI、评分与提示。
- `frontend/`：课程页、操作页、成绩单及设置。
- `frontend/ui.js` / `ui.css`：共享导航、明暗主题、本地 SVG 图标和新版布局。当前界面约定见 `docs/design/ui-upgrade.md`；`ui-layout.html` 为历史参考。
- `scenarios/`：28 道题与章节目录；历史真机验收记录见 `docs/question-bank-verify.md`。

## 3. 状态与恢复约定

- 设置优先级：数据库 > 环境变量 > 缺省；`AINET_LAB_FORCE=1` 时显式环境变量覆盖数据库。
- 干净基线按环境、沙箱名、设备保存。每题开始前与基线比对，结束后恢复开题快照。
- 每个环境 / 沙箱拥有独立看门狗、空闲计时器和后台 console hub。切换页面选中的环境后，原环境的后台任务继续监测。
- 同一环境的多个沙箱共享 REST 登录会话，避免互相挤掉会话。服务关闭时按共享客户端统一登出。
- 停机时刻按环境隔离，同一环境内按沙箱和设备保存。看门狗存 `lab:power:<环境>:<沙箱>`；启动时恢复所有有效记录，并兼容导入旧 `lab:power`。
- 删除环境或修改接入地址时，取消该环境全部后台任务并释放其会话；不再探测旧地址。
- 重新登录先清除旧 SSO cookie，再验证模拟器服务；旧终端连接重建，练习的 pin / freeze 保留。console 自身重登期间的按键暂存，登录成功后顺序转发，不发送到密码提示。
- 设备上限在每次开始一道题时检查，包含考试下一题与重试。eNSP 经典版开题前先核对可用提示符；尚在输出启动进度时返回可重试的 busy。
- `start_failed` 已有首次准备时停止再启动的恢复路径，本轮增加普通失败及慢设备已自行上线两种准备流程回归。

## 4. 验证

安装 `backend/requirements.txt`（直接依赖固定为本轮本机验证版本），在项目根目录运行：

```bash
python scripts/check_project.py
node scripts/check_frontend.mjs
cd backend
python -m pytest tests --ignore=tests/e2e --ignore=tests/manual -q
```

GitHub Actions 在 Linux / Windows、Python 3.11 / 3.13 上运行相同检查，工作流只有仓库读取权限。默认测试用假设备、假 HTTP 服务和临时库；不会登录真实模拟器或调用外部 AI。
新增回归集中在 `test_runtime_contexts.py`、`test_launcher.py` 及会话、考试、console 和准备流程用例中。
Windows 无符号链接权限时只跳过符号链接用例，项目目录排除扫描仍独立验证；TLS 用例使用现场生成的临时 OpenSSL 配置与证书。

真机 e2e 会占用设备和 eNSP Pro 会话，需单独安排。当前改动未部署、未进行真机验收；自动登录、AI 设置、“+”添加环境和本轮终端重连 UI 仍需浏览器及真机验收。

## 5. 待办

- 远程 eNSP 经典版运行时代理。
- STP、防火墙沙箱及缺少的章节题目。
- AI 生成场景与校验、掌握度画像 / 错题本、其他实验后端。
- 共享网络或多人部署前，应另行设计角色、密码散列及访问控制；当前仍按单人本机实验设计。
- 大模块拆分应围绕明确职责逐步进行，并保留现有回归覆盖。

## 6. 操作规矩

- 高危操作（git push、删除 worktree / 分支、不可逆的历史改写）先逐字列命令，等用户明确回复「确认 xxx」。
- 设备操作、部署或重启服务前先检查进行中的练习 / 考试。
- 登录 eNSP Pro 会挤掉其他同账号会话，操作前说明；设备 console 密码只尝试一次，不猜测。
- 多文件修改在独立 worktree 中完成，合并 master 前由用户确认。
- 本轮移出的个人交接记录、临时 `_p2` 工具和截图保留在本 worktree 的 Git 忽略目录 `_ai/archive-2026-10-14`，主 checkout 的 `_ai/project-audit-2026-10-14` 另有经哈希核对的独立副本；这些私有备份不要上传。
