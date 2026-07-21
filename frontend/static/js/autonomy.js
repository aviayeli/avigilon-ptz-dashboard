'use strict';

(() => {
  const toggleBtn = document.getElementById('autonomy-toggle');
  const statusEl = document.getElementById('autonomy-status');
  const alarmBanner = document.getElementById('alarm-banner');
  const alarmDismissBtn = document.getElementById('alarm-dismiss-btn');
  const ptzOverrideHint = document.getElementById('ptz-override-hint');
  const panMinInput = document.getElementById('range-pan-min');
  const panMaxInput = document.getElementById('range-pan-max');
  const tiltMinInput = document.getElementById('range-tilt-min');
  const tiltMaxInput = document.getElementById('range-tilt-max');
  const rangeInputs = [panMinInput, panMaxInput, tiltMinInput, tiltMaxInput];
  const setCenterBtn = document.getElementById('set-center-btn');
  if (!toggleBtn) return;

  // Populate the range inputs with this camera's real mechanical limits,
  // expressed as offsets from the current center (0, 0) reference (already
  // on the -100..100 friendly scale the API returns).
  function loadLimits() {
    return fetch('/api/autonomy/limits')
      .then((response) => response.json())
      .then((limits) => {
        panMinInput.min = tiltMinInput.min = -100;
        panMinInput.max = panMaxInput.max = Math.round(limits.pan_max);
        panMinInput.min = panMaxInput.min = Math.round(limits.pan_min);
        tiltMinInput.max = tiltMaxInput.max = Math.round(limits.tilt_max);
        tiltMinInput.min = tiltMaxInput.min = Math.round(limits.tilt_min);
        panMinInput.value = Math.round(limits.pan_min);
        panMaxInput.value = Math.round(limits.pan_max);
        tiltMinInput.value = Math.round(limits.tilt_min);
        tiltMaxInput.value = Math.round(limits.tilt_max);
      })
      .catch(() => {
        // Leave the inputs empty; the server will still clamp to the real
        // hardware limits when Start Search is pressed.
      });
  }

  loadLimits();

  if (setCenterBtn) {
    setCenterBtn.addEventListener('click', () => {
      setCenterBtn.disabled = true;
      Api.postJSON('/api/ptz/center', {})
        .then(loadLimits)
        .catch((err) => {
          statusEl.textContent = err.message || 'הבקשה נכשלה';
        })
        .then(() => {
          setCenterBtn.disabled = false;
        });
    });
  }

  const MODE_LABELS = {
    idle: 'במנוחה',
    searching: 'בסריקה...',
    investigating: 'עוקב אחר עצם לא מזוהה...',
    tracking: 'מעקב אחרי רחפן!'
  };

  let currentMode = 'idle';
  let alarmActive = false;
  let audioCtx = null;
  let oscillator = null;

  function ensureAudioContext() {
    // Must be created/resumed from a real user gesture (this button's
    // click), otherwise the browser's autoplay policy silently blocks it
    // and the later, poll-triggered alarm tone would never be heard.
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
  }

  function startAlarmTone() {
    if (!audioCtx || oscillator) return;
    oscillator = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    oscillator.type = 'square';
    oscillator.frequency.value = 900;
    gain.gain.value = 0.15;
    oscillator.connect(gain).connect(audioCtx.destination);
    oscillator.start();
  }

  function stopAlarmTone() {
    if (!oscillator) return;
    oscillator.stop();
    oscillator.disconnect();
    oscillator = null;
  }

  function applyMode(mode) {
    if (mode === currentMode) return;
    currentMode = mode;
    statusEl.textContent = MODE_LABELS[mode] || mode;
    toggleBtn.textContent = mode === 'idle' ? 'התחל סריקה' : 'עצור סריקה';

    // PTZ controls themselves are never disabled here — manual commands
    // now automatically take over from autonomy on the backend. Only the
    // range inputs + set-center button are disabled while a scan is
    // active, since those configure the scan itself.
    const active = mode !== 'idle';
    if (ptzOverrideHint) ptzOverrideHint.hidden = !active;
    rangeInputs.forEach((input) => {
      if (input) input.disabled = active;
    });
    if (setCenterBtn) setCenterBtn.disabled = active;
  }

  function applyAlarm(active) {
    if (active === alarmActive) return;
    alarmActive = active;
    if (alarmBanner) alarmBanner.hidden = !active;
    if (active) {
      startAlarmTone();
    } else {
      stopAlarmTone();
    }
  }

  if (alarmDismissBtn) {
    alarmDismissBtn.addEventListener('click', () => {
      // Stop the local tone immediately for responsiveness; the banner
      // itself will hide on the next status poll once the server clears
      // alarm_active (applyAlarm above).
      stopAlarmTone();
      Api.postJSON('/api/autonomy/alarm/dismiss', {}).catch(() => {});
    });
  }

  function pollStatus() {
    fetch('/api/autonomy/status')
      .then((response) => {
        if (!response.ok) throw new Error(`status ${response.status}`);
        return response.json();
      })
      .then((data) => {
        applyMode(data.mode);
        applyAlarm(!!data.alarm_active);
      })
      .catch(() => {
        // Leave the last-known state displayed; the shared connection
        // status pill already reports NVR/session problems separately.
      });
  }

  toggleBtn.addEventListener('click', () => {
    ensureAudioContext();
    const starting = currentMode === 'idle';
    const action = starting ? '/api/autonomy/start' : '/api/autonomy/stop';
    const body = starting
      ? {
          pan_min: parseFloat(panMinInput.value),
          pan_max: parseFloat(panMaxInput.value),
          tilt_min: parseFloat(tiltMinInput.value),
          tilt_max: parseFloat(tiltMaxInput.value)
        }
      : {};
    toggleBtn.disabled = true;
    Api.postJSON(action, body)
      .then(pollStatus)
      .catch((err) => {
        statusEl.textContent = err.message || 'הבקשה נכשלה';
      })
      .then(() => {
        toggleBtn.disabled = false;
      });
  });

  pollStatus();
  setInterval(pollStatus, 1000);
})();
