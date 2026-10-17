(() => {
  'use strict';

  const ROOT = '/device-icons/ensp-old/';
  const FALLBACK = 'generic-device.svg';
  const FILE_SIZE = {
    'cloud3.png': [85, 85],
    'ne9000_little_icon.png': [45, 50],
    's128.png': [45, 47],
  };
  const norm = (value) => String(value || '').toUpperCase().replace(/\s+/g, ' ').trim();

  function file(device = {}) {
    const model = norm(device.model || device.device_model || device.deviceType?.name || device.name);
    const name = norm(device.name);
    const text = `${model} ${name}`;
    const kind = norm(device.kind);

    if (/CLOUD/.test(text) || kind === 'CLOUD') return 'cloud3.png';
    if (/FIREWALL|NGFW|USG|\bFW\d*\b/.test(text)) return 'iFW.png';
    if (/WEB[ _-]?CLIENT/.test(text)) return 'iWebClient.png';
    if (/WEB[ _-]?SERVER/.test(text)) return 'iWebServer.png';
    if (/CELL ?PHONE|MOBILE|SMART ?PHONE/.test(text)) return 'iCellphone.png';
    if (/LAPTOP|NOTEBOOK/.test(text)) return 'iLaptop.png';
    if (/WLAN.*(?:AC|CONTROLLER)|\bWAC\d|\bAC(?:\d|\b)/.test(text)) return 'iAC.png';
    if (/WLAN.*AP|\bAP(?:\d|\b)/.test(text)) return 'iAP.png';
    if (/NE9000/.test(text)) return 'ne9000_little_icon.png';
    if (/S128(?:00)?/.test(text)) return 's128.png';
    if (/CE6800/.test(text)) return 'iCE6800.png';
    if (/CORE.*(?:SW|LSW)|(?:SW|LSW).*CORE/.test(text)) return 'iCorelsw.png';
    if (/AR\d|\b[RT]\d+\b|ROUTER|\bNE\d/.test(text) || kind === 'ROUTER') return 'iRouter.png';
    if (/L3.*(?:SW|LSW)|(?:SW|LSW).*L3|LSW|S\d|CE\d/.test(text) || kind === 'SWITCH') return 'iL3lsw.png';
    if (/SERVER/.test(text)) return 'iWebServer.png';
    if (/PC|STATION|HOST|CLIENT/.test(text) || kind === 'PC') return 'iStation.png';
    return FALLBACK;
  }

  function size(device = {}) {
    return FILE_SIZE[file(device)] || [54, 54];
  }

  function url(device = {}) {
    return ROOT + file(device);
  }

  function appendSvg(parent, device, svgEl, bounds = null) {
    const [intrinsicW, intrinsicH] = size(device);
    const width = bounds?.width || intrinsicW;
    const height = bounds?.height || intrinsicH;
    return svgEl('image', {
      class: 'device-icon',
      href: url(device),
      x: bounds?.x ?? -width / 2,
      y: bounds?.y ?? -height / 2,
      width,
      height,
      preserveAspectRatio: 'xMidYMid meet',
      'aria-hidden': 'true',
    }, parent);
  }

  window.AInetDeviceIcons = Object.freeze({ file, size, url, appendSvg });
})();
