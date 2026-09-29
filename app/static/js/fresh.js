/* Keeps the long-lived kiosk dashboard fresh without reloading it
 * (server side: app/freshness.py).
 *
 * Widgets: every POLL_MS the page asks /api/rev for one revision per
 * self-refreshing widget (roots marked data-refresh + data-rev-key) and
 * re-fetches only those whose revision differs from what it shows, through
 * htmx so dashboard.html's htmx:afterSwap re-binds the drag handle. A widget
 * someone is using is never swapped: the refresh waits (re-trying every
 * RETRY_MS) while
 *   - an input/textarea/select inside it has focus, or the on-screen
 *     keyboard is open anywhere;
 *   - a <details> inside it is open (e.g. the tasks "✓ N done" fold), or a
 *     panel marked data-panel is showing (the tasks "+ Add" form), and it
 *     was touched in the last ABANDONED_MS — a fold left open and walked
 *     away from doesn't freeze the widget for good;
 *   - a request of its own is in flight or a tick's 450ms reveal is pending;
 *   - a finger/mouse is down, or a Gridstack drag/resize is in progress.
 * The check runs again just before the swap, since the fetch takes time.
 * A re-fetch that fails isn't retried until /api/rev answers again.
 *
 * Date: the top bar shows the family's date (#today-date). The server says
 * how many seconds until it next changes and only elapsed time is measured
 * here — the approach base.html uses for day/night — so the tablet's own
 * clock and timezone never decide the date. If the server can't be reached
 * at midnight, the date still rolls over locally.
 */
(function () {
  'use strict';

  var POLL_MS = 30000;
  var RETRY_MS = 5000;
  var ABANDONED_MS = 120000;
  var DATE_RECHECK_MS = 600000;
  var PRESS_TIMEOUT_MS = 60000;  // a lost pointerup must not block refreshes for long
  var FETCH_TIMEOUT_MS = 10000;

  function fetchOptions() {
    var opts = { cache: 'no-store' };
    if (window.AbortSignal && AbortSignal.timeout) opts.signal = AbortSignal.timeout(FETCH_TIMEOUT_MS);
    return opts;
  }

  var grid = document.getElementById('dashboard-grid');
  var scroller = document.getElementById('dashboard-scroll');
  var dateEl = document.getElementById('today-date');
  var root = document.documentElement;

  // --- Is someone using this widget? ---

  var pressedAt = 0;
  var lastTouch = {};  // rev key -> last pointer/key/focus inside that widget
  function noteUse(evt) {
    var card = evt.target && evt.target.closest && evt.target.closest('[data-rev-key]');
    if (card) lastTouch[card.dataset.revKey] = Date.now();
  }
  document.addEventListener('pointerdown', function (evt) { pressedAt = Date.now(); noteUse(evt); }, true);
  document.addEventListener('pointerup', function () { pressedAt = 0; }, true);
  document.addEventListener('pointercancel', function () { pressedAt = 0; }, true);
  document.addEventListener('keydown', noteUse, true);
  document.addEventListener('focusin', noteUse, true);

  // `ownRequest`: called for our own refresh, whose request marks the card
  // itself .htmx-request.
  function busy(card, ownRequest) {
    if (!card.isConnected) return true;
    if (pressedAt && Date.now() - pressedAt < PRESS_TIMEOUT_MS) return true;
    if (root.classList.contains('osk-open') || document.querySelector('.osk--open')) return true;
    var active = document.activeElement;
    if (active && card.contains(active) && active.matches('input, textarea, select, [contenteditable]')) return true;
    if (card.matches(ownRequest ? '.htmx-swapping' : '.htmx-request, .htmx-swapping')) return true;
    if (card.querySelector('.htmx-request, .just-ticked')) return true;
    if (grid && grid.querySelector('.ui-draggable-dragging, .ui-resizable-resizing')) return true;
    if (card.querySelector('details[open], [data-panel]:not([hidden])') && Date.now() - (lastTouch[card.dataset.revKey] || 0) < ABANDONED_MS) return true;
    return false;
  }

  // --- Widget refresh ---

  var known = {};    // rev key -> revision the page shows
  var wanted = {};   // rev key -> newer revision the server has
  var inFlight = {};
  var outcome = {};  // rev key -> 'swapped' | 'deferred', for the refresh in flight
  var failed = {};   // rev key -> its last re-fetch failed: wait for the next good poll
  var lastPoll = Date.now();
  var polling = false;
  try { known = JSON.parse((grid && grid.dataset.revs) || '{}'); } catch (err) { known = {}; }

  function cardFor(key) {
    return document.querySelector('[data-rev-key="' + key + '"]');
  }

  function poll() {
    if (polling) return;
    polling = true;
    lastPoll = Date.now();
    fetch('/api/rev', fetchOptions())
      .then(function (r) { if (!r.ok) throw r; return r.json(); })
      .then(function (data) {
        var revs = data.widgets || {};
        failed = {};
        Object.keys(revs).forEach(function (key) {
          if (revs[key] !== known[key]) wanted[key] = revs[key];
          else delete wanted[key];
        });
        applyWanted();
      })
      .catch(function () { /* offline: the widgets keep what they show; next poll retries */ })
      .then(function () { polling = false; });
  }

  function applyWanted() {
    Object.keys(wanted).forEach(function (key) {
      var card = cardFor(key);
      if (!card) { delete wanted[key]; return; }
      if (inFlight[key] || failed[key] || busy(card)) return;  // retried on the next RETRY_MS tick
      inFlight[key] = wanted[key];
      delete outcome[key];
      htmx.ajax('GET', card.dataset.refresh, {
        source: card, target: card, swap: 'outerHTML',
        headers: { 'X-Huddle-Refresh': key },
      }).catch(function () {}).then(function () {
        // Neither swapped nor held back for someone using it: the server or
        // network failed. Don't hammer it every RETRY_MS.
        if (!outcome[key]) failed[key] = true;
        delete inFlight[key];
      });
    });
  }

  function refreshKey(evt) {
    var cfg = evt.detail && evt.detail.requestConfig;
    return cfg && cfg.headers && cfg.headers['X-Huddle-Refresh'];
  }

  var savedScroll = null;
  document.body.addEventListener('htmx:beforeSwap', function (evt) {
    var key = refreshKey(evt);
    if (!key || evt.detail.isError) return;  // an error response isn't swapped
    var card = cardFor(key);
    // Someone started using it while the fetch was out: keep their DOM and
    // try again later (the revision stays wanted).
    if (!card || card !== evt.detail.target || busy(card, true)) {
      evt.detail.shouldSwap = false;
      outcome[key] = 'deferred';
      return;
    }
    savedScroll = {
      key: key,
      page: scroller ? scroller.scrollTop : 0,
      bodies: Array.prototype.map.call(card.querySelectorAll('.widget-body'), function (b) { return b.scrollTop; }),
    };
  });

  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var key = refreshKey(evt);
    if (!key) return;
    outcome[key] = 'swapped';
    if (inFlight[key]) {
      known[key] = inFlight[key];
      if (wanted[key] === known[key]) delete wanted[key];
    }
    // Put every scroll position back before the browser paints.
    if (savedScroll && savedScroll.key === key) {
      var card = cardFor(key);
      if (card) {
        card.querySelectorAll('.widget-body').forEach(function (b, i) {
          if (savedScroll.bodies[i]) b.scrollTop = savedScroll.bodies[i];
        });
      }
      if (scroller) scroller.scrollTop = savedScroll.page;
      savedScroll = null;
    }
  });

  // A kiosk tap whose request failed (e.g. a tick while the server was
  // down) leaves its optimistic .just-ticked/done look behind, which would
  // also keep the widget "in use" forever: drop it and re-fetch the widget
  // once idle, so it shows the truth again.
  ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach(function (name) {
    document.body.addEventListener(name, function (evt) {
      if (refreshKey(evt)) return;
      var elt = evt.detail && evt.detail.elt;
      var card = elt && elt.closest && elt.closest('[data-rev-key]');
      if (!card) return;
      card.querySelectorAll('.just-ticked').forEach(function (el) { el.classList.remove('just-ticked'); });
      var key = card.dataset.revKey;
      known[key] = null;
      wanted[key] = wanted[key] || 'unknown';
    });
  });

  // --- Date ---

  var dateDue = Infinity;
  var lastDateCheck = Date.now();
  var dateChecking = false;

  function showDate(iso) {
    if (!dateEl || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return;
    var changed = dateEl.dataset.iso !== iso;
    dateEl.dataset.iso = iso;
    var p = iso.split('-');
    dateEl.textContent = new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString(undefined, {
      weekday: 'long', month: 'long', day: 'numeric',
    });
    if (changed && dateEl.textContent) poll();  // the new day changes widgets too
  }

  function nextDay(iso) {
    var p = iso.split('-');
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + 1)).toISOString().slice(0, 10);
  }

  function scheduleDate(seconds) {
    dateDue = Date.now() + Math.max(1, seconds) * 1000;
  }

  function checkDate(rolling) {
    if (dateChecking) return;
    dateChecking = true;
    lastDateCheck = Date.now();
    fetch('/api/today', fetchOptions())
      .then(function (r) { if (!r.ok) throw r; return r.json(); })
      .then(function (t) {
        if (typeof t.next_change_in !== 'number') throw t;
        showDate(t.date);
        scheduleDate(t.next_change_in);
      })
      .catch(function () {
        // Server unreachable at midnight: roll over locally; the periodic
        // re-check corrects it once the server answers again.
        if (rolling && dateEl) {
          showDate(nextDay(dateEl.dataset.iso));
          scheduleDate(86400);
        }
      })
      .then(function () { dateChecking = false; });
  }

  if (dateEl) {
    showDate(dateEl.dataset.iso);
    scheduleDate(Number(dateEl.dataset.nextChange) || 60);
  }

  // --- One timer for both ---

  setInterval(function () {
    if (document.visibilityState === 'hidden') return;
    var now = Date.now();
    if (dateEl && now >= dateDue) checkDate(true);
    else if (now - lastDateCheck >= DATE_RECHECK_MS) checkDate(false);
    if (!grid || !window.htmx) return;
    if (now - lastPoll >= POLL_MS) poll();
    else applyWanted();
  }, RETRY_MS);

  // A tablet waking from sleep catches up straight away.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    if (dateEl) checkDate(Date.now() >= dateDue);
    if (grid && window.htmx) poll();
  });
})();
