// fake_kit.js with the real kit's timing, for panel_cases.js: api() answers a macrotask later, or when the case
// releases it (HOLD picks the calls it holds), and a call under a `latest` name aborts the one before it with an
// AbortError, as does abandon(name). autoRefresh does not tick by itself: the case calls POLLERS[i].fn().
export * from './fake_kit.js';
import { ApiError } from './fake_kit.js';

const latestCalls = new Map();  // name -> abort() of the newest call under that name
globalThis.PENDING = [];         // held calls: {method, path, resolve}
globalThis.ABORTED = [];         // paths of the calls a newer one or abandon() aborted
globalThis.POLLERS = [];
globalThis.HOLD = () => false;

export async function api(path, { method = 'GET', json, latest = '', quiet = false } = {}) {
  globalThis.CALLS.push([method, path, json, latest]);
  let abort;
  const aborted = new Promise((_, reject) => {
    abort = () => {
      globalThis.ABORTED.push(path);
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    };
  });
  if (latest) {
    latestCalls.get(latest)?.();
    latestCalls.set(latest, abort);
  }
  const released = globalThis.HOLD(method, path)
    ? new Promise((resolve) => globalThis.PENDING.push({ method, path, resolve }))
    : new Promise((resolve) => setTimeout(resolve, 0));
  try {
    await Promise.race([released, aborted]);
    const answer = globalThis.SERVER(method, path, json);
    if (answer instanceof ApiError) {
      if (!quiet) globalThis.TOASTS.push(['error', answer.message]);
      throw answer;
    }
    return JSON.parse(JSON.stringify(answer));
  } finally {
    if (latest && latestCalls.get(latest) === abort) latestCalls.delete(latest);
  }
}

export function abandon(name) {
  latestCalls.get(name)?.();
  latestCalls.delete(name);
}

export function autoRefresh(fn, ms) {
  const handle = { on: false, fn, ms, get running() { return this.on; }, start() { this.on = true; }, stop() { this.on = false; } };
  globalThis.POLLERS.push(handle);
  return handle;
}
