/**
 * The LEASH onboarding: one page, one step visible at a time.
 *
 * Every step is already in the document (ui.py renders them all inside the consent form);
 * this only decides which one shows, keeps the four choices of step 4, and writes what the
 * cardholder chose into the hidden `setup` field before "Confirm Leash" submits. Without
 * JavaScript the form still works: every step shows, and the two submit buttons at the end
 * are the consent.
 *
 * Branches, as the storyboard draws them: Later on step 1 skips the pattern and the
 * suggestion; Adjust on step 2 and Adjust rules on step 3 go straight to the four purchases.
 */

const form = document.getElementById('consent');
const steps = [...form.querySelectorAll('[data-step]')];
const cfg = window.WALLET ?? {};
const state = {
  used_history: null,
  choices: Object.fromEntries(
    [...form.querySelectorAll('[data-case]')].map((c) => [
      c.dataset.case,
      c.querySelector('.seg__b[aria-pressed="true"]')?.dataset.choice ?? 'ask',
    ]),
  ),
};
const trail = [];

function show(name) {
  for (const s of steps) s.hidden = s.dataset.step !== name;
  window.scrollTo({ top: 0 });
  syncReady();
}

function current() {
  return steps.find((s) => !s.hidden)?.dataset.step ?? cfg.start ?? 'intro';
}

function go(name) {
  trail.push(current());
  show(name);
}

function back() {
  const previous = trail.pop();
  if (previous) show(previous);
}

/** The last step's second line follows the choices: what Leash will ask about. */
function syncReady() {
  const asks = [];
  if (state.choices.C === 'ask') asks.push('New merchants');
  if (state.choices.B === 'ask') asks.push('higher amounts');
  if (state.choices.D === 'ask') asks.push('time-limited purchases');
  const row = form.querySelector('[data-step="ready"] .row:nth-child(2)');
  if (!row) return;
  const k = row.querySelector('.row__k');
  const s = row.querySelector('.row__s');
  if (asks.length) {
    k.textContent = asks.length === 1 ? asks[0] : `${asks.slice(0, -1).join(', ')} or ${asks[asks.length - 1]}`;
    s.textContent = 'Leash asks you first';
  } else {
    k.textContent = 'Nothing is allowed automatically';
    s.textContent = 'Leash asks you before every purchase';
  }
  const cap = form.querySelector('[data-step="ready"] .row:first-child .row__k');
  if (cap) cap.textContent = state.choices.A === 'allow' ? `Up to CHF ${cfg.cap}` : 'Nothing automatically';
  const setup = form.querySelector('input[name="setup"]');
  setup.value = JSON.stringify({
    used_history: state.used_history,
    suggested_cap_chf: cfg.cap,
    choices: state.choices,
  });
}

form.addEventListener('click', (ev) => {
  const t = ev.target.closest('button');
  if (!t) return;
  if (t.dataset.go) {
    if (t.dataset.history) state.used_history = t.dataset.history === 'yes';
    go(t.dataset.go);
  } else if (t.dataset.back !== undefined) {
    back();
  } else if (t.dataset.choice) {
    const group = t.closest('.seg');
    for (const b of group.querySelectorAll('.seg__b')) b.setAttribute('aria-pressed', String(b === t));
    state.choices[t.closest('[data-case]').dataset.case] = t.dataset.choice;
    syncReady();
  }
});

show(form.dataset.start || 'intro');
