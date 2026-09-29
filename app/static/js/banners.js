/* Notification banner (templates/_banners.html, app/services/banners.py).
 *
 * - "+N more" shows the banners beyond the first two (and "Show less" hides
 *   them again). The extras are data-panel, so fresh.js holds the bar's
 *   refresh back while they're open and in use.
 * - A short, gentle chime when a banner whose trigger has sound switched on
 *   first appears. Keys chimed for are remembered in localStorage, so a
 *   reload or a refresh never repeats it. The server shows nothing in quiet
 *   hours, so there's nothing to chime for then.
 *
 * Browsers keep audio suspended until someone touches the page (unless the
 * kiosk allows autoplay). While it's suspended nothing is scheduled — a
 * suspended context would queue the notes and play them all at the first
 * touch, maybe hours later — and nothing is marked as chimed. Banners seen
 * then stay silent for this page even after audio starts: only banners
 * that appear from then on chime.
 */
(function () {
  'use strict';

  var STORE = 'huddle-banner-chimed';
  var KEEP_MS = 3 * 86400000;
  var audio = null;
  var silent = {};  // keys seen while audio couldn't play: never chimed late
  window.huddleChimes = 0;  // chimes actually played, for the browser tests

  try {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) audio = new Ctx();
  } catch (err) { audio = null; }

  function running() { return !!audio && audio.state === 'running'; }

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '{}') || {}; } catch (err) { return {}; }
  }
  function save(seen) {
    try { localStorage.setItem(STORE, JSON.stringify(seen)); } catch (err) { /* private mode: chime again next time */ }
  }

  function chime() {
    if (!running()) return;
    try {
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
      window.huddleChimes += 1;
    } catch (err) { /* no audio on this device: the banner still shows */ }
  }

  // A banner new to this page fires "huddle:banner" on document, so the idle
  // screen (idle.js) waits a minute before starting. Those already showing
  // when the page loads aren't new.
  var onPage = null;
  function announce(bar) {
    var fresh = false;
    var keys = {};
    bar.querySelectorAll('[data-key]').forEach(function (b) {
      keys[b.dataset.key] = true;
      if (onPage && !onPage[b.dataset.key]) fresh = true;
    });
    onPage = Object.assign(onPage || {}, keys);
    if (fresh) document.dispatchEvent(new CustomEvent('huddle:banner'));
  }

  function check(bar) {
    if (!bar) return;
    announce(bar);
    var seen = load();
    var now = Date.now();
    var playing = running();
    var ring = false;
    bar.querySelectorAll('[data-key]').forEach(function (b) {
      var key = b.dataset.key;
      if (seen[key] || silent[key]) return;
      if (!playing) { silent[key] = true; return; }
      seen[key] = now;
      if (b.hasAttribute('data-sound')) ring = true;
    });
    Object.keys(seen).forEach(function (key) { if (now - seen[key] > KEEP_MS) delete seen[key]; });
    if (playing) save(seen);
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

  // The first touch lets audio start; what's on show already stays silent.
  document.addEventListener('pointerdown', function () {
    if (audio && audio.state === 'suspended') {
      try { audio.resume(); } catch (err) { /* stays silent */ }
    }
  }, true);

  // An outerHTML swap replaces the bar, so look it up afresh each time.
  document.body.addEventListener('htmx:afterSwap', function () {
    check(document.getElementById('banner-bar'));
  });
  check(document.getElementById('banner-bar'));
})();
