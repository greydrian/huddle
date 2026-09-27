/* On-screen keyboard for the kiosk (admin toggle "On-screen keyboard").
 *
 * Opt-in per input: the server marks inputs with data-osk="text" or
 * data-osk="numeric" (PIN pad). Only once the keyboard has actually started do
 * we set inputmode="none" on them to suppress Android's own keyboard, so if
 * this script fails the native keyboard still works (including on the PIN
 * screen, which is how the setting gets turned back off). All listeners are
 * delegated on document because HTMX widgets replace their own DOM.
 */
(function () {
  'use strict';

  const lib = window.SimpleKeyboard;
  const Keyboard = lib && (lib.default || lib);
  if (!Keyboard) return;

  const LAYOUT = {
    default: [
      '1 2 3 4 5 6 7 8 9 0 {bksp}',
      'q w e r t y u i o p',
      "a s d f g h j k l '",
      '{shift} z x c v b n m , . -',
      '{done} {space} {enter}',
    ],
    shift: [
      '! ? & ( ) / : ; + @ {bksp}',
      'Q W E R T Y U I O P',
      'A S D F G H J K L "',
      '{shift} Z X C V B N M , . -',
      '{done} {space} {enter}',
    ],
    numeric: ['1 2 3', '4 5 6', '7 8 9', '{bksp} 0 {enter}'],
  };
  const DISPLAY = {
    '{bksp}': '⌫',
    '{shift}': '⇧',
    '{space}': ' ',
    '{enter}': 'Enter',
    '{done}': 'Done',
  };

  const panel = document.createElement('div');
  panel.className = 'osk';
  panel.setAttribute('aria-hidden', 'true');
  panel.innerHTML = '<div class="osk-keys simple-keyboard"></div>';
  document.body.appendChild(panel);

  let keyboard;
  try {
    keyboard = new Keyboard(panel.querySelector('.osk-keys'), {
      layout: LAYOUT,
      display: DISPLAY,
      theme: 'hg-theme-default osk-theme',
      mergeDisplay: false,
      preventMouseDownDefault: true,
      onKeyPress: handleKey,
    });
  } catch (err) {
    panel.remove();
    console.error('On-screen keyboard failed to start; using the native keyboard', err);
    return;
  }

  function suppressNativeKeyboard(root) {
    root.querySelectorAll('input[data-osk]').forEach((el) => el.setAttribute('inputmode', 'none'));
  }

  let target = null;       // the input currently being typed into
  let valueAtOpen = '';    // to know whether closing should fire `change`

  function isEligible(el) {
    return el instanceof HTMLInputElement
      && el.dataset.osk !== undefined
      && !el.disabled && !el.readOnly;
  }

  function isNumeric(el) {
    return el.dataset.osk === 'numeric';
  }

  // How to find this input again after HTMX re-renders its widget.
  function selectorFor(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const hxPost = el.getAttribute('hx-post');
    if (hxPost) return `input[hx-post="${CSS.escape(hxPost)}"]`;
    if (el.name) return `input[name="${CSS.escape(el.name)}"]`;
    return null;
  }

  function syncShift() {
    // Auto-capitalise the first letter; shift is otherwise one-shot.
    if (!target || isNumeric(target)) return;
    const wanted = target.value === '' && target.type !== 'password' ? 'shift' : 'default';
    if (keyboard.options.layoutName !== wanted) keyboard.setOptions({ layoutName: wanted });
  }

  function open(el) {
    if (target && target !== el) commitChange();
    target = el;
    valueAtOpen = el.value;
    keyboard.clearInput();
    const numeric = isNumeric(el);
    panel.classList.toggle('osk--numeric', numeric);
    keyboard.setOptions({ layoutName: numeric ? 'numeric' : 'default' });
    syncShift();
    panel.classList.add('osk--open');
    panel.setAttribute('aria-hidden', 'false');
    document.documentElement.classList.add('osk-open');
    document.documentElement.style.setProperty('--osk-height', panel.offsetHeight + 'px');
    requestAnimationFrame(() => reveal(el));
  }

  // Lift the input above the keyboard by scrolling whichever ancestors can
  // scroll — including the overflow:hidden body, since Gridstack gives the
  // grid a fixed inline height and the page itself overflows instead.
  const scrolled = new Map();  // element -> scrollTop before we moved it
  function reveal(el) {
    if (!el.isConnected) return;
    const margin = 16;
    const limit = window.innerHeight - panel.offsetHeight - margin;
    for (let s = el.parentElement; s; s = s.parentElement) {
      if (s.scrollHeight <= s.clientHeight || getComputedStyle(s).overflowY === 'visible') continue;
      const over = el.getBoundingClientRect().bottom - Math.min(limit, s.getBoundingClientRect().bottom - margin);
      if (over > 0) {
        if (!scrolled.has(s)) scrolled.set(s, s.scrollTop);
        s.scrollTop += over;
      }
    }
  }

  function close() {
    if (!target) return;
    commitChange();
    const el = target;
    target = null;
    keyboard.clearInput();
    panel.classList.remove('osk--open');
    panel.setAttribute('aria-hidden', 'true');
    document.documentElement.classList.remove('osk-open');
    if (el.isConnected && document.activeElement === el) el.blur();
    scrolled.forEach((top, s) => { s.scrollTop = top; });
    scrolled.clear();
    // The dashboard is overflow:hidden by design; focusing an input low on
    // the screen can still scroll it, which would leave the top bar hidden.
    // Scrollable pages (Admin) keep the user's position.
    if (getComputedStyle(document.documentElement).overflowY === 'hidden') {
      document.body.scrollTop = 0;
      document.documentElement.scrollTop = 0;
    }
  }

  // Programmatic value changes never fire `change` on blur, so inputs that
  // save on change (meal plan: hx-trigger="change") need it dispatched.
  function commitChange() {
    if (target && target.isConnected && target.value !== valueAtOpen) {
      valueAtOpen = target.value;
      target.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function edit(text, start, end) {
    target.setRangeText(text, start, end, 'end');
    target.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function handleKey(button) {
    const el = target;
    if (!el) return;
    if (!el.isConnected) { close(); return; }
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? start;

    if (button === '{done}') { close(); return; }
    if (button === '{shift}') {
      const next = keyboard.options.layoutName === 'shift' ? 'default' : 'shift';
      keyboard.setOptions({ layoutName: next });
      return;
    }
    if (button === '{bksp}') {
      if (start !== end) edit('', start, end);
      else if (start > 0) edit('', start - 1, start);
      syncShift();
      return;
    }
    if (button === '{enter}') {
      const onChange = (el.getAttribute('hx-trigger') || '').includes('change');
      if (onChange || !el.form) {
        close();
      } else {
        el.form.requestSubmit();
      }
      return;
    }

    const text = button === '{space}' ? ' ' : button;
    if (el.maxLength > 0 && el.value.length - (end - start) >= el.maxLength) return;
    edit(text, start, end);
    if (keyboard.options.layoutName === 'shift') keyboard.setOptions({ layoutName: 'default' });
  }

  document.addEventListener('focusin', (evt) => {
    if (isEligible(evt.target)) open(evt.target);
  });

  // Tapping the keyboard itself must not steal focus from the input (touch
  // taps would otherwise blur it).
  panel.addEventListener('pointerdown', (evt) => evt.preventDefault());

  // Tapping elsewhere closes, but on click, once the tap has landed: closing
  // un-scrolls the page, which on pointerdown would move the tapped element
  // out from under the finger. Taps inside the input's own widget (its Add
  // button, check/delete) keep the keyboard open.
  document.addEventListener('click', (evt) => {
    if (!target || panel.contains(evt.target)) return;
    if (evt.target === target || isEligible(evt.target)) return;
    const home = target.closest('.widget-card') || target.form;
    if (home && home.contains(evt.target)) return;
    close();
  });

  // A widget re-rendered while typing (e.g. the shopping list after Enter)
  // replaces the input: carry on in its replacement so several items can be
  // added in a row, or close if it's gone.
  document.addEventListener('htmx:afterSettle', () => {
    if (!target || target.isConnected) return;
    const selector = selectorFor(target);
    const replacement = selector && document.querySelector(selector);
    if (replacement && isEligible(replacement)) {
      target = null;
      replacement.focus();
      if (document.activeElement !== replacement) open(replacement);
    } else {
      close();
    }
  });

  document.addEventListener('htmx:load', (evt) => suppressNativeKeyboard(evt.target));
  suppressNativeKeyboard(document);

  // Autofocused inputs (the PIN field) may be focused before this script
  // runs, with the native keyboard already up: refocus so the new
  // inputmode="none" takes effect, which also opens ours via focusin.
  window.addEventListener('load', () => {
    const el = document.activeElement;
    if (target || !isEligible(el)) return;
    el.blur();
    el.focus();
    if (!target) open(el);
  });
})();
