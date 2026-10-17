// ainet 课程选择页（spec §16.3）：模拟环境 → 章节 → 课题 → 预览。登录后默认进入，选好课题再进操作页（工作台）。
// - 环境列：打开页面时 GET api/lab/environments 检测全部环境；在线可点，离线置灰写「（离线）」；「↻ 重新检测环境」带 refresh=1。
// - 点环境只是浏览：GET api/scenarios?env=<key> 取该环境平台的题（含 chapters 与每题 progress），不触发切换。
// - 章节从易到难，每章显示题数，没有题的灰色「待补充」；最下面「考试」列出该环境平台可用的多题考试模板。
// - 预览：标题、章节与难度、简介（工单）、知识点、拓扑预览（本题设备橙框）、将要准备的环境；
//   「自由实验 / 开始练习 / 单题考试」：eNSP Pro 走 LabSwitch.prepare，进度显示在居中的准备浮层（遮罩 + 卡片：步骤条、
//   整体进度条与预计还需、设备卡片、自动重启警示、轮播提示、本题工单；出错可重试 / 返回课程，就绪后停留约 0.8 秒再进操作页）；
//   eNSP 经典版不能远程打开拓扑，所需拓扑没在运行时浮层里提示在 eNSP 中打开，轮询到就绪才放行；
//   开练 / 开考返回 409 且含 busy（设备刚冷启动）时每 20 秒自动重试，最多 5 次，显示倒计时，可取消。
// - 离线时环境下方写原因（reason_code 的短文字 + reason 全文，悬停也能看）；当前环境端口能连上但登录被拒 / 会话被挤掉时
//   仍在线、带 ⚠。每个环境右上角「⚙ 接入设置」（考试中不显示）：当前地址与来源、凭据有无、改地址、测试连接、保存、恢复缺省
//   （LabSwitch.endpoint / testEndpoint / setEndpoint，spec §5.2「接入设置」）。
// - 环境列标题右侧「+」（考试中不显示，里程碑 6.19）：添加环境对话框（类型、显示名、地址、设备密码、启动超时、设备上限、
//   测试连接、添加）；eNSP Pro 类的 ⚙ 气泡里可填设备 console 密码（只写不读），用户添加的环境可改显示名、「删除此环境」。
//   准备环境时把正在浏览的环境传给后端（同一平台有几台 eNSP Pro 时在哪台上做）。
// - 自动识别 https（里程碑 6.20）：添加对话框与 ⚙ 气泡的地址框失焦（值不是 https:// 开头时）或点「测试连接」后，后端返回
//   normalized_from 就把输入框换成规范化后的 URL，下面一行小字说明（如「已自动改为 https」）；⚙ 打开时当前地址是 http
//   且该端口其实是 https（测试连接返回 suggest）就显示「建议改为 https」与一键改。已存的 http 地址不自动迁移。
// - 环境 / 章节 / 课题的选择存 localStorage（ainet.course.*，读写 try/catch）。
// 只用原生 JS，URL 一律相对路径（支持 /ainet 子路径）。
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const L = window.LabSwitch;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const EXAM_CH = '__exam__';
  const incoming = new URLSearchParams(location.search);
  let requestedTopic = incoming.get('s');
  const requestedPlatform = incoming.get('platform');
  const pageView = incoming.get('view') || (requestedTopic ? 'catalog' : 'home');
  const envSettingsPage = pageView === 'environments';
  const examPage = pageView === 'exams';
  const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const stars = (n) => '★'.repeat(Math.max(0, Math.min(5, n || 0))) || '—';
  const store = {
    get(k) { try { return localStorage.getItem(`ainet.course.${k}`); } catch (e) { return null; } },
    set(k, v) {
      try { if (v == null) localStorage.removeItem(`ainet.course.${k}`); else localStorage.setItem(`ainet.course.${k}`, String(v)); }
      catch (e) { /* 存储不可用：只在本页有效 */ }
    },
  };
  async function api(method, url, body) {
    try {
      return await L.api('', method, url, body);
    } catch (e) {
      if (e.status === 401) { location.href = 'login'; }
      throw e;
    }
  }
  const topoKey = (n) => String(n || '').split(/[\\/]/).pop().replace(/\.topo$/i, '').trim().toLowerCase();

  const st = {
    envs: null,          // GET api/lab/environments
    env: null,           // 正在浏览的环境 key
    data: null,          // GET api/scenarios?env=
    studentName: '',
    hasSelection: !!store.get('topic') || !!requestedTopic,
    chapter: null,
    topic: null,         // 场景 id，或 tpl:<模板 id>
    templates: null,
    cur: null,           // GET api/topology/current（当前环境的拓扑，用于预览）
    busy: false,
    token: null,
    epOpen: envSettingsPage ? incoming.get('env') : null, // 设置页可通过 ?env= 直接打开对应环境
    epMsg: null,         // 气泡里保存后的结果（重画后仍显示）
    epSuggest: {},       // 已存 http 地址的「建议改为 https」检测结果 {'<key>|<地址>': https 地址 | '' | 'pending'}
    examActive: false,   // 有考试进行中：不显示接入设置
    doneHold: 800,       // 准备浮层「环境已就绪」停留的毫秒数（e2e 截图时调大）
  };

  // ---------- 学员菜单 ----------
  function setMenu(open) {
    $('user-menu').hidden = !open;
    $('user-btn').setAttribute('aria-expanded', String(open));
  }
  $('user-btn').onclick = (e) => { e.stopPropagation(); setMenu($('user-menu').hidden); };
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('.user-wrap')) setMenu(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setMenu(false); });
  $('logout').onclick = async () => {
    try { await fetch('api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    location.href = 'login';
  };
  // AI 设置（里程碑 6.18，aiset.js）：考试中不显示（见 checkActive）
  if (window.AiSettings) {
    window.AiSettings.bind($('um-ai'), { root: '' });
    $('um-ai').addEventListener('click', () => setMenu(false));
  } else $('um-ai').hidden = true;

  // ---------- 环境列 ----------
  async function loadEnvs(force) {
    $('c-recheck').disabled = true;
    try {
      st.envs = await api('GET', `api/lab/environments${force ? '?refresh=1' : ''}`);
    } catch (e) {
      $('c-envs').innerHTML = `<div class="c-err c-pad">检测模拟环境失败：${esc(e.message)}</div>`;
      return;
    } finally { $('c-recheck').disabled = false; }
    const envs = st.envs.environments;
    const ok = (e) => !!e.available;
    let pick = st.env || store.get('env');
    if (!st.env && requestedPlatform) pick = (envs.find((e) => ok(e) && e.platform === requestedPlatform) || {}).key || pick;
    if (!envs.some((e) => e.key === pick && ok(e))) {
      pick = (envs.find((e) => e.current && ok(e)) || envs.find(ok) || {}).key || null;
    }
    renderEnvs();
    if (!envSettingsPage && (pick !== st.env || force)) await selectEnv(pick, true);
  }
  function renderEnvs() {
    const box = $('c-envs');
    box.innerHTML = '';
    $('c-env-add').hidden = !envSettingsPage || st.examActive || !(st.envs.types || []).length;
    $('c-env-manage').hidden = envSettingsPage;
    const displayOrder = (e) => e.key === 'ensp' ? 2 : e.key === 'enspro' ? 1 : 0;
    for (const e of [...st.envs.environments].sort((a, b) => displayOrder(a) - displayOrder(b))) {
      // 环境按钮与「接入设置」齿轮是兄弟元素（按钮里不能套按钮；离线时环境按钮禁用，齿轮仍可点——离线时最需要改地址）
      const wrap = document.createElement('div');
      wrap.className = 'c-env-wrap' + (st.epOpen === e.key ? ' open' : '');
      const b = document.createElement('button');
      b.className = 'c-env' + ((envSettingsPage ? e.current : e.key === st.env) ? ' on' : '') + (e.available ? '' : ' off');
      b.dataset.key = e.key;
      b.disabled = !e.available;
      const short = L.reasonText(e.reason_code);
      const why = e.available ? (e.warning ? e.reason || '' : '') : (e.reason || '离线');
      b.title = why || `浏览 ${e.label} 的课题（不会切换环境）`;
      const sub = e.available
        ? `（在线 · ${esc(L.platformText(e.platform))}${e.current ? ' · 当前' : ''}${e.sandboxes ? ' · 有沙箱' : ''}` +
          `${e.warning && short ? ` · ⚠ ${esc(short)}` : ''}）`
        : `（离线${short ? ` · ${esc(short)}` : ''}）`;
      b.innerHTML = `<span class="c-env-name"><span class="env-dot ${e.available ? (e.warning ? 'st-error' : 'st-running') : 'st-none'}"></span>${esc(e.label)}</span>` +
        `<span class="c-env-sub">${sub}</span>` + (why ? `<span class="c-env-why">${esc(why)}</span>` : '');
      b.onclick = () => {
        if (st.busy) return;
        if (!envSettingsPage) { selectEnv(e.key); return; }
        if (!e.endpoint) return;
        st.epOpen = st.epOpen === e.key ? null : e.key;
        st.epMsg = null;
        renderEnvs();
        const input = document.getElementById('c-ep-input');
        if (input) input.focus();
      };
      wrap.appendChild(b);
      if (envSettingsPage && e.endpoint && !st.examActive) {
        const g = document.createElement('button');
        g.type = 'button';
        g.className = 'c-env-cfg';
        g.dataset.icon = 'settings';
        g.title = '接入设置（地址、凭据状态、测试连接）';
        g.setAttribute('aria-label', `${e.label} 接入设置`);
        g.setAttribute('aria-expanded', String(st.epOpen === e.key));
        g.onclick = (ev) => {
          ev.stopPropagation();
          st.epOpen = st.epOpen === e.key ? null : e.key;
          st.epMsg = null;
          renderEnvs();
          if (st.epOpen) { const i = document.getElementById('c-ep-input'); if (i) i.focus(); }
        };
        wrap.appendChild(g);
        if (st.epOpen === e.key) wrap.appendChild(endpointPanel(e));
      }
      box.appendChild(wrap);
    }
    renderHome();
  }

  function renderHome() {
    const greeting = $('home-greeting');
    if (!greeting) return;
    greeting.textContent = st.studentName ? `欢迎回来，${st.studentName}` : '欢迎回来';
    $('home-subtitle').textContent = st.hasSelection
      ? '从你上次练习的位置继续，或换一个模拟环境挑战其他课题。'
      : st.env ? `已为你选择 ${envInfo().label}，查看课程进度与推荐课题，再开始排障练习。`
        : '先选择模拟环境，查看课程进度与推荐课题，再开始排障练习。';
    const envs = (st.envs && st.envs.environments) || [];
    $('home-env-count').textContent = String(envs.filter((e) => e.available).length);
    const scenarios = (st.data && st.data.scenarios) || [];
    const passed = scenarios.filter((s) => s.progress === 'passed').length;
    $('home-passed').textContent = st.data ? String(passed) : '—';
    $('home-remaining').textContent = st.data ? String(Math.max(0, scenarios.length - passed)) : '—';
    const env = envInfo();
    $('home-progress-caption').textContent = st.data
      ? `${env.label} · 共 ${scenarios.length} 道课题，已通过 ${passed} 道`
      : envs.some((e) => e.available) ? '正在读取课程进度…' : '模拟环境离线，启动后可查看课程进度';

    const storedId = st.hasSelection && st.topic && !st.topic.startsWith('tpl:') ? st.topic
      : st.hasSelection ? store.get('topic') : null;
    const last = scenarios.find((s) => s.id === storedId);
    const next = last && last.progress !== 'passed' ? last
      : scenarios.find((s) => s.progress !== 'passed') || last || null;
    const heading = $('home-next-heading');
    const label = $('home-next-label');
    const title = $('home-next-title');
    const desc = $('home-next-desc');
    const link = $('home-next-link');
    const continueLink = $('home-continue');
    if (!st.data || !env.key) {
      heading.textContent = '接下来做什么';
      label.textContent = '开始训练';
      title.textContent = '先选择一个在线模拟环境';
      desc.textContent = '环境就绪后，即可浏览课程并开始排障练习。';
      $('home-next-caption').textContent = '选择在线环境后，我们会按学习进度推荐课题';
      link.textContent = '查看课程';
      link.href = 'courses?view=catalog';
      continueLink.textContent = '浏览课程';
      continueLink.href = 'courses?view=catalog';
      return;
    }
    if (!next) {
      heading.textContent = '本环境课程';
      label.textContent = '课程已完成';
      title.textContent = '当前环境的课题都已通过';
      desc.textContent = '你可以回顾课程，也可以重复练习已通过的课题。';
      $('home-next-caption').textContent = `${env.label} · 当前环境的课题都已通过`;
      link.textContent = '查看课程';
      link.href = 'courses?view=catalog';
      continueLink.textContent = '回顾课程';
      continueLink.href = 'courses?view=catalog';
      return;
    }
    const repeat = next.progress === 'passed';
    const lastSelected = last === next && st.hasSelection;
    const target = `courses?view=catalog&s=${encodeURIComponent(next.id)}`;
    heading.textContent = lastSelected ? '继续上次训练' : '推荐下一道课题';
    label.textContent = lastSelected ? (repeat ? '已通过 · 可重练' : '上次练习') : (repeat ? '练习推荐' : '下一道课题');
    title.textContent = next.title;
    desc.textContent = `${next.summary || next.symptom || ''}${next.est_minutes ? ` · 约 ${next.est_minutes} 分钟` : ''}`;
    $('home-next-caption').textContent = `${env.label} · ${lastSelected ? '回到上次课题' : '按章节顺序推荐下一道未通过课题'}`;
    link.textContent = repeat ? '再次练习' : '查看课题';
    link.href = target;
    continueLink.textContent = lastSelected ? (repeat ? '再次练习' : '继续上次训练') : '开始推荐课题';
    continueLink.href = target;
  }

  // ---------- 接入设置气泡（spec §5.2「接入设置」）：地址与来源、凭据有无、测试连接、保存 ----------
  // 能由 ainet 启停设备的环境（eNSP Pro）另有启动看门狗参数（按环境配置，里程碑 6.16）：页面上只放「启动超时」
  // 「卡住自动重启次数」，早期卡死检查 / 就绪探测上限只经接口改；留空 = 回到缺省值（环境变量 / 内置）。
  // 同一参数集里另有「同时运行设备上限」（max_devices，里程碑 6.17），单列一节。
  const WD_UI = ['start_timeout', 'start_retries'];
  const CAP_UI = ['max_devices'];
  const WD_LABEL = { max_devices: '同时运行设备上限（不含 PC，0 不限）' };
  function wdField(x) {
    const src = L.sourceText(x.source) + (x.source === 'env' || x.source === 'force' ? `（${x.env_var}）` : '');
    return `<label class="c-ep-field"><span>${esc(WD_LABEL[x.name] || x.label)}${x.unit === '秒' ? '（秒）' : ''}</span>` +
      `<input id="c-wd-${esc(x.name)}" data-wd="${esc(x.name)}" type="number" inputmode="numeric" step="1"` +
      ` min="${x.min}" max="${x.max}" value="${esc(String(x.value))}" placeholder="${esc(String(x.default))}"></label>` +
      `<div class="c-ep-kv">来源：${esc(src)} · 缺省 ${esc(String(x.default))} · 范围 ${x.min}~${x.max}</div>`;
  }
  function watchdogFields(e) {
    const wd = e.watchdog;
    if (!wd) return '';
    const cap = wd.params.filter((x) => CAP_UI.includes(x.name));
    return '<div class="c-ep-sub">启动看门狗</div>' + wd.params.filter((x) => WD_UI.includes(x.name)).map(wdField).join('') +
      '<div class="c-ep-note">设备启动超过「启动超时」仍未运行按卡住处理，自动停止再启动；冷启动慢的主机请调大。' +
      '留空 = 缺省值；保存后立即生效，不断开终端</div>' +
      (cap.length ? '<div class="c-ep-sub">设备容量</div>' + cap.map(wdField).join('') +
        '<div class="c-ep-note">本机最多能同时稳定运行几台路由器 / 交换机（PC 不计）。需要更多设备的题不列出、不能开始；' +
        '准备环境时先停掉本沙箱里非本题的设备。0 = 不限</div>' : '');
  }
  // 自动识别 https（里程碑 6.20）：后端返回 normalized_from 时把输入框换成规范化后的 URL，并在 hint 行说明；
  // 明文 HTTP 的警告也写在 hint 行。返回是否改了输入框。
  function applyScheme(input, hint, r) {
    if (!r) return false;
    const changed = !!(r.normalized_from && r.value && input.value.trim() !== r.value);
    if (r.normalized_from && r.value) input.value = r.value;
    const text = [r.normalized_from ? `${r.notice || '地址已规范化'}：${r.normalized_from} → ${r.value}` : '', r.warning || '']
      .filter(Boolean).join('；');
    hint.textContent = text;
    hint.className = `c-ep-note${r.warning ? ' warn' : ''}`;
    hint.hidden = !text;
    return changed;
  }
  // 地址框失焦时自动识别（值不是 https:// 开头、和上次检查的不同）；点按钮 / 关闭引起的失焦由 skip() 判断跳过
  function blurDetect(input, run, skip) {
    let last = input.value.trim();
    input.addEventListener('blur', () => setTimeout(() => {
      const v = input.value.trim();
      if (!input.isConnected || skip() || !v || v === last || /^https:\/\//i.test(v)) return;
      last = v;
      run();
    }, 200));
  }
  function endpointPanel(e) {
    const ep = e.endpoint;
    const locked = (st.envs && st.envs.locked) || '';
    const secrets = Object.entries(e.secrets || {});
    const p = document.createElement('div');
    p.className = 'c-ep-pop';
    p.setAttribute('role', 'group');
    p.setAttribute('aria-label', `${e.label} 接入设置`);
    const src = L.sourceText(ep.source) + (ep.source === 'env' || ep.source === 'force' ? `（${ep.env_var}）` : '');
    const dp = e.device_password;                    // eNSP Pro 类：按实例的设备 console 密码（只报有无，里程碑 6.19）
    p.innerHTML =
      `<div class="c-ep-head"><b>${esc(e.label)} 接入设置</b>${e.custom ? ` <span class="muted mono">${esc(e.key)}</span>` : ''}</div>` +
      (e.custom ? `<label class="c-ep-field"><span>显示名</span><input id="c-ep-label" type="text" maxlength="40" spellcheck="false"` +
        ` autocomplete="off" value="${esc(e.label)}"></label>` : '') +
      `<label class="c-ep-field"><span>${esc(ep.label)}</span>` +
      `<input id="c-ep-input" type="text" spellcheck="false" autocomplete="off" value="${esc(ep.value)}"` +
      ` placeholder="${esc(ep.default || (ep.kind === 'url' ? 'https://主机:端口' : '主机名或 IP'))}"></label>` +
      '<div id="c-ep-hint" class="c-ep-note" role="status" aria-live="polite" hidden></div>' +
      '<div id="c-ep-suggest" class="c-ep-note" hidden></div>' +
      `<div class="c-ep-kv">来源：${esc(src)}</div>` +
      (ep.source === 'db' && ep.default ? `<div class="c-ep-kv">缺省值：<span class="mono">${esc(ep.default)}</span></div>` : '') +
      (secrets.length ? `<div class="c-ep-kv">凭据：${secrets.map(([n, ok]) =>
        `<span class="c-ep-sec ${ok ? 'ok' : 'no'}">${esc(n)} ${ok ? '已配置' : '未配置'}</span>`).join(' ')}</div>` +
        (dp ? '' : '<div class="c-ep-note">凭据只在 ainet 主机的 secrets.env 或环境变量里配置，页面上不能填写、也不显示</div>') : '') +
      (dp ? '<label class="c-ep-field"><span>设备 console 密码</span><input id="c-ep-pw" type="password" autocomplete="new-password"' +
        ` spellcheck="false" value="" placeholder="${dp.configured ? '留空 = 不改' : '填写后保存'}"></label>` +
        `<div class="c-ep-note">只写不读：保存时写进 ainet 主机 secrets 文件的 <span class="mono">${esc(dp.name)}</span> 行，` +
        `页面不显示${e.custom ? '' : '；优先于 ENSPRO_DEVICE_PASSWORD'}${dp.source === 'env' ? '；当前由环境变量提供，环境变量优先' : ''}</div>` : '') +
      (e.runtime_local === false ? '<div class="c-ep-note warn">设备运行状态无法检测（需在 eNSP 主机上运行 ainet 或以后的节点代理）</div>' : '') +
      (ep.kind === 'host' ? '<div class="c-ep-note">测试连接只解析主机名、看本机 eNSP 进程，不连设备 console 端口</div>' : '') +
      (e.current ? '<div class="c-ep-note">这是当前环境：保存新地址会断开所有终端，并按新地址重连</div>' : '') +
      watchdogFields(e) +
      (locked ? `<div class="c-ep-note warn">${esc(locked)}</div>` : '') +
      `<div id="c-ep-msg" class="c-ep-msg${st.epMsg ? ` ${st.epMsg.cls}` : ''}" role="status" aria-live="polite">${esc(st.epMsg ? st.epMsg.text : '')}</div>` +
      '<div class="c-ep-btns">' +
      (e.custom ? `<button id="c-ep-del" type="button" class="danger"${locked || e.current ? ` disabled title="${esc(locked || '这是当前环境，先切换到其他环境再删除')}"` : ''}>删除此环境</button>` : '') +
      (dp && dp.configured && dp.source !== 'env' ? '<button id="c-ep-pwclear" type="button" title="从 secrets 文件删掉该环境的设备密码行">清除密码</button>' : '') +
      (ep.source === 'db' && !e.custom ? '<button id="c-ep-reset" type="button" title="删掉页面上设置的地址，回到环境变量 / 缺省值（不影响看门狗参数）">恢复缺省地址</button>' : '') +
      '<button id="c-ep-test" type="button">测试连接</button>' +
      `<button id="c-ep-save" type="button" class="primary"${locked ? ` disabled title="${esc(locked)}"` : ''}>保存</button>` +
      '</div>';
    const q = (sel) => p.querySelector(sel);
    const input = q('#c-ep-input');
    const pwEl0 = q('#c-ep-pw');
    const msg =(text, cls) => { const m = q('#c-ep-msg'); m.textContent = text; m.className = `c-ep-msg ${cls || ''}`; };
    const setBtns = (dis) => p.querySelectorAll('button').forEach((x) => {
      x.disabled = dis || (x.id === 'c-ep-save' && !!locked) || (x.id === 'c-ep-del' && (!!locked || e.current));
    });
    let testing = false;
    async function test() {
      testing = true;
      setBtns(true);
      msg('测试中…');
      try {
        const r = await L.testEndpoint('', e.key, input.value.trim());
        if (ep.kind === 'url') applyScheme(input, q('#c-ep-hint'), r);
        const extra = r.runtime_local === false ? '；设备运行状态无法检测（不是本机）' : '';
        if (r.available) msg(`能连上 ${r.value}${extra}`, 'ok');
        else msg(`${L.reasonText(r.reason_code) || '不可用'}：${r.reason}`, 'err');
        if (r.suggest) showSuggest(r.suggest);
      } catch (err) {
        msg(err.message, 'err');
      } finally { testing = false; setBtns(false); }
    }
    // 已存的 http 地址：该端口其实是 https 时「建议改为 https」+ 一键改（只改地址，与「保存」同一套接口）
    function showSuggest(url) {
      const box = q('#c-ep-suggest');
      if (!box || !url || locked) return;
      box.innerHTML = `建议改为 https：<span class="mono">${esc(url)}</span> <button type="button" id="c-ep-usehttps">一键改为 https</button>`;
      box.hidden = false;
      box.querySelector('#c-ep-usehttps').onclick = () => { input.value = url; save(url, true); };
    }
    if (ep.kind === 'url') {
      blurDetect(input, test, () => testing);
      const sk = `${e.key}|${ep.value}`;
      if (/^http:\/\//i.test(ep.value || '')) {
        if (st.epSuggest[sk] && st.epSuggest[sk] !== 'pending') showSuggest(st.epSuggest[sk]);
        else if (st.epSuggest[sk] === undefined) {   // 每个地址只查一次（重画气泡不重复探测）
          st.epSuggest[sk] = 'pending';
          L.testEndpoint('', e.key, '').then((r) => {
            st.epSuggest[sk] = (r && r.suggest) || '';
            if (p.isConnected && st.epSuggest[sk]) showSuggest(st.epSuggest[sk]);
          }).catch(() => { delete st.epSuggest[sk]; });
        }
      }
    }
    // 看门狗参数的改动 {参数: 数 | null}；输入不是数字时返回 null（已提示）
    function wdChanges() {
      const out = {};
      for (const x of (e.watchdog ? e.watchdog.params : [])) {
        const el = p.querySelector(`[data-wd="${x.name}"]`);
        if (!el) continue;
        const v = el.value.trim();
        if (v === '') { if (x.source === 'db') out[x.name] = null; continue; }
        const n = Number(v);
        if (!Number.isFinite(n)) { msg(`${x.label}要填数字`, 'err'); el.focus(); return null; }
        if (n !== Number(x.value)) out[x.name] = n;
      }
      return out;
    }
    async function save(value, addrOnly) {
      const wd = addrOnly ? {} : wdChanges();
      if (wd === null) return;
      const hasWd = Object.keys(wd).length > 0;
      // 显示名（用户添加的环境）与设备密码（只写不读）：PATCH api/lab/environments/{key}（里程碑 6.19）
      const edit = {};
      const lab = q('#c-ep-label');
      if (!addrOnly && lab && lab.value.trim() !== e.label) edit.label = lab.value.trim();
      const pwEl = q('#c-ep-pw');
      if (!addrOnly && pwEl && pwEl.value) edit.device_password = pwEl.value;
      if (e.custom && !value) { msg('地址不能为空（页面上添加的环境没有缺省地址）', 'err'); return; }
      setBtns(true);
      msg('保存中…');
      try {
        const parts = [];
        if (Object.keys(edit).length) {
          const r = await L.editEnv('', e.key, edit);
          if (pwEl) pwEl.value = '';
          parts.push(r.changed.length ? `${r.changed.map((x) => (x === 'label' ? '显示名' : '设备密码')).join('、')}已保存` : '显示名 / 密码没有变化');
        }
        if (hasWd) {                                 // 先存看门狗参数（不重建后端），再存地址（当前环境会重建）
          const w = await L.setWatchdog('', e.key, wd);
          parts.push(!w.changed ? '看门狗参数没有变化'
            : '看门狗参数已保存' + (w.applied ? '（已立即生效）' : '（切到该环境时生效）'));
        }
        if (addrOnly || (!hasWd && !Object.keys(edit).length) || value !== ep.value) {
          const r = await L.setEndpoint('', e.key, value);
          if (r.normalized_from) parts.push(`${r.notice || '地址已规范化'}（${r.normalized_from} → ${r.endpoint.value}）`);
          parts.push(!r.changed ? '地址没有变化'
            : `已保存：${r.endpoint.value}` + (r.rebuilt ? '（终端已断开，已按新地址重建）' : ''));
          if (r.warning) parts.push(r.warning);
        }
        st.epMsg = { cls: 'ok', text: parts.join('；') };
        await refreshCurrent();
        await loadEnvs(true);                        // 重新探测并重画（气泡保持打开，显示结果）
      } catch (err) {
        msg(err.message, 'err');
        setBtns(false);
      }
    }
    q('#c-ep-test').onclick = test;
    q('#c-ep-save').onclick = () => save(input.value.trim());
    if (q('#c-ep-reset')) q('#c-ep-reset').onclick = () => save('', true);
    if (q('#c-ep-pwclear')) q('#c-ep-pwclear').onclick = async () => {
      if (!confirm(`从 ainet 主机的 secrets 文件删除 ${e.label} 的设备 console 密码（${dp.name}）？`)) return;
      setBtns(true);
      try {
        await L.editEnv('', e.key, { clear_password: true });
        st.epMsg = { cls: 'ok', text: '已清除设备密码' };
        await loadEnvs(true);
      } catch (err) { msg(err.message, 'err'); setBtns(false); }
    };
    if (q('#c-ep-del')) q('#c-ep-del').onclick = async () => {
      if (!confirm(`删除环境「${e.label}」（${e.key}）？\n\n会删掉它的地址、看门狗参数、沙箱记录、干净基线，以及 secrets 文件里的设备密码行；` +
        '不影响 eNSP Pro 本身。')) return;
      setBtns(true);
      msg('删除中…');
      try {
        await L.deleteEnv('', e.key);
        st.epOpen = null;
        st.epMsg = null;
        if (st.env === e.key) st.env = null;
        await loadEnvs(true);
      } catch (err) { msg(err.message, 'err'); setBtns(false); }
    };
    if (pwEl0) pwEl0.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); if (!locked) save(input.value.trim()); }
      if (ev.key === 'Escape') { st.epOpen = null; renderEnvs(); }
    };
    p.querySelectorAll('[data-wd]').forEach((el) => {
      el.onkeydown = (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); if (!locked) save(input.value.trim()); }
        if (ev.key === 'Escape') { st.epOpen = null; renderEnvs(); }
      };
    });
    input.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); test(); }
      if (ev.key === 'Escape') { st.epOpen = null; renderEnvs(); }
    };
    return p;
  }
  // ---------- 添加环境（里程碑 6.19，环境列「+」）：类型、显示名、地址、设备密码（只写不读）、启动超时、设备上限 ----------
  // 模拟环境是独立节点：可能有多台 eNSP Pro，各有地址、密码、启动超时、设备上限。添加不切换当前环境；连不上也保存（给警告）。
  let addBox = null;
  let addFocus = null;
  function closeAdd() {
    if (!addBox) return;
    addBox.remove();
    addBox = null;
    document.removeEventListener('keydown', addKey, true);
    if (addFocus && addFocus.focus) addFocus.focus();
  }
  function addKey(ev) { if (ev.key === 'Escape') { ev.preventDefault(); closeAdd(); } }
  function openAdd() {
    if (addBox || st.examActive) return;
    const types = (st.envs && st.envs.types) || [];
    if (!types.length) return;
    addFocus = document.activeElement;
    addBox = document.createElement('div');
    addBox.className = 'ai-mask';
    addBox.addEventListener('mousedown', (ev) => { if (ev.target === addBox) closeAdd(); });
    const card = document.createElement('div');
    card.className = 'ai-card c-ep-pop';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-labelledby', 'ce-title');
    addBox.appendChild(card);
    document.body.appendChild(addBox);
    document.addEventListener('keydown', addKey, true);
    const num = (t, name, label) => {
      const x = (t.watch || []).find((w) => w.name === name);
      if (!x) return '';
      return `<label class="c-ep-field"><span>${esc(label)}</span><input id="ce-${esc(name)}" data-wd="${esc(name)}" type="number"` +
        ` inputmode="numeric" step="1" min="${x.min}" max="${x.max}" value="" placeholder="${esc(String(x.default))}"></label>`;
    };
    function render(typeKey) {
      const t = types.find((x) => x.key === typeKey) || types[0];
      const keep = (id) => { const el = card.querySelector(id); return el ? el.value : ''; };
      const label = keep('#ce-label');
      const url = keep('#ce-url');
      card.innerHTML =
        '<div class="ai-head"><b id="ce-title">添加模拟环境</b><button type="button" class="icon-btn sm" id="ce-x" aria-label="关闭">✕</button></div>' +
        '<div class="ai-note">模拟环境是独立节点（局域网或公网都可以）：每台各有地址、设备密码、启动超时与设备上限。添加后不会切换当前环境。</div>' +
        `<label class="c-ep-field"><span>类型</span><select id="ce-type">${types.map((x) =>
          `<option value="${esc(x.key)}"${x.key === t.key ? ' selected' : ''}>${esc(x.label)}</option>`).join('')}</select></label>` +
        `<label class="c-ep-field"><span>显示名</span><input id="ce-label" type="text" maxlength="40" spellcheck="false" autocomplete="off"` +
        ` value="${esc(label)}" placeholder="如 机房 B 的 ${esc(t.label)}"></label>` +
        `<label class="c-ep-field"><span>${esc(t.endpoint_label)}</span><input id="ce-url" type="text" spellcheck="false" autocomplete="off"` +
        ` value="${esc(url)}" placeholder="${esc(t.placeholder)}"></label>` +
        '<div id="ce-hint" class="c-ep-note" role="status" aria-live="polite" hidden></div>' +
        (t.device_password ? '<label class="c-ep-field"><span>设备 console 密码（可选）</span><input id="ce-pw" type="password"' +
          ' autocomplete="new-password" spellcheck="false" value="" placeholder="可稍后在 ⚙ 接入设置里填写"></label>' +
          '<div class="ai-note">只写不读：保存到 ainet 主机的 secrets 文件，页面不显示；没有密码时该环境显示为离线</div>' : '') +
        `<div class="c-ep-row">${num(t, 'start_timeout', '启动超时（秒）')}${num(t, 'max_devices', '设备上限（不含 PC，0 不限）')}</div>` +
        ((t.watch || []).length ? '<div class="ai-note">留空 = 缺省值（启动超时 150 秒、设备上限不限）；冷启动慢的主机请调大启动超时</div>' : '') +
        '<div id="ce-msg" class="c-ep-msg" role="status" aria-live="polite"></div>' +
        '<div class="c-ep-btns"><button type="button" id="ce-cancel">取消</button><button type="button" id="ce-test">测试连接</button>' +
        '<button type="button" id="ce-add" class="primary">添加</button></div>';
      const q = (sel) => card.querySelector(sel);
      const say = (text, cls) => { const m = q('#ce-msg'); m.textContent = text; m.className = `c-ep-msg ${cls || ''}`; };
      const busy = (on) => card.querySelectorAll('.c-ep-btns button').forEach((b) => { b.disabled = on; });
      q('#ce-x').onclick = closeAdd;
      q('#ce-cancel').onclick = closeAdd;
      q('#ce-type').onchange = () => render(q('#ce-type').value);
      let testing = false;
      const test = async () => {
        const u = q('#ce-url').value.trim();
        if (!u) { say('先填地址', 'err'); q('#ce-url').focus(); return; }
        testing = true;
        busy(true);
        say('测试中…（只连端口 / TLS 握手，不登录）');
        try {
          const r = await L.probeNewEnv('', t.key, u);
          applyScheme(q('#ce-url'), q('#ce-hint'), r);    // 自动识别 https：输入框换成规范化后的 URL
          if (r.available) say(`能连上 ${r.value}`, 'ok');
          else say(`${L.reasonText(r.reason_code) || '不可用'}：${r.reason}`, 'err');
        } catch (err) { say(err.message, 'err'); } finally { testing = false; busy(false); }
      };
      q('#ce-test').onclick = test;
      if (t.endpoint_kind === 'url') blurDetect(q('#ce-url'), test, () => testing || !addBox);
      q('#ce-add').onclick = async () => {
        const body = { type: t.key, label: q('#ce-label').value.trim(), url: q('#ce-url').value.trim() };
        if (!body.label) { say('显示名不能为空', 'err'); q('#ce-label').focus(); return; }
        if (!body.url) { say('地址不能为空', 'err'); q('#ce-url').focus(); return; }
        const pw = q('#ce-pw');
        if (pw && pw.value) body.device_password = pw.value;
        for (const el of card.querySelectorAll('[data-wd]')) {
          const v = el.value.trim();
          if (v === '') continue;
          const n = Number(v);
          if (!Number.isFinite(n)) { say('要填数字', 'err'); el.focus(); return; }
          body[el.dataset.wd] = n;
        }
        testing = true;                              // 点「添加」引起的地址框失焦不再单独探测
        busy(true);
        say('探测并保存中…');
        try {
          const r = await L.addEnv('', body);
          if (pw) pw.value = '';
          closeAdd();
          st.epMsg = null;
          await loadEnvs(true);                      // 环境列立即出现并做在线检测
          const auto = r.normalized_from ? `${r.notice || '地址已规范化'}：${r.normalized_from} → ${r.environment.endpoint.value}` : '';
          if (r.warning) alert(`已添加「${r.environment.label}」（${r.key}）。\n\n${[auto, r.warning].filter(Boolean).join('\n')}`);
        } catch (err) {
          say(err.message, 'err');
          testing = false;
          busy(false);
        }
      };
      for (const id of ['#ce-label', '#ce-url', '#ce-pw', '#ce-start_timeout', '#ce-max_devices']) {
        const el = q(id);
        if (el) el.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); q('#ce-add').click(); } };
      }
    }
    render(types[0].key);
    const first = card.querySelector('#ce-label');
    if (first) first.focus();
  }
  $('c-env-add').onclick = (ev) => { ev.stopPropagation(); openAdd(); };

  async function selectEnv(key, keepTopic) {
    st.env = key;
    store.set('env', key);
    renderEnvs();
    if (!key) {
      $('c-chapters').innerHTML = '<div class="c-pad muted">没有在线的模拟环境，请启动模拟器后点「↻ 重新检测环境」</div>';
      $('c-topics').innerHTML = '';
      renderPreview();
      renderHome();
      return;
    }
    try {
      st.data = await api('GET', `api/scenarios?env=${encodeURIComponent(key)}`);
    } catch (e) {
      $('c-chapters').innerHTML = `<div class="c-err c-pad">读取课题失败：${esc(e.message)}</div>`;
      renderHome();
      return;
    }
    st.templates = null;
    const env = envInfo();
    $('c-chap-head').textContent = examPage ? `${env.label} · 考试` : `${env.label} · 学习章节`;
    let ch = examPage ? EXAM_CH : keepTopic ? (st.chapter || store.get('chapter')) : null;
    const target = requestedTopic && st.data.scenarios.find((s) => s.id === requestedTopic);
    if (target) { ch = target.chapter; st.topic = target.id; keepTopic = true; requestedTopic = null; }
    const counts = chapterCounts();
    if (ch !== EXAM_CH && !counts[ch]) ch = (st.data.chapters.find((c) => counts[c.id]) || {}).id || null;
    renderChapters();
    await selectChapter(ch, keepTopic);
    renderHome();
  }
  const envInfo = () => (st.envs && st.envs.environments.find((e) => e.key === st.env)) || { label: st.env || '', platform: '' };

  // ---------- 章节列 ----------
  function chapterCounts() {
    const out = {};
    for (const s of (st.data && st.data.scenarios) || []) if (s.chapter) out[s.chapter] = (out[s.chapter] || 0) + 1;
    return out;
  }
  function renderChapters() {
    const box = $('c-chapters');
    box.innerHTML = '';
    const counts = chapterCounts();
    for (const c of (examPage ? [] : st.data.chapters || [])) {
      const n = counts[c.id] || 0;
      const b = document.createElement('button');
      b.className = 'c-chap' + (c.id === st.chapter ? ' on' : '') + (n ? '' : ' empty');
      b.dataset.id = c.id;
      b.disabled = !n;
      const capN = (st.data.capacity_hidden_chapters || {})[c.id] || 0;
      b.innerHTML = `<span class="c-chap-id">${esc(c.id)}</span><span class="c-chap-t">${esc(c.title)}</span>` +
        `<span class="c-chap-n">${n || (capN ? '设备不足' : '待补充')}</span>`;
      b.title = (n ? `${c.title}：${n} 道题` : `${c.title}：${capN ? '本机可开的' : ''}还没有题目`) +
        (capN ? `（另有 ${capN} 道题需要更多设备，本机已隐藏）` : '');
      b.onclick = () => { if (!st.busy) selectChapter(c.id); };
      box.appendChild(b);
    }
    const nochap = examPage ? 0 : (st.data.scenarios || []).filter((s) => !s.chapter).length;
    if (nochap) box.insertAdjacentHTML('beforeend', `<div class="c-pad muted">另有 ${nochap} 道题未归入章节</div>`);
    if (!examPage && st.data.capacity_hidden) {    // 同时运行设备上限（里程碑 6.17）隐藏的题
      box.insertAdjacentHTML('beforeend', `<div class="c-pad muted" id="c-cap-hidden">另有 ${st.data.capacity_hidden} 道题需要更多设备` +
        `（超过同时运行 ${st.data.max_devices} 台），本机已隐藏</div>`);
    }
    const ex = document.createElement('button');
    ex.className = 'c-chap exam' + (st.chapter === EXAM_CH ? ' on' : '');
    ex.dataset.id = EXAM_CH;
    ex.innerHTML = '<span class="c-chap-id">考试</span><span class="c-chap-t">多题考试模板</span><span class="c-chap-n"></span>';
    ex.title = '本环境平台可用的考试模板';
    ex.onclick = () => { if (!st.busy) selectChapter(EXAM_CH); };
    box.appendChild(ex);
  }
  async function selectChapter(id, keepTopic) {
    st.chapter = id;
    store.set('chapter', id);
    for (const b of document.querySelectorAll('#c-chapters .c-chap')) b.classList.toggle('on', b.dataset.id === id);
    if (id === EXAM_CH) await loadTemplates();
    renderTopics(keepTopic);
  }

  // ---------- 课题列 ----------
  async function loadTemplates() {
    if (st.templates) return;
    try {
      const d = await api('GET', 'api/exam/templates');
      const plat = envInfo().platform;
      // 考试中心展示全部模板并标明适用环境；课程目录仍只显示当前环境可参加的模板。
      st.templates = examPage ? d.templates : d.templates.filter((t) =>
        t.platforms && t.platforms.length && t.platforms.every((p) => p === plat));
      st.templateTitles = d.scenarios || {};
    } catch (e) {
      st.templates = [];
      st.templateError = e.message;
    }
  }
  function templateAvailability(t) {
    const platforms = Array.isArray(t.platforms) ? t.platforms : [];
    const env = envInfo();
    const matches = platforms.length > 0 && platforms.every((p) => p === env.platform);
    const targetEnvs = [...new Set(platforms)].map((p) => {
      const names = ((st.envs && st.envs.environments) || [])
        .filter((e) => e.platform === p).map((e) => e.label);
      return { platform: p, names };
    });
    const targetLabel = targetEnvs.map(({ platform, names }) =>
      names.length ? `${names.join(' / ')}（${L.platformText(platform)}）` : L.platformText(platform)
    ).join('、') || '模板指定的平台';
    let reason = '';
    if (!matches) {
      const compatible = ((st.envs && st.envs.environments) || [])
        .filter((e) => platforms.includes(e.platform));
      const online = compatible.filter((e) => e.available);
      if (online.length) reason = `此模板适用于 ${targetLabel}，请切换到在线的「${online.map((e) => e.label).join(' / ')}」后开考。`;
      else if (compatible.length) reason = `此模板适用于 ${targetLabel}；匹配环境目前离线，启动后即可参加。`;
      else reason = `此模板适用于 ${targetLabel}，当前尚未配置匹配的模拟环境。`;
    }
    return { matches, targetLabel, reason };
  }
  function topicsOfChapter() {
    return (st.data.scenarios || []).filter((s) => s.chapter === st.chapter)
      .sort((a, b) => (a.difficulty || 0) - (b.difficulty || 0) || a.title.localeCompare(b.title));
  }
  const PROGRESS = { passed: ['已通过 ✓', 'ok'], failed: ['未通过', 'bad'], none: ['未开始', 'na'] };
  function renderTopics(keepTopic) {
    const box = $('c-topics');
    box.innerHTML = '';
    const env = envInfo();
    let pick = keepTopic ? (st.topic || store.get('topic')) : null;
    if (st.chapter === EXAM_CH) {
      $('c-topic-head').textContent = '考试 · 模板';
      $('c-topic-foot').textContent = examPage
        ? '列出所有已配置的多题考试；请按模板标注的环境参加。单题考试可在课程训练的课题详情中开启。'
        : `只列出适用于 ${env.label} 环境的模板（模板在「考试模板」页编辑）`;
      const ts = st.templates || [];
      if (!ts.length) {
        box.innerHTML = st.templateError
          ? `<div class="c-pad muted">${esc(st.templateError)}</div>`
          : examPage
            ? '<div class="c-pad muted">还没有配置多题考试模板。可到 <a href="exam/templates">考试模板</a> 创建；单题考试可在「课程训练」中选择课题后开启。</div>'
            : `<div class="c-pad muted">${esc(env.label)} 环境暂时没有适用的多题考试模板。单题考试可在「课程训练」中选择课题后开启。</div>`;
      }
      for (const t of ts) {
        const c = t.config || {};
        const availability = templateAvailability(t);
        const card = document.createElement('button');
        card.className = 'c-card';
        card.dataset.id = `tpl:${t.id}`;
        card.innerHTML = `<span class="c-card-top"><strong>${esc(t.name)}</strong><span class="c-prog ${availability.matches ? 'ok' : 'na'}">${availability.matches ? '当前可考' : '需匹配环境'}</span></span>` +
          `<span class="c-card-sum">适用环境：${esc(availability.targetLabel)}</span>` +
          `<span class="c-card-sum">总时长 ${Math.round((c.duration_s || 0) / 60)} 分钟${c.question_duration_s ? ` · 单题 ${Math.round(c.question_duration_s / 60)} 分钟` : ''}` +
          `${c.hints && c.hints.allowed ? ' · 允许提示' : ' · 禁用提示'}${c.show_topology === false ? ' · 不显示拓扑' : ''}</span>`;
        card.onclick = () => { if (!st.busy) selectTopic(card.dataset.id, true); };
        box.appendChild(card);
      }
      if (!ts.some((t) => `tpl:${t.id}` === pick)) pick = ts.length ? `tpl:${ts[0].id}` : null;
    } else {
      const chap = (st.data.chapters || []).find((c) => c.id === st.chapter);
      $('c-topic-head').textContent = chap ? `${chap.id} · ${chap.title} · 课题` : '课题';
      $('c-topic-foot').textContent = `只列出 ${env.label} 环境下的课题`;
      const items = st.chapter ? topicsOfChapter() : [];
      const capN = st.chapter ? ((st.data.capacity_hidden_chapters || {})[st.chapter] || 0) : 0;
      if (!items.length) box.innerHTML = `<div class="c-pad muted">${esc(st.data.empty_reason || '这一章还没有题目')}</div>`;
      if (capN) box.insertAdjacentHTML('beforeend', `<div class="c-pad muted">另有 ${capN} 道题需要更多设备，本机已隐藏</div>`);
      for (const s of items) {
        const [ptxt, pcls] = PROGRESS[s.progress] || PROGRESS.none;
        const card = document.createElement('button');
        card.className = 'c-card';
        card.dataset.id = s.id;
        card.innerHTML = `<span class="c-topic-label" data-icon="network">${esc(chap ? chap.title : '实操课题')}</span>` +
          `<span class="c-card-top"><strong>${esc(s.title)}</strong><span class="c-prog ${pcls}">${ptxt}</span></span>` +
          `<span class="c-card-sum">${esc(s.summary || '')}</span>` +
          `<span class="c-card-tags"><span class="c-diff">难度 ${stars(s.difficulty || s.level)}</span>` +
          `${s.est_minutes ? `<span class="c-mins">约 ${s.est_minutes} 分钟</span>` : ''}` +
          // 草稿题（未真机验证）只在服务端开了 AINET_SHOW_DRAFTS 时才会列出
          `${s.draft ? '<span class="c-mins" title="未在真机上验证（AINET_SHOW_DRAFTS=1 时才列出）">草稿 · 未验证</span>' : ''}</span>` +
          '<span class="c-topic-link">查看工单与拓扑<span aria-hidden="true">→</span></span>';
        card.onclick = () => { if (!st.busy) selectTopic(s.id, true); };
        box.appendChild(card);
      }
      if (!items.some((s) => s.id === pick)) pick = items.length ? items[0].id : null;
    }
    selectTopic(pick);
  }
  function selectTopic(id, userSelected = false) {
    st.topic = id;
    if (userSelected && id) st.hasSelection = true;
    store.set('topic', id);
    for (const c of document.querySelectorAll('#c-topics .c-card')) c.classList.toggle('on', c.dataset.id === id);
    renderPreview();
    renderHome();
  }
  const topicScenario = () => (st.data && (st.data.scenarios || []).find((s) => s.id === st.topic)) || null;
  const topicTemplate = () => (st.topic && st.topic.startsWith('tpl:') && (st.templates || [])
    .find((t) => `tpl:${t.id}` === st.topic)) || null;

  // ---------- 预览 ----------
  function sameTopology(s) {
    const cur = st.cur;
    if (!cur || !cur.topology || !cur.env || cur.env.key !== st.env) return false;
    const t = cur.topology;
    return topoKey(t.name) === topoKey(s.topology) || topoKey(t.path) === topoKey(s.topology);
  }
  function renderPreview() {
    const box = $('c-preview');
    const s = topicScenario();
    const tpl = topicTemplate();
    document.body.classList.toggle('c-empty-selection', !(s || tpl));
    const canRepeat = !!(s && s.progress === 'passed');
    $('c-actions').hidden = !(s || tpl);
    if (window.AInetUI) window.AInetUI.syncCourseEntry(!!tpl);
    if (!s && !tpl) {
      box.innerHTML = '<div class="muted">选择左侧的课题查看详情</div>';
      return;
    }
    const env = envInfo();
    for (const id of ['c-lab', 'c-practice', 'c-exam']) $(id).hidden = !s;
    $('c-exam-tpl').hidden = !tpl;
    if (tpl) {
      const c = tpl.config || {};
      const qs = c.questions || [];
      const availability = templateAvailability(tpl);
      const examButton = $('c-exam-tpl');
      examButton.dataset.envUnavailable = availability.matches ? '' : '1';
      examButton.dataset.envUnavailableTitle = availability.reason;
      examButton.disabled = !availability.matches || !!st.lockApplied;
      examButton.textContent = availability.matches ? '开始考试' : '当前环境不可开始';
      examButton.title = availability.matches ? '' : availability.reason;
      $('c-action-kind').textContent = '考试模板';
      $('c-action-topic').textContent = tpl.name;
      box.innerHTML = `<div class="c-title"><h1>${esc(tpl.name)}</h1><span class="muted">考试 · ${qs.length} 题 · 总时长 ${Math.round((c.duration_s || 0) / 60)} 分钟</span></div>` +
        `<ol class="c-qlist">${qs.map((q) => `<li>${esc((st.templateTitles || {})[q.scenario] || q.scenario)}</li>`).join('')}</ol>` +
        `<p class="c-desc">每题单独计时验收：排查修复后点「提交本题」，${c.retries ? `每题可重试 ${c.retries} 次，` : ''}` +
        `${c.hints && c.hints.allowed ? `允许提示（每级扣 ${c.hints.penalty} 分）` : '不提供提示'}；考试中不能使用 Agent。交卷后出成绩单。</p>`;
      $('c-prep').innerHTML = availability.matches
        ? `开始前会依次准备每道题需要的 ${esc(env.label)} 环境，计时从第 1 题注入故障完成后开始`
        : `${esc(availability.reason)} <a href="courses?view=environments">检查环境设置</a>`;
      return;
    }
    $('c-exam-tpl').dataset.envUnavailable = '';
    $('c-exam-tpl').dataset.envUnavailableTitle = '';
    $('c-exam-tpl').textContent = '开始考试';
    $('c-exam-tpl').title = '';
    const chap = (st.data.chapters || []).find((c) => c.id === s.chapter);
    $('c-action-kind').textContent = canRepeat ? '已通过课题' : '当前课题';
    $('c-action-topic').textContent = s.title;
    $('c-practice').textContent = canRepeat ? '再次练习' : '准备环境并开始练习';
    $('c-practice').title = canRepeat ? '本题已通过，可重复练习；每次会单独记录' : '';
    box.innerHTML = `<div class="c-title"><h1>${esc(s.title)}</h1><span class="muted">${esc(chap ? chap.id : '')}` +
      ` · 难度 ${stars(s.difficulty || s.level)}${s.est_minutes ? ` · 约 ${s.est_minutes} 分钟` : ''}</span></div>` +
      `<div class="c-tags">${(s.tags || s.knowledge_tags || []).map((t) => `<span class="c-chip">${esc(t)}</span>`).join('')}</div>` +
      '<div class="c-topo"><div class="c-topo-cap" id="c-topo-cap"></div><svg id="c-topo-svg" xmlns="http://www.w3.org/2000/svg"></svg>' +
      '<div class="c-topo-legend" id="c-topo-legend"></div></div>' +
      `<section class="c-ticket"><h2>工单与验收目标</h2><p class="c-desc">${esc(s.symptom)}</p></section>`;
    drawTopo(s);
    // 将要准备的环境
    const req = s.required_devices || [];
    let prep;
    if (env.power) {                      // 能由 ainet 启停设备的环境（eNSP Pro）
      let need = req.length;
      if (sameTopology(s)) {
        const by = Object.fromEntries(st.cur.topology.devices.map((d) => [d.name, d]));
        need = req.filter((n) => !by[n] || (st.cur.devices || {})[by[n].id] !== 'online').length;
      }
      prep = `进入时将切到 ${esc(env.label)}${env.sandboxes ? ' 的沙箱' : ' 的拓扑'} <b>${esc(s.topology)}</b>` +
        (need ? `，并启动本题需要的 ${need} 台设备（约 ${need > 2 ? 3 : 2} 分钟）` : '，本题设备都已在运行');
    } else {
      prep = `ainet 不能远程打开 eNSP 经典版的拓扑：需要在 eNSP 中打开 <b>${esc(s.topology.replace(/\.topo$/i, ''))}</b> ` +
        `拓扑并启动 ${esc(req.join('、'))}，检测到就绪后才进入`;
    }
    $('c-prep').innerHTML = prep + (canRepeat ? '<br><b>本题已通过，可再次练习；每次练习都会单独记录。</b>' : '');
  }

  function svgEl(tag, attrs, parent) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    if (parent) parent.appendChild(el);
    return el;
  }
  const ICON = { router: [54, 54], switch: [54, 54], pc: [54, 54], cloud: [85, 85], other: [54, 54] };
  const deviceIconSize = (d) => (window.AInetDeviceIcons && window.AInetDeviceIcons.size(d)) || ICON[d.kind] || ICON.other;
  const abbrIf = (name) => String(name || '').replace(/^GigabitEthernet/i, 'GE').replace(/^Ethernet/i, 'Eth').replace(/^Serial/i, 'S');
  const center = (d) => {
    if (d.anchor === 'center') return [d.x, d.y];
    const [w, h] = deviceIconSize(d);
    return [d.x + w / 2, d.y + h / 2];
  };
  function drawPreviewLink(svg, a, b, link, sizeA, sizeB, offset = 0) {
    const [ax, ay] = center(a), [bx, by] = center(b);
    const dx = bx - ax, dy = by - ay, length = Math.hypot(dx, dy) || 1;
    const ux = dx / length, uy = dy / length, nx = -uy, ny = ux;
    const sx = ax + nx * offset, sy = ay + ny * offset;
    const tx = bx + nx * offset, ty = by + ny * offset;
    const radiusA = Math.min(sizeA[0], sizeA[1]) / 2 + 1;
    const radiusB = Math.min(sizeB[0], sizeB[1]) / 2 + 1;
    const x1 = sx + ux * radiusA, y1 = sy + uy * radiusA;
    const x2 = tx - ux * radiusB, y2 = ty - uy * radiusB;
    const group = svgEl('g', { class: 'c-link-group' }, svg);
    svgEl('line', { x1, y1, x2, y2, class: 'c-link' }, group);
    svgEl('circle', { cx: x1, cy: y1, r: 2.1, class: 'c-link-port' }, group);
    svgEl('circle', { cx: x2, cy: y2, r: 2.1, class: 'c-link-port' }, group);
    const side = offset < 0 ? -1 : 1;
    const label = (x, y, value, anchor, deviceName) => {
      if (!value) return;
      const text = svgEl('text', { x, y, class: 'c-if-label', 'text-anchor': anchor, 'dominant-baseline': 'middle' }, group);
      text.textContent = abbrIf(value);
      svgEl('title', {}, text).textContent = `${deviceName} ${value}`;
    };
    label(x1 + ux * 7 + nx * 8 * side, y1 + uy * 7 + ny * 8 * side, link.src_label,
      Math.abs(ux) < 0.2 ? 'middle' : (ux > 0 ? 'start' : 'end'), a.name);
    label(x2 - ux * 7 - nx * 8 * side, y2 - uy * 7 - ny * 8 * side, link.dst_label,
      Math.abs(ux) < 0.2 ? 'middle' : (ux > 0 ? 'end' : 'start'), b.name);
    svgEl('title', {}, group).textContent = `${a.name} ${link.src_label || ''} ↔ ${b.name} ${link.dst_label || ''}`;
  }
  function drawTopo(s) {
    const svg = $('c-topo-svg');
    const cap = $('c-topo-cap');
    const legend = $('c-topo-legend');
    if (!svg) return;
    const req = new Set(s.required_devices || []);
    if (sameTopology(s)) {
      const t = st.cur.topology;
      cap.textContent = `拓扑预览 · ${envInfo().sandboxes ? '沙箱' : '拓扑'} ${t.name}`;
      legend.textContent = '橙色名称 = 本题设备 · 接口标签 = 两端端口 · 灰色名称 = 未启动';
      const devs = t.devices;
      const byId = Object.fromEntries(devs.map((d) => [d.id, d]));
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const d of devs) { const [x, y] = center(d); x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
      const span = Math.max(x1 - x0, y1 - y0, 200);
      const k = span / 8;     // 设备框按拓扑尺寸缩放
      svg.setAttribute('viewBox', `${x0 - k * 1.2} ${y0 - k} ${x1 - x0 + k * 2.4} ${y1 - y0 + k * 2}`);
      const pairTotals = {}, pairSeen = {};
      const pairKey = (l) => [l.src_id, l.dst_id].sort().join('|');
      for (const l of t.links || []) pairTotals[pairKey(l)] = (pairTotals[pairKey(l)] || 0) + 1;
      for (const l of t.links || []) {
        const a = byId[l.src_id], b = byId[l.dst_id];
        if (!a || !b) continue;
        const key = pairKey(l), count = pairTotals[key], index = pairSeen[key] = (pairSeen[key] ?? -1) + 1;
        const offset = (index - (count - 1) / 2) * 8;
        const iconSize = Math.min(52, k * 0.76);
        drawPreviewLink(svg, a, b, l, [iconSize, iconSize], [iconSize, iconSize], offset);
      }
      for (const d of devs) {
        const [x, y] = center(d);
        const on = (st.cur.devices || {})[d.id] === 'online';
        const g = svgEl('g', { class: `c-node${req.has(d.name) ? ' req' : ''}${on ? '' : ' off'}` }, svg);
        const iconSize = Math.min(52, k * 0.76);
        if (window.AInetDeviceIcons) window.AInetDeviceIcons.appendSvg(g, d, svgEl,
          { x: x - iconSize / 2, y: y - iconSize / 2, width: iconSize, height: iconSize });
        else svgEl('rect', { class: 'shape', x: x - iconSize / 2, y: y - iconSize / 2, width: iconSize, height: iconSize, rx: 4 }, g);
        const te = svgEl('text', { x, y: y + iconSize / 2 + 14, class: 'c-node-label', 'font-size': Math.min(13, k * 0.18), 'text-anchor': 'middle' }, g);
        te.textContent = d.name;
        svgEl('title', {}, g).textContent = `${d.name} · ${d.model || d.kind || '设备'}`;
      }
      return;
    }
    // 拓扑还没加载（进入时才切换）：只示意本题设备
    const previewLinks = s.preview_links || [];
    cap.textContent = `拓扑预览 · ${s.topology.replace(/\.topo$/i, '')}` +
      (previewLinks.length ? '（完整拓扑进入时加载；这里显示本题设备与已知端口连接）' : '（当前环境未匹配；这里只显示本题设备）');
    legend.textContent = previewLinks.length
      ? '橙色名称 = 本题设备 · 接口标签 = 题目拓扑的两端端口'
      : '橙色名称 = 本题设备 · 匹配拓扑后显示实际端口连接';
    const names = [...req];
    const gap = 26, previewW = 74, iconSize = 42;
    const width = names.length * (previewW + gap) - gap;
    svg.setAttribute('viewBox', `${-20} ${-20} ${Math.max(width + 40, 300)} 112`);
    const nodes = names.map((name, i) => {
      const x = i * (previewW + gap), cx = x + previewW / 2;
      return { name, model: name, kind: /^[RT]\d+$/i.test(name) ? 'router' : undefined, anchor: 'center', x: cx, y: 20 };
    });
    const nodeByName = Object.fromEntries(nodes.map((node) => [node.name, node]));
    const nodeSizes = Object.fromEntries(nodes.map((node) => [node.name, [iconSize, iconSize]]));
    const pairTotals = {}, pairSeen = {};
    const pairKey = (link) => [link.src_device, link.dst_device].sort().join('|');
    for (const link of previewLinks) pairTotals[pairKey(link)] = (pairTotals[pairKey(link)] || 0) + 1;
    for (const link of previewLinks) {
      const a = nodeByName[link.src_device], b = nodeByName[link.dst_device];
      if (!a || !b) continue;
      const key = pairKey(link), count = pairTotals[key], index = pairSeen[key] = (pairSeen[key] ?? -1) + 1;
      drawPreviewLink(svg, a, b, { src_label: link.src_label, dst_label: link.dst_label },
        nodeSizes[a.name], nodeSizes[b.name], (index - (count - 1) / 2) * 8);
    }
    nodes.forEach((device) => {
      const [, cy] = center(device), cx = device.x;
      const g = svgEl('g', { class: 'c-node req' }, svg);
      if (window.AInetDeviceIcons) window.AInetDeviceIcons.appendSvg(g, device, svgEl,
        { x: cx - iconSize / 2, y: cy - iconSize / 2, width: iconSize, height: iconSize });
      const te = svgEl('text', { x: cx, y: cy + iconSize / 2 + 15, class: 'c-node-label', 'font-size': 12, 'text-anchor': 'middle' }, g);
      te.textContent = device.name;
    });
  }

  // ---------- 进入操作页：准备环境 → 开练 / 开考（准备浮层，spec §16.3「进入」） ----------
  // 浮层 = 遮罩 + 居中卡片：步骤条（job.steps，跳过的显示「已跳过」）、整体进度条（job.progress 只增不减、每秒缓慢推进，
  // 不到完成不满格）、已用时间与「预计还需」（job.estimate）、设备卡片（job.device_info，重启中单列）、自动重启警示、
  // 轮播提示、本题工单；出错显示后端原因 +「重试 / 返回课程」；就绪后显示「环境已就绪」停留 doneHold 毫秒再进操作页。
  const fmt = (s) => { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const OV_TITLES = { lab: '正在准备实验环境', practice: '正在准备练习环境', exam: '正在准备考试环境', template: '正在准备考试环境' };
  const OV_FAIL = { lab: '没能进入自由实验', practice: '没能开始练习', exam: '没能开始考试', template: '没能开始考试' };
  // 没有 job.steps 时（旧后端）的缺省步骤
  const STEP_KEYS = ['switch', 'sandbox', 'start', 'wait', 'probe', 'done'];
  const STEP_LABELS = { switch: '切换环境', sandbox: '查找沙箱', start: '启动设备', wait: '等待运行', probe: '确认就绪',
    check: '核对设备', done: '完成' };
  // 设备卡片：图标 + 文字（颜色在 CSS 的 .st-*）；restarting 是看门狗正在「停止 → 启动」（接口状态里显示为启动中）
  const DEV = { stopped: ['○', '未启动'], starting: ['', '启动中'], initializing: ['◐', '系统初始化'], online: ['✓', '就绪'],
    restarting: ['↻', '重启中'], start_failed: ['✕', '启动失败'], error: ['!', '异常'], stopping: ['', '停止中'],
    unknown: ['?', '状态未知'] };
  const TIPS_POWER = [
    '设备首次启动约 2~3 分钟，可以先阅读下面的工单，想好排查思路',
    '请勿在 eNSP Pro 网页端登录：同一账号只能有一个会话，会挤掉 ainet 的连接',
    '设备启动卡住时 ainet 会自动停止后重新启动，最多 {n} 次',
    '设备报「运行中」后还要等系统初始化完成（约半分钟），这之前改配置会报 busy',
    '可以随时「取消」：已经发出的设备启动会在后台继续完成，下次进入更快',
  ];
  const TIPS_CLASSIC = [
    'ainet 不能远程打开 eNSP 经典版的拓扑：在 eNSP 中打开本题拓扑、启动设备后，这里会自动检测',
    'eNSP 经典版的 AR 路由器冷启动约 3~5 分钟，可以先阅读下面的工单',
    '每 5 秒重新检测一次；不想等可以点「取消」',
  ];
  const ov = { kind: '', s: null, t0: 0, timer: null, tipTimer: null, tip: 0, tips: TIPS_POWER, shown: 0, target: 0,
    qi: 0, qn: 1, stageKey: '', state: '', devs: {}, power: true, retries: 3 };
  const reduced = () => { try { return matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } };
  const fmtEta = (e, restarting) => {
    if (!e || e.hi_s == null) return '';
    if (e.hi_s < 60) return '预计还需不到 1 分钟';
    const lo = Math.max(1, Math.round(e.lo_s / 60)), hi = Math.max(lo, Math.ceil(e.hi_s / 60));
    return `预计还需约 ${lo === hi ? hi : `${lo}~${hi}`} 分钟${restarting ? '（含自动重启）' : ''}`;
  };
  function inert(on) {
    for (const id of ['bar', 'c-main', 'c-active']) {
      const el = $(id);
      if (!el) continue;
      if (on) el.setAttribute('inert', ''); else el.removeAttribute('inert');
    }
  }
  function ovSet(state) {
    ov.state = state;
    $('c-prep-ov').dataset.state = state;              // busy / wait / done / error（CSS 按它换图标与颜色）
  }
  function ovOpen(kind, s, sub) {
    clearTimeout(ov.closeT);
    ov.kind = kind; ov.s = s; ov.t0 = Date.now(); ov.shown = 0; ov.target = 0; ov.qi = 0; ov.qn = 1;
    ov.stageKey = ''; ov.devs = {};
    const env = envInfo();
    ov.power = !!env.power;
    ov.retries = 3;
    ov.tips = ov.power ? TIPS_POWER : TIPS_CLASSIC;
    ovSet('busy');
    $('c-prog-title').textContent = OV_TITLES[kind];
    $('pv-sub').textContent = sub;
    $('c-prog-stage').textContent = '准备中…';
    $('c-prog-devs').innerHTML = '';
    $('pv-alert').hidden = true;
    $('pv-bar').classList.remove('indet');
    progMsg('');
    renderSteps(null);
    $('pv-ticket').hidden = !(s && s.symptom);
    $('pv-ticket-body').textContent = s ? s.symptom || '' : '';
    $('c-cancel').hidden = false;
    $('c-cancel').disabled = false;
    $('pv-back').hidden = $('pv-retry').hidden = true;
    $('pv-eta').textContent = ov.power ? '设备冷启动通常需要 2~4 分钟' : '';
    $('c-prep-ov').hidden = false;
    $('pv-live').textContent = `${OV_TITLES[kind]}：${sub}`;      // 浮层显示之后再写，读屏才会播报
    inert(true);
    $('c-progress').focus();
    ov.tip = 0;
    showTip();
    clearInterval(ov.tipTimer);
    ov.tipTimer = setInterval(() => { ov.tip++; showTip(); }, 7000);
    clearInterval(ov.timer);
    ov.timer = setInterval(tick, 1000);
    tick();
  }
  function ovClose() {
    clearInterval(ov.timer); clearInterval(ov.tipTimer);
    $('c-prep-ov').hidden = true;
    inert(false);
  }
  function showTip() {
    const el = $('pv-tip-t');
    const text = ov.tips[ov.tip % ov.tips.length].replace('{n}', ov.retries);
    if (reduced() || !el.textContent) { el.textContent = text; return; }
    el.classList.add('out');
    setTimeout(() => { el.textContent = text; el.classList.remove('out'); }, 250);
  }
  // 进度：target 来自后端（只增不减）；显示值每秒向 target 靠拢，并在 target 之上最多再缓慢推进 4%（不越过 97%）
  function setTarget(p) { ov.target = Math.max(ov.target, Math.min(1, p)); paint(); }
  function tick() {
    $('c-prog-elapsed').textContent = `已用 ${fmt((Date.now() - ov.t0) / 1000)}`;
    if (ov.state === 'busy') {
      const cap = Math.min(0.97, ov.target + 0.04);
      ov.shown = Math.max(ov.shown, ov.target);
      if (ov.shown < cap) ov.shown = Math.min(cap, ov.shown + 0.002);
    }
    paint();
  }
  function paint() {
    ov.shown = Math.max(ov.shown, ov.target);
    const pct = ov.state === 'done' ? 100 : Math.min(99, Math.floor(ov.shown * 100));
    $('pv-fill').style.width = `${pct}%`;
    $('pv-pct').textContent = $('pv-bar').classList.contains('indet') ? '等待中' : `${pct}%`;
    $('pv-bar').setAttribute('aria-valuenow', String(pct));
    $('pv-bar').setAttribute('aria-valuetext', `${pct}%`);
  }
  function renderSteps(job, opts = {}) {
    let steps = job && job.steps;
    if (!steps) {
      const keys = ov.power ? STEP_KEYS : ['switch', 'check', 'done'];
      const cur = job ? ({ restart: 'wait', queued: '' }[job.stage] ?? job.stage) : '';
      const at = keys.indexOf(cur);
      steps = keys.map((k, i) => ({ key: k, label: STEP_LABELS[k],
        state: job && job.stage === 'done' ? 'done' : i < at ? 'done' : i === at ? 'active' : 'pending' }));
    }
    const ol = $('pv-steps');
    const sig = steps.map((x) => x.key).join(',');
    if (ol.dataset.sig !== sig) {
      ol.dataset.sig = sig;
      ol.innerHTML = steps.map((x, i) => `<li class="pv-step" data-key="${esc(x.key)}"><span class="pv-dot" aria-hidden="true">` +
        `<span class="pv-n">${i + 1}</span></span><span class="pv-sl">${esc(x.label || STEP_LABELS[x.key] || x.key)}</span>` +
        '<span class="pv-ss"></span></li>').join('');
    }
    const SS = { done: '已完成', active: '进行中', skipped: '已跳过', error: '出错', pending: '' };
    for (const x of steps) {
      const li = ol.querySelector(`[data-key="${CSS.escape(x.key)}"]`);
      let state = x.state;
      const warn = (job && job.stage === 'restart' && x.key === 'wait' && state === 'active') ||
        (opts.waitKey === x.key);
      if (opts.waitKey === x.key) state = 'active';
      li.className = `pv-step s-${state}${warn ? ' warn' : ''}`;
      li.querySelector('.pv-ss').textContent = warn ? (job && job.stage === 'restart' ? '自动重启' : '等待中') : SS[state] || '';
      if (state === 'active') li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
    }
  }
  function renderDevs(job) {
    const info = job.device_info || {};
    const devs = job.devices || {};
    const names = Object.keys(Object.keys(info).length ? info : devs);
    const box = $('c-prog-devs');
    for (const el of [...box.children]) if (!names.includes(el.dataset.name)) el.remove();
    for (const n of names) {
      const d = info[n] || { status: devs[n] };
      const stt = d.restarting ? 'restarting' : d.status;
      const [icon, text] = DEV[stt] || ['?', L.statusText(stt)];
      let el = box.querySelector(`[data-name="${CSS.escape(n)}"]`);
      if (!el) {
        el = document.createElement('div');
        el.dataset.name = n;
        el.innerHTML = '<span class="pv-di" aria-hidden="true"></span><span class="pv-dt"><b></b><span class="pv-ds"></span></span>' +
          '<span class="pv-dn"></span>';
        el.querySelector('b').textContent = n;
        box.appendChild(el);
      }
      if (el.dataset.st !== stt) {
        if (el.dataset.st && !reduced()) {
          el.classList.remove('chg');
          void el.offsetWidth;                         // 重新触发状态切换的动画
          el.classList.add('chg');
        }
        el.dataset.st = stt;
      }
      el.className = `c-dev pv-dev st-${stt}${el.classList.contains('chg') ? ' chg' : ''}`;
      el.querySelector('.pv-di').textContent = icon;
      el.querySelector('.pv-ds').textContent = text;
      // 进度小字：去掉与状态文字重复的前缀；启动失败的原因在警示框里（悬停卡片看全文）
      const note = d.restarting ? `第 ${d.restarts} 次自动重启` : stt === 'online' ? ''
        : (d.note || '').replace(/^已自动重启 (\d+) 次 · $/, '已自动重启 $1 次').replace(/^(启动中|启动失败：)\s*/, '')
          || (d.restarts ? `已自动重启 ${d.restarts} 次` : '');
      el.querySelector('.pv-dn').textContent = note;
      el.title = `${n}：${text}${note ? `（${note}）` : ''}`;
    }
  }
  function progMsg(text, cls) {
    $('c-prog-msg').textContent = text || '';
    $('c-prog-msg').className = `c-prog-msg ${cls || ''}`;
  }
  // 后端任务进度（LabSwitch.prepare 的 onProgress）
  function showJob(job) {
    if (job.retries != null) ov.retries = job.retries;
    if (job.power != null) ov.power = !!job.power;
    $('pv-bar').classList.remove('indet');
    const stage = L.stageText(job.stage);
    // 等待 / 探测 / 重启时各设备的状态已在卡片与警示里，阶段行不再重复英文状态列表
    const brief = ['wait', 'probe', 'restart'].includes(job.stage) && job.device_info && Object.keys(job.device_info).length;
    $('c-prog-stage').textContent = `阶段：${stage}${job.message && !brief ? ` · ${job.message}` : ''}`;
    renderSteps(job);
    renderDevs(job);
    // 卡住自动重启：警示色明确说明哪台、第几次
    const info = job.device_info || {};
    const rs = Object.keys(info).filter((n) => info[n].restarting);
    const alert = $('pv-alert');
    if (job.stage === 'restart' || rs.length) {
      const names = rs.length ? rs : [];
      const nth = Math.max(1, ...names.map((n) => info[n].restarts || 0));
      alert.className = 'pv-alert warn';
      alert.textContent = names.length
        ? `${names.join('、')} 启动卡住，正在自动重启（第 ${nth} 次${ov.retries ? `，最多 ${ov.retries} 次` : ''}）`
        : job.message;
      alert.hidden = false;
    } else if (alert.classList.contains('warn')) {
      alert.hidden = true;
    }
    const p = typeof job.progress === 'number' ? job.progress : job.stage === 'done' ? 1 : 0;
    setTarget(((ov.qi + p) / ov.qn) * 0.95);           // 留最后 5% 给「开始练习 / 开考」
    if (job.estimate) $('pv-eta').textContent = fmtEta(job.estimate, job.stage === 'restart' || rs.length > 0);
    else if (job.stage === 'done') $('pv-eta').textContent = '';
    // 阶段变化才播报（不播报每秒变化的计时）
    const key = `${job.stage}|${rs.join(',')}`;
    if (key !== ov.stageKey) {
      ov.stageKey = key;
      $('pv-live').textContent = job.stage === 'restart' && !alert.hidden ? alert.textContent : `阶段：${stage}`;
    }
  }
  // 浮层里的后续步骤（准备完成后：开练 / 开考）；live = 播报的文字（省略 = text），与上次相同不重复写（倒计时不逐秒播报）
  function ovPhase(text, cls, live) {
    $('c-prog-stage').textContent = text;
    const say = live ?? text;
    if ($('pv-live').textContent !== say) $('pv-live').textContent = say;
    $('pv-eta').textContent = '';
    if (cls !== 'warn') setTarget(0.96);
    progMsg('');
  }
  function ovDone(after) {
    ovSet('done');
    clearInterval(ov.tipTimer);
    for (const li of $('pv-steps').children) {
      if (!li.classList.contains('s-skipped')) { li.className = 'pv-step s-done'; li.querySelector('.pv-ss').textContent = '已完成'; }
    }
    $('pv-bar').classList.remove('indet');
    ov.target = ov.shown = 1;
    paint();
    $('c-prog-title').textContent = '环境已就绪';
    $('c-prog-stage').textContent = '正在进入操作页…';
    $('pv-live').textContent = '环境已就绪，正在进入操作页';
    $('pv-alert').hidden = true;
    $('pv-eta').textContent = '';
    $('c-cancel').hidden = true;
    progMsg('');
    setTimeout(after, st.doneHold ?? 800);
  }
  function ovError(kind, message) {
    ovSet('error');
    clearInterval(ov.timer); clearInterval(ov.tipTimer);
    $('c-prog-title').textContent = OV_FAIL[kind] || '没能进入';
    $('pv-bar').classList.remove('indet');
    const alert = $('pv-alert');
    alert.className = 'pv-alert err';
    alert.textContent = message;                      // 后端返回的中文原文
    alert.hidden = false;
    alert.setAttribute('role', 'alert');
    for (const li of $('pv-steps').querySelectorAll('.s-active')) {
      li.className = 'pv-step s-error'; li.querySelector('.pv-ss').textContent = '出错';
    }
    if (!$('pv-steps').querySelector('.s-error')) {      // 准备已完成、开练 / 开考失败：把「完成」一步标为出错
      const lis = [...$('pv-steps').children], last = lis[lis.length - 1];
      if (last && lis.slice(0, -1).every((li) => /s-(done|skipped)/.test(li.className))) { last.className = 'pv-step s-error'; last.querySelector('.pv-ss').textContent = '出错'; }
    }
    const bad = $('pv-steps').querySelector('.s-error .pv-sl');
    $('c-prog-stage').textContent = bad ? `在「${bad.textContent}」这一步出错，原因：` : '原因：';
    $('pv-eta').textContent = '';
    progMsg(`没能进入：${message}`, 'err sr-only');
    $('c-cancel').hidden = true;
    $('pv-back').hidden = $('pv-retry').hidden = false;
    $('pv-retry').focus();
  }
  function setBusy(on) {
    st.busy = on;
    $('c-entry-modes').disabled = on;
    for (const id of ['c-lab', 'c-practice', 'c-exam', 'c-exam-tpl', 'c-recheck']) $(id).disabled = on || $(id).dataset.locked === '1';
    document.body.classList.toggle('c-busy', on);
  }
  $('c-cancel').onclick = () => {
    if (st.token) st.token.cancelled = true;
    $('c-cancel').disabled = true;
    progMsg('正在取消…（已在后台进行的设备启动会继续完成）');
  };
  $('pv-back').onclick = () => { $('pv-alert').removeAttribute('role'); ovClose(); };
  $('pv-retry').onclick = () => {
    $('pv-alert').removeAttribute('role');
    const kind = ov.kind;
    ovClose();
    enter(kind, true);
  };

  // 能启停设备的环境（eNSP Pro）：按题目准备环境（切沙箱、启动设备）；
  // eNSP 经典版（不能远程打开拓扑 / 启动设备）：轮询到本题就绪（拓扑在 eNSP 中打开、设备都在运行）才放行
  async function ensureEnv(s, token) {
    const env = envInfo();
    if (env.power) {
      const job = await L.prepare('', s.id, showJob, token, env.key);
      showJob(job);
      if (!job.ok) throw Object.assign(new Error(job.error || '准备环境失败'), { status: job.error_status });
      return;
    }
    const topo = s.topology.replace(/\.topo$/i, '');
    for (let round = 0; ; round++) {
      if (token.cancelled) throw Object.assign(new Error('已取消'), { cancelled: true });
      let reason = '';
      let last = null;
      try {
        last = await L.prepare('', s.id, showJob, token, env.key);       // 切到 eNSP（已在则跳过）并核对设备
        showJob(last);
        if (!last.ok) reason = last.error;
      } catch (e) {
        if (e.cancelled) throw e;
        reason = e.message;
      }
      if (!reason) {
        const d = await api('GET', `api/scenarios?env=${encodeURIComponent(env.key)}`);
        const x = (d.scenarios || []).find((y) => y.id === s.id);
        if (x && x.ready) return;
        reason = x ? x.reason : '题目不存在';
      }
      // 等用户在 eNSP 里打开拓扑：这是等待态（不是错误），「核对设备」步骤保持进行中，进度条改为不确定
      renderSteps(last, { waitKey: 'check' });
      $('pv-bar').classList.add('indet');
      paint();
      $('pv-eta').textContent = '';
      const tip = `请在 eNSP 中打开 ${topo} 拓扑并启动设备（${(s.required_devices || []).join('、')}）`;
      $('c-prog-stage').textContent = tip;
      if (ov.stageKey !== 'classic-wait') { ov.stageKey = 'classic-wait'; $('pv-live').textContent = tip; }
      for (let left = 5; left > 0; left--) {
        if (token.cancelled) throw Object.assign(new Error('已取消'), { cancelled: true });
        progMsg(`尚未就绪：${reason}\n${left} 秒后重新检测…`, 'warn');
        await new Promise((r) => setTimeout(r, 1000));
      }
    }
  }
  function busyWait(what) {
    return (left, n, e) => {
      ovPhase(`设备刚启动，还在准备配置，${left} 秒后第 ${n} 次重试${what}（最多 5 次）`, 'warn',
        `设备刚启动，还在准备配置，稍后第 ${n} 次重试${what}`);
      progMsg(`设备刚启动，还在准备配置（${e.message}）。\n${left} 秒后第 ${n} 次重试${what}（最多 5 次），可点「取消」。`, 'warn');
    };
  }
  function go(mode, s) {
    location.href = `./?mode=${mode}${s ? `&s=${encodeURIComponent(s.id)}` : ''}`;
  }
  async function enter(kind, again) {
    if (st.busy) return;
    const s = topicScenario();
    const tpl = topicTemplate();
    if (kind === 'template' ? !tpl : !s) return;
    if (kind === 'template') {
      const availability = templateAvailability(tpl);
      if (!availability.matches) { alert(availability.reason); return; }
    }
    if (!again && kind === 'exam' && !confirm(`开始「${s.title}」的单题考试？\n\n准备好环境、注入故障后立即开始计时（15 分钟）；考试中不能使用 Agent、没有提示，` +
      '排查修复后点「提交本题」，交卷后出成绩单。')) return;
    if (!again && kind === 'template' && !confirm(`开始考试「${tpl.name}」？\n\n先准备每道题的环境，然后开始计时；考试中不能使用 Agent。`)) return;
    const token = st.token = { cancelled: false };
    setBusy(true);
    const env = envInfo();
    const where = env.label + (env.sandboxes && s ? ` · 沙箱 ${s.topology}` : '');
    ovOpen(kind, kind === 'template' ? null : s, `${kind === 'template' ? tpl.name : s.title} · ${where}`);
    try {
      if (kind === 'template') {
        const ids = [...new Set((tpl.config.questions || []).map((q) => q.scenario))];
        ov.qn = Math.max(1, ids.length);
        for (const [i, id] of ids.entries()) {
          const sc = (st.data.scenarios || []).find((x) => x.id === id);
          if (!sc) throw new Error(`模板里的题目 ${id} 不在 ${envInfo().label} 的题库里`);
          ov.qi = i;
          $('pv-sub').textContent = `${tpl.name} · 第 ${i + 1} / ${ids.length} 题：${sc.title} · ${env.label}`;
          $('c-prog-devs').innerHTML = '';
          progMsg(`准备第 ${i + 1} / ${ids.length} 道题的环境：${sc.title}`);
          await ensureEnv(sc, token);
        }
        ov.qi = ov.qn - 1;
        ovPhase('环境已就绪，正在开始考试（准备第 1 题：快照、注入故障）…');
        await L.retryBusy(() => api('POST', 'api/exam/start', { template_id: tpl.id }), { token, onWait: busyWait('开考') });
        ovDone(() => { location.href = './?mode=exam'; });
        return;
      }
      await ensureEnv(s, token);
      if (kind === 'lab') { ovDone(() => go('lab', s)); return; }
      if (kind === 'practice') {
        ovPhase('环境已就绪，正在开始练习（快照、注入故障、确认故障生效）…');
        await L.retryBusy(() => api('POST', 'api/practice/start', { scenario_id: s.id }), { token, onWait: busyWait('开始练习') });
        ovDone(() => go('practice', s));
        return;
      }
      ovPhase('环境已就绪，正在开始单题考试（快照、注入故障）…');
      await L.retryBusy(() => api('POST', 'api/exam/start_single', { scenario_id: s.id }), { token, onWait: busyWait('开考') });
      ovDone(() => go('exam', s));
    } catch (e) {
      setBusy(false);
      refreshCurrent();
      if (e.cancelled) {
        progMsg('已取消。', '');
        $('pv-live').textContent = '已取消';
        ov.closeT = setTimeout(ovClose, 600);
      } else {
        ovError(kind, e.message);
      }
    }
  }
  $('c-lab').onclick = () => enter('lab');
  $('c-practice').onclick = () => enter('practice');
  $('c-exam').onclick = () => enter('exam');
  $('c-exam-tpl').onclick = () => enter('template');
  $('c-recheck').onclick = async () => { if (!st.busy) { await refreshCurrent(); await loadEnvs(true); } };

  // ---------- 进行中的练习 / 考试 ----------
  async function checkActive() {
    let text = '', href = '', resetting = false;
    try {
      const ex = await api('GET', 'api/exam/current');
      st.examActive = !!ex.active;
      if (window.AInetUI) window.AInetUI.setExamLocked(st.examActive);
      $('um-ai').hidden = st.examActive || !window.AiSettings;
      if (ex.active) {
        text = `考试「${ex.name}」进行中（第 ${ex.current + 1} / ${ex.total} 题）`;
        const scenarioId = ex.question && ex.question.scenario_id;
        href = `./?mode=exam${scenarioId ? `&s=${encodeURIComponent(scenarioId)}` : ''}`;
        st.epOpen = null;
      }
      if (!text) {
        const p = await api('GET', 'api/practice/current');
        if (p.active) {
          text = `练习「${p.scenario.title}」进行中`;
          href = `./?mode=practice&s=${encodeURIComponent(p.scenario.id)}`;
        } else if (p.resetting) {
          // 刚结束的练习：设备正在后台复位，完成后自动解锁（不需要回操作页做什么）
          resetting = true;
          const t = p.last_result && p.last_result.scenario ? `「${p.last_result.scenario.title}」` : '';
          text = `正在后台复位上一题${t}的设备`;
        }
      }
    } catch (e) { /* 忽略 */ }
    $('c-active').hidden = !text;
    $('c-active-text').textContent = !text ? ''
      : (resetting ? `${text}（通常几十秒），完成后即可换题、换环境…`
        : st.examActive ? `${text}：进度已保存，考试计时继续；返回工作台可继续作答。结束本场考试后才能开始其他练习。`
        : `${text}：换题、换环境之前请先回到操作页结束它。`);
    const resume = $('c-active-go');
    resume.hidden = resetting;
    resume.dataset.resumeHref = href;
    resume.href = href || './';
    resume.setAttribute('aria-disabled', String(resetting || !href));
    resume.tabIndex = resetting || !href ? -1 : 0;
    document.body.classList.toggle('c-locked', !!text);
    clearTimeout(st.resetTimer);
    if (resetting) st.resetTimer = setTimeout(async () => { applyLock(await checkActive()); }, 1500);
    return !!text;
  }
  // 有进行中的练习 / 考试（或上一题还在后台复位）时锁住进入按钮
  function applyLock(locked) {
    for (const id of ['c-lab', 'c-practice', 'c-exam', 'c-exam-tpl']) {
      const button = $(id);
      const unavailable = id === 'c-exam-tpl' && button.dataset.envUnavailable === '1';
      button.dataset.locked = locked ? '1' : '';
      if (locked) {
        button.disabled = true;
        button.title = '有进行中的练习 / 考试，或上一题还在后台复位，请稍候';
      } else {
        button.disabled = unavailable;
        if (id === 'c-exam-tpl') button.title = unavailable ? button.dataset.envUnavailableTitle : '';
        else if (st.lockApplied) button.title = '';
      }
    }
    if (!locked && st.lockApplied) loadEnvs(false);      // 环境列表的「切换锁」也随之解除
    st.lockApplied = locked;
  }
  async function refreshCurrent() {
    try { st.cur = await api('GET', 'api/topology/current'); } catch (e) { st.cur = null; }
    renderPreview();
  }

  async function init() {
    if (envSettingsPage) {
      document.title = '环境设置 · ainet';
      document.querySelector('.topbar .page-title').textContent = '环境设置';
      document.querySelector('.course-intro .ui-eyebrow').textContent = '模拟器接入与运行参数';
      document.querySelector('.course-intro h1').textContent = '管理模拟环境';
      document.querySelector('.course-intro p').textContent = '集中设置环境地址、设备密码、启动超时和设备容量。';
      $('c-env-foot').textContent = '离线环境也可以修改接入参数；保存后会重新检测连接状态。';
    } else if (examPage) {
      document.title = '考试中心 · ainet';
      document.querySelector('.topbar .page-title').textContent = '考试中心';
      document.querySelector('.course-intro .ui-eyebrow').textContent = '网络实操训练 · 阶段测评';
      document.querySelector('.course-intro h1').textContent = '选择一场考试';
      document.querySelector('.course-intro p').textContent = '查看所有已配置的考试模板及适用环境；选择匹配的在线环境后即可开考。';
      $('c-env-heading').textContent = '考试环境';
      $('c-env-foot').textContent = '模板会标明适用环境；匹配环境在线时才能开始。环境参数可在环境设置页修改。';
    } else if (pageView === 'catalog') {
      document.title = '课程训练 · ainet';
      document.querySelector('.topbar .page-title').textContent = '课程训练';
      $('c-env-heading').textContent = '当前练习环境';
      $('c-env-foot').textContent = '更换环境只切换课程浏览；进入课题时才准备设备。环境参数可在环境设置页修改。';
    } else {
      document.title = '学习首页 · ainet';
      $('c-env-foot').textContent = '选择环境后，首页会显示对应课程进度与下一道推荐课题。';
    }
    const res = await fetch('api/me');
    if (res.status === 401) { location.href = 'login'; return; }
    const me = await res.json();
    $('who').textContent = `${me.student.name}（${me.student.login}）`;
    st.studentName = me.student.name;
    if (me.auto_login) { $('um-switch').hidden = true; $('logout').hidden = true; }   // 自动登录模式：不切换、不登出
    const locked = await checkActive();
    for (const id of ['c-lab', 'c-practice', 'c-exam', 'c-exam-tpl']) {
      $(id).dataset.locked = locked ? '1' : '';
    }
    await Promise.all([refreshCurrent(), loadEnvs(false)]);
    renderHome();
    if (locked) applyLock(true);
  }

  window.__ainetCourses = { st, selectEnv, selectChapter, selectTopic, enter, loadEnvs, EXAM_CH };
  init();
})();
