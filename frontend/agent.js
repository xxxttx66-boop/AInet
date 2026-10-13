// ainet 工作台右侧「Agent」面板（spec §16）：向 POST api/agent/chat 提问，SSE 流式逐字显示；
// 回复支持基本 Markdown（代码块、行内代码、列表、粗体、简单表格）；显示 Agent 执行过的只读命令
// （「已在 X 执行（只读）：命令」）；练习中为导师模式（三级提示按钮也在这里），判定泄露时显示扣分提示。
// 考试作答中面板不出现（后端也返回 403）。对话按场景分开：自由实验一份、每次练习一份（刷新页面后从服务端恢复）。
// 依赖 app.js 暴露的 window.__ainet。
(() => {
  'use strict';
  const A = window.__ainet;
  const { state, esc } = A;
  const L = A.layout;
  const $ = (id) => document.getElementById(id);
  const CLIENT_TIMEOUT_MS = 150000;      // 后端整轮上限 120 秒；再留余量，超时就断开并提示
  const HISTORY_TURNS = 12;

  const contexts = new Map();            // key -> { key, items: [], loaded }
  const ag = { ctx: null, busy: false, abort: null };

  function ctxKey() {
    const p = state.practice;
    return p && p.active ? `practice:${p.attempt_id}` : 'lab';
  }
  function getCtx(key) {
    let c = contexts.get(key);
    if (!c) { c = { key, items: [], loaded: false }; contexts.set(key, c); }
    return c;
  }

  // ---------- Markdown（先整体转义，再做有限的格式化；模型输出与设备输出都不可信） ----------
  function inline(t) {
    return t.replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  }
  function renderMd(src) {
    const text = esc(src || '');
    const out = [];
    const parts = text.split(/```/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {                             // 代码块（第一行可能是语言名）
        const body = part.replace(/^[A-Za-z0-9_+-]*\n/, '');
        out.push(`<pre><code>${body.replace(/\n$/, '')}</code></pre>`);
        return;
      }
      const lines = part.split('\n');
      let list = null, para = [], table = [];
      const flushPara = () => { if (para.length) out.push(`<p>${inline(para.join('<br>'))}</p>`); para = []; };
      const flushList = () => {
        if (list) {
          out.push(`<${list.tag}>${list.items.map((x, k) => `<li>${inline(x)}${list.subs[k]
            ? `<ul>${list.subs[k].map((y) => `<li>${inline(y)}</li>`).join('')}</ul>` : ''}</li>`).join('')}</${list.tag}>`);
        }
        list = null;
      };
      const flushTable = () => {
        if (!table.length) return;
        const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map((r) =>
          r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => inline(c.trim())));
        out.push('<table>' + rows.map((r, j) => `<tr>${r.map((c) => (j ? `<td>${c}</td>` : `<th>${c}</th>`)).join('')}</tr>`).join('') + '</table>');
        table = [];
      };
      for (const raw of lines) {
        const line = raw.replace(/\s+$/, '');
        let m;
        if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line); continue; }
        flushTable();
        if (!line.trim()) { flushPara(); flushList(); continue; }
        if ((m = line.match(/^\s*#{1,6}\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
        if ((m = line.match(/^(\s*)[-*•]\s+(.*)$/))) {
          flushPara();
          // 有序列表里缩进的无序子项：挂到上一条下面（只支持一层嵌套）
          if (list && list.tag === 'ol' && m[1].length >= 2 && list.items.length) {
            const k = list.items.length - 1;
            list.subs[k] = (list.subs[k] || []).concat(m[2]);
            continue;
          }
          if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [], subs: {} }; }
          list.items.push(m[2]);
          continue;
        }
        if ((m = line.match(/^\s*\d+[.)、]\s+(.*)$/))) {
          flushPara();
          if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [], subs: {} }; }
          list.items.push(m[1]);
          continue;
        }
        if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += '<br>' + line.trim(); continue; }
        flushList();
        para.push(line);
      }
      flushTable(); flushPara(); flushList();
    });
    return out.join('');
  }

  // ---------- 渲染 ----------
  const log = $('agent-log');
  function stick() { return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }
  function toBottom() { log.scrollTop = log.scrollHeight; }

  function toolHtml(t) {
    const ok = t.status === 'ok';
    const head = ok ? `已在 ${esc(t.device)} 执行（只读）：<span class="cmd">${esc(t.command)}</span>`
      : `未执行（${esc(t.reason || t.status)}）：<span class="cmd">${esc(t.device ? `${t.device}> ` : '')}${esc(t.command)}</span>`;
    if (ok && t.output) return `<details class="ag-tool"><summary>${head}</summary><pre>${esc(t.output)}</pre></details>`;
    return `<div class="ag-tool${ok ? '' : ' bad'}"><summary>${head}</summary></div>`;
  }
  function itemEl(it) {
    const el = document.createElement('div');
    fillItem(el, it);
    return el;
  }
  function fillItem(el, it) {
    if (it.role === 'user') {
      el.className = 'ag-user';
      el.textContent = it.content;
      return;
    }
    if (it.role === 'note') {                  // 系统说明（如「模拟环境已切换」），不进对话历史
      el.className = 'ag-note';
      el.textContent = it.text;
      return;
    }
    if (it.role === 'hint') {
      el.className = `ag-hint${it.level === 3 ? ' lv3' : ''}`;
      el.innerHTML = `<b>第 ${it.level} 级提示 · ${esc(['方向', '命令', '揭示'][it.level - 1] || '')}</b>` +
        ` <span class="hint-src ${it.source === 'ai' ? 'ai' : ''}">${it.source === 'ai' ? 'AI' : '静态'}</span><br>${esc(it.text)}`;
      return;
    }
    el.className = 'ag-bot';
    let html = '';
    for (const seg of it.segments) {
      if (seg.type === 'text') html += seg.text.trim() ? `<div class="md">${renderMd(seg.text)}</div>` : '';
      else if (seg.type === 'tool') html += toolHtml(seg);
    }
    if (it.streaming) html += '<div class="md ag-typing"></div>';
    if (it.penalty) html += `<div class="ag-penalty">${esc(it.penalty)}</div>`;
    if (it.error) html += `<div class="ag-error">${esc(it.error)}${it.retry ? '<button class="ag-retry">重试</button>' : ''}</div>`;
    if (!html) html = '<div class="md muted">（无回复）</div>';
    el.innerHTML = html;
    const rb = el.querySelector('.ag-retry');
    if (rb) rb.onclick = () => { if (!ag.busy && it.retry) send(it.retry, true); };
  }
  function renderAll() {
    log.innerHTML = '';
    const c = ag.ctx;
    $('agent-empty') && $('agent-empty').remove();
    if (!c || !c.items.length) {
      const e = document.createElement('div');
      e.className = 'ag-empty';
      e.id = 'agent-empty';
      e.innerHTML = c && c.key.startsWith('practice')
        ? '有思路卡住了就问导师：它会引导你自己找到故障，不直接给答案。<br>需要时可用下方的分级提示（第 3 级会扣分）。'
        : '问问当前环境：拓扑、设备状态、命令输出的含义……<br>Agent 可以在设备上执行只读命令（display / ping），你的终端里看不到这些命令。';
      log.appendChild(e);
      return;
    }
    for (const it of c.items) { it.el = itemEl(it); log.appendChild(it.el); }
    toBottom();
  }
  function append(c, it) {
    c.items.push(it);
    if (c !== ag.ctx) return it;
    const empty = $('agent-empty');
    if (empty) empty.remove();
    const s = stick();
    it.el = itemEl(it);
    log.appendChild(it.el);
    if (s || it.role === 'user') toBottom();
    return it;
  }
  let rafPending = new Set();
  function update(c, it) {
    if (c !== ag.ctx || !it.el) return;
    rafPending.add(it);
    if (rafPending.size > 1) return;
    requestAnimationFrame(() => {
      const s = stick();
      for (const x of rafPending) if (x.el) fillItem(x.el, x);
      rafPending = new Set();
      if (s) toBottom();
    });
  }

  // ---------- 场景（自由问答 / 导师）切换 ----------
  async function loadHistory(c) {
    c.loaded = true;
    try {
      const h = await A.api('GET', 'api/agent/history');
      const key = h.mode === 'practice' ? `practice:${h.attempt_id}` : 'lab';
      if (key !== c.key || c.items.length) return;
      for (const m of h.messages) {
        if (m.role === 'user') c.items.push({ role: 'user', content: m.content });
        else {
          c.items.push({ role: 'bot', segments: [...(m.tools || []).map((t) => ({ type: 'tool', ...t })),
            { type: 'text', text: m.content }], penalty: m.leaked ? '这条回复被判定含答案级内容，已按第 3 级提示记扣分。' : '' });
        }
      }
      if (c === ag.ctx) renderAll();
    } catch (e) { /* 考试中 403 等：忽略 */ }
  }
  // initial：脚本加载时只建好对话上下文，不取历史（操作页还不知道有没有进行中的考试；考试中接口 403）
  function syncContext(initial) {
    const key = ctxKey();
    const practice = key.startsWith('practice');
    $('agent-note').hidden = !practice;
    $('agent-badge').textContent = practice ? '导师模式' : '自由问答';
    $('agent-badge').hidden = practice;
    $('agent-input').placeholder = practice ? '向导师提问…' : '问问当前环境…';
    if (!ag.ctx || ag.ctx.key !== key) {
      ag.ctx = getCtx(key);
      renderAll();
    }
    // 考试模式不取对话：考试中接口一律 403，交卷切回其他模式时再取
    if (!initial && !ag.ctx.loaded && !(state.exam && state.exam.active) && state.mode !== 'exam') loadHistory(ag.ctx);
  }
  A.viewHooks.change.push((what) => {
    if (what && what.env) {                    // 模拟环境切换：在当前对话里注明，之前的回答基于旧环境
      syncContext();
      const e = what.env;
      append(ag.ctx, { role: 'note', text: `—— 模拟环境已切换为 ${e.label}${e.sandbox != null ? `（沙箱 ${e.sandbox}）` : ''}，`
        + '之前的对话基于切换前的环境 ——' });
      return;
    }
    if (what && what.hint) {                   // 三级提示：也放进导师对话流
      const c = getCtx(ctxKey());
      if (c !== ag.ctx) syncContext();
      append(ag.ctx, { role: 'hint', ...what.hint });
      L.notifyAgent();
      return;
    }
    syncContext();
  });

  // ---------- 发送与流式接收 ----------
  function historyFor(c, upto) {
    const out = [];
    for (const it of c.items.slice(0, upto)) {
      if (it.role === 'user') out.push({ role: 'user', content: it.content });
      else if (it.role === 'bot') {
        const t = it.segments.filter((s) => s.type === 'text').map((s) => s.text).join('').trim();
        if (t) out.push({ role: 'assistant', content: t });
      }
    }
    return out.slice(-HISTORY_TURNS);
  }
  function setBusy(on) {
    ag.busy = on;
    const b = $('agent-send');
    b.textContent = on ? '停止' : '发送';
    b.classList.toggle('primary', !on);
    b.type = on ? 'button' : 'submit';
    for (const q of document.querySelectorAll('#agent-quick button')) q.disabled = on;
  }

  async function send(text, isRetry) {
    text = (text || '').trim();
    if (!text || ag.busy) return;
    if (state.exam && state.exam.active) return;
    ag.lastText = text;
    syncContext();
    const c = ag.ctx;
    const hist = historyFor(c, c.items.length);
    if (!isRetry || !c.items.length || c.items[c.items.length - 1].role !== 'bot') append(c, { role: 'user', content: text });
    else {                                    // 重试：去掉上一条失败的回复，复用同一个问题
      const last = c.items.pop();
      if (last.el) last.el.remove();
      hist.pop();
      if (hist.length && hist[hist.length - 1].role === 'user') hist.pop();
    }
    const it = append(c, { role: 'bot', segments: [], streaming: true });
    setBusy(true);
    const ctrl = new AbortController();
    ag.abort = ctrl;
    const timer = setTimeout(() => ctrl.abort('timeout'), CLIENT_TIMEOUT_MS);
    let gotDone = false;
    try {
      const res = await fetch('api/agent/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history: hist }), signal: ctrl.signal,
      });
      if (res.status === 401) { location.href = 'login'; return; }
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        throw new Error((d && d.detail) || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of block.split('\n')) {
            if (!line.startsWith('data: ')) continue;
            let ev;
            try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (onEvent(c, it, ev)) gotDone = true;
          }
        }
      }
      if (!gotDone && !it.error) { it.error = '连接意外中断，回答可能不完整'; it.retry = text; }
    } catch (e) {
      const aborted = ctrl.signal.aborted;
      it.error = aborted ? (ctrl.signal.reason === 'timeout' ? 'AI 响应超时，请稍后再问一次' : '已停止回答')
        : `Agent 暂不可用：${e.message}`;
      if (!aborted || ctrl.signal.reason === 'timeout') it.retry = text;
    } finally {
      clearTimeout(timer);
      it.streaming = false;
      ag.abort = null;
      setBusy(false);
      update(c, it);
      L.notifyAgent();
    }
  }
  // 返回 true 表示收到 done
  function onEvent(c, it, ev) {
    const segs = it.segments;
    if (ev.type === 'delta') {
      const last = segs[segs.length - 1];
      if (last && last.type === 'text') last.text += ev.text;
      else segs.push({ type: 'text', text: ev.text });
    } else if (ev.type === 'tool') {
      segs.push({ type: 'tool', device: ev.device, command: ev.command, status: ev.status, output: ev.output, reason: ev.reason });
    } else if (ev.type === 'notice') {
      it.penalty = ev.message;                 // 复用醒目的提示样式
    } else if (ev.type === 'penalty') {
      it.penalty = ev.message;
      const p = state.practice;
      if (p && p.active && ev.hints_used != null) A.applyPractice({ ...p, hints_used: Math.max(p.hints_used, ev.hints_used) });
    } else if (ev.type === 'error') {
      it.error = ev.message;
      it.retry = ag.lastText;
    } else if (ev.type === 'done') {
      return true;
    }
    update(c, it);
    return false;
  }

  const input = $('agent-input');
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(140, input.scrollHeight)}px`;
  }
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $('agent-form').requestSubmit();
    }
  });
  $('agent-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (ag.busy) return;
    const t = input.value;
    if (!t.trim()) return;
    ag.lastText = t.trim();
    input.value = '';
    autoGrow();
    send(t);
  });
  $('agent-send').addEventListener('click', (e) => {
    if (ag.busy && ag.abort) { e.preventDefault(); ag.abort.abort('stop'); }
  });
  for (const b of document.querySelectorAll('#agent-quick button')) {
    b.onclick = () => { ag.lastText = b.dataset.q; send(b.dataset.q); };
  }

  syncContext(true);
  window.__ainetAgent = { ag, contexts, send, renderMd, syncContext };
})();
