/* Notification banner (templates/_banners.html, app/services/banners.py).
 *
 * - "+N more" shows the banners beyond the first two (and "Show less" hides
 *   them again). The extras are data-panel, so fresh.js holds the bar's
 *   refresh back while they're open and in use.
 * - A short, gentle chime when a banner whose trigger has sound switched on
 *   first appears. Keys already chimed for are remembered in localStorage,
 *   so a reload or a refresh never repeats it. The server shows nothing in
 *   quiet hours, so there's nothing to chime for then.
 */
(function () {
  'use strict';

  var STORE = 'huddle-banner-chimed';
  var KEEP_MS = 3 * 86400000;
  var audio = null;
  window.huddleChimes = 0;  // for the browser tests

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '{}') || {}; } catch (err) { return {}; }
  }
  function save(seen) {
    try { localStorage.setItem(STORE, JSON.stringify(seen)); } catch (err) { /* private mode: chime again next time */ }
  }

  function chime() {
    window.huddleChimes += 1;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audio = audio || new Ctx();
      if (audio.state === 'suspended') audio.resume();
      var t = audio.currentTime + 0.02;
      // Two soft sine notes (E5 then A5), each fading out.
      [[659.25, 0], [880, 0.18]].forEach(function (note) {
        var osc = audio.createOscillator();
        var gain = audio.createGain();
        osc.type = 'sine';
        osc.frequency.value = note[0];
        gain.gain.setValueAtTime(0.0001, t + note[1]);
        gain.gain.exponentialRampToValueAtTime(0.12, t + note[1] + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + note[1] + 0.6);
        osc.connect(gain).connect(audio.destination);
        osc.start(t + note[1]);
        osc.stop(t + note[1] + 0.65);
      });
    } catch (err) { /* no audio on this device: the banner still shows */ }
  }

  function check(bar) {
    if (!bar) return;
    var seen = load();
    var now = Date.now();
    var ring = false;
    bar.querySelectorAll('[data-key]').forEach(function (b) {
      var key = b.dataset.key;
      if (seen[key]) return;
      seen[key] = now;
      if (b.hasAttribute('data-sound')) ring = true;
    });
    Object.keys(seen).forEach(function (key) { if (now - seen[key] > KEEP_MS) delete seen[key]; });
    save(seen);
    if (ring) chime();
  }

  document.addEventListener('click', function (evt) {
    var more = evt.target.closest && evt.target.closest('#banner-bar [data-more]');
    if (!more) return;
    var bar = more.closest('#banner-bar');
    var open = more.getAttribute('aria-expanded') !== 'true';
    bar.querySelectorAll('.banner-extra').forEach(function (b) { b.hidden = !open; });
    more.setAttribute('aria-expanded', open ? 'true' : 'false');
    more.textContent = open ? more.dataset.lessLabel : more.dataset.moreLabel;
  });

  // A tablet's audio starts suspended until someone touches the screen.
  document.addEventListener('pointerdown', function () {
    if (audio && audio.state === 'suspended') audio.resume();
  }, true);

  // An outerHTML swap replaces the bar, so look it up afresh each time.
  document.body.addEventListener('htmx:afterSwap', function () {
    check(document.getElementById('banner-bar'));
  });
  check(document.getElementById('banner-bar'));
})();
