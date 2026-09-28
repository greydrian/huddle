// Pen test page (templates/pen_test.html; spec §3 checks, §10.10 pen input).
// Shows what the browser reports for each pointer, draws pen and touch in
// different colours, and demonstrates web-page palm rejection: while a pen
// is down, new touch pointers are ignored and counted. Nothing leaves the page.
(function () {
  'use strict';

  var canvas = document.getElementById('pt-canvas');
  var ctx = canvas.getContext('2d');
  var root = document.querySelector('.pen-test');
  function $(id) { return document.getElementById(id); }

  var down = {};          // pointerId -> {type, x, y} for pointers drawing
  var rejectedIds = {};   // touch pointers ignored because a pen was down
  var penDown = 0;
  var rejected = 0;
  var seen = { pen: false, touch: false, penHover: false, penStrokes: 0,
               pMin: Infinity, pMax: -Infinity };

  // --- Canvas sizing (sharp on high-DPI screens) ---
  function resize() {
    var r = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
  }
  resize();
  window.addEventListener('resize', resize);

  function ink(type) {
    var name = type === 'pen' ? '--pen-ink' : type === 'touch' ? '--touch-ink' : '--mouse-ink';
    return getComputedStyle(root).getPropertyValue(name).trim() || '#888';
  }

  function pos(e) {
    var r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  function lineWidth(e) {
    if (e.pointerType === 'pen') {
      // Pressure 0 while pressed means "not reported": use a mid width.
      var p = e.pressure > 0 ? e.pressure : 0.5;
      return 1 + p * 11;
    }
    return e.pointerType === 'touch' ? 7 : 3;
  }

  function stroke(from, e) {
    var to = pos(e);
    ctx.strokeStyle = ink(e.pointerType);
    ctx.lineWidth = lineWidth(e);
    ctx.beginPath();
    ctx.moveTo(from.x, from.y);
    ctx.lineTo(to.x + (to.x === from.x && to.y === from.y ? 0.01 : 0), to.y);
    ctx.stroke();
    from.x = to.x;
    from.y = to.y;
  }

  // --- Readout ---
  function fmt(n, digits) { return typeof n === 'number' ? n.toFixed(digits) : '–'; }

  function show(e) {
    var t = $('pt-type');
    t.textContent = e.pointerType || 'unknown';
    t.dataset.type = e.pointerType || 'none';
    $('pt-pressure').textContent = fmt(e.pressure, 2);
    $('pt-size').textContent = fmt(e.width, 1) + ' × ' + fmt(e.height, 1);
    $('pt-tilt').textContent = fmt(e.tiltX, 0) + '° / ' + fmt(e.tiltY, 0) + '°';
  }

  function showCount() {
    $('pt-count').textContent = String(Object.keys(down).length + Object.keys(rejectedIds).length);
    $('pt-rejected').textContent = String(rejected);
  }

  function checklist() {
    $('chk-pen').textContent = seen.pen ? '✅ yes'
      : seen.touch ? '❌ only touch so far' : 'not yet seen';
    var spread = seen.pMax - seen.pMin;
    $('chk-pressure').textContent = spread > 0.1 ? '✅ ' + fmt(seen.pMin, 2) + '–' + fmt(seen.pMax, 2)
      : seen.penStrokes ? '❌ stays at ' + fmt(seen.pMax, 2) : 'not yet seen';
    $('chk-hover').textContent = seen.penHover ? '✅ yes'
      : seen.penStrokes ? '❌ none from the pen' : 'not tested';
    $('chk-palm').textContent = rejected + (rejected === 1 ? ' touch' : ' touches') + ' ignored';
  }

  // --- Pointer handling ---
  function hover(e) {
    if (e.buttons !== 0) return;
    show(e);
    $('pt-hover').textContent = 'yes (' + e.pointerType + ')';
    if (e.pointerType === 'pen' && !seen.penHover) { seen.penHover = true; checklist(); }
  }

  canvas.addEventListener('pointerover', hover);

  canvas.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    show(e);
    if (e.pointerType === 'touch') seen.touch = true;
    if (e.pointerType === 'touch' && penDown > 0) {
      // Palm rejection: a pen is writing, so this contact is a resting hand.
      rejectedIds[e.pointerId] = true;
      rejected++;
      showCount();
      checklist();
      return;
    }
    if (e.pointerType === 'pen') { penDown++; seen.pen = true; }
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ }
    var p = pos(e);
    down[e.pointerId] = { type: e.pointerType, x: p.x, y: p.y };
    stroke(down[e.pointerId], e);
    showCount();
    checklist();
  });

  canvas.addEventListener('pointermove', function (e) {
    if (e.buttons === 0) { hover(e); return; }
    if (rejectedIds[e.pointerId]) return;
    var from = down[e.pointerId];
    if (!from) return;
    show(e);
    var events = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    if (!events.length) events = [e];
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if (ev.pointerType === 'pen' && ev.pressure > 0) {
        seen.pMin = Math.min(seen.pMin, ev.pressure);
        seen.pMax = Math.max(seen.pMax, ev.pressure);
      }
      stroke(from, ev);
    }
  });

  function lift(e) {
    if (rejectedIds[e.pointerId]) {
      delete rejectedIds[e.pointerId];
    } else if (down[e.pointerId]) {
      if (down[e.pointerId].type === 'pen') {
        penDown = Math.max(0, penDown - 1);
        seen.penStrokes++;
      }
      delete down[e.pointerId];
    }
    showCount();
    checklist();
  }
  canvas.addEventListener('pointerup', lift);
  canvas.addEventListener('pointercancel', lift);
  canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  $('pt-clear').addEventListener('click', function () {
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.restore();
  });

  // --- Fully Kiosk (the JS interface needs PLUS and "Enable JavaScript
  // Interface" in Fully's Advanced Web Settings) ---
  var fullyEl = $('pt-fully');
  if (typeof fully !== 'undefined') {
    var bits = ['Fully Kiosk detected.'];
    /* global fully */
    [['getScreenBrightness', 'Screen brightness'],
     ['getBatteryLevel', 'Battery %'],
     ['isPlugged', 'Plugged in']].forEach(function (f) {
      if (typeof fully[f[0]] !== 'function') {
        bits.push(f[1] + ': not available (PLUS JS off?)');
        return;
      }
      try {
        bits.push(f[1] + ': ' + fully[f[0]]() + ' ✅');
      } catch (err) {
        bits.push(f[1] + ': error (' + err.message + ')');
      }
    });
    fullyEl.innerHTML = '';
    bits.forEach(function (b) {
      var line = document.createElement('div');
      line.textContent = b;
      fullyEl.appendChild(line);
    });
  } else {
    fullyEl.textContent = 'Not detected. In Fully Kiosk, turn on "Enable JavaScript Interface" (PLUS) and reload.';
  }

  checklist();
})();
