// AI 接口设置对话框（里程碑 6.18，学员菜单「AI 设置」；课程页与操作页共用，考试中不显示菜单项）。
// API URL / 模型存库，API Key 写 ainet 主机的 secrets 文件；接口只回显「已配置」与掩码（sk-***abcd），Key 输入框只写不读。
// 用法：AiSettings.bind(按钮元素, { root: '' })。
(() => {
  'use strict';
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SRC = { db: '页面设置', page: '页面设置', env: '环境变量', force: '环境变量（AINET_LAB_FORCE=1）', file: 'secrets 文件', default: '内置缺省值', none: '未配置' };
  let root = '';
  let box = null;
  let lastFocus = null;

  async function call(method, url, body) {
    const r = await fetch(`${root}${url}`, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
    });
    let d = null;
    try { d = await r.json(); } catch (e) { /* 忽略 */ }
    if (!r.ok) {
      const det = d && d.detail;
      throw new Error(typeof det === 'string' ? det : (Array.isArray(det) ? det.map((x) => x.msg).join('；') : `HTTP ${r.status}`));
    }
    return d;
  }

  function close() {
    if (!box) return;
    box.remove();
    box = null;
    document.removeEventListener('keydown', onKey, true);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); close(); } }

  function render(s, msg) {
    const k = s.key;
    const keyState = k.configured ? `已配置 <span class="mono">${esc(k.masked)}</span>（${esc(SRC[k.source] || k.source)}）` : '未配置';
    box.querySelector('.ai-card').innerHTML =
      '<div class="ai-head"><b id="ai-title">AI 设置</b><button type="button" class="icon-btn sm" id="ai-x" aria-label="关闭">✕</button></div>' +
      '<div class="ai-note">AI 评分、分级提示、Agent 面板共用这一套接口（OpenAI 兼容）。保存后下一次请求即生效，已发出的请求不受影响。</div>' +
      `<label class="c-ep-field"><span>API URL</span><input id="ai-url" type="text" spellcheck="false" autocomplete="off" value="${esc(s.base_url.value)}" placeholder="${esc(s.base_url.default)}"></label>` +
      `<div class="c-ep-kv">来源：${esc(SRC[s.base_url.source] || s.base_url.source)} · 缺省 <span class="mono">${esc(s.base_url.default)}</span>（留空 = 缺省）</div>` +
      '<label class="c-ep-field"><span>API Key</span><input id="ai-key" type="password" autocomplete="new-password" spellcheck="false" value=""' +
      ` placeholder="${k.configured ? '留空 = 不改' : '粘贴 API Key'}"></label>` +
      `<div class="c-ep-kv">当前：${keyState}</div>` +
      `<div class="ai-note">Key 只保存在 ainet 主机的 <span class="mono">${esc(k.file)}</span>，页面不会显示完整的 Key。</div>` +
      `<label class="c-ep-field"><span>模型（可选）</span><input id="ai-model" type="text" spellcheck="false" autocomplete="off" value="${esc(s.model.value)}" placeholder="${esc(s.model.default)}"></label>` +
      `<div class="c-ep-kv">来源：${esc(SRC[s.model.source] || s.model.source)} · 缺省 <span class="mono">${esc(s.model.default)}</span>（留空 = 缺省）</div>` +
      (s.force ? '<div class="ai-note warn">AINET_LAB_FORCE=1：设了环境变量的项以环境变量为准，页面保存的值暂不生效</div>' : '') +
      `<div id="ai-msg" class="c-ep-msg${msg ? ` ${msg.cls}` : ''}" role="status" aria-live="polite">${esc(msg ? msg.text : '')}</div>` +
      '<div class="c-ep-btns">' +
      (k.page ? '<button type="button" id="ai-clear" class="danger">清除 Key</button>' : '') +
      '<button type="button" id="ai-test">测试连接</button>' +
      '<button type="button" id="ai-save" class="primary">保存</button></div>';
    const q = (sel) => box.querySelector(sel);
    const say = (text, cls) => { const m = q('#ai-msg'); m.textContent = text; m.className = `c-ep-msg ${cls || ''}`; };
    const busy = (on) => box.querySelectorAll('.ai-card button').forEach((b) => { if (b.id !== 'ai-x') b.disabled = on; });
    q('#ai-x').onclick = close;
    q('#ai-test').onclick = async () => {
      busy(true);
      say('测试中…');
      try {
        const r = await call('POST', 'api/ai/settings/test', {
          base_url: q('#ai-url').value.trim() || null, api_key: q('#ai-key').value.trim() || null, model: q('#ai-model').value.trim() || null,
        });
        say(`${r.ok ? '' : '失败：'}${r.message}（${r.base_url} · ${r.model} · Key ${r.key_masked}）`, r.ok ? 'ok' : 'err');
      } catch (e) { say(e.message, 'err'); } finally { busy(false); }
    };
    q('#ai-save').onclick = async () => {
      const body = {};
      const url = q('#ai-url').value.trim();
      const model = q('#ai-model').value.trim();
      const key = q('#ai-key').value.trim();
      if (url !== s.base_url.value || (!url && s.base_url.source === 'db')) body.base_url = url || null;
      if (model !== s.model.value || (!model && s.model.source === 'db')) body.model = model || null;
      if (key) body.api_key = key;
      if (!Object.keys(body).length) { say('没有改动', ''); return; }
      busy(true);
      say('保存中…');
      try {
        const r = await call('POST', 'api/ai/settings', body);
        render(r, { cls: 'ok', text: r.changed.length ? '已保存，立即生效' : '没有变化' });
      } catch (e) { say(e.message, 'err'); busy(false); }
    };
    if (q('#ai-clear')) q('#ai-clear').onclick = async () => {
      if (!confirm('从 secrets 文件删除页面保存的 API Key？之后改用环境变量或其他 secrets 文件里的 Key（没有则 AI 功能不可用）。')) return;
      busy(true);
      try {
        const r = await call('POST', 'api/ai/settings', { clear_key: true });
        render(r, { cls: 'ok', text: '已清除 Key' });
      } catch (e) { say(e.message, 'err'); busy(false); }
    };
    for (const id of ['#ai-url', '#ai-key', '#ai-model']) {
      q(id).onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); q('#ai-save').click(); } };
    }
  }

  async function open() {
    if (box) return;
    lastFocus = document.activeElement;
    box = document.createElement('div');
    box.className = 'ai-mask';
    box.innerHTML = '<div class="ai-card c-ep-pop" role="dialog" aria-modal="true" aria-labelledby="ai-title"><div class="c-pad muted">读取中…</div></div>';
    box.addEventListener('mousedown', (e) => { if (e.target === box) close(); });
    document.body.appendChild(box);
    document.addEventListener('keydown', onKey, true);
    try {
      render(await call('GET', 'api/ai/settings'));
      const i = box && box.querySelector('#ai-url');
      if (i) i.focus();
    } catch (e) {
      if (box) box.querySelector('.ai-card').innerHTML = `<div class="c-err c-pad">读取 AI 设置失败：${esc(e.message)}</div>`;
    }
  }

  window.AiSettings = {
    bind(btn, opts) {
      root = (opts && opts.root) || '';
      if (btn) btn.onclick = (e) => { e.preventDefault(); open(); };
    },
    open, close,
  };
})();
