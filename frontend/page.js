// 独立页面（档案 / 成绩单 / 模板管理）共用的顶栏：显示当前学生、登出。
// body 的 data-root 是回到站点根的相对前缀（profile 为 ""，exam/* 为 "../"），支持部署在子路径（如 /ainet/）下。
(() => {
  'use strict';
  const root = document.body.dataset.root || '';
  const who = document.getElementById('who');
  fetch(`${root}api/me`).then((r) => (r.ok ? r.json() : null)).then((p) => {
    if (p && who) who.textContent = `${p.student.name}（${p.student.login}）`;
    if (p && window.AInetUI) window.AInetUI.setExamLocked((p.exams || []).some((x) => x.status === 'running'));
    // 自动登录模式（AINET_AUTO_LOGIN）：隐藏「切换」「登出」
    if (p && p.auto_login) {
      const sw = document.querySelector('#user a[href$="login"]');
      if (sw) sw.hidden = true;
      const lo = document.getElementById('logout');
      if (lo) lo.hidden = true;
    }
  }).catch(() => { /* 未登录时页面自己会跳转 */ });
  // 顶栏只读显示当前模拟环境（labswitch.js；在工作台顶栏切换）
  const logo = document.querySelector('.topbar .logo');
  if (logo && window.LabSwitch) {
    const el = document.createElement('span');
    el.id = 'lab-ro';
    logo.after(el);
    window.LabSwitch.readonly(el, { root });
  }
  const lo = document.getElementById('logout');
  if (lo) {
    lo.onclick = async () => {
      try { await fetch(`${root}api/logout`, { method: 'POST' }); } catch (e) { /* 忽略 */ }
      location.href = `${root}login`;
    };
  }
})();
