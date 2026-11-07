// VIGÍA – página de sesión. Consulta el estado del orquestador y muestra la
// pantalla del Figma que corresponde. No procesa imágenes: sólo muestra datos.
(function () {
  'use strict';

  const root = document.getElementById('session');
  const urls = root.dataset;
  const POLL_MS = 250;
  const DEGRADED_HOLD_MS = 1000; // Evita parpadeos entre «medición no disponible» y normal.
  const RECOVERED_MS = 5000; // Tiempo que se muestra «Mirada recuperada».

  const LEVELS = {
    0: { name: 'Sin indicios', pattern: '' },
    1: { name: 'Señal inicial', pattern: 'Pulsos suaves' },
    2: { name: 'Atención reducida', pattern: 'Pulsos moderados' },
    3: { name: 'Riesgo alto', pattern: 'Pulsos intensos' },
  };
  const DEGRADED = {
    low_light: 'Hay poca luz: la luz media de la imagen es menor a 40.',
    no_face: 'No se detecta tu rostro desde hace más de medio segundo.',
  };

  const ui = {
    // Dentro de «preview»: 'camera' o 'bracelet'. Se guarda en la URL (#pulsera)
    // para no volver al paso de la cámara si se recarga la página.
    step: window.location.hash === '#pulsera' ? 'bracelet' : 'camera',
    previousLevel: 0,
    acknowledged: false, // «Continuar monitoreo» pulsado durante una alerta.
    recoveredUntil: 0,
    degradedVisible: false,
    lastDegradedAt: 0,
    videoOn: false,
  };

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
  const fixed = (value, digits) => (value === null || value === undefined ? '–' : value.toFixed(digits));

  function clock(seconds) {
    const s = Math.floor(seconds);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  function setCheck(id, status, text) {
    const item = document.getElementById(id);
    item.className = status;
    if (text !== undefined) $('[data-text]', item).textContent = text;
  }

  // ---------- Elegir pantalla ----------

  function pickScreen(s, now) {
    if (s.phase === 'error') return 'error';
    if (s.phase === 'starting' || s.phase === 'preview') return ui.step;
    if (s.phase === 'calibrating') return 'calibrating';
    if (s.phase === 'calibrated') return 'calibrated';
    if (s.phase === 'monitoring') {
      if (s.degraded_reason) {
        ui.lastDegradedAt = now;
        ui.degradedVisible = true;
      } else if (now - ui.lastDegradedAt > DEGRADED_HOLD_MS) {
        ui.degradedVisible = false;
      }
      if (ui.degradedVisible) return 'degraded';
      if (s.bracelet && !s.bracelet.connected) return 'disconnected';
      return 'monitor';
    }
    return null;
  }

  function showScreen(name, s) {
    let active = null;
    $$('[data-screen]').forEach((section) => {
      const visible = section.dataset.screen === name;
      section.hidden = !visible;
      if (visible) active = section;
    });
    if (!active) return;

    // Un solo <img> de video, que se mueve a la cámara de la pantalla visible.
    const slot = $('[data-camera-slot]', active);
    const live = document.getElementById('live');
    if (slot && live.parentElement !== slot) slot.prepend(live);
    if (!slot) live.hidden = true; // Pantallas sin tarjeta de cámara.

    const status = $('#top-status');
    if (name === 'monitor') {
      const alert = s.level > 0 && !ui.acknowledged;
      status.textContent = alert ? 'Alerta activa' : 'Sesión en curso';
      status.className = alert ? 'status is-alert' : 'status';
    } else {
      status.textContent = active.dataset.status || '';
      status.className = name === 'error' ? 'status is-error'
        : (name === 'degraded' || name === 'disconnected') ? 'status is-alert' : 'status';
    }
  }

  // ---------- Dibujar datos ----------

  function renderVideo(s) {
    const live = document.getElementById('live');
    const hasFrames = s.running && s.phase !== 'starting';
    if (hasFrames && !ui.videoOn) {
      live.src = urls.videoUrl;
      ui.videoOn = true;
    } else if (!s.running && ui.videoOn) {
      live.removeAttribute('src');
      ui.videoOn = false;
    }
    live.hidden = !ui.videoOn;
    $$('[data-camera-slot] .placeholder').forEach((p) => { p.style.display = ui.videoOn ? 'none' : ''; });
    $$('[data-camera-dot]').forEach((dot) => dot.classList.toggle('off', !ui.videoOn));
    $$('[data-fps]').forEach((el) => { el.textContent = s.fps ? `${Math.round(s.fps)} FPS` : '– FPS'; });
  }

  function renderSetup(s) {
    const cameraReady = s.phase !== 'starting';
    setCheck('chk-camera', cameraReady ? 'ok' : 'wait', cameraReady ? 'Cámara integrada lista.' : 'Abriendo la cámara…');
    setCheck('chk-face', !cameraReady ? 'wait' : s.face_detected ? 'ok' : 'bad');
    setCheck('chk-light', !cameraReady ? 'wait' : s.light_ok ? 'ok' : 'bad');
    document.getElementById('camera-continue').disabled = s.phase !== 'preview';

    const notice = document.getElementById('camera-notice');
    notice.hidden = !s.notice;
    if (s.notice) {
      $('[data-text]', notice).textContent = s.notice;
      if (ui.step !== 'camera') {
        ui.step = 'camera'; // Si falló la calibración se vuelve a la cámara.
        window.history.replaceState(null, '', '#');
      }
    }

    const bracelet = document.getElementById('chk-bracelet');
    const name = $('[data-name]', bracelet);
    if (!s.bracelet) {
      setCheck('chk-bracelet', 'wait', 'Preparando la conexión.');
      name.textContent = 'Buscando la pulsera…';
    } else if (s.bracelet.connected) {
      setCheck('chk-bracelet', 'ok', s.bracelet_simulated ? 'Modo de demostración, sin hardware.' : 'Conectada por Bluetooth.');
      name.textContent = `${s.bracelet_name} conectada`;
    } else {
      setCheck('chk-bracelet', 'wait', s.bracelet.last_error || 'Buscando…');
      name.textContent = 'Buscando la pulsera…';
    }

    const remaining = Math.ceil(5 * (1 - s.calibration_progress));
    document.getElementById('calib-seconds').textContent = `${remaining} s`;
    document.getElementById('calib-bar').style.width = `${Math.round(s.calibration_progress * 100)}%`;
    document.getElementById('threshold').textContent = fixed(s.threshold, 2);
  }

  function renderMonitor(s, now) {
    if (ui.previousLevel > 0 && s.level === 0) ui.recoveredUntil = now + RECOVERED_MS;
    if (s.level > ui.previousLevel) ui.acknowledged = false; // Si sube de nivel, vuelve a avisar.
    if (s.level === 0) ui.acknowledged = false;
    if (s.level > 0) ui.recoveredUntil = 0;
    ui.previousLevel = s.level;
    const recovered = s.level === 0 && now < ui.recoveredUntil;
    const alert = s.level > 0 && !ui.acknowledged;

    document.getElementById('monitor-title').textContent =
      alert ? 'Somnolencia detectada' : recovered ? 'Mirada recuperada' : 'Monitoreo activo';
    document.getElementById('continue-monitoring').hidden = !(alert || recovered);
    document.getElementById('monitor-footnote').hidden = alert || recovered;

    const card = document.getElementById('level-card');
    card.dataset.level = String(s.level);
    document.getElementById('level-number').textContent = String(s.level);
    document.getElementById('level-name').textContent = LEVELS[s.level].name;
    let detail = 'Sin cierre ocular prolongado.';
    if (s.level > 0) {
      detail = s.closed_ms > 0 ? `Cierre ocular: ${(s.closed_ms / 1000).toFixed(1)} s` : 'Bostezos frecuentes.';
    } else if (recovered) {
      detail = 'Ojos abiertos · nivel normal.';
    }
    document.getElementById('level-detail').textContent = detail;

    const line = document.getElementById('bracelet-line');
    const connected = s.bracelet && s.bracelet.connected;
    line.classList.toggle('off', !connected);
    let title = 'Pulsera desconectada';
    let text = 'Reintentando la conexión…';
    if (connected) {
      title = s.level > 0 ? 'Vibración activa' : 'Pulsera conectada';
      text = s.level > 0 ? LEVELS[s.level].pattern : recovered ? 'Vibración detenida' : 'Vibración configurada';
      if (s.bracelet_simulated) text += ' · simulada';
    }
    $('[data-title]', line).textContent = title;
    $('[data-text]', line).textContent = text;

    document.getElementById('ear').textContent = fixed(s.ear, 2);
    document.getElementById('mar').textContent = fixed(s.mar, 2);
    document.getElementById('yawns').textContent = String(s.yawn_count);
    $$('[data-timer]').forEach((el) => { el.textContent = clock(s.elapsed_s); });

    const reason = DEGRADED[s.degraded_reason];
    if (reason) document.getElementById('degraded-reason').textContent = reason;
  }

  function render(s) {
    if (s.phase === 'finished' || (s.phase === 'idle' && !s.running)) {
      window.location.href = urls.summaryUrl;
      return;
    }
    const now = Date.now();
    renderVideo(s);
    renderSetup(s);
    renderMonitor(s, now);
    document.getElementById('error-text').textContent = s.error || '';
    $$('[data-simulated-only]').forEach((el) => { el.hidden = !s.bracelet_simulated; });
    showScreen(pickScreen(s, now), s);
  }

  // ---------- Botones ----------

  async function sendCommand(action, button) {
    if (button) button.disabled = true;
    try {
      const body = new FormData();
      body.append('action', action);
      await fetch(urls.commandUrl, {
        method: 'POST',
        body,
        headers: { 'X-CSRFToken': urls.csrf },
        credentials: 'same-origin',
      });
    } finally {
      if (button) setTimeout(() => { button.disabled = false; }, 800);
    }
  }

  document.addEventListener('click', (event) => {
    const goto = event.target.closest('[data-goto]');
    if (goto && !goto.disabled) {
      ui.step = goto.dataset.goto;
      window.history.replaceState(null, '', ui.step === 'bracelet' ? '#pulsera' : '#');
      poll();
      return;
    }
    const command = event.target.closest('[data-command]');
    if (command && !command.disabled) {
      sendCommand(command.dataset.command, command);
      return;
    }
    if (event.target.closest('#continue-monitoring')) {
      ui.acknowledged = true;
      ui.recoveredUntil = 0;
      poll();
    }
  });

  // ---------- Consulta periódica ----------

  let pending = false;
  async function poll() {
    if (pending) return;
    pending = true;
    try {
      const response = await fetch(urls.stateUrl, { cache: 'no-store' });
      if (response.ok) render(await response.json());
    } catch (error) {
      // Si el servidor se detiene un momento, se reintenta en la siguiente consulta.
    } finally {
      pending = false;
    }
  }

  poll();
  setInterval(poll, POLL_MS);
}());
