// 模拟环境模块（spec §5.2 / §16.3）：独立模块，不依赖 app.js。课程页（courses.js）用它探测环境、按题目准备环境、
// 开练 / 开考的 busy 重试；工作台与独立页面顶栏只读显示当前环境。环境切换只在课程页进行（工作台不再挂可操作的选择器）。
// 全局导出 window.LabSwitch：
//   LabSwitch.api(root, method, url, body)   通用请求：非 2xx 抛 Error（message = detail，status = HTTP 状态码）
//   LabSwitch.environments(root, force)   GET api/lab/environments（force = refresh=1 强制重新探测）
//   LabSwitch.sandboxes(root)             GET api/lab/sandboxes
//   LabSwitch.switchTo(root, key, sandbox)   POST api/lab/environment（sandbox 省略 = 沿用该环境上次的沙箱）
//   LabSwitch.prepare(root, scenarioId, onProgress, token, environment)   POST api/lab/prepare（environment：在哪个环境里做，
//       同一平台有几台 eNSP Pro 时课程页传正在浏览的环境；省略 = 当前环境平台相同就用它） 后每秒轮询 GET api/lab/prepare，
//       resolve 最终任务（job.ok / job.error / job.error_status），onProgress(job) 每次轮询回调；token.cancelled 置真时停止等待
//       （后台任务照常跑完）。job.stage / job.devices 的取值以后端为准，可能新增（如 probe、initializing），显示时用
//       stageText / statusText，不认识的原样显示。课程页准备浮层另用 job.steps（步骤条）、job.device_info（每台设备详情，
//       restarting 单独报）、job.progress（0~1）、job.estimate（预计还需的秒数区间）、job.env_label / sandbox_name / retries。
//       注意：job.ok 只表示设备在 eNSP Pro 接口里是「运行中」；刚冷启动的 YunShan 设备头一两分钟改配置会报
//       「Error: The system is busy in building configuration」，此时 POST api/practice/start 返回 409（detail 含 busy），
//       调用方用 retryBusy 每 20 秒重试（真机 e2e 同样处理）。
//   LabSwitch.retryBusy(call, {tries, interval, token, onWait})   call() 抛 409 且 detail 含 busy 时等 interval 秒再试，
//       最多 tries 次（缺省 5 次、20 秒）；onWait(剩余秒, 第几次重试, 错误) 每秒回调（倒计时）；token.cancelled 置真即放弃
//       （抛 Error，cancelled = true）。
//   LabSwitch.endpoint(root, key) / setEndpoint(root, key, value) / testEndpoint(root, key, value)   接入设置（spec §5.2）：
//       GET|POST api/lab/environments/{key}/endpoint、POST …/endpoint/test（value 省略 = 测当前地址；不保存）。
//       凭据只在 ainet 主机上，接口里只有 secrets{名: 有无}。
//   LabSwitch.watchdog(root, key) / setWatchdog(root, key, values)   启动看门狗参数（按环境，spec §5.2 / §10.5）：
//       GET|POST api/lab/environments/{key}/watchdog；values 只含要改的参数（start_timeout / start_retries / early_check /
//       probe_timeout），null = 回到缺省。环境列表与接入设置里每个环境另带 watchdog{values, params[]}（没有看门狗为 null）。
//   LabSwitch.addEnv(root, body) / probeNewEnv(root, type, url) / editEnv(root, key, body) / deleteEnv(root, key)
//       用户添加的环境实例（里程碑 6.19）：POST api/lab/environments {type, label, url, device_password?, start_timeout?,
//       max_devices? …}（返回 {key, environment, probe, warning}）、POST api/lab/environments/probe {type, url}（添加前测试
//       连接）、PATCH api/lab/environments/{key} {label?, device_password?, clear_password?}、DELETE 同路径。
//       环境列表另带 types[]（可添加的类型）、每个环境的 type / custom / device_password{name, configured, source}。
//   LabSwitch.reasonText(code)   离线原因分类的短文字（unreachable / tls / auth / missing_secret / other）
//   LabSwitch.sourceText(source)   接入地址来源的文字（db / force / env / default）
//   LabSwitch.stageText(stage) / statusText(status) / platformText(platform)   显示用文字
//   LabSwitch.platformTag(platform)   平台标签 HTML（<span class="plat-tag plat-yunshan">YunShan</span>），与题名并列显示
//   LabSwitch.readonly(el, opts) 只读显示「环境 X · 沙箱 Y」（独立页面顶栏用）；返回 { refresh() }。
// 不用 CDN、不引新库，URL 一律相对路径（支持 /ainet 子路径部署）。
(() => {
  'use strict';
  async function req(root, method, url, body) {
    const res = await fetch(`${root || ''}${url}`, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error((data && data.detail) || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }
  const environments = (root, force) => req(root, 'GET', `api/lab/environments${force ? '?refresh=1' : ''}`);
  const sandboxes = (root) => req(root, 'GET', 'api/lab/sandboxes');
  const switchTo = (root, key, sandbox) =>
    req(root, 'POST', 'api/lab/environment', sandbox === undefined ? { key } : { key, sandbox });

  const epUrl = (key) => `api/lab/environments/${encodeURIComponent(key)}/endpoint`;
  const endpoint = (root, key) => req(root, 'GET', epUrl(key));
  const setEndpoint = (root, key, value) => req(root, 'POST', epUrl(key), { value: value || '' });
  const testEndpoint = (root, key, value) => req(root, 'POST', `${epUrl(key)}/test`, value ? { value } : {});
  const wdUrl = (key) => `api/lab/environments/${encodeURIComponent(key)}/watchdog`;
  const watchdog = (root, key) => req(root, 'GET', wdUrl(key));
  const setWatchdog = (root, key, values) => req(root, 'POST', wdUrl(key), values);

  const envUrl = (key) => `api/lab/environments/${encodeURIComponent(key)}`;
  const addEnv = (root, body) => req(root, 'POST', 'api/lab/environments', body);
  const probeNewEnv = (root, type, url) => req(root, 'POST', 'api/lab/environments/probe', { type, url });
  const editEnv = (root, key, body) => req(root, 'PATCH', envUrl(key), body);
  const deleteEnv = (root, key) => req(root, 'DELETE', envUrl(key));

  const cancelledError =() => Object.assign(new Error('已取消'), { cancelled: true });

  async function prepare(root, scenarioId, onProgress, token, environment) {
    let job = await req(root, 'POST', 'api/lab/prepare',
      environment ? { scenario_id: scenarioId, environment } : { scenario_id: scenarioId });
    const id = job.id;
    while (!job.done) {
      if (onProgress) onProgress(job);
      await new Promise((r) => setTimeout(r, 1000));
      if (token && token.cancelled) throw cancelledError();
      const st = await req(root, 'GET', 'api/lab/prepare');
      // 服务重启（job 为空）或别处又开了任务（id 不同）：不再空等
      if (!st.job || st.job.id !== id) throw new Error('准备环境的任务已中断（服务重启或有新的任务），请重试');
      job = st.job;
    }
    if (onProgress) onProgress(job);
    return job;
  }

  function readonly(el, opts = {}) {
    const root = opts.root || '';
    el.classList.add('labswitch', 'ro');
    async function refresh() {
      try {
        const d = await environments(root);
        const env = d.environments.find((e) => e.current) || { label: d.current };
        el.textContent = `环境 ${env.label}` + (d.sandbox_name ? ` · 沙箱 ${d.sandbox_name}`
          : d.sandbox != null ? ` · 沙箱 ${d.sandbox}` : '');
        el.title = d.locked || '当前模拟环境（在课程页切换）';
      } catch (e) {
        el.textContent = '环境未知';
      }
    }
    refresh();
    return { refresh };
  }

  async function retryBusy(call, opts = {}) {
    const tries = opts.tries ?? 5, interval = opts.interval ?? 20, token = opts.token;
    for (let n = 0; ; n++) {
      if (token && token.cancelled) throw cancelledError();
      try {
        return await call();
      } catch (e) {
        if (!(e.status === 409 && /busy/i.test(e.message || '')) || n >= tries) throw e;
        for (let left = interval; left > 0; left--) {
          if (token && token.cancelled) throw cancelledError();
          if (opts.onWait) opts.onWait(left, n + 1, e);
          await new Promise((r) => setTimeout(r, 1000));
        }
      }
    }
  }

  const STAGES = { queued: '排队中', switch: '切换环境', sandbox: '查找沙箱', start: '启动设备', wait: '等待设备运行',
    restart: '重启卡住的设备', probe: '确认设备就绪', check: '核对设备', done: '完成', error: '出错' };
  const STATUSES = { online: '运行中', stopped: '已停止', starting: '启动中', stopping: '停止中', error: '异常',
    unknown: '状态未知', initializing: '系统初始化中', na: '无终端' };
  const PLATFORMS = { yunshan: 'YunShan', vrp5: 'VRP5' };
  const REASONS = { unreachable: '连不上', tls: 'TLS 握手失败', auth: '登录被拒 / 会话冲突', missing_secret: '缺少凭据',
    other: '' };
  const SOURCES = { db: '数据库（页面上设置）', force: '环境变量（AINET_LAB_FORCE=1）', env: '环境变量', default: '内置缺省值' };
  const reasonText = (c) => (c in REASONS ? REASONS[c] : String(c ?? ''));
  const sourceText = (s) => SOURCES[s] || String(s ?? '');
  const stageText = (s) => STAGES[s] || String(s ?? '');
  const statusText = (s) => STATUSES[s] || String(s ?? '');
  const platformText = (p) => PLATFORMS[p] || String(p ?? '');
  // 平台标签（HTML）：题名不带平台后缀，档案 / 成绩单 / 模板管理里与题名并列，区分两个平台的同名题
  const platformTag = (p) => {
    const key = p || 'vrp5';
    const cls = /^[a-z0-9_-]+$/i.test(key) ? ` plat-${key}` : '';
    const t = platformText(key).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    return `<span class="plat-tag${cls}" title="平台：${t}">${t}</span>`;
  };

  window.LabSwitch = { api: req, readonly, environments, sandboxes, switchTo, prepare, retryBusy,
    endpoint, setEndpoint, testEndpoint, watchdog, setWatchdog, addEnv, probeNewEnv, editEnv, deleteEnv, reasonText, sourceText,
    stageText, statusText, platformText, platformTag };
})();
