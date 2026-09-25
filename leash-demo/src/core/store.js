/**
 * A ~60-line reactive store. Enough for this app, and nothing to install.
 *
 * State is one plain object. `patch()` shallow-merges and notifies; components
 * subscribe with a selector so they only re-render when their slice changes.
 */

export function createStore(initial) {
  let state = initial;
  const subs = new Set();

  function get() {
    return state;
  }

  function patch(partial) {
    const next = typeof partial === 'function' ? partial(state) : partial;
    if (!next) return state;
    state = { ...state, ...next };
    for (const fn of [...subs]) fn(state);
    return state;
  }

  function subscribe(fn, { immediate = true } = {}) {
    subs.add(fn);
    if (immediate) fn(state);
    return () => subs.delete(fn);
  }

  /** Subscribe to a derived slice; fires only when the slice changes identity. */
  function select(selector, fn) {
    let prev = selector(state);
    fn(prev, undefined);
    return subscribe(
      (s) => {
        const next = selector(s);
        if (next !== prev) {
          const old = prev;
          prev = next;
          fn(next, old);
        }
      },
      { immediate: false },
    );
  }

  return { get, patch, subscribe, select };
}

/** Tiny event bus for one-shot UI moments (toasts, focus, scroll). */
export function createBus() {
  const handlers = new Map();
  return {
    on(type, fn) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(fn);
      return () => handlers.get(type).delete(fn);
    },
    emit(type, payload) {
      for (const fn of handlers.get(type) ?? []) fn(payload);
    },
  };
}
