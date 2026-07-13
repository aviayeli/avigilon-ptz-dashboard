'use strict';

(() => {
  const root = document.getElementById('ptz-controls');
  if (!root) return;

  // Wraps a PTZ API call with a visible loading state, and a brief error
  // flash on failure. Rejections are swallowed here: PTZ move/stop calls
  // are fire-and-forget, callers don't need to handle them individually.
  function withLoadingState(button, promise) {
    if (button) button.classList.add('is-loading');
    return promise.then(
      (result) => {
        if (button) button.classList.remove('is-loading');
        return result;
      },
      () => {
        if (button) {
          button.classList.remove('is-loading');
          button.classList.add('is-error');
          setTimeout(() => button.classList.remove('is-error'), 600);
        }
      }
    );
  }

  // Pointer events unify mouse/touch/pen input, so a single set of listeners
  // covers press-and-hold continuous motion for dpad/zoom/focus/iris without
  // needing separate mouse/touch handlers (which would double-fire on touch).
  //
  // Pointer capture is essential here: buttons visually shrink on press
  // (.is-pressed { transform: scale(0.96) }), which can shift the button's
  // edge out from under the pointer the instant it's pressed. Without
  // capture, that triggers a spurious pointerleave -> release, stopping
  // the move immediately after it starts. Capturing the pointer keeps
  // up/cancel events targeted at this button regardless of where the
  // pointer physically ends up.
  function bindPressHold(selector, buildMoveBody, moveUrl, stopUrl, buildStopBody) {
    root.querySelectorAll(selector).forEach((btn) => {
      btn.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        btn.setPointerCapture(event.pointerId);
        btn.classList.add('is-pressed');
        withLoadingState(btn, Api.postJSON(moveUrl, buildMoveBody(btn)));
      });

      const release = () => {
        btn.classList.remove('is-pressed');
        withLoadingState(btn, Api.postJSON(stopUrl, buildStopBody ? buildStopBody(btn) : {}));
      };
      btn.addEventListener('pointerup', release);
      btn.addEventListener('pointercancel', release);
      btn.addEventListener('lostpointercapture', release);
    });
  }

  // D-pad: continuous pan/tilt while held.
  bindPressHold(
    '.ptz-dpad .ptz-btn[data-pan]',
    (btn) => ({ pan: parseFloat(btn.dataset.pan), tilt: parseFloat(btn.dataset.tilt), zoom: 0 }),
    '/api/ptz/move',
    '/api/ptz/stop'
  );

  // Zoom: continuous zoom while held.
  bindPressHold(
    '.ptz-zoom .ptz-btn[data-zoom]',
    (btn) => ({ pan: 0, tilt: 0, zoom: parseFloat(btn.dataset.zoom) }),
    '/api/ptz/move',
    '/api/ptz/stop'
  );

  // Focus near/far: continuous focus while held, speed 0 on release.
  bindPressHold(
    '.ptz-focus .ptz-btn[data-focus-speed]',
    (btn) => ({ mode: 'continuous', speed: parseFloat(btn.dataset.focusSpeed) }),
    '/api/ptz/focus',
    '/api/ptz/focus',
    () => ({ mode: 'continuous', speed: 0 })
  );

  // Iris open/close: continuous iris while held, speed 0 on release.
  bindPressHold(
    '.ptz-iris .ptz-btn[data-iris-speed]',
    (btn) => ({ speed: parseFloat(btn.dataset.irisSpeed) }),
    '/api/ptz/iris',
    '/api/ptz/iris',
    () => ({ speed: 0 })
  );

  const homeBtn = document.getElementById('ptz-home');
  if (homeBtn) {
    homeBtn.addEventListener('click', () => {
      withLoadingState(homeBtn, Api.postJSON('/api/ptz/home', {}));
    });
  }

  const focusAutoBtn = document.getElementById('focus-auto');
  if (focusAutoBtn) {
    focusAutoBtn.addEventListener('click', () => {
      withLoadingState(focusAutoBtn, Api.postJSON('/api/ptz/focus', { mode: 'auto' }));
    });
  }

  // Aux toggle buttons live in the info panel (aside.info-panel), not under
  // nav#ptz-controls, so they're queried from the document rather than root.
  // They're instant commands, not press/hold.
  document.querySelectorAll('.aux-btn[data-aux]').forEach((btn) => {
    btn.addEventListener('click', () => {
      withLoadingState(btn, Api.postJSON('/api/ptz/aux', { command: btn.dataset.aux }));
    });
  });
})();
