'use strict';

/*
 * Bootstrap for index.html. Every handler below guards on the relevant
 * element existing.
 */
(() => {
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then((response) => {
      return response
        .json()
        .catch(() => ({}))
        .then((data) => {
          if (!response.ok) {
            const message = data && data.detail ? data.detail : `הבקשה נכשלה (${response.status})`;
            throw new Error(message);
          }
          return data;
        });
    });
  }

  // Shared across app.js / video-stream.js / ptz-controls.js via plain globals
  // (no ES modules, no build step — scripts load in order and share scope).
  window.Api = { postJSON };

  const statusPill = document.getElementById('connection-status');
  if (statusPill) {
    const statusDot = statusPill.querySelector('.status-dot');
    const statusText = statusPill.querySelector('.status-text');
    const infoChannel = document.getElementById('info-channel');
    const infoNvr = document.getElementById('info-nvr');

    function setStatus(connected, label) {
      if (statusDot) {
        statusDot.classList.toggle('connected', connected);
        statusDot.classList.toggle('disconnected', !connected);
      }
      if (statusText) statusText.textContent = label;
    }

    function pollStatus() {
      fetch('/api/system/status')
        .then((response) => {
          if (!response.ok) throw new Error(`status ${response.status}`);
          return response.json();
        })
        .then((data) => {
          const connected = !!(data && data.nvr_connected && data.onvif_connected);
          setStatus(connected, connected ? 'מחובר' : 'מנותק');

          if (infoChannel && data && data.channel !== undefined && data.channel !== null) {
            infoChannel.textContent = data.channel;
          }
          if (infoNvr) {
            infoNvr.textContent = connected ? 'מחובר דרך NVR' : '-';
          }
        })
        .catch(() => {
          setStatus(false, 'מנותק');
          if (infoNvr) infoNvr.textContent = '-';
        });
    }

    pollStatus();
    setInterval(pollStatus, 10000);
  }
})();
