'use strict';

(() => {
  const motionBadge = document.getElementById('motion-badge');
  const droneBadge = document.getElementById('drone-badge');
  const infoFps = document.getElementById('info-fps');
  const infoInference = document.getElementById('info-inference');

  wireCollapsibleToggle('range-info-toggle', 'range-info-panel');

  if (!motionBadge && !droneBadge) return;

  function setBadge(badge, active, label) {
    if (!badge) return;
    badge.classList.toggle('active', active);
    badge.querySelector('.detection-badge-label').textContent = label;
  }

  function pollDetectionStatus() {
    fetch('/api/detection/status')
      .then((response) => {
        if (!response.ok) throw new Error(`status ${response.status}`);
        return response.json();
      })
      .then((data) => {
        const motion = data.motion || { active: false, confidence: 0 };
        setBadge(
          motionBadge,
          motion.active,
          motion.active ? `תנועה ${Math.round(motion.confidence * 100)}%` : 'אין תנועה'
        );

        const drone = data.drone;
        if (drone && drone.detected) {
          const distance = drone.distance_m != null ? ` ~${drone.distance_m} מ'` : '';
          setBadge(droneBadge, true, `רחפן ${Math.round(drone.confidence * 100)}%${distance}`);
        } else {
          setBadge(droneBadge, false, 'אין רחפן');
        }

        const perf = data.performance;
        if (perf) {
          if (infoFps) infoFps.textContent = `${perf.fps.toFixed(1)} fps`;
          if (infoInference) infoInference.textContent = `${perf.inference_ms} ms`;
        }
      })
      .catch(() => {
        // Leave the last-known badges displayed.
      });
  }

  pollDetectionStatus();
  setInterval(pollDetectionStatus, 1000);
})();
