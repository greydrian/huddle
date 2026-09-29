/* The calendar widget's return to its default view (spec 10.5).
 *
 * #widget-calendar polls /widgets/calendar (the Admin default view for
 * today, unfiltered) every IDLE seconds, and each re-render restarts that
 * timer; its hx-trigger asks huddleCalendarIdle() first. The poll is held
 * while someone is still using the widget without re-rendering it: a tap,
 * key or focus inside it in the last HOLD_MS (e.g. scrolling, filling in
 * the add form), or its input focused with the on-screen keyboard open.
 * A held poll simply tries again one interval later.
 */
(function () {
  'use strict';

  var HOLD_MS = 150000;
  var lastUse = 0;

  function noteUse(evt) {
    var el = evt.target;
    if (el && el.closest && el.closest('#widget-calendar')) lastUse = Date.now();
  }
  document.addEventListener('pointerdown', noteUse, true);
  document.addEventListener('keydown', noteUse, true);
  document.addEventListener('focusin', noteUse, true);
  document.addEventListener('scroll', noteUse, true);

  window.huddleCalendarIdle = function (card) {
    if (Date.now() - lastUse < HOLD_MS) return false;
    var active = document.activeElement;
    if (active && card.contains(active) && active.matches('input, textarea, select')) return false;
    if (card.querySelector('.htmx-request')) return false;
    return true;
  };
})();
