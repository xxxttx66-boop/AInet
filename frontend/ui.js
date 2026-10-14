// Shared application chrome. Icons are local SVG data, with no CDN dependency.
(() => {
  'use strict';
  const html = document.documentElement;
  const read = (key) => { try { return localStorage.getItem(`ainet.ui.${key}`); } catch (_) { return null; } };
  const write = (key, value) => { try { localStorage.setItem(`ainet.ui.${key}`, value); } catch (_) { /* Session-only preference. */ } };
  html.dataset.theme = read('theme') === 'dark' ? 'dark' : 'light';
  const paths = {
    book: '<path d="M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H4zM13 7a3 3 0 0 1 3-3h5v15h-4a4 4 0 0 0-4 2"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3M13 15h4"/>',
    chart: '<path d="M4 3v17h17M8 15v-4M13 15V7M18 15v-7"/>',
    settings: '<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h4l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3z"/><circle cx="11" cy="12" r="3"/>',
    network: '<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M5 16v-4h14v4"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    shield: '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6zM8 12l3 3 5-6"/>',
    refresh: '<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
    left: '<path d="m15 5-7 7 7 7"/>',
    right: '<path d="m9 5 7 7-7 7"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    logout: '<path d="M9 4H4v16h5M10 12h10m-4-4 4 4-4 4"/>',
    moon: '<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1"/>',
    panel: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 3v18"/>',
    split: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>',
    focus: '<path d="M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4M12 17h.01"/>',
    bot: '<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M2 12h2M20 12h2M8 16h8"/><circle cx="9" cy="12" r=".5"/><circle cx="15" cy="12" r=".5"/>',
    send: '<path d="m3 3 18 9-18 9 4-9zM7 12h14"/>',
    clipboard: '<rect x="5" y="5" width="14" height="16" rx="2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 11h6M9 15h6"/>',
    play: '<path d="m8 4 12 8-12 8z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
  };
  const iconUrl = (name) => `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] || paths.book}</svg>`)}")`;
  function decorate(scope) {
    if (!(scope instanceof Element)) return;
    if (scope.closest('.xterm')) return;
    const nodes = [...scope.querySelectorAll('[data-icon]')];
    if (scope.hasAttribute('data-icon')) nodes.unshift(scope);
    for (const node of nodes) node.style.setProperty('--ui-icon', iconUrl(node.dataset.icon));
    if (scope.matches('.c-env-wrap > .c-ep-pop')) requestAnimationFrame(alignEnvironmentPanel);
  }
  function alignEnvironmentPanel() {
    if (window.matchMedia('(max-width: 700px)').matches) return;
    const min = parseFloat(getComputedStyle(document.body).paddingLeft) + 16;
    for (const panel of document.querySelectorAll('.c-env-wrap > .c-ep-pop')) {
      const owner = panel.parentElement.getBoundingClientRect();
      const left = Math.max(min, Math.min(owner.left, window.innerWidth - panel.offsetWidth - 16));
      panel.style.left = `${left - owner.left}px`;
    }
  }
  const modeNotes = {
    practice: '注入故障，可使用 AI 导师与分级提示，结束后查看复盘。',
    lab: '使用本题拓扑自由操作，不注入故障。',
    exam: '单题限时 15 分钟，不提供 AI 导师与提示，交卷后查看成绩。',
  };
  function syncCourseEntry(template = false) {
    const field = document.getElementById('c-entry-modes');
    const actions = document.getElementById('c-actions');
    if (!field || !actions) return;
    field.hidden = template;
    const note = document.getElementById('c-entry-note');
    note.hidden = template;
    const checked = field.querySelector('input:checked');
    const mode = checked ? checked.value : 'practice';
    actions.dataset.entryMode = mode;
    note.textContent = modeNotes[mode];
  }
  const setExamLocked = (locked) => document.body.classList.toggle('ui-exam-locked', !!locked);
  window.AInetUI = { decorate, syncCourseEntry, setExamLocked };

  function init() {
    const body = document.body;
    const root = body.dataset.root || '';
    const workbench = body.dataset.uiPage === 'workbench';
    const page = body.dataset.uiPage || 'courses';
    body.classList.add('ui-upgraded');
    if (page !== 'login') {
      const sidebar = document.createElement('aside');
      sidebar.id = 'app-sidebar';
      sidebar.className = 'app-sidebar';
      sidebar.setAttribute('aria-label', '应用导航');
      sidebar.innerHTML = `<a class="sidebar-brand" href="${root}courses" aria-label="ainet 课程中心"><span class="brand-mark">a</span><span class="sidebar-label">ainet</span></a>` +
        '<p class="sidebar-tagline sidebar-label">网络实操训练</p>' +
        '<nav class="sidebar-nav" aria-label="主要页面">' +
        `<a href="${root}courses" data-nav="courses" data-icon="book" title="课程中心"><span class="sidebar-label">课程中心</span></a>` +
        `<a href="${root}./" data-nav="workbench" data-icon="terminal" title="实验工作台"><span class="sidebar-label">实验工作台</span></a>` +
        `<a href="${root}profile" data-nav="profile" data-icon="chart" title="学习记录"><span class="sidebar-label">学习记录</span></a></nav>` +
        '<div class="sidebar-secondary">' +
        `<a href="${root}exam/templates" data-nav="templates" data-icon="clipboard" title="考试模板"><span class="sidebar-label">考试模板</span></a>` +
        `<a href="${root}courses#environment-settings" data-nav="settings" data-icon="settings" title="环境设置"><span class="sidebar-label">环境设置</span></a>` +
        '<button id="ui-ai-settings" type="button" data-icon="bot" title="AI 设置"><span class="sidebar-label">AI 设置</span></button></div>' +
        '<div class="sidebar-footer"><span class="sidebar-student sidebar-label" id="ui-student">学员空间</span>' +
        '<button id="ui-theme" type="button" title="切换界面主题"><span class="sidebar-label"></span></button>' +
        '<button id="ui-nav-toggle" type="button" data-icon="panel" aria-controls="app-sidebar" title="展开或收起导航"><span class="sidebar-label">收起导航</span></button></div>';
      body.prepend(sidebar);
      const current = sidebar.querySelector(`[data-nav="${page === 'report' ? 'profile' : page}"]`);
      if (current) current.setAttribute('aria-current', 'page');
      const preference = read('navCollapsed');
      body.classList.toggle('nav-collapsed', preference === '1' || (preference === null && workbench));
      const toggle = document.getElementById('ui-nav-toggle');
      const updateNav = () => {
        const mobile = window.matchMedia('(max-width: 700px)').matches;
        const open = mobile ? body.classList.contains('nav-mobile-open') : !body.classList.contains('nav-collapsed');
        toggle.setAttribute('aria-expanded', String(open));
        toggle.dataset.icon = mobile ? 'menu' : 'panel';
        toggle.querySelector('span').textContent = open ? '收起导航' : '展开导航';
        decorate(toggle);
      };
      toggle.onclick = () => {
        if (window.matchMedia('(max-width: 700px)').matches) body.classList.toggle('nav-mobile-open');
        else { body.classList.toggle('nav-collapsed'); write('navCollapsed', body.classList.contains('nav-collapsed') ? '1' : '0'); }
        updateNav();
      };
      window.addEventListener('resize', updateNav);
      window.addEventListener('resize', alignEnvironmentPanel);
      updateNav();
      if (window.AiSettings) window.AiSettings.bind(document.getElementById('ui-ai-settings'), { root });
      else document.getElementById('ui-ai-settings').hidden = true;
      if (workbench) {
        const exit = document.getElementById('btn-exit');
        sidebar.addEventListener('click', (event) => {
          const link = event.target.closest('a');
          if (!link) return;
          if (body.classList.contains('exam-active')) { event.preventDefault(); return; }
          if (link.dataset.nav === 'courses' || link.dataset.nav === 'settings' || link.classList.contains('sidebar-brand')) {
            event.preventDefault(); exit.click();
          }
        });
        const syncLocks = () => {
          const exam = body.classList.contains('exam-active');
          for (const link of sidebar.querySelectorAll('a')) {
            if (exam) { if (link.hasAttribute('href')) link.dataset.uiHref = link.getAttribute('href'); link.removeAttribute('href'); link.setAttribute('aria-disabled', 'true'); }
            else { if (link.dataset.uiHref) link.setAttribute('href', link.dataset.uiHref); link.removeAttribute('aria-disabled'); }
          }
          document.getElementById('ui-ai-settings').hidden = exam || !window.AiSettings;
        };
        new MutationObserver(syncLocks).observe(body, { attributes: true, attributeFilter: ['class'] });
        syncLocks();
      }
      const who = document.getElementById('who');
      if (who) {
        const syncStudent = () => { document.getElementById('ui-student').textContent = who.textContent || '学员空间'; };
        new MutationObserver(syncStudent).observe(who, { childList: true, subtree: true, characterData: true });
        syncStudent();
      }
    }
    if (page === 'login') {
      const brand = document.createElement('div');
      brand.className = 'login-brand';
      brand.innerHTML = '<span class="brand-mark">a</span><span>ainet</span>';
      document.querySelector('main').prepend(brand);
    }
    function updateTheme() {
      const button = document.getElementById('ui-theme');
      if (!button) return;
      const dark = html.dataset.theme === 'dark';
      button.dataset.icon = dark ? 'sun' : 'moon';
      button.setAttribute('aria-label', dark ? '切换到浅色主题' : '切换到深色主题');
      button.title = button.getAttribute('aria-label');
      button.querySelector('span').textContent = dark ? '浅色主题' : '深色主题';
      decorate(button);
    }
    const theme = document.getElementById('ui-theme');
    if (theme) theme.onclick = () => { html.dataset.theme = html.dataset.theme === 'dark' ? 'light' : 'dark'; write('theme', html.dataset.theme); updateTheme(); };
    updateTheme();
    const icons = {
      'c-recheck':'refresh','c-env-add':'plus','c-lab':'terminal','c-practice':'play','c-exam':'shield','c-exam-tpl':'shield',
      'c-active-go':'arrow','c-cancel':'close','pv-back':'back','pv-retry':'refresh','user-btn':'user','um-profile':'chart',
      'um-ai':'bot','um-switch':'user','logout':'logout','btn-exit':'back','btn-refresh':'refresh','btn-relogin':'refresh',
      'vt-topo':'terminal','vt-practice':'book','vt-exam':'shield','pr-check':'check','pr-end':'stop','ex-submit':'check',
      'ex-next':'arrow','ex-finish':'shield','ex-hint':'help','st-task':'clipboard','st-agent':'bot','agent-send':'send',
      'btn-single':'terminal','btn-split':'split','btn-fit':'focus','btn-collapse':'left','btn-side-collapse':'right',
      'tp-full':'clipboard','pr-refresh':'refresh','ex-refresh':'refresh','rp-prev':'back','rp-play':'play','rp-next':'arrow','rp-all':'clipboard',
    };
    for (const [id, name] of Object.entries(icons)) { const el = document.getElementById(id); if (el) el.dataset.icon = name; }
    for (const id of ['btn-collapse', 'btn-side-collapse']) { const el = document.getElementById(id); if (el) el.textContent = ''; }
    if (workbench) upgradeWorkbench();
    const modes = document.getElementById('c-entry-modes');
    if (modes) {
      const previous = read('entryMode');
      const selected = [...modes.querySelectorAll('input')].find((n) => n.value === previous);
      if (selected) selected.checked = true;
      modes.addEventListener('change', () => { syncCourseEntry(); write('entryMode', modes.querySelector('input:checked').value); });
      syncCourseEntry(!!window.__ainetCourses?.st.topic?.startsWith('tpl:'));
    }
    decorate(body);
    new MutationObserver((records) => {
      for (const record of records) for (const node of record.addedNodes) decorate(node);
    }).observe(body, { childList: true, subtree: true });
  }

  function upgradeWorkbench() {
    const bar = document.getElementById('bar');
    const title = document.createElement('div'); title.className = 'ui-workbench-title';
    const identity = document.createElement('div'); identity.className = 'ui-workbench-identity';
    identity.append(document.getElementById('topic-wrap'));
    const meta = document.createElement('div'); meta.className = 'ui-context-meta'; identity.append(meta);
    title.append(identity);
    for (const id of ['ex-progress','btn-relogin']) title.append(document.getElementById(id));
    const user = bar.querySelector('.user-wrap'); if (user) title.append(user);
    title.append(document.getElementById('btn-exit'));
    const actions = document.createElement('div'); actions.className = 'ui-workbench-actions';
    for (const id of ['mode-nav','topo-msg','pr-active-view','ex-active-view']) actions.append(document.getElementById(id));
    const focus = document.createElement('button'); focus.id = 'ui-focus'; focus.type = 'button'; focus.dataset.icon = 'focus'; focus.className = 'tb-btn'; focus.textContent = '专注终端'; focus.setAttribute('aria-pressed', 'false');
    actions.append(focus);
    for (const el of bar.querySelectorAll('.tb-sep')) el.remove();
    bar.append(title); bar.after(actions);
    let snapshot = null;
    focus.onclick = () => {
      const layout = window.__ainet && window.__ainet.layout;
      if (!layout) return;
      if (!snapshot) {
        snapshot = { topo: document.body.classList.contains('topo-collapsed'), side: document.body.classList.contains('side-collapsed') };
        layout.setTopoCollapsed(true); layout.setSideCollapsed(true);
      } else {
        layout.setTopoCollapsed(snapshot.topo); layout.setSideCollapsed(snapshot.side); snapshot = null;
      }
      focus.textContent = snapshot ? '恢复面板' : '专注终端'; focus.setAttribute('aria-pressed', String(!!snapshot));
    };
    const syncContext = () => {
      meta.textContent = [document.getElementById('tp-env').textContent, document.getElementById('topo-status').textContent].filter(Boolean).join(' / ');
    };
    for (const id of ['tp-env','topo-status']) new MutationObserver(syncContext).observe(document.getElementById(id), { childList: true, subtree: true, characterData: true });
    syncContext();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
