// ainet 操作页（工作台）：ui.js / ui.css 提供平台导航与两行控制区；拓扑图 | 大终端 | 工单 / Agent。
// 只用原生 JS 与本地化的 xterm.js；每个设备一个常驻 xterm 实例和 WebSocket。
// 课题从课程页带入（?s=场景 id&mode=lab|practice|exam；进行中的练习 / 考试以服务端为准）。模式切换规则（spec §16.1）：
//   实验 ↔ 练习可以切（练习中切到实验 = 放弃本次练习，确认后结束并复位）；实验 / 练习 → 考试（本题的单题考试）要确认；
//   考试中不能切到其他模式，只能交卷；操作页内不能换题，换题 / 换环境 / 换拓扑要「退出到课程」（练习中确认后按放弃结束）。
// 布局状态（docs/design/ui-layout.html）都落在 body 的类上：mode-lab|practice|exam（顶栏模式）、practice-active /
// exam-active（进行中）、topo-collapsed / side-collapsed（48px 图标栏）、narrow（实际工作区 < 1050px 时右侧改抽屉）、
// drawer-open。面板宽度与折叠状态存 localStorage（读写都 try/catch）。
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const POLL_MS = 5000;
  const GRID = 100;      // eNSP config_sys.ini sizeOfGrid

  const state = {
    cur: null,           // 最近一次 /api/topology/status|current 的结果（不含 topology）
    topo: null,          // 当前拓扑（由 eNSP 中运行的设备确定）
    status: {},          // 设备 id -> online / stopped / na（不探测端口，见 topology.py）
    tabs: new Map(),     // port -> tab
    active: null,        // 最近聚焦的 tab port
    split: false,
    panes: [null, null], // 每屏显示的 port
    focusedPane: 0,
    practice: null,      // /api/practice/current（active=true 时为进行中的练习）
    exam: null,          // /api/exam/current（exam.js 维护；active=true 时为进行中的考试）
    mode: 'lab',         // 顶栏模式：lab（实验）/ practice（练习）/ exam（考试）
    topic: null,         // 当前课题（api/scenarios 里的面向学员字段）；没有 = 自由实验
    chapters: [],
  };

  // ---------- 工具 ----------
  function svgEl(tag, attrs, parent) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    if (parent) parent.appendChild(el);
    return el;
  }
  function abbrIf(name) {
    return name.replace(/^GigabitEthernet/, 'GE').replace(/^Ethernet/, 'Eth')
      .replace(/^Serial/, 'S');
  }
  function msg(text, isErr) {
    const el = $('topo-msg');
    el.textContent = text || '';
    el.classList.toggle('err', !!isErr);
  }
  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) { location.href = 'login'; throw new Error('未登录'); }
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
    return data;
  }

  // ---------- 拓扑图 ----------
  // .topo 中 cx/cy 是图标左上角；按类型估算图标尺寸取中心
  const ICON_SIZE = { router: [54, 42], switch: [54, 42], pc: [40, 40], cloud: [85, 50], other: [50, 50] };
  function center(d) {
    if (d.anchor === 'center') return [d.x, d.y];   // eNSP Pro：坐标就是图标中心
    const [w, h] = ICON_SIZE[d.kind] || ICON_SIZE.other;
    return [d.x + w / 2, d.y + h / 2];
  }

  const view = { box: null, fitBox: null };
  const svg = $('topo-svg');

  function setViewBox(b) {
    view.box = b;
    svg.setAttribute('viewBox', `${b.x} ${b.y} ${b.w} ${b.h}`);
    drawGrid();
  }

  function drawGlyph(g, kind) {
    switch (kind) {
      case 'router':
        svgEl('circle', { class: 'shape', cx: 0, cy: 0, r: 18 }, g);
        // 四向箭头
        svgEl('path', { class: 'glyph', d: 'M-10 0H10M-10 0l4-4M-10 0l4 4M10 0l-4-4M10 0l-4 4M0 -10V10M0 -10l-4 4M0 -10l4 4M0 10l-4-4M0 10l4-4' }, g);
        break;
      case 'switch':
        svgEl('rect', { class: 'shape', x: -22, y: -14, width: 44, height: 28, rx: 3 }, g);
        svgEl('path', { class: 'glyph', d: 'M-13 -5H13M13 -5l-4-3M13 -5l-4 3M13 5H-13M-13 5l4-3M-13 5l4 3' }, g);
        break;
      case 'cloud':
        svgEl('path', { class: 'shape', d: 'M-20 10a10 10 0 0 1 0-20a13 13 0 0 1 22-6a11 11 0 0 1 18 8a9 9 0 0 1 0 18z' }, g);
        break;
      case 'pc':
        svgEl('rect', { class: 'shape', x: -15, y: -14, width: 30, height: 20, rx: 2 }, g);
        svgEl('path', { class: 'glyph', d: 'M0 6V12M-8 13H8' }, g);
        break;
      default:
        svgEl('rect', { class: 'shape', x: -16, y: -16, width: 32, height: 32, rx: 8 }, g);
    }
  }

  const STATUS_TEXT = { online: '运行中', stopped: '已停止', na: '无 console', starting: '启动中',
    stopping: '停止中', error: '异常', unknown: '状态未知', initializing: '系统初始化中', start_failed: '启动失败' };
  // 实验后端（/api/topology/current 的 lab）：kind ensp / enspro；power = 能否由 ainet 启停设备
  const labInfo = () => (state.cur && state.cur.lab) || { kind: 'ensp', power: false, label: 'eNSP' };
  const powerPending = new Set();   // 已发出启停请求、还在等接口返回的设备（console 键）
  const consoleLabel = (d) => (labInfo().kind === 'enspro' ? `设备 ${ck(d)}` : `console:${ck(d)}`);
  // 统一设备标识（后端 lab.py）：能开终端的设备 console > 0（eNSP 经典版 = com_port，eNSP Pro = deviceId）
  const ck = (d) => Number(d.console ?? d.com_port ?? 0);
  function devStatus(d) {
    return ck(d) > 0 ? (state.status[d.id] || 'stopped') : 'na';
  }
  // 练习 / 考试进行中只开放当前题的可访问设备（后端白名单同步限制）
  function practiceAllows(d) {
    const ex = state.exam;
    if (ex && ex.active) {
      const q = ex.question;
      return !!(q && q.devices && q.devices.some((x) => x.port === ck(d)));
    }
    const p = state.practice;
    if (!p || !p.active) return true;
    return p.devices.some((x) => x.port === ck(d));
  }
  function busyLabel() { return state.exam && state.exam.active ? '考试' : '练习'; }
  function canOpen(d) {
    const st = devStatus(d);
    return (st === 'online' || st === 'initializing') && practiceAllows(d);
  }

  // 图层：背景网格 → 图形（按文件顺序）→ 连线 → 设备 → 注释文字
  const layers = {};

  function drawShapes(t, g) {
    for (const s of t.shapes || []) {
      const paint = s.fill ? { fill: s.color, stroke: 'none' }
        : { fill: 'none', stroke: s.color, 'stroke-width': s.width, 'vector-effect': 'non-scaling-stroke' };
      if (s.kind === 'line') {
        svgEl('line', { x1: s.x1, y1: s.y1, x2: s.x2, y2: s.y2, stroke: s.color,
          'stroke-width': s.width, 'vector-effect': 'non-scaling-stroke' }, g);
      } else if (s.kind === 'rect') {
        svgEl('rect', { x: s.x, y: s.y, width: s.w, height: s.h, ...paint }, g);
      } else if (s.kind === 'ellipse') {
        svgEl('ellipse', { cx: s.x + s.w / 2, cy: s.y + s.h / 2, rx: s.w / 2, ry: s.h / 2, ...paint }, g);
      }
    }
  }

  function drawNotes(t, g) {
    for (const n of t.notes) {
      const lines = n.lines || n.text.split('\n');
      const fs = 12 * (n.size || 100) / 100, lh = fs * 1.35;
      const ng = svgEl('g', { class: 'note-g' }, g);
      const bg = svgEl('rect', { class: 'note-bg', x: n.x, y: n.y, width: Math.max(n.w, 10),
        height: Math.max(n.h, lines.length * lh + 2), fill: n.bg || '#90ee90' }, ng);
      const te = svgEl('text', { class: 'note', x: n.x + 2, y: n.y + fs, fill: n.color || '#000',
        'font-size': fs, 'font-family': `${n.font ? `"${n.font}", ` : ''}Consolas, monospace`,
        'xml:space': 'preserve' }, ng);
      lines.forEach((line, i) => {
        const ts = svgEl('tspan', { x: n.x + 2, dy: i ? lh : 0 }, te);
        ts.textContent = line;
      });
      // 文件里的框按 eNSP 字体量好；本机字体更宽时按实际文字撑大底色
      try {
        const bb = te.getBBox();
        if (bb.width + 4 > n.w) bg.setAttribute('width', bb.width + 4);
      } catch (e) { /* 面板折叠时无法测量 */ }
    }
  }

  // 网格只画可视范围内的线（平移 / 缩放后重画），线宽与虚线不随缩放变化
  let gridKey = '';
  function drawGrid() {
    const g = layers.grid, vb = view.box;
    if (!g || !vb) return;
    // preserveAspectRatio meet：可视范围比 viewBox 宽或高，按面板尺寸换算
    const r = svg.getBoundingClientRect();
    const sc = Math.min(r.width / vb.w, r.height / vb.h) || 1;
    const vw = Math.max(vb.w, r.width / sc), vh = Math.max(vb.h, r.height / sc);
    const b = { x: vb.x - (vw - vb.w) / 2, y: vb.y - (vh - vb.h) / 2, w: vw, h: vh };
    let step = GRID;
    while ((b.w / step) + (b.h / step) > 160) step *= 2;
    const x0 = Math.floor(b.x / step) * step, x1 = b.x + b.w;
    const y0 = Math.floor(b.y / step) * step, y1 = b.y + b.h;
    const key = [step, x0, y0, Math.ceil(x1 / step), Math.ceil(y1 / step)].join(',');
    if (key === gridKey) return;
    gridKey = key;
    g.innerHTML = '';
    let d = '';
    for (let x = x0; x <= x1; x += step) d += `M${x} ${y0}V${y1 + step}`;
    for (let y = y0; y <= y1; y += step) d += `M${x0} ${y}H${x1 + step}`;
    svgEl('path', { class: 'grid', d }, g);
  }

  function renderTopo() {
    svg.innerHTML = '';
    gridKey = '';
    const t = state.topo;
    // 考试模板设定不可看拓扑：后端只给设备清单（终端标签要用），这里整块打码
    const hidden = !!(t && t.hidden);
    $('topo-empty').style.display = t && !hidden ? 'none' : 'flex';
    $('topo-empty').textContent = hidden ? '本场考试不提供拓扑图：只列出可登录的设备，连线已隐藏。点下面的设备打开终端。'
      : (state.cur ? state.cur.message : '正在检测运行中的设备…');
    $('topo-body').classList.toggle('masked', hidden);
    $('topo-title').textContent = t ? (hidden ? '拓扑（考试中隐藏）' : `拓扑 · ${t.name}`) : '拓扑';
    renderDevList();
    svg.classList.toggle('has-topo', !!t && !hidden);
    if (!t || hidden) return;
    const byId = Object.fromEntries(t.devices.map((d) => [d.id, d]));

    layers.grid = svgEl('g', { class: 'grid-layer' }, svg);
    const gShapes = svgEl('g', { class: 'shapes' }, svg);
    const gLinks = svgEl('g', { class: 'links' }, svg);
    const gNodes = svgEl('g', { class: 'nodes' }, svg);
    const gNotes = svgEl('g', { class: 'notes' }, svg);
    drawShapes(t, gShapes);

    // 同一对设备间的平行链路：按序号沿法线错开
    const pairKey = (l) => [l.src_id, l.dst_id].sort().join('|');
    const pairTotal = {}, pairSeen = {};
    for (const l of t.links) pairTotal[pairKey(l)] = (pairTotal[pairKey(l)] || 0) + 1;

    for (const l of t.links) {
      const a = byId[l.src_id], b = byId[l.dst_id];
      if (!a || !b) continue;
      let [x1, y1] = center(a), [x2, y2] = center(b);
      const key = pairKey(l), n = pairTotal[key], i = pairSeen[key] = (pairSeen[key] ?? -1) + 1;
      if (n > 1) {
        const len0 = Math.hypot(x2 - x1, y2 - y1) || 1;
        // 法线方向按无序设备对固定，避免 src/dst 顺序不同导致方向相反
        const sign = l.src_id < l.dst_id ? 1 : -1;
        const off = (i - (n - 1) / 2) * 12;
        const nx = (-(y2 - y1) / len0) * off * sign, ny = ((x2 - x1) / len0) * off * sign;
        x1 += nx; y1 += ny; x2 += nx; y2 += ny;
      }
      const g = svgEl('g', { class: 'link' }, gLinks);
      svgEl('line', { x1, y1, x2, y2 }, g);
      const len = Math.hypot(x2 - x1, y2 - y1) || 1;
      const k = Math.min(0.4, 56 / len); // 标注离节点中心约 56 单位，避开设备名
      for (const [lx, ly, text] of [
        [x1 + (x2 - x1) * k, y1 + (y2 - y1) * k, l.src_label],
        [x2 - (x2 - x1) * k, y2 - (y2 - y1) * k, l.dst_label],
      ]) {
        const te = svgEl('text', { class: 'if-label', x: lx, y: ly, 'text-anchor': 'middle', 'dominant-baseline': 'middle' }, g);
        te.textContent = abbrIf(text);
        const title = svgEl('title', {}, te);
        title.textContent = text;
      }
    }

    for (const d of t.devices) {
      const [cx, cy] = center(d);
      const g = svgEl('g', { class: 'node', transform: `translate(${cx} ${cy})`, 'data-id': d.id, 'data-name': d.name }, gNodes);
      drawGlyph(g, d.kind);
      svgEl('circle', { class: 'st-dot', cx: 16, cy: -14, r: 4.5 }, g);
      const label = svgEl('text', { y: 34 }, g);
      label.textContent = d.name;
      svgEl('title', {}, g);
      if (ck(d) > 0) {
        // 启停按钮（只有支持启停的实验后端显示，见 updateNodes）
        const pw = svgEl('g', { class: 'power', transform: 'translate(0 52)' }, g);
        svgEl('rect', { x: -26, y: -9, width: 52, height: 17, rx: 3 }, pw);
        svgEl('text', { y: 4 }, pw);
        pw.addEventListener('click', (e) => { e.stopPropagation(); if (!pan.moved) powerClick(d); });
        pw.addEventListener('mousedown', (e) => e.stopPropagation());
      }
      if (d.kind === 'pc') {
        // PC：点击弹出 PC 设置气泡（路由器 / 交换机不变：点击打开终端）
        g.addEventListener('click', (e) => { e.stopPropagation(); if (!pan.moved) openPcPop(d, g); });
      } else if (ck(d)) {
        g.addEventListener('click', () => { if (!pan.moved && canOpen(d)) openTab(d); });
      }
    }
    drawNotes(t, gNotes);
    updateNodes();
    fitView();
  }

  // 考试不可看拓扑时：左栏只列出设备（可开终端的才能点）
  function renderDevList() {
    const box = $('topo-devlist');
    const t = state.topo;
    if (!t || !t.hidden) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    // 只列出本题可登录的设备（练习 / 考试白名单）；未运行的灰显
    const devs = t.devices.filter((d) => ck(d) > 0 && practiceAllows(d));
    const key = devs.map((d) => `${d.name}:${ck(d)}:${canOpen(d)}`).join(',');
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = '';
    for (const d of devs) {
      const b = document.createElement('button');
      b.className = 'topo-dev';
      b.dataset.port = ck(d);
      b.textContent = d.name;
      if (d.kind === 'pc') {
        // PC：与拓扑图上一样弹出 PC 设置气泡（气泡里有「打开终端」）；隐藏拓扑的考试里也能改 PC 地址
        b.title = `${d.name}：PC 设置 / 打开终端`;
        b.onclick = (e) => { e.stopPropagation(); openPcPop(d, b); };
      } else {
        b.disabled = !canOpen(d);
        b.title = canOpen(d) ? `打开 ${d.name} 的终端` : `${busyLabel()}进行中，本题不开放此设备`;
        b.onclick = () => { if (canOpen(d)) openTab(d); };
      }
      box.appendChild(b);
    }
  }
  function renderTopoFoot() {
    const lab = labInfo();
    const p = state.practice;
    let text = '';
    if (state.exam && state.exam.active) text = '';
    else if (p && p.active) {
      text = `本题涉及：${p.devices.map((x) => x.name).join('、')}` + (lab.power ? '<br>本题设备不可停止' : '');
    } else if (state.topo && !state.topo.hidden) {
      text = '点设备打开终端 · 橙框 = 当前终端，蓝框 = 已打开' +
        (lab.power ? '<br>虚线 = 已停止（可点「▶ 启动」）' : `<br>虚线 = 已停止（请在 ${esc(lab.label || 'eNSP')} 中启动）`);
    }
    $('topo-foot').innerHTML = text;
  }

  function updateNodes() {
    renderDevList();
    renderTopoFoot();
    if (!state.topo) return;
    for (const g of svg.querySelectorAll('.node')) {
      const d = state.topo.devices.find((x) => x.id === g.dataset.id);
      if (!d) continue;
      const st = devStatus(d);
      g.setAttribute('class', `node ${st}` + (canOpen(d) || d.kind === 'pc' ? ' clickable' : '') +
        (state.tabs.has(ck(d)) ? ' opened' : '') + (state.active === ck(d) ? ' current' : ''));
      g.dataset.status = st;
      const lab = labInfo();
      const prog = (state.cur && state.cur.progress && state.cur.progress[d.id]);
      g.querySelector('title').textContent = `${d.name}  ${d.model}` +
        (ck(d) ? `  ${consoleLabel(d)}` : '') + `  ${STATUS_TEXT[st] || st}` +
        (st === 'online' ? '（刚启动的设备 console 可能尚未就绪）' : '') +
        (st === 'stopped' ? (lab.power ? '（点下方「启动」）' : `（请先在 ${lab.label || 'eNSP'} 中启动）`) : '') +
        (st === 'online' && !practiceAllows(d) ? `（${busyLabel()}进行中，本题不开放此设备）` : '');
      const pw = g.querySelector('.power');
      if (pw) {
        let text = '', cls = 'power';
        if (!lab.power || state.topo.hidden) cls += ' hidden';
        else if (st === 'online') text = '■ 停止';
        else if (st === 'stopped' || st === 'error') text = '▶ 启动';
        else if (st === 'start_failed') text = '▶ 重新启动';
        else if (st === 'starting') { text = prog || '启动中…'; cls += ' busy'; }
        else if (st === 'initializing') { text = prog || '初始化中…'; cls += ' busy'; }
        else if (st === 'stopping') { text = prog || '停止中…'; cls += ' busy'; }
        else cls += ' hidden';
        if (powerPending.has(ck(d)) && !cls.includes('busy')) { text = '请求中…'; cls += ' busy'; }
        pw.setAttribute('class', cls + (st === 'online' ? ' stop' : ''));
        pw.querySelector('text').textContent = text;
        let w = 52;      // 按钮宽度随文字（「启动中 35s（启动系统）」比「■ 停止」长）
        try { w = Math.max(52, pw.querySelector('text').getComputedTextLength() + 14); } catch (e) { /* 未渲染 */ }
        const rc = pw.querySelector('rect');
        rc.setAttribute('x', -w / 2);
        rc.setAttribute('width', w);
      }
    }
  }

  // ---------- 设备启停（eNSP Pro） ----------
  let fastPoll = null;
  function anyTransient() {
    return Object.values(state.status || {}).some((s) => s === 'starting' || s === 'stopping' || s === 'initializing');
  }
  function scheduleFastPoll() {
    if (fastPoll) return;
    fastPoll = setInterval(async () => {
      await refreshStatus();
      if (!anyTransient() && !powerPending.size) { clearInterval(fastPoll); fastPoll = null; }
    }, 2000);
  }
  async function powerClick(d) {
    const st = devStatus(d);
    const action = st === 'online' ? 'stop' : (st === 'stopped' || st === 'error' || st === 'start_failed') ? 'start' : null;
    if (!action || powerPending.has(ck(d))) return;
    const verb = action === 'start' ? '启动' : '停止';
    const warn = action === 'stop' ? '\n停止后设备上未保存的运行状态会丢失，已打开的终端会断开。' : '\n启动约需 1~2 分钟。';
    if (!confirm(`确定${verb}设备 ${d.name}？${warn}`)) return;
    powerPending.add(ck(d));
    updateNodes();
    try {
      await api('POST', `api/lab/devices/${action}`, { devices: [ck(d)] });
      msg(`已请求${verb} ${d.name}`);
    } catch (e) {
      msg(`${verb} ${d.name} 失败：${e.message}`, true);
    } finally {
      powerPending.delete(ck(d));
      await refreshStatus();
      scheduleFastPoll();
    }
  }

  // ---------- PC 设置气泡（eNSP Pro：IP / 掩码 / 网关 / DNS；eNSP 经典版只提示在 eNSP 软件中设置） ----------
  const pcPop = { dev: null, busy: false, seq: 0 };
  function pcMsg(text, cls) {
    $('pc-msg').textContent = text || '';
    $('pc-msg').className = `pc-msg ${cls || ''}`;
  }
  function pcInputs(enabled) {
    for (const id of ['pc-ip', 'pc-mask', 'pc-gw', 'pc-dns']) $(id).disabled = !enabled;
    $('pc-apply').disabled = !enabled || pcPop.busy;
  }
  function placePcPop(g) {
    const pop = $('pc-pop'), box = $('topo-body');
    const br = box.getBoundingClientRect(), nr = g.getBoundingClientRect();
    const w = pop.offsetWidth || 260, h = pop.offsetHeight || 260;
    let x = nr.right - br.left + 8, y = nr.top - br.top - 10;
    if (x + w > br.width - 4) x = nr.left - br.left - w - 8;            // 右侧放不下放左侧
    if (x < 4) { x = Math.max(4, Math.min(br.width - w - 4, nr.left - br.left)); y = nr.bottom - br.top + 6; }
    y = Math.max(4, Math.min(y, br.height - h - 4));
    pop.style.left = `${Math.round(x)}px`;
    pop.style.top = `${Math.round(y)}px`;
  }
  function closePcPop() {
    $('pc-pop').hidden = true;
    pcPop.dev = null;
  }
  async function openPcPop(d, g) {
    const seq = ++pcPop.seq;
    pcPop.dev = d;
    const lab = labInfo();
    const st = devStatus(d);
    $('pc-name').textContent = d.name;
    $('pc-status').textContent = lab.kind === 'enspro' ? (STATUS_TEXT[st] || st) : '由 eNSP 模拟';
    $('pc-status').className = `pc-st st-${st}`;
    for (const id of ['pc-ip', 'pc-mask', 'pc-gw', 'pc-dns']) $(id).value = '';
    pcMsg('');
    const note = $('pc-note');
    note.hidden = true;
    $('pc-term').disabled = !canOpen(d);
    $('pc-term').title = canOpen(d) ? `打开 ${d.name} 的终端` : (ck(d) > 0 ? '设备未运行或本题不开放' : '这台 PC 没有可连接的终端');
    $('pc-pop').hidden = false;
    if (g) placePcPop(g);
    if (lab.kind !== 'enspro') {
      note.hidden = false;
      note.textContent = `请在 eNSP 软件中双击 ${d.name} 设置（eNSP 经典版的 PC 由 eNSP 模拟，ainet 无法配置）`;
      pcInputs(false);
      return;
    }
    if (!practiceAllows(d)) {
      note.hidden = false;
      note.textContent = `${busyLabel()}进行中，本题不开放 ${d.name}`;
      pcInputs(false);
      return;
    }
    if (st !== 'online') {
      note.hidden = false;
      note.textContent = `${d.name} 没有运行（${STATUS_TEXT[st] || st}），启动后才能设置`;
      pcInputs(false);
      return;
    }
    pcInputs(false);
    pcMsg('读取当前地址…');
    try {
      const r = await api('GET', `api/lab/devices/${ck(d)}/host`);
      if (seq !== pcPop.seq) return;
      $('pc-ip').value = r.config.ip || '';
      $('pc-mask').value = r.config.mask || '';
      $('pc-gw').value = r.config.gateway || '';
      $('pc-dns').value = r.config.dns || '';
      pcPop.dnsSupported = !!r.dns_supported;
      pcInputs(true);
      const busy = state.exam && state.exam.active ? '考试' : state.practice && state.practice.active ? '练习' : '';
      pcMsg(busy ? `${busy}中的修改会写进操作记录，结束时随复位还原` : '');
    } catch (e) {
      if (seq !== pcPop.seq) return;
      pcMsg(`读取失败：${e.message}`, 'err');
    }
  }
  $('pc-close').onclick = closePcPop;
  $('pc-term').onclick = () => {
    const d = pcPop.dev;
    if (d && canOpen(d)) { openTab(d); closePcPop(); }
  };
  $('pc-form').onsubmit = async (e) => {
    e.preventDefault();
    const d = pcPop.dev;
    if (!d || pcPop.busy) return;
    const body = { ip: $('pc-ip').value.trim(), mask: $('pc-mask').value.trim(), gateway: $('pc-gw').value.trim() };
    const dns = $('pc-dns').value.trim();
    if (dns || pcPop.dnsSupported) body.dns = dns;
    pcPop.busy = true;
    pcInputs(false);
    pcMsg('正在应用…PC 的命令行会重启，请稍候（约几秒到 20 秒）');
    const seq = pcPop.seq;
    try {
      const r = await api('POST', `api/lab/devices/${ck(d)}/host`, body);
      if (seq !== pcPop.seq) return;
      $('pc-mask').value = r.config.mask || '';
      pcMsg(`已生效：${r.config.ip || '无地址'}${r.config.mask ? ` / ${r.config.mask}` : ''}` +
        `${r.config.gateway ? `，网关 ${r.config.gateway}` : ''}${r.config.dns ? `，DNS ${r.config.dns}` : ''}`, 'ok');
      msg(`${d.name} 的地址已生效`);
    } catch (err) {
      if (seq === pcPop.seq) pcMsg(`没有生效：${err.message}`, 'err');
    } finally {
      pcPop.busy = false;
      if (seq === pcPop.seq && !$('pc-pop').hidden) pcInputs(true);
    }
  };

  function fitView() {
    const t = state.topo;
    if (!t || !t.devices.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const grow = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
    for (const d of t.devices) {
      const [cx, cy] = center(d);
      grow(cx - 45, cy - 30); grow(cx + 45, cy + (labInfo().power ? 66 : 45));
    }
    for (const n of t.notes) { grow(n.x, n.y); grow(n.x + n.w, n.y + n.h); }
    for (const s of t.shapes || []) {
      if (s.kind === 'line') { grow(s.x1, s.y1); grow(s.x2, s.y2); }
      else { grow(s.x, s.y); grow(s.x + s.w, s.y + s.h); }
    }
    const m = 30;
    const b = { x: x0 - m, y: y0 - m, w: x1 - x0 + 2 * m, h: y1 - y0 + 2 * m };
    view.fitBox = b;
    setViewBox({ ...b });
  }

  // 滚轮缩放（以鼠标位置为中心）+ 拖拽平移（超过阈值才算拖，避免吞掉节点 click）
  function svgPoint(evt) {
    const r = svg.getBoundingClientRect();
    const b = view.box;
    // preserveAspectRatio 默认 xMidYMid meet
    const s = Math.min(r.width / b.w, r.height / b.h);
    const ox = (r.width - b.w * s) / 2, oy = (r.height - b.h * s) / 2;
    return { x: b.x + (evt.clientX - r.left - ox) / s, y: b.y + (evt.clientY - r.top - oy) / s, s };
  }
  svg.addEventListener('wheel', (e) => {
    if (!view.box) return;
    e.preventDefault();
    const p = svgPoint(e);
    const f = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    const b = view.box;
    setViewBox({ x: p.x - (p.x - b.x) * f, y: p.y - (p.y - b.y) * f, w: b.w * f, h: b.h * f });
  }, { passive: false });

  const pan = { down: false, moved: false, sx: 0, sy: 0, box: null, s: 1 };
  svg.addEventListener('mousedown', (e) => {
    if (!view.box || e.button !== 0) return;
    pan.down = true; pan.moved = false; pan.sx = e.clientX; pan.sy = e.clientY;
    pan.box = { ...view.box }; pan.s = svgPoint(e).s;
  });
  window.addEventListener('mousemove', (e) => {
    if (!pan.down) return;
    const dx = e.clientX - pan.sx, dy = e.clientY - pan.sy;
    if (!pan.moved && Math.hypot(dx, dy) < 4) return;
    pan.moved = true;
    svg.classList.add('dragging');
    setViewBox({ ...pan.box, x: pan.box.x - dx / pan.s, y: pan.box.y - dy / pan.s });
  });
  window.addEventListener('mouseup', () => {
    pan.down = false;
    svg.classList.remove('dragging');
    // click 在 mouseup 之后派发；下一轮再清 moved
    setTimeout(() => { pan.moved = false; }, 0);
  });
  svg.addEventListener('dblclick', fitView);
  $('btn-fit').onclick = fitView;
  $('chk-if').onchange = (e) => svg.classList.toggle('hide-if', !e.target.checked);
  // 面板尺寸变化会改变可视范围（preserveAspectRatio meet），网格跟着补齐
  new ResizeObserver(() => { gridKey = ''; drawGrid(); }).observe($('topo-body'));

  // ---------- 拓扑数据：按 eNSP 中运行的设备实时确定 ----------
  // 环境与拓扑不在顶栏常驻：题名气泡里显示「模拟环境」「拓扑（沙箱）· 运行 x/y 台」，顶栏题名按钮上只留状态点
  const PLATFORM = { yunshan: 'YunShan', vrp5: 'VRP5' };
  function showState(cur) {
    const el = $('topo-status');
    const lab = cur.lab || labInfo();
    const t = 'topology' in cur ? cur.topology : state.topo;
    let run = cur.running, total = cur.total;
    if (run == null || total == null) {
      const devs = (t && t.devices || []).filter((d) => ck(d) > 0);
      total = devs.length;
      run = devs.filter((d) => (cur.devices || {})[d.id] === 'online').length;
    }
    const parts = [];
    if (t && !t.hidden && t.name) parts.push(`${lab.kind === 'enspro' ? '沙箱' : '拓扑'} ${t.name}`);
    if (t) parts.push(`运行 ${run}/${total} 台`);
    else parts.push(cur.message || '未检测到拓扑');
    el.textContent = parts.join(' · ');
    const env = cur.env || state.env || {};
    $('tp-env').innerHTML = `<span class="env-dot st-${esc(cur.state)}"></span>${esc(env.label || lab.label || 'eNSP')}` +
      ` · ${esc(PLATFORM[env.platform || lab.platform] || env.platform || lab.platform || '')}`;
    $('env-dot').className = `env-dot st-${cur.state}`;
    $('env-dot').title = `模拟环境：${cur.message || cur.state}`;
    $('btn-relogin').hidden = !cur.relogin;
    el.title = `${cur.message || ''}\n检测方法：${cur.method}` + (cur.path ? `\n文件：${cur.path}` : '') +
      ((cur.duplicates || []).length ? `\n相同设备的其他副本：${cur.duplicates.map((x) => x.path).join('，')}` : '');
  }

  // 模拟环境切换（spec §5.2，只在课程页进行）：切换代数 env.gen 变了 = 环境被切换（别的标签页 / 课程页）
  function envChanged(env) {
    const n = state.tabs.size;
    for (const port of [...state.tabs.keys()]) closeTab(port);       // 旧环境的终端都已被服务端断开
    msg(`模拟环境已切换为 ${env.label}${env.sandbox != null ? `（沙箱 ${env.sandbox}）` : ''}` +
      (n ? `，已关闭 ${n} 个旧终端标签` : ''));
    if (state.mode === 'practice' && !(state.practice && state.practice.active) && !pr.summary) loadScenarios();
    if (state.mode === 'exam' && viewHooks.exam) viewHooks.exam();
    fire({ env });
  }

  // cur：/api/topology/current（带 topology）或 /api/topology/status（不带）
  function applyCurrent(cur) {
    if (cur.env) {
      // 环境 / 沙箱变了，或切换代数增加了才算切换（服务重启后 gen 归零，不当作切换）
      const was = state.env;
      state.env = cur.env;
      if (was && (was.key !== cur.env.key || was.sandbox !== cur.env.sandbox || cur.env.gen > was.gen)) envChanged(cur.env);
    }
    const changed = 'topology' in cur &&
      (!state.cur || cur.path !== state.cur.path || cur.mtime !== state.cur.mtime ||
       !!(cur.topology && cur.topology.hidden) !== !!(state.topo && state.topo.hidden));
    const { topology, ...rest } = cur;
    state.cur = rest;
    state.status = cur.devices || {};
    if (changed) {
      state.topo = topology;
      renderTopo();           // 拓扑文件变了才重绘（会复位缩放）
    }
    showState(cur);
    if (changed) {
      /* 已重绘 */
    } else {
      updateNodes();
      if (!state.topo) $('topo-empty').textContent = cur.message;
    }
    refreshOpenSelect();
    for (const tab of state.tabs.values()) updateTabLabel(tab);
  }

  let polling = null;
  async function refreshStatus(force) {
    if (polling) return polling;
    polling = (async () => {
      try {
        const st = await api('GET', 'api/topology/status');
        const same = state.cur && st.path === state.cur.path && st.mtime === state.cur.mtime;
        applyCurrent(force || !same ? await api('GET', 'api/topology/current') : st);
        msg('');
      } catch (e) { msg(`状态刷新失败：${e.message}`, true); }
    })();
    try { await polling; } finally { polling = null; }
  }

  $('btn-relogin').onclick = async () => {
    if (!confirm('重新登录 eNSP Pro？\n同一账号只能有一个会话：如果你正在别处（如浏览器里的 eNSP Pro 网页）使用它，那边会被挤掉。')) return;
    try {
      await api('POST', 'api/lab/relogin');
      await refreshStatus(true);
      for (const tab of state.tabs.values()) {
        const device = tabDevice(tab);
        if (device && canOpen(device)) connectTab(tab);
      }
      msg('已重新登录 eNSP Pro，终端正在重连');
    } catch (e) { msg(`重新登录失败：${e.message}`, true); }
  };
  $('btn-refresh').onclick = async () => { msg('刷新中…'); await refreshStatus(); if (!$('topo-msg').classList.contains('err')) msg('状态已刷新'); };

  function consoleTargets() {
    if (!state.topo) return [];
    return state.topo.devices.filter((d) => ck(d) > 0 && canOpen(d));
  }
  function refreshOpenSelect() {
    const sel = $('open-select');
    sel.innerHTML = '';
    sel.add(new Option('打开终端…', ''));
    for (const d of consoleTargets()) {
      sel.add(new Option(`${d.name}（${ck(d)}）`, ck(d)));
    }
  }
  $('open-select').onchange = (e) => {
    const port = Number(e.target.value);
    e.target.value = '';
    const d = consoleTargets().find((x) => ck(x) === port);
    if (d) openTab(d);
  };

  // ---------- 终端标签 ----------
  const enc = new TextEncoder();

  function deviceForPort(port) {
    return state.topo && state.topo.devices.find((d) => ck(d) === port);
  }
  // 标签对应的设备：按 GUID 匹配（console 端口在不同拓扑间复用）；?port= 打开的标签先按端口认领
  function tabDevice(tab) {
    const d = deviceForPort(tab.port);
    if (!d) return null;
    if (tab.devId == null) tab.devId = d.id;
    return d.id === tab.devId ? d : null;
  }

  function openTab(dev) {
    const port = ck(dev);
    const old = state.tabs.get(port);
    if (old && !tabDevice(old)) closeTab(port); // 旧拓扑留下的同端口标签，换成新设备
    else if (old) { focusTab(port); return old; }
    const host = document.createElement('div');
    host.className = 'term-host';
    const tab = {
      port, name: dev.name, devId: dev.id || null, host,
      term: new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000,
        theme: { background: '#101923', foreground: '#d7e1ec', cursor: '#d7e1ec' } }),
      fit: new FitAddon.FitAddon(), ws: null, status: 'closed', opened: false, el: null,
    };
    tab.term.loadAddon(tab.fit);
    tab.term.onData((data) => {
      if (tab.ws && tab.ws.readyState === WebSocket.OPEN) tab.ws.send(enc.encode(data));
    });
    $('parking').appendChild(host);
    tab.el = buildTabEl(tab);
    state.tabs.set(port, tab);
    focusTab(port);
    connectTab(tab);
    refreshPaneSelects();
    updateNodes();
    return tab;
  }

  function buildTabEl(tab) {
    const el = document.createElement('div');
    el.className = 'tab';
    el.dataset.port = tab.port;
    el.innerHTML = '<span class="dot"></span><span class="nm"></span>' +
      '<button class="re" title="重连">↻</button><button class="x" title="关闭">×</button>';
    el.onclick = () => focusTab(tab.port);
    el.querySelector('.re').onclick = (e) => {
      e.stopPropagation();
      if (!tabDevice(tab)) return;
      connectTab(tab); focusTab(tab.port);
    };
    el.querySelector('.x').onclick = (e) => { e.stopPropagation(); closeTab(tab.port); };
    $('tab-list').appendChild(el);
    updateTabLabel(tab);
    return el;
  }

  // 状态点同时看 WebSocket 与设备状态：断开或设备已停止时不显示绿色
  function tabDot(tab, d) {
    if (!d) return ['gone', '设备不在当前拓扑中（不可用）'];
    if (tab.status === 'connecting') return ['connecting', '连接中'];
    if (tab.status !== 'open') return ['closed', '连接已断开'];
    const st = devStatus(d);
    if (st === 'online' || st === 'initializing') return ['open', '已连接'];
    return ['stopped', '设备已停止'];
  }

  function updateTabLabel(tab) {
    if (!tab.el) return;
    const d = tabDevice(tab);
    if (d) tab.name = d.name;
    const [cls, text] = tabDot(tab, d);
    tab.el.querySelector('.nm').textContent = tab.name;
    tab.el.title = `${tab.name}  console:${tab.port}  ${text}`;
    tab.el.querySelector('.dot').className = `dot ${cls}`;
    tab.el.classList.toggle('gone', !d);
    tab.el.querySelector('.re').disabled = !d;
  }

  function setTabStatus(tab, status) {
    tab.status = status;
    updateTabLabel(tab);
  }

  function connectTab(tab) {
    if (tab.ws) {
      tab.ws.onclose = null;
      try { tab.ws.close(); } catch (e) { /* ignore */ }
    }
    setTabStatus(tab, 'connecting');
    // 相对当前页面地址拼 WS 地址，部署在子路径（如反向代理的 /ainet/）下也能用
    const wsUrl = new URL(`ws/console/${tab.port}`, location.href);
    wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(wsUrl.href);
    ws.binaryType = 'arraybuffer';
    tab.ws = ws;
    const dec = new TextDecoder('utf-8'); // 每条连接独立的流式解码器，半个多字节字符不串到别的标签
    ws.onopen = () => { if (tab.ws === ws) setTabStatus(tab, 'open'); };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') tab.term.write(ev.data);
      else tab.term.write(dec.decode(new Uint8Array(ev.data), { stream: true }));
    };
    ws.onclose = () => {
      if (tab.ws !== ws) return;
      setTabStatus(tab, 'closed');
      tab.term.write('\r\n\x1b[33m[连接已断开，点标签上的 ↻ 重连]\x1b[0m\r\n');
    };
  }

  function closeTab(port) {
    const tab = state.tabs.get(port);
    if (!tab) return;
    if (tab.ws) { tab.ws.onclose = null; try { tab.ws.close(); } catch (e) { /* ignore */ } }
    tab.host.remove();
    // xterm 可能已排了下一帧的视口刷新（刚写入 / 清屏后），立即 dispose 会在那一帧抛
    // "Cannot read properties of undefined (reading 'dimensions')"：等两帧再释放
    requestAnimationFrame(() => requestAnimationFrame(() => {
      try { tab.term.dispose(); } catch (e) { /* ignore */ }
    }));
    tab.el.remove();
    state.tabs.delete(port);
    state.panes = state.panes.map((p) => (p === port ? null : p));
    if (state.active === port) state.active = null;
    // 空出来的屏补一个未显示的标签
    for (let i = 0; i < (state.split ? 2 : 1); i++) {
      if (state.panes[i] == null) {
        const rest = [...state.tabs.keys()].filter((p) => !state.panes.includes(p));
        if (rest.length) state.panes[i] = rest[rest.length - 1];
      }
    }
    layoutPanes();
    refreshPaneSelects();
    updateNodes();
  }

  // 把标签显示到当前聚焦的屏；已在另一屏显示时直接聚焦那一屏
  function focusTab(port) {
    if (!state.tabs.has(port)) return;
    const visible = state.split ? [0, 1] : [0];
    const at = visible.find((i) => state.panes[i] === port);
    if (at !== undefined) state.focusedPane = at;
    else state.panes[state.focusedPane] = port;
    state.active = port;
    layoutPanes();
    refreshPaneSelects();
    updateNodes();
    state.tabs.get(port).term.focus();
  }

  function assignPane(idx, port) {
    if (port != null) {
      const other = idx === 0 ? 1 : 0;
      // 同一终端不能同时在两屏：冲突时交换
      if (state.panes[other] === port) state.panes[other] = state.panes[idx];
    }
    state.panes[idx] = port;
    state.focusedPane = idx;
    if (port != null) state.active = port;
    layoutPanes();
    refreshPaneSelects();
  }

  const paneEls = [...document.querySelectorAll('.pane')];

  function layoutPanes() {
    const visible = state.split ? [0, 1] : [0];
    if (!state.split && state.active != null) state.panes[0] = state.active;
    $('panes').classList.toggle('split', state.split);
    paneEls[1].style.display = state.split ? '' : 'none';
    const shown = new Set(visible.map((i) => state.panes[i]).filter((p) => p != null));
    // 先把不显示的收进 parking（xterm 实例常驻，输出不丢）
    for (const tab of state.tabs.values()) {
      if (!shown.has(tab.port) && tab.host.parentElement !== $('parking')) $('parking').appendChild(tab.host);
    }
    visible.forEach((i) => {
      const body = paneEls[i].querySelector('.pane-body');
      const tab = state.tabs.get(state.panes[i]);
      body.querySelector('.pane-empty').style.display = tab ? 'none' : 'flex';
      paneEls[i].classList.toggle('focused', state.split && i === state.focusedPane);
      if (!tab) return;
      if (tab.host.parentElement !== body) body.appendChild(tab.host);
      if (!tab.opened) { tab.term.open(tab.host); tab.opened = true; }
    });
    for (const tab of state.tabs.values()) {
      tab.el.classList.toggle('active', tab.port === state.active);
      tab.el.classList.toggle('shown', shown.has(tab.port));
    }
    applyPaneRatio();
    const act = state.tabs.get(state.active);
    if (act) revealTab(act.el);
    refitAll();
  }

  function fitVisible() {
    for (const tab of state.tabs.values()) {
      if (tab.opened && tab.host.parentElement !== $('parking')) {
        try { tab.fit.fit(); } catch (e) { /* 容器尺寸为 0 时忽略 */ }
      }
    }
  }
  // 同一帧内多次触发（ResizeObserver、拖动、切换布局）只 fit 一次
  let fitRaf = 0;
  function refitAll() {
    if (!fitRaf) fitRaf = requestAnimationFrame(() => { fitRaf = 0; fitVisible(); });
  }

  // 标签栏单行横向滚动：只滚 #tab-list 自身，不用 scrollIntoView（会连带滚动外层容器）
  function revealTab(el) {
    const list = $('tab-list');
    const lr = list.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (r.left < lr.left) list.scrollLeft -= lr.left - r.left;
    else if (r.right > lr.right) list.scrollLeft += r.right - lr.right;
  }
  $('tab-list').addEventListener('wheel', (e) => {
    const list = e.currentTarget;
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || list.scrollWidth <= list.clientWidth) return;
    e.preventDefault();
    list.scrollLeft += e.deltaY;
  }, { passive: false });

  // ---------- 面板宽度、折叠与分栏滑块 ----------
  // 拓扑面板 / 右侧面板：像素宽度；终端左右分屏：左屏比例。都存 localStorage（读写 try/catch，不可用时用默认值）。
  const store = {
    get(k) { try { return localStorage.getItem(`ainet.ui.${k}`); } catch (e) { return null; } },
    set(k, v) {
      try {
        if (v == null) localStorage.removeItem(`ainet.ui.${k}`);
        else localStorage.setItem(`ainet.ui.${k}`, String(v));
      } catch (e) { /* 存储不可用：只在本页有效 */ }
    },
  };
  const MIN_TERM = 260, RAIL = 48;
  const PANEL = { topo: { def: 220, min: 180, max: 720 }, side: { def: 300, min: 280, max: 760 } };
  const widths = {};
  for (const k of Object.keys(PANEL)) {
    const v = parseFloat(store.get(`${k}W`));
    widths[k] = v >= PANEL[k].min && v <= PANEL[k].max ? v : PANEL[k].def;
  }
  const ratios = { panes: (() => { const v = parseFloat(store.get('panes')); return v > 0 && v < 1 ? v : 0.5; })() };
  const body = document.body;
  const isNarrow = () => body.classList.contains('narrow');
  const topoOpen = () => !body.classList.contains('topo-collapsed');
  // 右侧面板是否占用主区宽度（窄屏是浮层抽屉，考试作答中不出现）
  const sideInline = () => !body.classList.contains('exam-active') && !isNarrow() &&
    !body.classList.contains('side-collapsed');
  const sideRail = () => !body.classList.contains('exam-active') && (isNarrow() || body.classList.contains('side-collapsed'));

  // 按主区宽度收紧两侧面板，保证终端区至少 MIN_TERM（分屏时两倍）：先收右侧，再收拓扑
  function effWidths() {
    const mainW = $('main').clientWidth || window.innerWidth;
    const need = state.split && window.innerWidth > 700 ? 2 * MIN_TERM : MIN_TERM;
    let topo = topoOpen() ? widths.topo : 0;
    let side = sideInline() ? widths.side : 0;
    const rails = (topoOpen() ? 0 : RAIL) + (sideRail() ? RAIL : 0);
    let over = topo + side + rails + need - mainW;
    if (over > 0 && side) { const d = Math.min(over, side - PANEL.side.min); side -= d; over -= d; }
    if (over > 0 && topo) { const d = Math.min(over, topo - PANEL.topo.min); topo -= d; over -= d; }
    return { topo, side, mainW, need, rails };
  }
  function applyWidths() {
    const e = effWidths();
    if (e.topo) $('topo-panel').style.width = `${e.topo}px`;
    $('side').style.width = isNarrow() ? '' : (e.side ? `${e.side}px` : `${widths.side}px`);
    applyPaneRatio();
    refitAll();
  }
  // 兼容旧接口名（e2e / 调试脚本）
  const applyTopoRatio = applyWidths;
  function applyPaneRatio() {
    paneEls[0].style.flexBasis = state.split ? `${(ratios.panes * 100).toFixed(2)}%` : '';
    paneEls[0].style.flexGrow = state.split ? '0' : '';
  }

  // 通用拖动：onMove(clientX) 在每帧最多调一次；onEnd 保存；dblclick 恢复默认
  function makeDrag(el, onMove, onEnd, onReset) {
    let raf = 0, lastX = 0;
    const update = () => { raf = 0; onMove(lastX); fitVisible(); };
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add('dragging');
      body.classList.add('resizing');
      lastX = e.clientX;
    });
    el.addEventListener('pointermove', (e) => {
      if (!el.classList.contains('dragging')) return;
      lastX = e.clientX;
      if (!raf) raf = requestAnimationFrame(update);
    });
    const end = () => {
      if (!el.classList.contains('dragging')) return;
      if (raf) { cancelAnimationFrame(raf); update(); }
      el.classList.remove('dragging');
      body.classList.remove('resizing');
      onEnd();
      refitAll();
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('lostpointercapture', end);
    el.addEventListener('dblclick', () => { onReset(); refitAll(); });
  }
  function dragPanel(key, px) {
    const e = effWidths();
    const other = key === 'topo' ? e.side : e.topo;
    const hi = Math.min(PANEL[key].max, e.mainW - other - e.rails - e.need);
    widths[key] = Math.round(Math.min(Math.max(px, PANEL[key].min), Math.max(PANEL[key].min, hi)));
    applyWidths();
  }
  makeDrag($('split-main'), (x) => dragPanel('topo', x - $('topo-panel').getBoundingClientRect().left),
    () => store.set('topoW', widths.topo), () => { widths.topo = PANEL.topo.def; store.set('topoW', null); applyWidths(); });
  makeDrag($('split-side'), (x) => dragPanel('side', $('side').getBoundingClientRect().right - x),
    () => store.set('sideW', widths.side), () => { widths.side = PANEL.side.def; store.set('sideW', null); applyWidths(); });
  makeDrag($('split-panes'), (x) => {
    const r = $('panes').getBoundingClientRect();
    if (!r.width) return;
    const v = Math.min(Math.max(x - r.left, MIN_TERM), Math.max(MIN_TERM, r.width - MIN_TERM));
    ratios.panes = v / r.width;
    applyPaneRatio();
  }, () => store.set('panes', ratios.panes.toFixed(4)), () => { ratios.panes = 0.5; store.set('panes', null); applyPaneRatio(); });

  // ---- 折叠 / 展开 ----
  function setTopoCollapsed(on) {
    body.classList.toggle('topo-collapsed', on);
    store.set('topoCollapsed', on ? 1 : null);
    applyWidths();
    gridKey = ''; drawGrid();
  }
  $('btn-collapse').onclick = () => setTopoCollapsed(true);
  $('btn-expand-topo').onclick = () => setTopoCollapsed(false);

  // 右侧面板页签：task（工单详情 / 练习、考试列表）/ agent
  const side = { tab: 'agent', hooks: [] };
  function sideTabs() {
    if (state.mode === 'lab') return ['agent'];
    if (state.mode === 'exam') return ['task'];
    return ['task', 'agent'];
  }
  function setSideTab(tab) {
    if (!sideTabs().includes(tab)) tab = sideTabs()[0];
    side.tab = tab;
    for (const b of document.querySelectorAll('#side-tabs [role=tab]')) {
      b.setAttribute('aria-selected', String(b.dataset.tab === tab));
    }
    $('side-task').hidden = tab !== 'task';
    $('side-agent').hidden = tab !== 'agent';
    for (const b of document.querySelectorAll('#side-rail .rail-btn')) b.classList.toggle('on', b.dataset.tab === tab);
    if (tab === 'agent' && agentVisible()) clearAgentDot();
    for (const fn of side.hooks) fn('tab', tab);
  }
  function setSideCollapsed(on) {
    if (isNarrow()) {                       // 窄屏：抽屉开关，不改宽屏的折叠记录
      body.classList.toggle('drawer-open', !on);
    } else {
      body.classList.toggle('side-collapsed', on);
      store.set('sideCollapsed', on ? 1 : null);
    }
    applyWidths();
    if (!on && side.tab === 'agent') clearAgentDot();
  }
  function openSide(tab) {
    if (tab) setSideTab(tab);
    setSideCollapsed(false);
  }
  const agentVisible = () => !body.classList.contains('exam-active') && side.tab === 'agent' &&
    (isNarrow() ? body.classList.contains('drawer-open') : !body.classList.contains('side-collapsed'));
  // 面板收起（或不在 Agent 页签）时有新回复：图标上显示圆点
  function notifyAgent() {
    if (agentVisible()) return;
    $('agent-dot').hidden = false;
    $('rail-label').textContent = state.mode === 'practice' ? '导师有新回复' : 'Agent 有新回复';
    $('st-agent').classList.add('has-new');
  }
  function clearAgentDot() {
    $('agent-dot').hidden = true;
    $('rail-label').textContent = '';
    $('st-agent').classList.remove('has-new');
  }
  for (const b of document.querySelectorAll('#side-tabs [role=tab]')) b.onclick = () => setSideTab(b.dataset.tab);
  for (const b of document.querySelectorAll('#side-rail .rail-btn')) b.onclick = () => openSide(b.dataset.tab);
  $('btn-side-collapse').onclick = () => setSideCollapsed(true);
  $('drawer-mask').onclick = () => setSideCollapsed(true);
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && body.classList.contains('drawer-open')) setSideCollapsed(true);
  });

  // 按实际工作区宽度决定抽屉模式，导航展开或收起时也重新计算。
  const narrowMain = () => $('main').clientWidth < 1050;
  function applyNarrow() {
    const narrow = narrowMain();
    body.classList.toggle('narrow', narrow);
    if (!narrow) body.classList.remove('drawer-open');
    applyWidths();
  }
  new ResizeObserver(applyNarrow).observe($('main'));

  // 初始折叠状态
  body.classList.toggle('topo-collapsed', store.get('topoCollapsed') === '1');
  body.classList.toggle('side-collapsed', store.get('sideCollapsed') === '1');
  body.classList.toggle('narrow', narrowMain());
  if (window.innerWidth < 700 && store.get('topoCollapsed') === null) body.classList.add('topo-collapsed');
  window.addEventListener('resize', applyNarrow);
  new ResizeObserver(() => { gridKey = ''; drawGrid(); }).observe($('topo-panel'));

  // 隐藏式滚动条：滚动中的容器短暂加 .scrolling，让滚动条在非悬停时也显示
  const scrollTimers = new WeakMap();
  document.addEventListener('scroll', (e) => {
    const el = e.target;
    if (!(el instanceof Element)) return;
    el.classList.add('scrolling');
    clearTimeout(scrollTimers.get(el));
    scrollTimers.set(el, setTimeout(() => el.classList.remove('scrolling'), 800));
  }, true);

  function refreshPaneSelects() {
    paneEls.forEach((pane, i) => {
      const sel = pane.querySelector('.pane-select');
      sel.innerHTML = '';
      sel.add(new Option('（空）', ''));
      for (const tab of state.tabs.values()) sel.add(new Option(`${tab.name}（${tab.port}）`, tab.port));
      sel.value = state.panes[i] != null ? String(state.panes[i]) : '';
    });
  }
  paneEls.forEach((pane, i) => {
    pane.querySelector('.pane-select').onchange = (e) => {
      assignPane(i, e.target.value ? Number(e.target.value) : null);
    };
    pane.addEventListener('mousedown', () => {
      if (state.split && state.focusedPane !== i) {
        state.focusedPane = i;
        if (state.panes[i] != null) state.active = state.panes[i];
        layoutPanes();
      }
    });
    new ResizeObserver(refitAll).observe(pane.querySelector('.pane-body'));
  });

  function setSplit(on) {
    state.split = on;
    $('btn-single').classList.toggle('on', !on);
    $('btn-split').classList.toggle('on', on);
    if (on) {
      // 右屏缺省显示另一个标签
      if (state.panes[1] == null || state.panes[1] === state.panes[0]) {
        const other = [...state.tabs.keys()].find((p) => p !== state.panes[0]);
        state.panes[1] = other != null ? other : null;
      }
    } else {
      state.focusedPane = 0;
    }
    layoutPanes();
    refreshPaneSelects();
  }
  $('btn-single').onclick = () => setSplit(false);
  $('btn-split').onclick = () => setSplit(true);

  // ---------- 顶栏模式（实验 / 练习 / 考试 / 档案） ----------
  const PR_POLL_MS = 2000;
  const pr = { view: 'lab', timer: null, poll: null, hints: [], summary: null, busy: false, t0: 0, elapsed0: 0,
    resetAid: null };   // resetAid：已结束、设备还在后台复位的那次练习
  const esc = (v) => String(v ?? '').replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtDur = (s) => {
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60), r = s % 60;
    return `${m < 10 ? '0' : ''}${m}:${r < 10 ? '0' : ''}${r}`;
  };
  const LEVEL = { 1: '入门', 2: '基础', 3: '进阶', 4: '困难', 5: '专家' };
  const MODE_LABEL = { lab: '实验', practice: '练习', exam: '考试' };

  // 考试页由 exam.js 渲染（切到考试模式时调 viewHooks.exam）；agent.js 关心模式 / 练习变化（viewHooks.change）
  const viewHooks = { exam: null, change: [] };
  const fire = (what) => { for (const fn of viewHooks.change) { try { fn(what); } catch (e) { console.error(e); } } };
  let busyOp = '';           // 正在进行的模式切换（结束练习 / 开练 / 开考），期间禁用模式按钮与「退出到课程」
  // 进行中的练习 / 考试
  function lockedMode() {
    if (state.exam && state.exam.active) return 'exam';
    if (state.practice && state.practice.active) return 'practice';
    return null;
  }
  // 模式按钮：考试中只能交卷（其他模式禁用）；没有课题（自由实验）时练习 / 考试不可用；切换动作见 requestMode
  function refreshLock() {
    const lock = lockedMode();
    body.classList.toggle('practice-active', lock === 'practice');
    body.classList.toggle('exam-active', lock === 'exam');
    for (const b of document.querySelectorAll('#mode-nav button')) {
      const m = b.dataset.mode;
      let why = '';
      if (lock === 'exam' && m !== 'exam') why = '考试中不能切换模式，交卷后才能切换';
      else if (!state.topic && m !== 'lab' && lock !== m) why = '自由实验没有课题：请回到课程页选择课题后再练习 / 考试';
      else if (busyOp && m !== state.mode) why = busyOp;
      b.disabled = !!why;
      b.title = why || { lab: '实验：不注入故障，自由操作', practice: '练习：注入本题故障，自动判定',
        exam: '考试：本题的单题考试（计时 15 分钟、无 Agent、无提示）' }[m];
    }
    const ex = $('btn-exit');
    ex.disabled = lock === 'exam' || !!busyOp;
    ex.title = lock === 'exam' ? '考试中不能退出到课程，请先交卷' : '回到课程选择页（换题、换环境、换拓扑）';
    if (lock && state.mode !== lock) setMode(lock);
    else applyWidths();
    renderTopic();
  }
  function setMode(m) {
    const lock = lockedMode();
    if (lock && m !== lock) m = lock;
    const was = state.mode;
    state.mode = pr.view = m;
    store.set('mode', m);
    for (const k of ['lab', 'practice', 'exam']) body.classList.toggle(`mode-${k}`, k === m);
    for (const b of document.querySelectorAll('#mode-nav button[data-mode]')) b.classList.toggle('on', b.dataset.mode === m);
    $('practice-body').hidden = m !== 'practice';
    $('exam-body').hidden = m !== 'exam';
    $('st-agent').textContent = m === 'practice' ? 'Agent 导师' : 'Agent';
    $('st-task').textContent = m === 'exam' ? '考试' : (state.practice && state.practice.active ? '工单详情' : '本题练习');
    if (m === 'lab') setSideTab('agent');
    else if (m === 'exam') { setSideTab('task'); if (viewHooks.exam) viewHooks.exam(); }
    else {
      if (was !== 'practice') setSideTab(state.practice && state.practice.active ? 'agent' : 'task');
      if (!(state.practice && state.practice.active) && !pr.summary) loadScenarios();
    }
    refreshLock();
    gridKey = ''; drawGrid();
    fire('mode');
  }
  // 兼容旧接口：setView('topo' | 'practice' | 'exam')
  const setView = (v) => setMode(v === 'topo' ? 'lab' : v);

  // ---- 模式切换（同一道题）----
  function setBusyOp(text) {
    busyOp = text || '';
    refreshLock();
  }
  // 后台复位（结束练习后设备在后台复位到开练前配置）：轮询 current 直到复位完成，返回最终结果（last_result）
  const RESET_POLL_MS = 1500;
  async function waitResetDone(maxMs = 300000) {
    const end = Date.now() + maxMs;
    while (Date.now() < end) {
      let cur = null;
      try { cur = await api('GET', 'api/practice/current'); } catch (e) { /* 下次再试 */ }
      if (cur && !cur.resetting) { pr.resetAid = null; return cur.last_result || null; }
      await new Promise((r) => setTimeout(r, RESET_POLL_MS));
    }
    return null;
  }
  // 结束进行中的练习（按放弃记录）：接口立即返回，设备在后台复位（终端在复位完成前不接受输入）。请求失败返回 false
  async function abandonPractice() {
    const p = state.practice;
    if (!p || !p.active) return true;
    prMsg('正在结束练习…', true);
    try {
      const r = await api('POST', 'api/practice/end');
      if (r.resetting) pr.resetAid = r.attempt_id;
      stopPracticeTimers();
      pr.summary = null;
      $('pr-banner').hidden = true;
      pr.hints = [];
      renderHints();
      state.practice = { active: false };
      refreshLock();
      renderHintButtons();
      updateNodes();
      refreshOpenSelect();
      fire('practice');
      prMsg('');
      return true;
    } catch (e) {
      prMsg(`结束练习失败：${e.message}`);
      return false;
    }
  }
  // 开考（本题的单题考试）：设备刚冷启动报 busy 时每 20 秒重试，最多 5 次
  async function startSingleExam() {
    const t = state.topic;
    const r = await window.LabSwitch.retryBusy(() => api('POST', 'api/exam/start_single', { scenario_id: t.id }), {
      onWait: (left, n) => msg(`设备还在准备配置，${left} 秒后第 ${n} 次重试开考…`),
    });
    msg('');
    if (window.__ainetExam) window.__ainetExam.apply(r);
    return r;
  }
  async function requestMode(m) {
    if (busyOp) return;
    const lock = lockedMode();
    if (lock === 'exam') { msg('考试中不能切换模式，交卷后才能切换', true); return; }
    if (m === lock) return;
    const t = state.topic;
    if (m !== 'lab' && !t) { msg('自由实验没有课题：请回到课程页选择课题', true); return; }
    if (m === 'lab') {
      if (lock === 'practice') {
        if (!confirm('切换到实验模式会放弃本次练习：\n结束练习、把设备复位到开练前的配置，本次按「放弃」记录。\n\n确定切换吗？')) return;
        setBusyOp('正在结束练习…');
        try { if (!(await abandonPractice())) return; } finally { setBusyOp(''); }
        msg('已放弃本次练习，设备正在后台复位到开练前的配置（完成前终端不接受输入）。现在是实验模式（不注入故障）。');
        waitResetDone().then((res) => {
          if (res) msg(res.reset_ok ? '上一题的设备已复位完成，终端已恢复输入。' : `上一题的设备没有完全复位：${res.note}`, !res.reset_ok);
        });
      }
      pr.summary = null;
      if (state.mode !== 'lab') setMode('lab');
      return;
    }
    if (m === 'practice') {
      // 实验 → 练习：直接开始本题练习（注入故障）；练习已结束（总结页）时也是「再练一次」
      pr.summary = null;
      if (state.mode !== 'practice') setMode('practice');
      await startPractice(t.id);
      return;
    }
    // m === 'exam'：本题的单题考试
    const text = lock === 'practice'
      ? `开始「${t.title}」的单题考试？\n\n本次练习会按「放弃」结束并复位设备，然后重新注入故障、开始计时 15 分钟；\n考试中不能使用 Agent、没有提示，不能切换模式，只能交卷。`
      : `开始「${t.title}」的单题考试？\n\n注入故障后立即开始计时 15 分钟；考试中不能使用 Agent、没有提示，不能切换模式，只能交卷。`;
    if (!confirm(text)) return;
    setBusyOp('正在开考…');
    try {
      if (lock === 'practice' && !(await abandonPractice())) return;
      if (pr.resetAid) { msg('上一题的设备正在后台复位，完成后自动开考…'); await waitResetDone(); }
      pr.summary = null;
      msg('正在开始单题考试（快照、注入故障）…');
      await startSingleExam();
    } catch (e) {
      msg(`开考失败：${e.message}`, true);
    } finally { setBusyOp(''); }
  }
  for (const b of document.querySelectorAll('#mode-nav button')) b.onclick = () => requestMode(b.dataset.mode);

  // 退出到课程：练习中确认后按放弃结束（复位）；考试中不可用
  $('btn-exit').onclick = async () => {
    const lock = lockedMode();
    if (lock === 'exam' || busyOp) return;
    if (lock === 'practice') {
      // 已通关的练习退出时仍按通关记录（后端 end() 不覆盖 passed），提示里不能说「按放弃记录」
      const done = state.practice && state.practice.passed;
      if (!confirm(done
        ? '退出到课程会结束本次练习：\n把设备复位到开练前的配置，本次按「已通关」记录。\n\n确定退出吗？'
        : '退出到课程会结束本次练习：\n把设备复位到开练前的配置，本次按「放弃」记录。\n\n确定退出吗？')) return;
      setBusyOp('正在结束练习…');
      try { if (!(await abandonPractice())) return; } finally { setBusyOp(''); }
    }
    for (const port of [...state.tabs.keys()]) closeTab(port);
    location.href = 'courses';
  };

  // ---- 顶栏题名与气泡 ----
  const stars = (n) => '★'.repeat(Math.max(0, Math.min(5, Number(n) || 0))) || '—';
  function renderTopic() {
    const ex = state.exam && state.exam.active ? state.exam : null;
    const p = state.practice && state.practice.active ? state.practice : null;
    const t = state.topic;
    const badge = $('topic-badge');
    let title;
    if (ex) {
      badge.hidden = false; badge.textContent = '考试'; badge.className = 'tb-badge red';
      title = `第 ${ex.current + 1} / ${ex.total} 题`;
    } else if (p) {
      badge.hidden = false; badge.textContent = '练习'; badge.className = 'tb-badge';
      title = p.scenario.title;
    } else if (t) {
      badge.hidden = false;
      badge.textContent = MODE_LABEL[state.mode];
      badge.className = `tb-badge ${state.mode === 'exam' ? 'red' : state.mode === 'lab' ? 'gray' : ''}`;
      title = t.title;
    } else {
      badge.hidden = true;
      title = '自由实验';
    }
    $('topic-title').textContent = title;
    $('topic-btn').title = ex ? `${ex.name}：${title}` : title;
    $('topic-btn').classList.toggle('practice', !!p);
    // 气泡：考试只放工单（模板隐藏拓扑时也不显示环境与拓扑）；练习 / 实验放环境、拓扑、本题设备、章节、工单摘要
    const hideEnv = !!(ex && ex.config && ex.config.show_topology === false);
    for (const el of document.querySelectorAll('#tp-grid .tp-envrow')) el.hidden = hideEnv;
    for (const el of document.querySelectorAll('#tp-grid .tp-info')) el.hidden = !!ex || !t;
    $('tp-grid').hidden = hideEnv && !!ex;
    $('ex-ticket').hidden = !ex;
    $('ex-name').hidden = !ex;
    $('tp-ticket').hidden = !!ex;
    $('tp-full').hidden = !!ex;
    if (t) {
      const devs = p ? p.devices.map((d) => d.name) : t.required_devices || [];
      $('tp-devs').textContent = devs.join(' · ');
      const ch = state.chapters.find((c) => c.id === t.chapter);
      $('tp-chapter').textContent = `${ch ? `${ch.id} ${ch.title}` : (t.chapter || '—')} · 难度 ${stars(t.difficulty || t.level)}`;
    }
    const tk = $('tp-ticket');
    if (!t && !p) {
      tk.innerHTML = '<span class="muted">没有选择课题：可以自由操作当前拓扑。练习 / 考试请回到课程页选题。</span>';
    } else if (tk.dataset.full === '1' && !p) {
      tk.textContent = t.symptom || '';
    } else {
      const sym = p ? p.scenario.symptom : t.symptom;
      const brief = String(sym || '').split('\n')[0].replace(/^\s*工单[:：]\s*/, '');
      tk.textContent = `工单：${brief.length > 90 ? `${brief.slice(0, 90)}…` : brief}`;
    }
    $('tp-full').textContent = !p && tk.dataset.full === '1' ? '收起工单' : '查看完整工单';
    $('tp-full').disabled = !t && !p;
  }
  function setTopicPop(open) {
    $('topic-pop').hidden = !open;
    $('topic-btn').setAttribute('aria-expanded', String(open));
    if (open) renderTopic();
  }
  $('topic-btn').onclick = (e) => { e.stopPropagation(); setTopicPop($('topic-pop').hidden); };
  $('tp-full').onclick = () => {
    if (state.practice && state.practice.active) { setTopicPop(false); openSide('task'); return; }
    const tk = $('tp-ticket');
    tk.dataset.full = tk.dataset.full === '1' ? '' : '1';
    renderTopic();
  };
  // 学员菜单
  function setUserMenu(open) {
    $('user-menu').hidden = !open;
    $('user-btn').setAttribute('aria-expanded', String(open));
  }
  $('user-btn').onclick = (e) => { e.stopPropagation(); setUserMenu($('user-menu').hidden); };
  document.addEventListener('mousedown', (e) => {
    if (!$('topic-pop').hidden && !e.target.closest('#topic-wrap')) setTopicPop(false);
    if (!$('user-menu').hidden && !e.target.closest('.user-wrap')) setUserMenu(false);
    if (!$('pc-pop').hidden && !e.target.closest('#pc-pop') && !e.target.closest('.node')) closePcPop();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    setTopicPop(false); setUserMenu(false); closePcPop();
  });

  function prMsg(text, info) {
    const el = $('pr-msg');
    el.textContent = text || '';
    el.classList.toggle('info', !!info);
  }
  // 练习的三个视图：list（本题练习卡片，右侧面板）/ active（顶栏控制 + 右侧工单详情）/ summary（右侧总结 + 评分卡）
  function showPane(name) {
    $('pr-list-view').hidden = name !== 'list';
    $('pr-active-view').hidden = name !== 'active';
    $('pr-detail').hidden = name !== 'active';
    $('pr-summary-view').hidden = name !== 'summary';
    $('st-task').textContent = name === 'active' ? '工单详情' : '本题练习';
  }

  // 练习模式、没有进行中的练习：右侧只显示本题（操作页内不能换题）
  async function loadScenarios() {
    showPane('list');
    const list = $('pr-list');
    const t = state.topic;
    if (!t) {
      list.innerHTML = '<p class="pr-notready">没有选择课题。请<a href="courses">回到课程页</a>选择课题后再练习。</p>';
      return;
    }
    try {
      const data = await api('GET', 'api/scenarios?all=1');
      const s = data.scenarios.find((x) => x.id === t.id);
      list.innerHTML = '';
      if (!s) { list.innerHTML = '<p class="pr-notready">课题不存在（题库已更新？），请回到课程页重新选择</p>'; return; }
      state.topic = { ...t, ...s };
      const card = document.createElement('div');
      card.className = 'pr-card';
      card.dataset.id = s.id;
      card.innerHTML = `<div class="t">${esc(s.title)}</div>` +
        `<div class="meta">难度 ${esc(stars(s.difficulty || s.level))}${s.est_minutes ? ` · 约 ${s.est_minutes} 分钟` : ''}` +
        ` · 设备：${esc(s.required_devices.join('、'))}</div>` +
        `<div>${(s.tags || s.knowledge_tags || []).map((x) => `<span class="pr-tag">${esc(x)}</span>`).join('')}</div>` +
        (s.ready ? '<div class="pr-ready pr-state">● 就绪</div>'
          : `<div class="pr-notready pr-state">未就绪：${esc(s.reason)}</div>`) +
        '<button class="pr-start primary">开始练习</button>';
      const btn = card.querySelector('.pr-start');
      btn.disabled = !s.ready || !!data.active || !!(state.exam && state.exam.active);   // 考试中不能开始练习
      btn.onclick = () => startPractice(s.id, btn);
      list.appendChild(card);
      $('pr-errors').textContent = '';
    } catch (e) {
      list.innerHTML = `<p class="pr-notready">加载课题失败：${esc(e.message)}</p>`;
    }
  }
  $('pr-refresh').onclick = async () => { await refreshStatus(true); loadScenarios(); };

  // 开始练习：设备刚冷启动报 busy（409）时每 20 秒重试，最多 5 次（显示倒计时）
  async function startPractice(id, btn) {
    if (pr.busy) return;
    pr.busy = true;
    if (pr.resetAid) {               // 上一题还在后台复位：等它完成再开始（否则后端 409）
      prMsg('上一题的设备正在后台复位，完成后自动开始…', true);
      setBusyOp('正在等待上一题复位…');
      try { await waitResetDone(); } finally { setBusyOp(''); }
    }
    for (const b of document.querySelectorAll('.pr-start')) b.disabled = true;
    if (btn) btn.textContent = '正在注入故障…';
    prMsg('正在开始练习（快照、注入故障、确认故障生效）…', true);
    setBusyOp('正在开始练习…');
    try {
      const st = await window.LabSwitch.retryBusy(() => api('POST', 'api/practice/start', { scenario_id: id }), {
        onWait: (left, n) => prMsg(`设备还在准备配置，${left} 秒后第 ${n} 次重试开始练习…`, true),
      });
      pr.hints = [];
      pr.summary = null;
      renderHints();
      applyPractice(st);
      // 新开始的练习：清掉已打开终端里上一次练习留下的命令和答案（只留当前提示符行）
      for (const dv of st.devices) {
        const tab = state.tabs.get(dv.port);
        if (tab) tab.term.clear();
      }
      prMsg('练习开始：请在终端中排查并修复。系统会在你执行命令后自动判定。', true);
    } catch (e) {
      await loadScenarios();
      prMsg(`开始失败：${e.message}`);
      const card = document.querySelector(`.pr-card[data-id="${CSS.escape(id)}"]`);
      if (card) {
        const err = document.createElement('div');
        err.className = 'pr-notready pr-start-err';
        err.textContent = `开始失败：${e.message}`;
        card.appendChild(err);
      }
    } finally { pr.busy = false; setBusyOp(''); }
  }

  function renderDevices(p) {
    const box = $('pr-devices');
    const key = p.devices.map((x) => `${x.name}:${x.port}`).join(',');
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = '';
    for (const dv of p.devices) {
      const b = document.createElement('button');
      b.className = 'pr-dev';
      b.dataset.port = dv.port;
      b.textContent = `${dv.name} 终端`;
      b.title = `打开 ${dv.name}（${labInfo().kind === 'enspro' ? '设备' : 'console'} ${dv.port}）的终端`;
      b.onclick = () => {
        const d = deviceForPort(dv.port);
        if (d && canOpen(d)) openTab(d);
        else prMsg(`${dv.name} 当前不可打开（设备未运行？）`);
      };
      box.appendChild(b);
    }
  }

  // 判定进度：任务条上只显示「通过 x/y 项」（不暴露判定命令），右侧工单详情写明时间与来源
  function renderCheck(p) {
    const lc = p.last_check, el = $('pr-check-result'), det = $('pr-check-detail');
    el.className = 'tb-chip';
    det.classList.remove('ok');
    if (!lc) { el.textContent = '尚未检查'; el.classList.add('na'); det.textContent = '尚未检查'; return; }
    const at = lc.at ? new Date(lc.at).toLocaleTimeString() : '';
    if (lc.status === 'ok') {
      el.textContent = lc.all ? `通过 ${lc.passed}/${lc.total} ✓` : `通过 ${lc.passed}/${lc.total}`;
      el.classList.toggle('ok', !!lc.all);
      det.textContent = `最近一次检查（${at}${lc.source === 'auto' ? '，自动' : ''}）：通过 ${lc.passed} / ${lc.total} 项` +
        (lc.skipped ? `；之后一次未执行：${lc.skipped}` : '');
      det.classList.toggle('ok', !!lc.all);
    } else {
      el.textContent = '暂不可检查';
      el.classList.add('na');
      det.textContent = `暂不可检查（${at}）：${lc.reason || ''}`;
    }
    el.title = det.textContent;
  }

  function tickTimer() {
    const p = state.practice;
    if (!p || !p.active) return;
    const s = p.passed ? p.elapsed_s : pr.elapsed0 + (Date.now() - pr.t0) / 1000;
    $('pr-timer').textContent = fmtDur(s);
  }

  // 应用 /api/practice/current 或 start / check 的返回
  function applyPractice(p) {
    const was = state.practice;
    state.practice = p;
    if (!p.active) {
      stopPracticeTimers();
      refreshLock();
      if (was && was.active && !pr.summary) loadScenarios();
      updateNodes();
      refreshOpenSelect();
      if (was && was.active) fire('practice');
      return;
    }
    const fresh = !was || !was.active || was.attempt_id !== p.attempt_id;
    if (!was || !was.active) {
      // 新开始（或页面刷新后恢复）：切到练习模式，关掉不开放设备的终端标签
      refreshLock();
      setMode('practice');
      setSideTab('task');
      for (const port of [...state.tabs.keys()]) {
        if (!p.devices.some((x) => x.port === port)) closeTab(port);
      }
      startPracticeTimers();
    }
    pr.t0 = Date.now();
    pr.elapsed0 = p.elapsed_s;
    showPane('active');
    $('pr-symptom').textContent = p.scenario.symptom;
    if (!state.topic || state.topic.id !== p.scenario.id) state.topic = { ...(state.topic || {}), ...p.scenario };
    renderTopic();
    renderDevices(p);
    renderCheck(p);
    renderHintButtons();
    $('pr-check').disabled = p.passed;
    const banner = $('pr-banner');
    if (p.passed) {
      banner.hidden = false;
      banner.innerHTML = `<b>通关！</b> 用时 ${fmtDur(p.elapsed_s)}，使用提示 ${p.hints_used} 级。点「结束练习」把设备复位到开练前的配置并保存记录。`;
      if (!(was && was.passed)) { msg(`练习通关：${p.scenario.title}`); prMsg(''); }
    } else banner.hidden = true;
    tickTimer();
    updateNodes();
    refreshOpenSelect();
    if (fresh) fire('practice');
  }

  function startPracticeTimers() {
    stopPracticeTimers();
    pr.timer = setInterval(tickTimer, 1000);
    pr.poll = setInterval(pollPractice, PR_POLL_MS);
  }
  function stopPracticeTimers() {
    clearInterval(pr.timer);
    clearInterval(pr.poll);
    pr.timer = pr.poll = null;
  }
  async function pollPractice() {
    if (pr.summary) return;
    try { applyPractice(await api('GET', 'api/practice/current')); } catch (e) { /* 下次再试 */ }
  }

  $('pr-check').onclick = async () => {
    const b = $('pr-check');
    b.disabled = true;
    prMsg('检查中…', true);
    try {
      const r = await api('POST', 'api/practice/check');
      applyPractice(r);
      const c = r.check || r.last_check;
      if (c && c.status !== 'ok') prMsg(`暂不可检查：${c.reason}`);
      else if (!r.passed) prMsg('还没有修复，继续排查。');
      else prMsg('');
    } catch (e) { prMsg(`检查失败：${e.message}`); }
    finally { if (state.practice && state.practice.active && !state.practice.passed) b.disabled = false; }
  };

  // ---- 三级提示（按钮在「Agent 导师」页签底部；结果同时进对话流和「工单详情」） ----
  const HINT_NAMES = ['方向', '命令', '揭示'];
  function renderHintButtons() {
    const p = state.practice;
    const active = !!(p && p.active);
    $('agent-hints').hidden = !active;
    if (!active) return;
    const total = p.scenario.hints_total || 3;
    const next = p.hints_used + 1;
    for (const b of document.querySelectorAll('#agent-hints .hint-lv')) {
      const lv = Number(b.dataset.level);
      const got = lv <= p.hints_used;
      b.classList.toggle('got', got);
      b.hidden = lv > total;
      // 已看过的级别可以再看；下一级可以取；更高级别要先看前一级。生成中全部禁用
      b.disabled = !!pr.hintBusy || lv > next || (lv === next && p.passed);
      b.textContent = pr.hintBusy === lv ? `${lv} 级 · 生成中…`
        : `${lv} 级 · ${HINT_NAMES[lv - 1] || ''}${lv === 3 ? '（扣分）' : ''}`;
      b.title = got ? '已查看，点击再看一次' : (lv > next ? `请先查看第 ${next} 级提示` :
        (lv === 3 ? '直接揭示故障点和修复方法，按规则扣分' : `查看第 ${lv} 级提示`));
    }
  }
  async function requestHint(level) {
    const p = state.practice;
    if (!p || !p.active || pr.hintBusy) return null;
    if (level === 3 && p.hints_used < 3 &&
        !confirm('第 3 级提示会直接揭示故障点和修复方法，并按使用第 3 级提示扣分（过程分受影响）。确定查看？')) return null;
    pr.hintBusy = level;                      // AI 结合当前操作生成，最多约 15 秒，超时回退静态提示
    renderHintButtons();
    let h = null;
    try {
      h = await api('POST', 'api/practice/hint', { level });
      pr.hints[h.level - 1] = { text: h.text, source: h.source, reason: h.fallback_reason || '' };
      renderHints();
      fire({ hint: { level: h.level, text: h.text, source: h.source, reason: h.fallback_reason || '' } });
    } catch (e) {
      prMsg(`获取提示失败：${e.message}`);
    } finally { pr.hintBusy = 0; }
    // busy 标记清掉之后再刷新按钮（否则按钮停在禁用状态直到下一次轮询）
    if (state.practice && state.practice.active) {
      applyPractice(h ? { ...state.practice, hints_used: Math.max(state.practice.hints_used, h.hints_used) } : state.practice);
    }
    return h;
  }
  for (const b of document.querySelectorAll('#agent-hints .hint-lv')) b.onclick = () => requestHint(Number(b.dataset.level));
  function hintSrcTag(h) {
    const ai = h.source === 'ai';
    return `<span class="hint-src ${ai ? 'ai' : ''}" title="${esc(ai ? '由 AI 结合你当前的操作生成'
      : (h.reason ? `AI 不可用，使用题目自带提示：${h.reason}` : '题目自带的静态提示'))}">${ai ? 'AI' : '静态'}</span>`;
  }
  function renderHints() {
    const html = pr.hints.map((h, i) => (h
      ? `<div class="hint"><b>第 ${i + 1} 级 · ${HINT_NAMES[i] || ''}</b> ${hintSrcTag(h)}<br>${esc(h.text)}</div>` : '')).join('');
    $('pr-hints').innerHTML = html || '<span class="muted">还没有使用提示</span>';
  }

  $('pr-end').onclick = async () => {
    const p = state.practice;
    if (!p || !p.active) return;
    if (!p.passed && !confirm('还没有通关，确定放弃并结束练习吗？')) return;
    const b = $('pr-end');
    b.disabled = true;
    prMsg('正在结束练习…', true);
    try {
      // 立即返回：设备在后台复位，总结页上显示复位进度
      showSummary(await api('POST', 'api/practice/end'));
    } catch (e) { prMsg(`结束失败：${e.message}`); }
    finally { b.disabled = false; }
  };

  function showSummary(r) {
    pr.summary = r;
    $('pr-banner').hidden = true;
    pr.hints = [];
    renderHints();
    prMsg('');
    state.practice = { active: false };
    stopPracticeTimers();
    refreshLock();
    showPane('summary');
    renderHintButtons();
    openSide('task');
    $('pr-summary').innerHTML =
      `<div class="row">场景：${esc(r.scenario.title)}</div>` +
      `<div class="row">结果：${r.passed ? '<span class="pr-ready">通关</span>' : '<span class="pr-notready">未通关（已放弃）</span>'}</div>` +
      `<div class="row">用时：${fmtDur(r.duration_s || 0)}</div>` +
      `<div class="row">使用提示：${r.hints_used} 级</div>` +
      `<div class="row">执行命令：${r.commands} 条</div>` +
      '<div id="pr-reset"></div>' +
      '<div id="pr-score"></div>';
    renderReset(r);
    updateNodes();
    refreshOpenSelect();
    fire('practice');
    if (r.resetting) pollReset(r.attempt_id);
    else if (r.attempt_id) pollScore(r.attempt_id);
  }
  // 总结页的设备复位状态：后台复位中显示进度，完成后换成结果（评分在复位完成后才开始）
  function renderReset(r) {
    const el = $('pr-reset');
    if (!el) return;
    if (r.resetting) {
      el.innerHTML = '<div class="row">设备复位：<span class="sc-spin"></span>后台复位中…</div>' +
        `<div class="note">${esc(r.note)}</div>`;
      return;
    }
    el.innerHTML = `<div class="row">设备复位：${r.reset_ok ? '<span class="pr-ready">已复位到开练前配置</span>'
      : '<span class="pr-notready">未完全复位</span>'}</div>` +
      `<div class="note${r.reset_ok ? ' ok' : ''}">${esc(r.note)}</div>`;
  }
  async function pollReset(aid) {
    pr.resetAid = aid;
    const res = await waitResetDone();
    if (!pr.summary || pr.summary.attempt_id !== aid) return;      // 已离开总结页
    if (!res || res.attempt_id !== aid) return;
    pr.summary = res;
    renderReset(res);
    pollScore(aid);
  }
  // 总结页「再练一次」：同一道题重新开始练习（操作页内不能换题）
  $('pr-back').onclick = () => {
    stopScorePoll();
    pr.summary = null;
    if (state.topic) startPractice(state.topic.id); else loadScenarios();
  };

  // ---------- 评分卡（结束练习后异步评分，轮询结果） ----------
  const SCORE_POLL_MS = 1500;
  function stopScorePoll() { clearTimeout(pr.scoreTimer); pr.scoreTimer = null; }
  async function pollScore(aid) {
    stopScorePoll();
    pr.scoreAid = aid;
    let res;
    try { res = await api('GET', `api/practice/result/${aid}`); }
    catch (e) { res = { status: 'error', error: e.message }; }
    if (pr.scoreAid !== aid || !pr.summary) return;      // 已离开总结页
    renderScore(aid, res);
    if (res.status === 'scoring' || res.status === 'none' || res.status === 'error') {
      pr.scoreTimer = setTimeout(() => pollScore(aid), SCORE_POLL_MS);
    }
  }
  const fmtScore = (v) => (v == null ? '—' : String(Math.round(v * 10) / 10));
  function renderScore(aid, res) {
    const el = $('pr-score');
    if (!el) return;
    const d = res.detail || {};
    if (res.status === 'scoring' || res.status === 'none') {
      el.innerHTML = '<div class="sc-card"><div class="sc-loading"><span class="sc-spin"></span>AI 评分中…（可以先返回列表，档案页稍后可见总分）</div></div>';
      return;
    }
    if (res.status === 'error') {
      el.innerHTML = `<div class="sc-card"><div class="sc-loading">获取评分结果失败：${esc(res.error)}，稍后重试…</div></div>`;
      return;
    }
    const rescore = '<button class="sc-rescore">重新评分</button>';
    if (res.status === 'failed') {
      el.innerHTML = `<div class="sc-card"><div class="sc-warn">${esc(d.reason || '评分失败')}</div>${rescore}</div>`;
    } else {
      const s = d.scores || {}, dims = s.dimensions || {};
      const row = (key, name) => {
        const x = dims[key] || {};
        const pct = x.score == null || !x.max ? 0 : Math.max(0, Math.min(100, (x.score / x.max) * 100));
        return `<div class="sc-dim"><div class="sc-dim-h"><span>${name}</span>` +
          `<span class="sc-num">${fmtScore(x.score)} / ${fmtScore(x.max)}</span></div>` +
          `<div class="sc-bar"><i style="width:${pct}%"></i></div>` +
          (x.reason ? `<div class="sc-reason">${esc(x.reason)}</div>` : '') +
          (x.adjust ? `<div class="sc-adjust">${esc(x.adjust)}</div>` : '') + '</div>';
      };
      const list = (title, items, cls) => (items && items.length
        ? `<div class="sc-list ${cls}"><b>${title}</b><ul>${items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>` : '');
      const c = d.comment;
      el.innerHTML = '<div class="sc-card">' +
        `<div class="sc-total"><span class="sc-big">${fmtScore(s.total)}</span><span class="sc-of">/ 100</span>` +
        (res.status === 'degraded' ? '<span class="sc-partial">仅结果分</span>' : '') + '</div>' +
        (res.status === 'degraded' ? `<div class="sc-warn">${esc(d.reason || 'AI 点评不可用')}</div>` : '') +
        (d.reset && !d.reset.ok ? '<div class="sc-adjust">设备未完全复位属于系统问题，评分已知晓，不影响你的得分</div>' : '') +
        row('result', '结果') + row('process', '过程（方法论）') + row('side', '规范 / 副作用') +
        (c ? (c.summary ? `<div class="sc-summary">${esc(c.summary)}</div>` : '') +
          list('做得好的', c.good, 'good') + list('存在的问题', c.problems, 'bad') + list('改进建议', c.suggestions, '') : '') +
        (d.standard_path ? `<div class="sc-list"><b>标准排障思路</b><div class="sc-path">${esc(d.standard_path)}</div></div>`
          : (d.reference_path ? `<div class="sc-list"><b>参考排障路径</b><div class="sc-path">${esc(d.reference_path)}</div></div>` : '')) +
        (res.status === 'degraded' ? rescore : '') + '</div>';
    }
    const b = el.querySelector('.sc-rescore');
    if (b) b.onclick = async () => {
      b.disabled = true;
      try { await api('POST', `api/practice/rescore/${aid}`); pollScore(aid); }
      catch (e) { b.disabled = false; b.textContent = `重新评分（上次失败：${e.message}）`; }
    };
  }

  // ---------- 学生状态栏 ----------
  // AI 设置（里程碑 6.18，aiset.js）；考试中由 CSS（body.exam-active #um-ai）隐藏
  if (window.AiSettings) {
    window.AiSettings.bind($('um-ai'), { root: '' });
    $('um-ai').addEventListener('click', () => { $('user-menu').hidden = true; $('user-btn').setAttribute('aria-expanded', 'false'); });
  } else $('um-ai').hidden = true;
  $('logout').onclick = async () => {
    for (const port of [...state.tabs.keys()]) closeTab(port);
    await fetch('api/logout', { method: 'POST' });
    location.href = 'login';
  };

  // ---------- 启动 ----------
  // 课题：?s=场景 id（课程页带入）；进行中的练习以服务端为准。场景信息取 api/scenarios?all=1 的面向学员字段
  async function loadTopic(id) {
    try {
      const data = await api('GET', 'api/scenarios?all=1');
      state.chapters = data.chapters || [];
      const s = id && data.scenarios.find((x) => x.id === id);
      state.topic = s || null;
    } catch (e) { /* 题库读不到：按自由实验处理 */ }
  }
  async function init() {
    const res = await fetch('api/me');
    if (res.status === 401) { location.href = 'login'; return; }
    const p = await res.json();
    $('who').textContent = `${p.student.name}（${p.student.login}）`;
    if (p.auto_login) { $('um-switch').hidden = true; $('logout').hidden = true; }   // 自动登录模式：不切换、不登出
    applyNarrow();
    const q = new URLSearchParams(location.search);
    let practice = null;
    try {
      practice = await api('GET', 'api/practice/current');
    } catch (e) { /* 忽略 */ }
    if (window.__ainet.examReady) await window.__ainet.examReady;     // exam.js 取回进行中的考试（已 apply）
    await loadTopic(practice && practice.active ? practice.scenario.id : q.get('s'));
    // ?mode=lab|practice|exam（课程页 / 独立页面的链接）优先于上次记住的模式；进行中的练习 / 考试会再锁定
    const saved = q.get('mode') || (state.topic ? store.get('mode') : 'lab');
    setMode(lockedMode() || (['lab', 'practice', 'exam'].includes(saved) ? saved : 'lab'));

    await refreshStatus(true);
    // 页面刷新后恢复进行中的练习；刚结束、设备还在后台复位时回到总结页
    if (practice && practice.active) applyPractice(practice);
    else if (practice && practice.resetting && practice.last_result && state.mode === 'practice') showSummary(practice.last_result);

    // 兼容 ?port=：拓扑加载后再映射到设备
    const qp = Number(q.get('port'));
    if (qp) {
      const d = deviceForPort(qp) || { name: `端口 ${qp}`, console: qp };
      openTab(d);
    }
    setInterval(refreshStatus, POLL_MS);
    if (anyTransient()) scheduleFastPoll();
  }

  // 供调试 / e2e 读取页面状态
  window.__ainet = { state, openTab, closeTab, focusTab, assignPane, setSplit, refreshStatus, fitView,
    setView, setMode, refreshLock, loadScenarios, pr, api, esc, fmtDur, deviceForPort, canOpen, updateNodes,
    refreshOpenSelect, viewHooks, requestHint, applyPractice, msg, requestMode, renderTopic, setTopicPop,
    openPcPop, closePcPop,
    layout: { openSide, setSideTab, setSideCollapsed, setTopoCollapsed, notifyAgent, clearAgentDot, agentVisible,
      applyWidths, widths, side, store, isNarrow } };
  init();
})();
