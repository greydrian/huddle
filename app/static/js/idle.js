/* The idle screen (spec 10.2; server side: app/idle.py, templates/_idle.html).
 *
 * Huddle owns idle, not Fully Kiosk (whose screensaver and screen-off timer
 * stay off). After `delay_minutes` with no pointer, touch or key input the
 * wall does one of: a photo slideshow (a calm clock when there are no
 * photos), dim, or nothing ("stay on the dashboard"). At night it does
 * `night_mode` instead. Night is Admin's own night_start–night_end when set
 * (read off the family clock below), otherwise Appearance's night mode
 * (<html data-mode>, kept current by base.html).
 *
 * Never idle while the wall is in use: fresh.js's busy rules
 * (window.huddleBusy: typing, the on-screen keyboard, a finger down, a drag,
 * a fold open and recently touched, a tick still showing), and not within
 * BANNER_GRACE_MS of a new banner (banners.js fires "huddle:banner").
 *
 * Waking: the first pointerdown/touchstart/key only wakes. It is caught on
 * window in the capture phase, before anything else sees it, and it and the
 * rest of its gesture (up, click, mouse compatibility events) are cancelled,
 * so it never ticks a task, presses a button or starts a Gridstack drag. The
 * next tap works normally.
 *
 * Dim: with Fully Kiosk's JavaScript interface (Fully PLUS), the backlight
 * is turned down with fully.setScreenBrightness and put back on wake;
 * otherwise a dark overlay. The brightness to restore is kept in
 * localStorage, so a reload while dim (fresh.js reloads for a layout change
 * or a new day) comes back dim instead of leaving the backlight low for good.
 *
 * Clock: family time from the server (like the top bar's date) plus the
 * time elapsed on the tablet; the tablet's own clock and timezone never
 * show. Next event and weather come from the server's caches (/api/idle,
 * re-read on going idle and every minute while idle), so they work offline.
 *
 * Slideshow: two stacked <img>s crossfaded with CSS opacity only (cheap on
 * the tablet). The pure functions are on window.HuddleIdle.logic for the
 * browser tests.
 */
(function () {
  'use strict';

  var TICK_MS = 1000;
  var BANNER_GRACE_MS = 60000;
  var REFRESH_MS = 60000;
  var SWALLOW_MS = 400;     // after the waking tap lifts, what's left of it is still cancelled
  var RESUME_MS = 120000;   // a reload this soon after going idle comes back idle
  var STORE = 'huddle-idle';
  var FETCH_TIMEOUT_MS = 10000;

  // --- Pure logic -----------------------------------------------------------------------

  function parseHHMM(value) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(value || '');
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  }

  // Whether `minutes` past midnight is in [start, end), which may run past midnight.
  function inRange(minutes, start, end) {
    if (start === null || end === null || start === end) return false;
    return start < end ? (minutes >= start && minutes < end) : (minutes >= start || minutes < end);
  }

  // What the wall does when idle now: 'slideshow' | 'dim' | 'dashboard'.
  function effectiveMode(cfg, night) {
    return night ? cfg.night_mode : cfg.mode;
  }

  // s: {now, lastActivity, delayMs, mode, busy, lastBannerAt}
  function shouldGoIdle(s) {
    if (s.mode === 'dashboard' || s.busy) return false;
    if (s.lastBannerAt && s.now - s.lastBannerAt < BANNER_GRACE_MS) return false;
    return s.now - s.lastActivity >= s.delayMs;
  }

  // Fully's brightness runs 0-255.
  function brightnessLevel(percent) {
    return Math.max(1, Math.min(255, Math.round(percent * 255 / 100)));
  }

  function nextIndex(index, count) {
    return count ? (index + 1) % count : -1;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // Family wall-clock time as a Date whose UTC fields hold it.
  function parseLocal(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(iso || '');
    if (!m) return null;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }

  function formatTime(d) { return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()); }

  function formatDate(d) {
    return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' });
  }

  var logic = {
    parseHHMM: parseHHMM, inRange: inRange, effectiveMode: effectiveMode, shouldGoIdle: shouldGoIdle,
    brightnessLevel: brightnessLevel, nextIndex: nextIndex, parseLocal: parseLocal, formatTime: formatTime,
  };

  var screen = document.getElementById('idle-screen');
  if (!screen) { window.HuddleIdle = { logic: logic }; return; }

  var root = document.documentElement;
  var cfg = {};
  try { cfg = JSON.parse(screen.dataset.config || '{}'); } catch (err) { cfg = {}; }

  // --- Family clock -----------------------------------------------------------------------

  var clockBase = null;
  var clockAt = 0;
  function setClock(iso) {
    var base = parseLocal(iso);
    if (base === null) return;
    clockBase = base;
    clockAt = Date.now();
  }
  function familyNow() {
    return new Date((clockBase === null ? Date.now() : clockBase) + (Date.now() - clockAt));
  }
  setClock(cfg.now);

  function isNight() {
    var start = parseHHMM(cfg.night_start), end = parseHHMM(cfg.night_end);
    if (start !== null && end !== null) {
      var now = familyNow();
      return inRange(now.getUTCHours() * 60 + now.getUTCMinutes(), start, end);
    }
    return root.dataset.mode === 'night';
  }

  // --- Fully Kiosk brightness -------------------------------------------------------------

  function fullyOk() {
    return typeof window.fully !== 'undefined' && window.fully && typeof window.fully.setScreenBrightness === 'function';
  }
  function readStore() {
    try { return JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (err) { return null; }
  }
  function writeStore(value) {
    try {
      if (value) localStorage.setItem(STORE, JSON.stringify(value));
      else localStorage.removeItem(STORE);
    } catch (err) { /* private mode: nothing to resume */ }
  }

  var savedBrightness = null;  // what to put back on wake
  function dimBacklight() {
    if (savedBrightness === null) {
      var current = null;
      try { if (typeof window.fully.getScreenBrightness === 'function') current = Number(window.fully.getScreenBrightness()); } catch (err) { current = null; }
      savedBrightness = isFinite(current) && current > 0 ? current : 255;
    }
    try { window.fully.setScreenBrightness(brightnessLevel(Number(cfg.dim_percent) || 8)); } catch (err) { /* stays as it is */ }
  }
  function restoreBacklight() {
    if (savedBrightness === null) return;
    try { if (fullyOk()) window.fully.setScreenBrightness(savedBrightness); } catch (err) { /* nothing more to do */ }
    savedBrightness = null;
  }

  // --- Idle state -----------------------------------------------------------------------

  var idle = false;
  var kind = null;           // 'slideshow' | 'dim' while idle
  var lastActivity = Date.now();
  var lastBannerAt = 0;
  var lastRefresh = 0;
  var swallowing = false;    // the waking gesture is still being cancelled
  var gestureEnded = false;
  var swallowUntil = 0;

  var layers = screen.querySelectorAll('.idle-photo');
  var front = 0;
  var index = -1;
  var lastAdvance = 0;

  function photos() { return Array.isArray(cfg.photos) ? cfg.photos : []; }

  function renderOverlay() {
    var now = familyNow();
    var time = screen.querySelector('.idle-time');
    var date = screen.querySelector('.idle-date');
    if (time) time.textContent = formatTime(now);
    if (date) date.textContent = formatDate(now);
    var ev = screen.querySelector('.idle-event');
    if (ev) {
      ev.hidden = !cfg.next_event;
      if (cfg.next_event) {
        ev.querySelector('.idle-event-time').textContent = cfg.next_event.time;
        ev.querySelector('.idle-event-title').textContent = cfg.next_event.title;
      }
    }
    var wx = screen.querySelector('.idle-weather');
    if (wx) {
      wx.hidden = !cfg.weather;
      if (cfg.weather) {
        var icon = wx.querySelector('.idle-weather-icon');
        var src = '/static/icons/meteocons/' + encodeURIComponent(cfg.weather.category) + '.svg';
        if (icon.getAttribute('src') !== src) icon.setAttribute('src', src);
        wx.querySelector('.idle-weather-temp').textContent = cfg.weather.temperature + '°';
        wx.querySelector('.idle-weather-condition').textContent = cfg.weather.condition || '';
      }
    }
  }

  function showNextPhoto() {
    var list = photos();
    lastAdvance = Date.now();
    if (!list.length || layers.length < 2) return;
    index = nextIndex(index, list.length);
    screen.dataset.index = String(index);
    var back = layers[1 - front];
    var src = list[index];
    function reveal() {
      back.classList.add('is-shown');
      layers[front].classList.remove('is-shown');
      front = 1 - front;
    }
    back.onload = back.onerror = null;
    if (back.getAttribute('src') === src && back.complete && back.naturalWidth) { reveal(); return; }
    back.onload = function () { back.onload = back.onerror = null; if (idle && kind === 'slideshow') reveal(); };
    back.onerror = function () { back.onload = back.onerror = null; };  // a missing photo is skipped next time round
    back.setAttribute('src', src);
  }

  function applyKind(next) {
    if (kind === next) return;
    if (next !== 'dim') restoreBacklight();  // leaving dim, or back from a reload that was dim
    kind = next;
    root.classList.remove('idle-slideshow', 'idle-dim');
    root.classList.add('idle-' + next);
    screen.classList.remove('is-slideshow', 'is-clock', 'is-dim', 'is-dim-backlight');
    if (next === 'dim') {
      if (fullyOk()) {
        dimBacklight();
        screen.classList.add('is-dim-backlight');  // transparent: the backlight does the dimming
      } else {
        screen.classList.add('is-dim');
      }
    } else {
      screen.classList.add(photos().length ? 'is-slideshow' : 'is-clock');
      renderOverlay();
      if (photos().length) showNextPhoto();
    }
    screen.dataset.kind = next;
    remember();
  }

  function remember() {
    writeStore(idle ? { at: Date.now(), brightness: savedBrightness } : null);
  }

  function goIdle(next) {
    if (idle) return;
    idle = true;
    screen.hidden = false;
    screen.setAttribute('aria-hidden', 'false');
    root.classList.add('idle-on');
    applyKind(next);
    refresh();
  }

  function wake() {
    if (!idle) return;
    idle = false;
    restoreBacklight();
    kind = null;
    screen.hidden = true;
    screen.setAttribute('aria-hidden', 'true');
    screen.dataset.kind = '';
    root.classList.remove('idle-on', 'idle-slideshow', 'idle-dim');
    screen.classList.remove('is-slideshow', 'is-clock', 'is-dim', 'is-dim-backlight');
    layers.forEach(function (l) { l.classList.remove('is-shown'); });
    lastActivity = Date.now();
    remember();
  }

  function refresh() {
    lastRefresh = Date.now();
    var opts = { cache: 'no-store' };
    if (window.AbortSignal && AbortSignal.timeout) opts.signal = AbortSignal.timeout(FETCH_TIMEOUT_MS);
    fetch('/api/idle', opts)
      .then(function (r) { if (!r.ok) throw r; return r.json(); })
      .then(function (data) {
        var before = JSON.stringify(photos());
        cfg = data;
        setClock(data.now);
        if (!idle) return;
        if (JSON.stringify(photos()) !== before) index = -1;
        var wasClock = screen.classList.contains('is-clock');
        if (kind === 'slideshow' && wasClock === !!photos().length) {
          kind = null;  // photos appeared or went: redraw
          applyKind('slideshow');
        } else if (kind === 'slideshow') {
          renderOverlay();
        }
      })
      .catch(function () { /* offline: keep showing what we have */ });
  }

  // --- Busy ---------------------------------------------------------------------------

  function busy() {
    if (typeof window.huddleBusy === 'function' && window.huddleBusy()) return true;
    if (root.classList.contains('osk-open') || document.querySelector('.osk--open')) return true;
    return !!document.querySelector('.ui-draggable-dragging, .ui-resizable-resizing');
  }

  document.addEventListener('huddle:banner', function () { lastBannerAt = Date.now(); });

  // --- Input ----------------------------------------------------------------------------

  var STARTS = { pointerdown: 1, touchstart: 1, mousedown: 1, keydown: 1 };
  var ENDS = { pointerup: 1, pointercancel: 1, touchend: 1, touchcancel: 1, mouseup: 1, click: 1, keyup: 1 };
  var ACTIVITY = { pointerdown: 1, pointermove: 1, touchstart: 1, touchmove: 1, mousedown: 1, keydown: 1, wheel: 1 };

  // Moves and wheels are listened to passively, so scrolling never waits on
  // this script; cancelling their gesture's touchstart already stops it.
  var PASSIVE = { pointermove: 1, touchmove: 1, mousemove: 1, wheel: 1 };

  function cancel(evt) {
    if (evt.cancelable && !PASSIVE[evt.type]) evt.preventDefault();
    evt.stopImmediatePropagation();
    evt.stopPropagation();
  }

  function onInput(evt) {
    var now = Date.now();
    if (idle) {
      if (STARTS[evt.type]) {
        wake();
        swallowing = true;
        gestureEnded = evt.type === 'keydown';
        swallowUntil = gestureEnded ? now + SWALLOW_MS : Infinity;
        cancel(evt);
      }
      return;  // a hover or scroll while idle doesn't wake
    }
    if (swallowing) {
      if (evt.type === 'pointerdown' && gestureEnded) {
        swallowing = false;  // a new tap: it works normally
      } else if (now > swallowUntil) {
        swallowing = false;
      } else {
        if (ENDS[evt.type] && !gestureEnded) {
          gestureEnded = true;
          swallowUntil = now + SWALLOW_MS;
        }
        cancel(evt);
        return;
      }
    }
    if (ACTIVITY[evt.type]) lastActivity = now;
  }

  ['pointerdown', 'pointerup', 'pointercancel', 'pointermove', 'touchstart', 'touchend', 'touchmove',
   'touchcancel', 'mousedown', 'mouseup', 'mousemove', 'click', 'dblclick', 'contextmenu', 'auxclick',
   'keydown', 'keyup', 'wheel'].forEach(function (type) {
    window.addEventListener(type, onInput, { capture: true, passive: Boolean(PASSIVE[type]) });
  });

  // --- The one timer --------------------------------------------------------------------

  function tick() {
    var now = Date.now();
    var mode = effectiveMode(cfg, isNight());
    if (idle) {
      if (mode === 'dashboard') { wake(); return; }  // e.g. morning, with "stay on the dashboard" by day
      applyKind(mode);
      if (kind === 'slideshow') {
        renderOverlay();
        if (photos().length > 1 && now - lastAdvance >= (Number(cfg.interval_seconds) || 20) * 1000) showNextPhoto();
      }
      if (now - lastRefresh >= REFRESH_MS) refresh();
      return;
    }
    var isBusy = busy();
    if (isBusy) lastActivity = now;  // the full delay starts again once they're done
    if (shouldGoIdle({
      now: now, lastActivity: lastActivity, delayMs: (Number(cfg.delay_minutes) || 5) * 60000,
      mode: mode, busy: isBusy, lastBannerAt: lastBannerAt,
    })) goIdle(mode);
  }

  // A reload just after going idle (fresh.js reloads for a layout change or a
  // new day) comes back idle, with the brightness to restore carried over.
  // Anything older only has its brightness put back.
  var stored = readStore();
  if (stored && typeof stored === 'object') {
    if (typeof stored.brightness === 'number') savedBrightness = stored.brightness;
    if (Date.now() - (Number(stored.at) || 0) < RESUME_MS) {
      var resumeMode = effectiveMode(cfg, isNight());
      if (resumeMode !== 'dashboard') goIdle(resumeMode);
    }
    if (!idle) {
      restoreBacklight();
      writeStore(null);
    }
  }
  window.addEventListener('pagehide', function () { if (idle) remember(); });

  setInterval(tick, TICK_MS);

  window.HuddleIdle = {
    logic: logic,
    state: function () {
      return { idle: idle, kind: kind, index: index, photos: photos().length, savedBrightness: savedBrightness };
    },
  };
})();
