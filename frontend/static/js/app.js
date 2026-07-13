'use strict';

/*
 * Shared bootstrap for both login.html and index.html. Every handler below
 * guards on the relevant element existing, since this same script loads on
 * both pages.
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

  // Wires a toggle button to show/hide a collapsible panel (used for both
  // the range-info and event-log panels). Shared here since app.js loads
  // first on every page.
  function wireCollapsibleToggle(toggleId, panelId) {
    const toggle = document.getElementById(toggleId);
    const panel = document.getElementById(panelId);
    if (!toggle || !panel) return;
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!expanded));
      panel.hidden = expanded;
    });
  }

  // Shared across app.js / video-stream.js / ptz-controls.js via plain globals
  // (no ES modules, no build step — scripts load in order and share scope).
  window.Api = { postJSON };
  window.wireCollapsibleToggle = wireCollapsibleToggle;

  // ---- Login page ----
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const errorEl = document.getElementById('login-error');
    const submitBtn = document.getElementById('login-submit');

    loginForm.addEventListener('submit', (event) => {
      event.preventDefault();
      if (errorEl) errorEl.hidden = true;
      if (submitBtn) {
        submitBtn.classList.add('is-loading');
        submitBtn.disabled = true;
      }

      Api.postJSON('/api/auth/login', {
        username: usernameInput ? usernameInput.value : '',
        password: passwordInput ? passwordInput.value : ''
      })
        .then(() => {
          window.location.href = '/';
        })
        .catch((err) => {
          if (errorEl) {
            errorEl.textContent = err.message || 'ההתחברות נכשלה.';
            errorEl.hidden = false;
          }
          if (submitBtn) {
            submitBtn.classList.remove('is-loading');
            submitBtn.disabled = false;
          }
        });
    });
  }

  // ---- Dashboard page ----
  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', () => {
      // Best-effort logout: redirect regardless of whether the API call succeeds.
      Api.postJSON('/api/auth/logout', {})
        .catch(() => {})
        .then(() => {
          window.location.href = '/login';
        });
    });
  }

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
