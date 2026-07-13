'use strict';

(() => {
  const img = document.getElementById('video-stream');
  if (!img) return;

  const overlay = document.getElementById('video-overlay');

  img.addEventListener('error', (event) => {
    console.error(
      '[VIDEO-CLIENT] error event fired',
      'time=' + new Date().toISOString(),
      'src=' + img.src,
      'naturalWidth=' + img.naturalWidth,
      'naturalHeight=' + img.naturalHeight,
      'complete=' + img.complete,
      event
    );
    if (overlay) {
      overlay.textContent = 'מתחבר מחדש לשידור...';
      overlay.hidden = false;
    }
    // Cache-bust on retry so the browser re-requests the MJPEG endpoint
    // instead of replaying the failed response from cache.
    setTimeout(() => {
      console.log('[VIDEO-CLIENT] retrying, time=' + new Date().toISOString());
      img.src = `/api/stream/mjpeg?retry=${Date.now()}`;
    }, 3000);
  });

  img.addEventListener('load', () => {
    console.log(
      '[VIDEO-CLIENT] load event fired',
      'time=' + new Date().toISOString(),
      'naturalWidth=' + img.naturalWidth,
      'naturalHeight=' + img.naturalHeight
    );
    if (overlay) overlay.hidden = true;
  });

  console.log('[VIDEO-CLIENT] starting stream, time=' + new Date().toISOString());
  img.src = '/api/stream/mjpeg';
})();
