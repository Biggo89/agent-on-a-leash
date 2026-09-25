/**
 * The agent column — what the shopping agent is doing, as it does it.
 *
 * Steps are revealed one at a time so the run reads as activity rather than as
 * a log dump. The agent's tool calls are reconstructed from each event's own
 * facts (see src/data/agent-script.js); the outcome badge is engine output.
 */

import { html, el, money, esc, raw } from '../core/format.js';
import * as ico from './icons.js';

const TOOL_LABEL = {
  plan: 'plan',
  search_merchants: 'search_merchants',
  select_merchant: 'select_merchant',
  read_product_page: 'read_product_page',
  add_to_cart: 'add_to_cart',
  session: 'session',
  request_authorization: 'request_authorization',
};

const FLAG_TEXT = {
  injection: 'seller text addresses automated systems',
  lookalike: 'name resembles a shop the card uses',
  'new-merchant': null,
  device: 'unrecognised device',
  authorize: null,
};

export function createAgentPanel(root) {
  const body = root.querySelector('.col__body');
  const tasks = new Map(); // authId → { node, stepsNode }

  function idle(text = 'Waiting for the agent to start.') {
    body.innerHTML = html`<div class="agent-idle"><span class="pulse"></span>${text}</div>`;
  }

  function clear() {
    tasks.clear();
    idle();
  }

  /** Open a task card for an event; steps are added by `pushStep`. */
  function openTask(event) {
    body.querySelector('.agent-idle')?.remove();
    const node = el(html`
      <article class="task task--live" data-auth="${event.id}">
        <header class="task__hd">
          <span class="task__id">${event.id}</span>
          <span class="task__amt">${money(event.amount, event.currency)}</span>
        </header>
        <ol class="steps"></ol>
      </article>
    `);
    body.appendChild(node);
    tasks.set(event.id, { node, steps: node.querySelector('.steps') });
    node.scrollIntoView({ behavior: 'smooth', block: 'end' });
    return node;
  }

  function pushStep(authId, step) {
    const t = tasks.get(authId);
    if (!t) return;
    const flagText = step.flag ? FLAG_TEXT[step.flag] : null;
    const li = el(html`
      <li class="step ${step.flag ? 'step--flag' : ''} ${step.flag === 'authorize' ? 'step--authorize' : ''}">
        <span class="step__rail"><i class="step__dot"></i></span>
        <span class="step__main">
          <span class="step__tool">${TOOL_LABEL[step.tool] ?? step.tool}</span>
          <span class="step__arg">${step.arg}</span>
          ${step.note ? raw(`<span class="step__note">${esc(step.note)}</span>`) : ''}
          ${flagText ? raw(`<span class="step__flag">${esc(flagText)}</span>`) : ''}
        </span>
      </li>
    `);
    t.steps.appendChild(li);
    t.node.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  /** Land the engine's decision on the task card. */
  function closeTask(authId, decision, reaction) {
    const t = tasks.get(authId);
    if (!t) return;
    t.node.classList.remove('task--live');
    t.node.classList.add(`task--${decision}`);
    const prev = t.node.querySelector('.task__out');
    if (prev) prev.remove();
    const icon = decision === 'approve' ? ico.tick : decision === 'decline' ? ico.cross : ico.bang;
    t.node.appendChild(
      el(html`
        <footer class="task__out task__out--${decision}">
          <span class="chk__dot chk--${decision === 'approve' ? 'pass' : decision === 'decline' ? 'violation' : 'concern'} chk--done"
                style="background:currentColor;border-color:currentColor">${raw(icon)}</span>
          <span>${reaction.text}</span>
        </footer>
      `),
    );
    t.node.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  /** Re-label a card after the cardholder answers a step-up. */
  function resolveTask(authId, status, note) {
    const t = tasks.get(authId);
    if (!t) return;
    t.node.classList.remove('task--step_up');
    t.node.classList.add(status === 'approved' ? 'task--approve' : 'task--decline');
    const out = t.node.querySelector('.task__out');
    if (out) {
      out.className = `task__out task__out--${status === 'approved' ? 'approve' : 'decline'}`;
      out.lastElementChild.textContent =
        note ?? (status === 'approved' ? 'Cardholder confirmed. Order placed.' : 'Cardholder declined. Order not placed.');
    }
  }

  function halt(text) {
    body.appendChild(el(html`<div class="agent-idle" style="border-color:var(--no-line);color:var(--no)">${text}</div>`));
    body.lastElementChild.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  clear();
  return { clear, idle, openTask, pushStep, closeTask, resolveTask, halt };
}
