'use strict';

(() => {
  const eventLogList = document.getElementById('event-log-list');
  const sensitivitySlider = document.getElementById('sensitivity-slider');
  const sensitivityValue = document.getElementById('sensitivity-value');

  const EVENT_TYPE_LABELS = {
    search_started: 'התחלת סריקה',
    search_stopped: 'עצירת סריקה',
    drone_detected: 'זוהה רחפן',
    object_identified: 'זוהה עצם שאינו רחפן',
    target_lost: 'המטרה אבדה',
    loop_error: 'שגיאה במערכת האוטונומית',
    video_stale: 'שידור הווידאו קפא - סריקה מושהית',
    video_recovered: 'שידור הווידאו חזר - סריקה ממשיכה'
  };

  function formatTime(isoString) {
    try {
      return new Date(isoString).toLocaleTimeString('he-IL');
    } catch (e) {
      return isoString;
    }
  }

  function renderEvents(events) {
    if (!eventLogList) return;
    if (!events.length) {
      eventLogList.innerHTML = '<li class="event-log-empty">אין אירועים עדיין</li>';
      return;
    }
    eventLogList.innerHTML = events
      .map((event) => {
        const label = EVENT_TYPE_LABELS[event.type] || event.type;
        const time = formatTime(event.timestamp);
        const message = event.message ? ` - ${event.message}` : '';
        const thumb = event.snapshot
          ? `<img class="event-log-thumb" src="${event.snapshot}" alt="תמונת אירוע">`
          : '';
        return `
          <li class="event-log-item event-log-item-${event.type}">
            ${thumb}
            <div class="event-log-text">
              <span class="event-log-time">${time}</span>
              <span class="event-log-label">${label}${message}</span>
            </div>
          </li>
        `;
      })
      .join('');
  }

  function pollEvents() {
    fetch('/api/events?limit=30')
      .then((response) => {
        if (!response.ok) throw new Error(`status ${response.status}`);
        return response.json();
      })
      .then(renderEvents)
      .catch(() => {
        // Leave the last-known event list displayed.
      });
  }

  if (eventLogList) {
    pollEvents();
    setInterval(pollEvents, 3000);
  }

  // ---- Sensitivity ----
  if (sensitivitySlider) {
    fetch('/api/detection/sensitivity')
      .then((response) => response.json())
      .then((data) => {
        const percent = Math.round(data.threshold * 100);
        sensitivitySlider.min = Math.round(data.min * 100);
        sensitivitySlider.max = Math.round(data.max * 100);
        sensitivitySlider.value = String(percent);
        if (sensitivityValue) sensitivityValue.textContent = `${percent}%`;
      })
      .catch(() => {});

    sensitivitySlider.addEventListener('input', () => {
      if (sensitivityValue) sensitivityValue.textContent = `${sensitivitySlider.value}%`;
    });

    sensitivitySlider.addEventListener('change', () => {
      const threshold = parseInt(sensitivitySlider.value, 10) / 100;
      Api.postJSON('/api/detection/sensitivity', { threshold }).catch(() => {});
    });
  }
})();
