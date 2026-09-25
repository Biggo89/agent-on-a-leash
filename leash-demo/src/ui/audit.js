/**
 * The audit drawer — `GET /v1/audit/{authorization_id}`.
 *
 * The answer to "a decision looks wrong on stage": every check that ran, its
 * verdict, its evidence, the concern score against the threshold. Read it out;
 * do not argue from memory.
 */

import { html, el, esc, latency, money, raw } from '../core/format.js';
import { CHECK_META } from '../core/client.js';
import * as ico from './icons.js';

export function createAuditDrawer(mount) {
  let openEl = null;

  function close() {
    if (!openEl) return;
    openEl.remove();
    openEl = null;
    document.removeEventListener('keydown', onKey);
  }

  function onKey(e) {
    if (e.key === 'Escape') close();
  }

  function open(record, event, extra = {}) {
    close();
    const rows = record.checks
      .map(
        (c) => `<tr>
          <td style="padding:5px 10px 5px 0;vertical-align:top;white-space:nowrap">
            <span style="font-family:var(--mono);font-size:10.5px;color:var(--text-mute)">${esc(c.check_id)}</span></td>
          <td style="padding:5px 10px 5px 0;vertical-align:top;color:${
            c.verdict === 'violation' ? 'var(--no)' : c.verdict === 'concern' || c.verdict === 'unknown' ? 'var(--ask)' : c.verdict === 'pass' ? 'var(--ok)' : 'var(--text-faint)'
          };font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em">${esc(c.verdict)}</td>
          <td style="padding:5px 0;vertical-align:top;font-size:12px">${esc(c.detail || '—')}${
            c.evidence?.length
              ? `<div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px 10px">${c.evidence
                  .map((e) => `<span class="ev"><b>${esc(e.field)}</b><span>${esc(e.value)}</span></span>`)
                  .join('')}</div>`
              : ''
          }${c.weight ? `<div style="margin-top:3px;font-size:11px;color:var(--ask)">concern weight ${c.weight}</div>` : ''}</td>
        </tr>`,
      )
      .join('');

    const trail = [
      {
        type: 'decision',
        authorization_id: record.authorization_id,
        decision: record.decision,
        reason_codes: record.reason_codes,
        concern_score: record.score.concern_score,
        step_up_threshold: record.score.step_up_threshold,
        latency_ms: record.timing.latency_ms,
        engine_version: 'leash-0.1.0',
      },
    ];
    if (extra.resolution)
      trail.push({
        type: 'resolution',
        authorization_id: record.authorization_id,
        resolved_by: 'customer',
        final_status: extra.resolution,
      });

    openEl = el(html`
      <div>
        <div class="scrim" data-close></div>
        <aside class="drawer" role="dialog" aria-label="Audit record">
          <header class="drawer__hd">
            ${raw(ico.doc)}
            <span class="drawer__ttl">Audit record · ${record.authorization_id}</span>
            <button class="btn btn--ghost btn--sm" data-close style="margin-left:auto">${raw(ico.close)}</button>
          </header>
          <div class="drawer__body">
            <section class="drawer__sec">
              <h4>What was asked</h4>
              <div class="req__body" style="padding:0;gap:7px">
                <div class="req__row"><span class="req__k">Amount</span><span class="req__v">${money(event.billing_amount_chf)}${
                  event.currency !== 'CHF' ? ` (charged ${money(event.amount, event.currency)})` : ''
                }</span></div>
                <div class="req__row"><span class="req__k">Seller</span><span class="req__v">${event.merchant_name} · ${event.merchant_id}</span></div>
                <div class="req__row"><span class="req__k">Device</span><span class="req__v">${event.device_id}</span></div>
                <div class="req__row"><span class="req__k">At</span><span class="req__v">${event.timestamp}</span></div>
              </div>
            </section>

            <section class="drawer__sec">
              <h4>Every check that ran (${record.checks.length})</h4>
              <table style="width:100%;border-collapse:collapse"><tbody>${raw(rows)}</tbody></table>
            </section>

            <section class="drawer__sec">
              <h4>Outcome</h4>
              <p style="margin:0 0 8px;font-size:13px;line-height:1.55;color:var(--ink)">${record.customer_message}</p>
              <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--text-mute)">
                <span>concern <b style="color:var(--ink)">${record.score.concern_score.toFixed(1)}</b> / ${record.score.step_up_threshold.toFixed(1)}</span>
                <span>decided in <b style="color:var(--ink)">${latency(record.timing.latency_ms)}</b></span>
                <span>budget <b style="color:var(--ink)">8 000 ms</b></span>
              </div>
            </section>

            <section class="drawer__sec">
              <h4>Append-only trail · GET /v1/audit/${record.authorization_id}/raw</h4>
              <pre class="jsonl">${trail.map((t) => JSON.stringify(t)).join('\n')}</pre>
              <p style="font-size:11px;color:var(--text-faint);margin:7px 0 0;line-height:1.5">
                A resolution is appended as its own record. The original decision is never rewritten.</p>
            </section>
          </div>
        </aside>
      </div>`);

    openEl.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', close));
    document.addEventListener('keydown', onKey);
    mount.appendChild(openEl);
  }

  return { open, close };
}
