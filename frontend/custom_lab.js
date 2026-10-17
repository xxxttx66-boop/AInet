(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const state = { id: null, detail: null, models: [], selected: null, busy: false, dragging: false };
  const canvas = $('cl-canvas');
  const statusLabel = (status) => ({ online:'运行中', stopped:'已停止', starting:'启动中', stopping:'停止中', error:'异常', unknown:'未知' }[status] || status);

  async function api(method, path, body) {
    const response = await fetch(path, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) { location.href = 'login'; throw new Error('请重新登录'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
    return data;
  }

  function message(text, error = false) {
    const el = $('cl-message');
    el.hidden = !text;
    el.classList.toggle('error', error);
    el.textContent = text || '';
  }

  async function busy(action) {
    if (state.busy) return;
    state.busy = true;
    document.body.classList.add('cl-busy');
    for (const button of document.querySelectorAll('.cl-main button')) button.disabled = true;
    message('正在与 eNSP Pro 同步…');
    try { await action(); }
    catch (error) { if (state.detail) render(); message(error.message || String(error), true); }
    finally {
      state.busy = false;
      document.body.classList.remove('cl-busy');
      for (const button of document.querySelectorAll('.cl-main button')) button.disabled = false;
      $('cl-create').disabled = !$('cl-environment').value;
      $('cl-device-form').querySelector('button').disabled = !state.models.length;
      const selected = state.detail?.devices.find((d) => d.id === state.selected);
      if ($('cl-power') && selected && (!selected.powerable || selected.status === 'stopping'))
        $('cl-power').disabled = true;
    }
  }

  function iconize(root = document.body) {
    if (window.AInetUI) window.AInetUI.decorate(root);
  }

  async function list() {
    const [labs, environments] = await Promise.all([
      api('GET', 'api/custom-labs'), api('GET', 'api/lab/environments'),
    ]);
    const envSelect = $('cl-environment');
    const prior = envSelect.value;
    envSelect.replaceChildren();
    const available = (environments.environments || []).filter((env) => (env.type || env.key) === 'enspro');
    for (const env of available) {
      const option = document.createElement('option');
      option.value = env.key;
      option.textContent = `${env.label}${env.available ? '' : '（离线）'}`;
      option.disabled = !env.available;
      envSelect.append(option);
    }
    const first = available.find((env) => env.available);
    envSelect.value = available.some((env) => env.key === prior && env.available) ? prior
      : (available.find((env) => env.key === environments.current && env.available) || first || {}).key || '';
    $('cl-create').disabled = !first;
    $('cl-count').textContent = `${labs.labs.length} 个实验`;
    const host = $('cl-labs');
    host.replaceChildren();
    if (!labs.labs.length) {
      host.innerHTML = '<div class="cl-empty">还没有自定义实验。填写上方名称，创建第一个空白拓扑。</div>';
      return;
    }
    for (const lab of labs.labs) {
      const card = document.createElement('article');
      card.className = 'cl-lab-card';
      card.innerHTML = `<h3>${esc(lab.name)}</h3><div class="cl-lab-meta">${esc(lab.environment_label)} · 沙箱 ${lab.sandbox}</div>
        <div class="cl-lab-actions"><button type="button" class="primary cl-open" data-icon="network">编辑拓扑</button>
        <button type="button" class="cl-run" data-icon="terminal">进入实验</button></div>`;
      card.querySelector('.cl-open').onclick = () => busy(() => openLab(lab.id));
      card.querySelector('.cl-run').onclick = () => busy(async () => {
        const active = await api('POST', `api/custom-labs/${lab.id}/activate`);
        enter(active);
      });
      host.append(card);
      iconize(card);
    }
  }

  async function openLab(id) {
    await api('POST', `api/custom-labs/${id}/activate`);
    state.id = id;
    const [detail, catalog] = await Promise.all([
      api('GET', `api/custom-labs/${id}`), api('GET', `api/custom-labs/${id}/models`),
    ]);
    state.detail = detail;
    state.models = catalog.models || [];
    state.selected = null;
    $('cl-list-view').hidden = true;
    $('cl-editor').hidden = false;
    const url = new URL(location.href);
    url.searchParams.set('id', id);
    history.replaceState(null, '', url);
    render();
    message('拓扑已打开。添加设备后可拖动位置，并选择接口连线。');
  }

  function enter(lab = state.detail) {
    const params = new URLSearchParams({ custom: String(lab.id), environment: lab.environment,
      sandbox: String(lab.sandbox), name: lab.name });
    location.href = `./?${params}`;
  }

  async function refresh() {
    if (state.id == null) return;
    state.detail = await api('GET', `api/custom-labs/${state.id}`);
    if (state.selected && !(state.detail.devices || []).some((d) => d.id === state.selected)) state.selected = null;
    render();
  }

  function nodePoint(device) {
    const x = Math.max(20, Math.min(980, (device.x + 0.5) * 1000));
    const y = Math.max(20, Math.min(980, (device.y + 0.5) * 1000));
    return { x, y };
  }

  function drawLinks(override) {
    const svg = $('cl-links');
    svg.replaceChildren();
    const devices = state.detail?.devices || [];
    const byPort = new Map(devices.flatMap((d) => d.interfaces.map((port) => [port.id, { device: d, port }])));
    const rect = canvas.getBoundingClientRect();
    const halfW = rect.width ? 56 / rect.width * 1000 : 56;
    const halfH = rect.height ? 62 / rect.height * 1000 : 62;
    for (const link of state.detail?.links || []) {
      const left = byPort.get(link.left_if), right = byPort.get(link.right_if);
      if (!left || !right) continue;
      const a = nodePoint(override?.id === left.device.id ? { ...left.device, ...override } : left.device);
      const b = nodePoint(override?.id === right.device.id ? { ...right.device, ...override } : right.device);
      const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1;
      const ux = dx / length, uy = dy / length, nx = -uy, ny = ux;
      const scaleA = 1 / Math.max(Math.abs(dx) / halfW, Math.abs(dy) / halfH, 1);
      const scaleB = 1 / Math.max(Math.abs(dx) / halfW, Math.abs(dy) / halfH, 1);
      const x1 = a.x + dx * scaleA, y1 = a.y + dy * scaleA;
      const x2 = b.x - dx * scaleB, y2 = b.y - dy * scaleB;
      const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      group.setAttribute('class', 'cl-link-group');
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      for (const [key, value] of Object.entries({ x1, y1, x2, y2 })) line.setAttribute(key, value);
      group.append(line);
      for (const [x, y] of [[x1, y1], [x2, y2]]) {
        const portDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        for (const [key, value] of Object.entries({ cx: x, cy: y, r: 2.3, class: 'cl-link-port' })) portDot.setAttribute(key, value);
        group.append(portDot);
      }
      const label = (x, y, name, anchor, deviceName) => {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('class', 'cl-link-label');
        text.setAttribute('x', x);
        text.setAttribute('y', y);
        text.setAttribute('text-anchor', anchor);
        text.setAttribute('dominant-baseline', 'middle');
        text.textContent = String(name || '').replace(/^GigabitEthernet/i, 'GE').replace(/^Ethernet/i, 'Eth').replace(/^Serial/i, 'S');
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        title.textContent = `${deviceName} ${name || ''}`;
        text.append(title);
        group.append(text);
      };
      const labelSide = Math.sign(ux) || 1;
      label(x1 + ux * 8 + nx * 9 * labelSide, y1 + uy * 8 + ny * 9 * labelSide,
        left.port.name, Math.abs(ux) < 0.2 ? 'middle' : (ux > 0 ? 'start' : 'end'), left.device.name);
      label(x2 - ux * 8 - nx * 9 * labelSide, y2 - uy * 8 - ny * 9 * labelSide,
        right.port.name, Math.abs(ux) < 0.2 ? 'middle' : (ux > 0 ? 'end' : 'start'), right.device.name);
      svg.append(group);
    }
  }

  function dragNode(element, device) {
    element.addEventListener('pointerdown', (event) => {
      if (state.busy || event.button !== 0) return;
      state.dragging = true;
      const start = { x: event.clientX, y: event.clientY };
      let point = { x: device.x, y: device.y };
      let moved = false;
      element.setPointerCapture(event.pointerId);
      const move = (e) => {
        const rect = canvas.getBoundingClientRect();
        if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 5) moved = true;
        if (!moved) return;
        const maxX = Math.min(0.48, 0.5 - (element.offsetWidth / 2 + 8) / rect.width);
        const maxY = Math.min(0.48, 0.5 - (element.offsetHeight / 2 + 8) / rect.height);
        point = { x: Math.max(-maxX, Math.min(maxX, (e.clientX - rect.left) / rect.width - 0.5)),
          y: Math.max(-maxY, Math.min(maxY, (e.clientY - rect.top) / rect.height - 0.5)) };
        element.style.left = `${(point.x + 0.5) * 100}%`;
        element.style.top = `${(point.y + 0.5) * 100}%`;
        drawLinks({ id: device.id, ...point });
      };
      const end = async () => {
        element.removeEventListener('pointermove', move);
        element.removeEventListener('pointerup', end);
        element.removeEventListener('pointercancel', end);
        state.dragging = false;
        state.selected = device.id;
        if (moved) await busy(async () => {
          state.detail = await api('PATCH', `api/custom-labs/${state.id}/devices/${device.id}`, point);
          render();
          message(`${device.name} 的位置已保存`);
        });
        else render();
      };
      element.addEventListener('pointermove', move);
      element.addEventListener('pointerup', end);
      element.addEventListener('pointercancel', end);
    });
  }

  function renderNodes() {
    const host = $('cl-nodes');
    host.replaceChildren();
    for (const device of state.detail.devices) {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'cl-node' + (state.selected === device.id ? ' selected' : '');
      el.style.left = `${(device.x + 0.5) * 100}%`;
      el.style.top = `${(device.y + 0.5) * 100}%`;
      el.title = `${device.name} · ${device.model} · ${statusLabel(device.status)}`;
      const icon = window.AInetDeviceIcons ? window.AInetDeviceIcons.url(device) : '';
      el.innerHTML = `<span class="cl-device-icon" aria-hidden="true"><img class="cl-device-img" src="${esc(icon)}" alt=""></span><strong>${esc(device.name)}</strong><small>${esc(device.model)}</small>
        <span class="cl-node-status"><i class="${esc(device.status)}"></i>${esc(statusLabel(device.status))}</span>`;
      el.onclick = (event) => { if (!event.detail && !state.busy) { state.selected = device.id; render(); } };
      host.append(el);
      dragNode(el, device);
    }
    drawLinks();
  }

  function renderModels() {
    const select = $('cl-model');
    const old = select.value;
    select.replaceChildren();
    const groups = new Map();
    for (const model of state.models) {
      const category = model.category || '其他';
      if (!groups.has(category)) {
        const group = document.createElement('optgroup');
        group.label = category;
        groups.set(category, group);
        select.append(group);
      }
      const option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.label === model.name ? model.name : `${model.label} · ${model.name}`;
      groups.get(category).append(option);
    }
    if (state.models.some((m) => String(m.id) === old)) select.value = old;
    $('cl-device-form').querySelector('button').disabled = !state.models.length;
  }

  function renderPorts() {
    const used = new Set(state.detail.links.flatMap((l) => [l.left_if, l.right_if]));
    for (const id of ['cl-left-if', 'cl-right-if']) {
      const select = $(id);
      const old = select.value;
      select.replaceChildren();
      const prompt = document.createElement('option');
      prompt.value = '';
      prompt.textContent = '选择设备接口';
      select.append(prompt);
      for (const device of state.detail.devices) {
        const group = document.createElement('optgroup');
        group.label = device.name;
        for (const port of device.interfaces) {
          const option = document.createElement('option');
          option.value = port.id;
          option.textContent = `${device.name} · ${port.name}${used.has(port.id) ? '（已占用）' : ''}`;
          option.disabled = used.has(port.id);
          group.append(option);
        }
        select.append(group);
      }
      if ([...select.options].some((o) => o.value === old && !o.disabled)) select.value = old;
    }
  }

  function renderSelected() {
    const host = $('cl-selected');
    const device = state.detail.devices.find((d) => d.id === state.selected);
    if (!device) { host.textContent = '在画布上选择设备，查看状态与接口。'; return; }
    const action = device.status === 'stopped' ? 'start' : 'stop';
    const active = !device.powerable || device.status === 'stopping';
    host.innerHTML = `<div class="cl-selected-title"><strong>${esc(device.name)}</strong><span>${esc(statusLabel(device.status))}</span></div>
      <div class="muted">${esc(device.model)} · ${device.interfaces.length} 个接口</div>
      <div class="cl-port-list">${device.interfaces.length ? device.interfaces.map((p) => esc(p.name)).join(' · ') : '无可用接口'}</div>
      <div class="cl-selected-actions"><button type="button" id="cl-power" ${active ? 'disabled' : ''}>${!device.powerable ? '无需启动' : action === 'stop' ? '停止设备' : '启动设备'}</button>
      <button type="button" id="cl-remove-device" class="danger">删除设备</button></div>`;
    $('cl-power').onclick = () => busy(async () => {
      await api('POST', `api/lab/devices/${action}`, { devices: [device.id] });
      await refresh();
      message(`已发送${action === 'stop' ? '停止' : '启动'} ${device.name} 的请求，请稍候查看状态。`);
    });
    $('cl-remove-device').onclick = () => {
      if (!confirm(`删除设备 ${device.name}？与它相连的连线也会被删除。`)) return;
      busy(async () => {
        state.detail = await api('DELETE', `api/custom-labs/${state.id}/devices/${device.id}`);
        state.selected = null;
        render();
        message(`已删除设备 ${device.name}`);
      });
    };
  }

  function renderLinkList() {
    const host = $('cl-link-list');
    host.replaceChildren();
    const byPort = new Map(state.detail.devices.flatMap((d) => d.interfaces.map((p) => [p.id, `${d.name} ${p.name}`])));
    if (!state.detail.links.length) { host.innerHTML = '<div class="muted">暂无连线</div>'; return; }
    for (const link of state.detail.links) {
      const row = document.createElement('div');
      row.className = 'cl-link-row';
      row.innerHTML = `<span>${esc(byPort.get(link.left_if) || link.left_if)}<br>↔ ${esc(byPort.get(link.right_if) || link.right_if)}</span><button type="button" class="danger">删除</button>`;
      row.querySelector('button').onclick = () => {
        if (!confirm('删除这条连线？')) return;
        busy(async () => {
          state.detail = await api('DELETE', `api/custom-labs/${state.id}/links/${link.id}`);
          render();
          message('连线已删除');
        });
      };
      host.append(row);
    }
  }

  function render() {
    if (!state.detail) return;
    const d = state.detail;
    $('cl-title').textContent = d.name;
    $('cl-env-label').textContent = `${d.environment_label} · 沙箱 ${d.sandbox}`;
    $('cl-summary').textContent = `${d.devices.length} 台设备 · ${d.links.length} 条连线 · 拓扑上限 ${d.limit} 台`
      + (d.max_running > 0 ? ` · 同时运行上限 ${d.max_running} 台网络设备` : '');
    renderNodes();
    renderModels();
    renderPorts();
    renderSelected();
    renderLinkList();
    iconize($('cl-editor'));
  }

  $('cl-create-form').onsubmit = (event) => {
    event.preventDefault();
    busy(async () => {
      const lab = await api('POST', 'api/custom-labs', {
        name: $('cl-name').value.trim(), environment: $('cl-environment').value,
      });
      $('cl-name').value = '';
      await openLab(lab.id);
    });
  };
  $('cl-back').onclick = () => busy(async () => {
    state.id = null;
    state.detail = null;
    $('cl-editor').hidden = true;
    $('cl-list-view').hidden = false;
    const url = new URL(location.href);
    url.searchParams.delete('id');
    history.replaceState(null, '', url);
    await list();
    message('');
  });
  $('cl-refresh').onclick = () => busy(async () => { await refresh(); message('拓扑状态已刷新'); });
  $('cl-enter').onclick = () => busy(async () => {
    const lab = await api('POST', `api/custom-labs/${state.id}/activate`);
    enter(lab);
  });
  $('cl-device-form').onsubmit = (event) => {
    event.preventDefault();
    busy(async () => {
      const n = state.detail.devices.length;
      const x = Math.max(-0.4, Math.min(0.4, -0.36 + (n % 5) * 0.18));
      const y = Math.max(-0.4, Math.min(0.4, -0.30 + Math.floor(n / 5) * 0.18));
      state.detail = await api('POST', `api/custom-labs/${state.id}/devices`, {
        name: $('cl-device-name').value.trim(), model_id: Number($('cl-model').value), x, y,
      });
      const created = state.detail.devices.find((d) => d.name === $('cl-device-name').value.trim());
      state.selected = created?.id || null;
      $('cl-device-name').value = '';
      render();
      message('设备已添加。可以在画布上拖动到合适的位置。');
    });
  };
  $('cl-link-form').onsubmit = (event) => {
    event.preventDefault();
    busy(async () => {
      state.detail = await api('POST', `api/custom-labs/${state.id}/links`, {
        left_if: Number($('cl-left-if').value), right_if: Number($('cl-right-if').value),
      });
      render();
      message('连线已添加');
    });
  };
  $('cl-delete-lab').onclick = () => {
    const d = state.detail;
    if (!confirm(`删除自定义实验「${d.name}」及其全部拓扑？此操作无法撤销，请先停止设备。`)) return;
    busy(async () => {
      await api('DELETE', `api/custom-labs/${d.id}`);
      state.id = null;
      state.detail = null;
      $('cl-editor').hidden = true;
      $('cl-list-view').hidden = false;
      const url = new URL(location.href);
      url.searchParams.delete('id');
      history.replaceState(null, '', url);
      await list();
      message(`已删除实验「${d.name}」`);
    });
  };

  const requested = Number(new URLSearchParams(location.search).get('id'));
  busy(async () => {
    await list();
    if (requested > 0) await openLab(requested);
    else message('');
  });
  setInterval(() => {
    if (state.id && !state.busy && !state.dragging && !document.hidden) refresh().catch((error) => message(error.message, true));
  }, 6000);
})();
