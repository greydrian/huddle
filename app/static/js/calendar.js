/* The calendar widget's return to its default view (spec 10.5), and its
 * add form's request key.
 *
 * #widget-calendar polls /widgets/calendar (the Admin default view for
 * today, unfiltered) every 180 s (calendar_view.IDLE_SECONDS). Each
 * re-render restarts that timer, so it only fires 180 s after the last
 * tap that re-rendered the widget. Its hx-trigger first asks
 * huddleCalendarIdle(), which holds the poll while someone is still using
 * the widget without re-rendering it:
 *   - a tap, key, focus or scroll inside it in the last RECENT_MS;
 *   - the add form open (data-panel), or a field in it focused, unless
 *     nothing inside the widget was touched for ABANDONED_MS (fresh.js's
 *     cap): a form or select left open and walked away from doesn't hold
 *     the wall off its default view for good;
 *   - one of its own requests in flight.
 * A held poll tries again one interval later.
 */
(function () {
  'use strict';

  var RECENT_MS = 60000;
  var ABANDONED_MS = 120000;  // as fresh.js
  var lastUse = 0;

  function noteUse(evt) {
    var el = evt.target;
    if (el && el.closest && el.closest('#widget-calendar')) lastUse = Date.now();
  }
  document.addEventListener('pointerdown', noteUse, true);
  document.addEventListener('keydown', noteUse, true);
  document.addEventListener('focusin', noteUse, true);
  document.addEventListener('scroll', noteUse, true);
  document.addEventListener('input', noteUse, true);

  window.huddleCalendarIdle = function (card) {
    var since = Date.now() - lastUse;
    if (since < RECENT_MS) return false;
    if (card.querySelector('.htmx-request')) return false;
    if (since < ABANDONED_MS) {
      if (card.querySelector('[data-panel]:not([hidden])')) return false;
      var active = document.activeElement;
      if (active && card.contains(active) && active.matches('input, textarea, select')) return false;
    }
    return true;
  };

  // After a refused add, the first edit gives the form a fresh request key
  // (data-fresh-key): an edited form is a different event.
  window.calendarFreshKey = function (form) {
    var fresh = form.dataset.freshKey;
    if (!fresh) return;
    form.querySelector('input[name=request_key]').value = fresh;
    delete form.dataset.freshKey;
  };
})();
