// ainet 操作页「考试」模式（里程碑 6 / 6.9）：未开考时右侧面板是本题的「单题考试」卡片（多题考试在课程页「考试」里选模板）
// → 作答（题名处显示题号；当前题目和工单固定显示在工作区上方，题名气泡可查看设备 / 提示；题号进度、总倒计时、本题计时、提交本题、下一题、
// 交卷（二次确认）；作答中没有右侧面板，也不能切换模式 / 退出到课程）→ 已交卷（右侧面板，成绩单链接）。
// 计时以服务端为准：每 2 秒拉一次 /api/exam/current，两次之间按本地时钟插值显示。
// 依赖 app.js 暴露的 window.__ainet（终端标签、拓扑状态、api / esc / fmtDur）。
(() => {
  'use strict';
  const A = window.__ainet;
  const { state, api, esc, fmtDur } = A;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2000;
  const ex = { poll: null, timer: null, t0: 0, rem0: 0, qrem0: null, busy: false, activeKey: null, doneId: null };

  function msg(text, info) {
    const el = $('ex-msg');
    el.textContent = text || '';
    el.classList.toggle('info', !!info);
  }
  function showPane(name) {
    $('ex-list-view').hidden = name !== 'list';
    $('ex-active-view').hidden = name !== 'active';
    $('ex-done-view').hidden = name !== 'done';
    if (name !== 'active') setTicket(false);
  }
  // 工单在顶栏题名气泡里（app.js 的 setTopicPop）
  function setTicket(open) {
    A.setTopicPop(open);
  }
  const mins = (s) => (s == null ? '不限' : (s % 60 ? `${Math.round(s / 6) / 10} 分钟` : `${s / 60} 分钟`));

  // ---------- 未开考：本题的单题考试 ----------
  async function loadList() {
    if (state.exam && state.exam.active) return;
    showPane(ex.doneId ? 'done' : 'list');
    if (ex.doneId) return;
    const list = $('ex-list');
    const t = state.topic;
    list.innerHTML = '';
    if (t) {
      const busy = state.practice && state.practice.active;
      const card = document.createElement('div');
      card.className = 'pr-card ex-card';
      card.dataset.id = t.id;
      card.innerHTML = `<div class="t">单题考试 · ${esc(t.title)}</div>` +
        '<div class="meta">计时 15 分钟 · 无 Agent · 无提示 · 每题可重试 1 次 · 交卷后出成绩单 · 拓扑可见</div>' +
        '<button class="ex-start primary">开始单题考试</button>';
      const btn = card.querySelector('.ex-start');
      btn.title = busy ? '练习进行中：点顶栏「考试」确认后先结束练习再开考' : '';
      btn.onclick = () => A.requestMode('exam');
      list.appendChild(card);
    } else {
      list.innerHTML = '<p class="pr-notready">没有选择课题。单题考试请<a href="courses">回到课程页</a>选题；多题考试在课程页章节列最下方的「考试」里选择模板。</p>';
    }
    list.insertAdjacentHTML('beforeend', '<p class="muted">多题考试：在课程页章节列最下方的「考试」里选择模板。</p>');
    try {
      const mine = await api('GET', 'api/exams');
      $('ex-last').innerHTML = mine.length ? '最近考试：' + mine.slice(0, 3).map((x) =>
        `<a href="exam/report?id=${x.id}">${esc(x.template_name)}（${esc(new Date(x.started_at).toLocaleString())}` +
        `${x.score_total != null ? `，${Math.round(x.score_total * 10) / 10} 分` : ''}）</a>`).join('；') +
        '　<a href="profile">全部记录</a>' : '';
    } catch (e) { /* 忽略 */ }
  }
  $('ex-refresh').onclick = async () => { await A.refreshStatus(true); loadList(); };

  // ---------- 作答 ----------
  function apply(st) {
    const was = state.exam;
    state.exam = st;
    if (!st.active) {
      stopTimers();
      $('exam-question').hidden = true;
      if (was && was.active) {                 // 刚交卷（或计时器到点自动交卷）
        finished(st.exam_id || st.last_exam_id || was.exam_id);
        A.refreshStatus(true);
      }
      A.refreshLock();
      A.updateNodes();
      A.refreshOpenSelect();
      $('ex-progress').hidden = true;
      return;
    }
    if (!was || !was.active) {                 // 新开考或刷新页面后恢复
      ex.doneId = null;
      A.refreshLock();                         // 进入考试模式：锁定模式切换，任务条出现，右侧面板消失
      startTimers();
      A.refreshStatus(true);                   // 不可看拓扑的考试：重新拉取（后端打码）
    }
    ex.t0 = Date.now();
    ex.rem0 = st.remaining_s;
    ex.qrem0 = st.question ? st.question.remaining_s : null;
    showPane('active');
    render(st);
    const q = st.question;
    if (q && q.status === 'active') {
      const key = `${st.exam_id}:${st.current}:${q.devices.map((d) => d.port).join(',')}`;
      if (key !== ex.activeKey) {              // 新的一题开始：关掉不开放设备的终端，清掉上一题留下的屏幕
        ex.activeKey = key;
        for (const port of [...state.tabs.keys()]) {
          if (!q.devices.some((x) => x.port === port)) A.closeTab(port);
        }
        for (const d of q.devices) {
          const tab = state.tabs.get(d.port);
          if (tab) tab.term.clear();
        }
        setTicket(true);                       // 新的一题：先展开工单
      }
    }
    A.updateNodes();
    A.refreshOpenSelect();
  }

  function render(st) {
    const q = st.question || {};
    const cfg = st.config || {};
    $('ex-name').textContent = st.name;
    $('exam-question-title').textContent = q.title || '';
    $('exam-question-text').textContent = q.symptom || '';
    $('exam-question').hidden = !q.title && !q.symptom;
    $('ex-progress').hidden = st.total <= 1;          // 单题考试不显示题号进度
    A.renderTopic();
    $('ex-progress').innerHTML = st.questions.map((x) => {
      const cls = ['ex-dot', x.index === st.current ? 'cur' : '', x.status === 'done' ? 'done' : '',
        x.passed ? 'ok' : '', x.status === 'unanswered' ? 'skip' : ''].join(' ');
      const tip = `${x.title}：${{ pending: '未开始', starting: '准备中', active: '作答中', ending: '复位中', done: '已结束',
        unanswered: '未作答' }[x.status] || x.status}${x.passed ? '（已通过）' : ''}`;
      return `<span class="${cls}" title="${esc(tip)}">${x.index + 1}</span>`;
    }).join('');
    $('ex-qno').textContent = `第 ${st.current + 1} / ${st.total} 题 · ${q.title || ''}`;
    $('ex-qtimer-wrap').hidden = q.remaining_s == null || q.status !== 'active';
    $('ex-symptom').textContent = q.symptom || '';
    renderDevices(q);
    const banner = $('ex-banner');
    let text = '', cls = '', actions = '';
    if (st.phase === 'finishing') {
      text = '正在交卷：等待当前题的设备复位完成。';
      if (q.reset_error) text += `\n复位暂未完成：${q.reset_error}\n系统会自动重试，无需操作终端。`;
    } else if (q.status === 'pending' && q.error) {
      cls = 'err';
      text = `本题开始失败：${q.error}`;
      actions = '<div class="pr-actions"><button id="ex-retry">重新开始本题</button>' +
        (st.current + 1 < st.total ? '<button id="ex-skip">跳过本题</button>' : '') + '</div>';
    } else if (q.status === 'starting' || q.status === 'pending') {
      text = '正在准备本题（快照、注入故障、确认故障生效）…';
    } else if (q.status === 'ending') {
      text = '本题已结束，正在复位设备。';
      if (q.reset_error) text += `\n复位暂未完成：${q.reset_error}\n系统会自动重试，无需操作终端。`;
    } else if (q.passed) {
      cls = 'ok';
      text = st.current + 1 < st.total ? '本题已通过验收。点「下一题」继续（本题设备会复位）。' : '本题已通过验收。这是最后一题，可以交卷了。';
    } else if (q.locked) {
      text = '本题提交次数已用完。' + (st.current + 1 < st.total ? '点「下一题」继续。' : '这是最后一题，可以交卷了。');
    }
    banner.hidden = !text;
    banner.className = cls;
    banner.innerHTML = esc(text) + actions;
    if ($('ex-retry')) $('ex-retry').onclick = () => op('api/exam/retry_start', '正在重新准备本题…');
    if ($('ex-skip')) $('ex-skip').onclick = () => {
      if (confirm('跳过本题？本题记为未作答（0 分）。')) op('api/exam/next', '正在进入下一题…');
    };
    const active = q.status === 'active' && st.phase !== 'finishing';
    const sb = $('ex-submit');
    sb.textContent = `提交本题（剩余 ${q.remaining_submissions ?? 0} 次）`;
    sb.disabled = ex.busy || !active || !!q.locked;
    const hb = $('ex-hint');
    hb.hidden = !cfg.hints_allowed;
    const lvl = (q.hints_used || 0) + 1;
    hb.textContent = lvl <= cfg.hint_max_level ? `提示（第 ${lvl} 级，扣 ${cfg.hint_penalty} 分）` : '提示已用完';
    hb.disabled = ex.busy || !active || lvl > cfg.hint_max_level;
    const nb = $('ex-next');
    nb.hidden = st.current + 1 >= st.total;      // 最后一题只有「交卷」
    nb.disabled = ex.busy || !(active || q.status === 'ending');
    $('ex-finish').disabled = ex.busy;
    $('ex-subs').innerHTML = (q.submissions || []).map((s) =>
      `<div class="sub ${s.passed ? 'ok' : 'bad'}">第 ${s.n} 次提交（${esc(new Date(s.at).toLocaleTimeString())}）：` +
      `${s.passed ? '通过' : '未通过'}（${s.passed_items} / ${s.total} 项）</div>`).join('');
    const names = ['方向', '命令', '揭示'];
    $('ex-hints').innerHTML = (q.hints || []).map((h, i) => (h ? `<div class="hint"><b>第 ${i + 1} 级 · ${names[i]}</b>` +
      ` <span class="hint-src ${h.source === 'ai' ? 'ai' : ''}">${h.source === 'ai' ? 'AI' : '静态'}</span><br>${esc(h.text)}</div>` : '')).join('');
    tick();
  }

  function renderDevices(q) {
    const box = $('ex-devices');
    const devs = q.devices || [];
    const key = devs.map((x) => `${x.name}:${x.port}`).join(',');
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = devs.length ? '' : '<span class="pr-notready">（本题未开始或已结束）</span>';
    for (const dv of devs) {
      const b = document.createElement('button');
      b.className = 'ex-dev';
      b.dataset.port = dv.port;
      b.textContent = `${dv.name} 终端`;
      b.onclick = () => {
        const d = A.deviceForPort(dv.port);
        if (d && A.canOpen(d)) A.openTab(d);
        else msg(`${dv.name} 当前不可打开（设备未运行？）`);
      };
      box.appendChild(b);
    }
  }

  function tick() {
    const st = state.exam;
    if (!st || !st.active) return;
    const el = (Date.now() - ex.t0) / 1000;
    const rem = ex.rem0 - el;
    $('ex-timer').textContent = fmtDur(rem);
    $('ex-timer').parentElement.classList.toggle('urgent', rem < 60);
    if (ex.qrem0 != null) {
      const qr = ex.qrem0 - el;
      $('ex-qtimer').textContent = fmtDur(qr);
      $('ex-qtimer-wrap').classList.toggle('urgent', qr < 60);
    }
  }
  function startTimers() {
    stopTimers();
    ex.timer = setInterval(tick, 1000);
    ex.poll = setInterval(poll, POLL_MS);
  }
  function stopTimers() {
    clearInterval(ex.timer);
    clearInterval(ex.poll);
    ex.timer = ex.poll = null;
  }
  async function poll() {
    if (ex.busy) return;
    try { apply(await api('GET', 'api/exam/current')); } catch (e) { /* 下次再试 */ }
  }

  async function op(url, busyText, body) {
    if (ex.busy) return null;
    ex.busy = true;
    msg(busyText, true);
    if (state.exam) render(state.exam);
    let r = null;
    try {
      r = await api('POST', url, body);
      msg('');
    } catch (e) {
      msg(`操作失败：${e.message}`);
    } finally { ex.busy = false; }
    if (r) apply(r.submission ? r : r);
    else if (state.exam && state.exam.active) render(state.exam);
    return r;
  }

  $('ex-submit').onclick = async () => {
    const r = await op('api/exam/submit', '系统验收中（在设备上执行判定）…');
    if (!r) return;
    const s = r.submission || {};
    if (s.status !== 'ok') msg(`暂不可验收：${s.reason || ''}（未计入提交次数，稍后再试）`);
    else if (s.passed) msg(`第 ${s.n} 次提交：通过！`, true);
    else msg(`第 ${s.n} 次提交：未通过（${s.passed_items} / ${s.total} 项）。` +
      (r.question && r.question.remaining_submissions ? `还可以再提交 ${r.question.remaining_submissions} 次。` : '提交次数已用完。'));
  };

  $('ex-hint').onclick = async () => {
    const st = state.exam;
    if (!st || !st.question || ex.busy) return;
    const level = (st.question.hints_used || 0) + 1;
    ex.busy = true;
    $('ex-hint').disabled = true;
    $('ex-hint').textContent = '提示生成中…';
    try {
      await api('POST', 'api/exam/hint', { level });
    } catch (e) { msg(`获取提示失败：${e.message}`); }
    finally { ex.busy = false; }
    try { apply(await api('GET', 'api/exam/current')); } catch (e) { /* 忽略 */ }
  };

  $('ex-next').onclick = () => {
    const st = state.exam;
    const q = st && st.question;
    if (!q) return;
    if (q.status === 'active' && !q.locked) {
      const left = q.remaining_submissions;
      if (!confirm(q.submissions.length ? `本题还有 ${left} 次提交机会，结束本题时系统会按当前设备状态自动验收一次（计入次数）。确定进入下一题？`
        : '本题还没有提交。进入下一题时系统会按当前设备状态自动验收一次。确定进入下一题？')) return;
    }
    op('api/exam/next', '正在结束本题：验收、复位设备，然后准备下一题（每题约十几秒到一分钟）…');
  };

  // 交卷：二次确认（面板内确认条，避免误点）
  $('ex-finish').onclick = () => {
    const st = state.exam;
    if (!st) return;
    const left = st.questions.filter((x) => x.status === 'pending').length;
    $('ex-confirm-text').textContent = `确定交卷吗？交卷后不能再作答。当前题会按设备状态自动验收${left ? `，还有 ${left} 道题未作答（记 0 分）` : ''}。`;
    $('ex-confirm').hidden = false;
  };
  $('ex-confirm-no').onclick = () => { $('ex-confirm').hidden = true; };
  $('ex-confirm-yes').onclick = async () => {
    $('ex-confirm').hidden = true;
    const r = await op('api/exam/finish', '正在交卷：验收当前题并复位设备…');
    if (r && !r.active) finished(r.exam_id);
  };

  function finished(id) {
    stopTimers();
    $('exam-question').hidden = true;
    state.exam = { active: false, last_exam_id: id };
    ex.doneId = id;
    ex.activeKey = null;
    showPane('done');
    $('ex-progress').hidden = true;
    A.refreshLock();
    A.setMode('exam');
    A.layout.openSide('task');
    $('ex-done').innerHTML = '<div class="row">已交卷。各题的 AI 评分在后台进行，成绩单稍后完整显示。</div>' +
      (id ? `<div class="row"><a class="ex-link" id="ex-report-link" href="exam/report?id=${id}">查看成绩单与命令回放</a></div>` : '');
    A.updateNodes();
    A.refreshOpenSelect();
  }
  $('ex-back').onclick = () => { ex.doneId = null; loadList(); };

  A.viewHooks.exam = () => { if (!(state.exam && state.exam.active)) loadList(); };

  // 页面加载：恢复进行中的考试（app.js 的 init 等它取回再定模式，Agent 也据此决定要不要取对话历史）
  A.examReady = (async () => {
    try {
      const st = await api('GET', 'api/exam/current');
      if (st.active) apply(st);
      else state.exam = st;
    } catch (e) { /* 忽略 */ }
  })();

  window.__ainetExam = { ex, apply, loadList, poll };
})();
