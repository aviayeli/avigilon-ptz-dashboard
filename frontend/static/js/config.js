'use strict';

/*
 * First-run connection settings panel (gear icon in the topbar). Lets a
 * non-technical operator set NVR/ONVIF connection details from the
 * dashboard instead of hand-editing backend/.env. Purely additive: the
 * modal is hidden by default and position:fixed, so it never affects the
 * zero-scroll dashboard layout underneath it.
 */
(() => {
  const openBtn = document.getElementById('config-open-btn');
  const modal = document.getElementById('config-modal');
  if (!openBtn || !modal) return;

  const closeBtn = document.getElementById('config-close-btn');
  const testBtn = document.getElementById('config-test-btn');
  const saveBtn = document.getElementById('config-save-btn');
  const statusEl = document.getElementById('config-status');

  const ipInput = document.getElementById('config-nvr-ip');
  const portInput = document.getElementById('config-nvr-port');
  const onvifPortInput = document.getElementById('config-onvif-port');
  const usernameInput = document.getElementById('config-username');
  const passwordInput = document.getElementById('config-password');
  const channelInput = document.getElementById('config-channel');

  function setBusy(busy) {
    [testBtn, saveBtn].forEach((btn) => {
      if (!btn) return;
      btn.disabled = busy;
      btn.classList.toggle('is-loading', busy);
    });
  }

  function setStatus(message) {
    if (statusEl) statusEl.textContent = message || '';
  }

  function currentPayload() {
    return {
      nvr_ip: ipInput.value.trim(),
      nvr_port: parseInt(portInput.value, 10),
      onvif_port: parseInt(onvifPortInput.value, 10),
      nvr_username: usernameInput.value.trim(),
      // Empty means "keep the currently saved password" -- the server
      // never sends the real password back to us (see loadConfig below),
      // so leaving this field blank is the only way to say "unchanged".
      nvr_password: passwordInput.value,
      camera_channel_id: parseInt(channelInput.value, 10)
    };
  }

  function loadConfig() {
    setStatus('');
    return fetch('/api/config')
      .then((response) => {
        if (!response.ok) throw new Error(`status ${response.status}`);
        return response.json();
      })
      .then((data) => {
        ipInput.value = data.nvr_ip || '';
        portInput.value = data.nvr_port;
        onvifPortInput.value = data.onvif_port;
        usernameInput.value = data.nvr_username || '';
        channelInput.value = data.camera_channel_id;
        passwordInput.value = '';
        passwordInput.placeholder = data.has_password ? '(ללא שינוי)' : '';
      })
      .catch(() => {
        setStatus('טעינת ההגדרות הנוכחיות נכשלה');
      });
  }

  function openModal() {
    modal.hidden = false;
    loadConfig();
  }

  function closeModal() {
    modal.hidden = true;
  }

  openBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);

  // Backdrop click: only when the click target is the backdrop itself, not
  // a bubbled click from inside the card.
  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) closeModal();
  });

  if (testBtn) {
    testBtn.addEventListener('click', () => {
      setBusy(true);
      setStatus('בודק חיבור...');
      Api.postJSON('/api/config/test', currentPayload())
        .then((result) => {
          if (!result.ok) {
            setStatus(result.error || 'החיבור נכשל');
            return;
          }
          let message = `החיבור תקין - ${result.channels} ערוצים זוהו`;
          if (!result.channel_valid) {
            message += ' - שים לב: מספר הערוץ שהוזן חורג מהערוצים שה-NVR חושף';
          }
          setStatus(message);
        })
        .catch((err) => {
          setStatus(err.message || 'הבדיקה נכשלה');
        })
        .then(() => setBusy(false));
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      setBusy(true);
      setStatus('שומר...');
      Api.postJSON('/api/config', currentPayload())
        .then(() => {
          setStatus('נשמר. יש להפעיל מחדש את השרת כדי להחיל את ההגדרות.');
          // Re-fetch so the password placeholder reflects what's now saved,
          // without ever putting the real password back into the field.
          return loadConfig().then(() => {
            setStatus('נשמר. יש להפעיל מחדש את השרת כדי להחיל את ההגדרות.');
          });
        })
        .catch((err) => {
          setStatus(err.message || 'השמירה נכשלה');
        })
        .then(() => setBusy(false));
    });
  }
})();
