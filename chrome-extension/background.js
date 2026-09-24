import { E as ERROR_CODES, f as formatRefusal } from "./chunks/refusals.js";
import { a as attachabilityOf, d as describeAttachFailure, i as isForeignExtensionRefusal, f as foreignExtensionIds, p as preloadedPageHint, s as summariseFrameRefs, u as unreportedParentCount, r as redactText, g as getReplayStore, b as redactCaption, R as REDACT_THEN_TRUNCATE_PREFIX_MULTIPLIER, c as secretRedactionMarker, t as trimTrailingPartialToken, e as redactThenTruncate, h as redactSecrets } from "./chunks/replay-store.js";
import { g as getSettings, s as setSettings, p as powerPolicyOf, r as redactionPolicyOf, c as clearCredentials, a as setCredentials, b as getCredentials } from "./chunks/storage.js";
import { s as send } from "./chunks/messages.js";
const SHARED_GROUP_TITLE = "Shared with Hermes";
const SHARED_GROUP_COLOR = "purple";
const SHARED_BADGE_COLOR = "#0891b2";
const NO_GROUP$1 = -1;
const attachedTabIds = /* @__PURE__ */ new Set();
const priorGroupByTab = /* @__PURE__ */ new Map();
const windowQueue = /* @__PURE__ */ new Map();
function withWindowLock(windowId, fn) {
  const prior = windowQueue.get(windowId) ?? Promise.resolve();
  const result = prior.then(fn, fn);
  windowQueue.set(
    windowId,
    result.then(
      () => {
      },
      () => {
      }
    )
  );
  return result;
}
function sharedTabCount() {
  return attachedTabIds.size;
}
const countChangeListeners = [];
function onSharedCountChanged(listener) {
  countChangeListeners.push(listener);
}
function notifyCountChanged() {
  for (const listener of countChangeListeners) listener();
}
function reconcileAttachedTabs(tabIds) {
  const next = new Set(tabIds);
  let changed = next.size !== attachedTabIds.size;
  if (!changed) {
    for (const id of next) {
      if (!attachedTabIds.has(id)) {
        changed = true;
        break;
      }
    }
  }
  attachedTabIds.clear();
  for (const id of next) attachedTabIds.add(id);
  if (changed) notifyCountChanged();
}
async function moveIntoSharedGroup(tabId, windowId) {
  await withWindowLock(windowId, async () => {
    const existing = await chrome.tabGroups.query({ windowId, title: SHARED_GROUP_TITLE });
    const groupId = existing[0]?.id;
    if (typeof groupId === "number") {
      await chrome.tabs.group({ tabIds: [tabId], groupId });
      return;
    }
    const newGroupId = await chrome.tabs.group({ tabIds: [tabId] });
    await chrome.tabGroups.update(newGroupId, { title: SHARED_GROUP_TITLE, color: SHARED_GROUP_COLOR });
  });
}
async function restoreFromSharedGroup(tabId, windowId, priorGroupId) {
  await withWindowLock(windowId, async () => {
    if (priorGroupId === NO_GROUP$1) {
      await chrome.tabs.ungroup(tabId);
      return;
    }
    try {
      await chrome.tabs.group({ tabIds: [tabId], groupId: priorGroupId });
    } catch {
      await chrome.tabs.ungroup(tabId);
    }
  });
}
async function onTabAttached(tabId) {
  attachedTabIds.add(tabId);
  try {
    notifyCountChanged();
  } catch {
  }
  try {
    const settings = await getSettings();
    if (!settings.highlightSharedTabs) return;
    const tab = await chrome.tabs.get(tabId);
    if (typeof tab.windowId !== "number") return;
    priorGroupByTab.set(tabId, typeof tab.groupId === "number" ? tab.groupId : NO_GROUP$1);
    await moveIntoSharedGroup(tabId, tab.windowId);
  } catch {
  }
}
async function onTabReleased(tabId) {
  attachedTabIds.delete(tabId);
  try {
    notifyCountChanged();
  } catch {
  }
  const priorGroupId = priorGroupByTab.get(tabId);
  priorGroupByTab.delete(tabId);
  if (priorGroupId === void 0) return;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (typeof tab.windowId !== "number") return;
    await restoreFromSharedGroup(tabId, tab.windowId, priorGroupId);
  } catch {
  }
}
async function pausedNow() {
  try {
    return Boolean((await getSettings()).paused);
  } catch {
    return false;
  }
}
const PROTOCOL_VERSION = "1.3";
const DESIRED_ATTACH_KEY = "cdp.desiredAttach";
const LIMITED_ATTACH_KEY = "cdp.limitedAttach";
const UPGRADE_RETRY_INTERVAL_MS = 5e3;
const DOMAINS = ["Page", "DOM", "Runtime", "Network", "Log"];
async function enableFocusEmulation(tabId) {
  try {
    await chrome.debugger.sendCommand({ tabId }, "Emulation.setFocusEmulationEnabled", { enabled: true });
  } catch {
  }
}
function describeError(error) {
  return error instanceof Error ? error.message : String(error);
}
async function framesOf(tabId) {
  try {
    const frames = await chrome.webNavigation.getAllFrames({ tabId });
    return (frames ?? []).filter((frame) => typeof frame.url === "string").map((frame) => ({
      frameId: frame.frameId,
      parentFrameId: frame.parentFrameId,
      url: frame.url,
      lifecycle: frame.documentLifecycle,
      frameType: frame.frameType,
      // getAllFrames reports every DOCUMENT in the tab's WebContents, not
      // just the active frame tree -- a prerendered or back-forward-cached
      // page included, each with its own documentId. This is what lets
      // stripCandidateFrames below target those documents directly (via
      // chrome.scripting.executeScript's documentIds / chrome.tabs.
      // sendMessage's documentId) instead of only the frames reachable
      // through the primary page's own frameId tree.
      documentId: frame.documentId
    }));
  } catch {
    return [];
  }
}
const MAX_FOREIGN_FRAME_ATTACH_ATTEMPTS = 3;
const FOREIGN_FRAME_RETRY_BACKOFF_MS = [700, 1300];
function sleep$2(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
function notifyAttachRetried(tabId, attempt, maxAttempts) {
  void chrome.runtime.sendMessage({ target: "offscreen", type: "attach.retried", tabId, attempt, maxAttempts }).catch(() => {
  });
}
function stripCandidateFrames(frames) {
  return frames.filter((frame) => !frame.url.startsWith("chrome-extension://")).map((frame) => ({ frameId: frame.frameId, documentId: frame.documentId }));
}
function childFrameUrlsOf(frameId, frames) {
  return frames.filter((frame) => frame.parentFrameId === frameId).map((frame) => frame.url);
}
function dedupeCandidates(candidates) {
  const seen = /* @__PURE__ */ new Set();
  const result = [];
  for (const candidate of candidates) {
    const key = candidate.documentId ?? `frame:${candidate.frameId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(candidate);
  }
  return result;
}
function injectContentScript(tabId, candidate) {
  const target = candidate.documentId ? { tabId, documentIds: [candidate.documentId] } : { tabId, frameIds: [candidate.frameId] };
  return chrome.scripting.executeScript({ target, files: ["content.js"] });
}
function sendToFrame(tabId, message, candidate) {
  const options = candidate.documentId ? { documentId: candidate.documentId } : { frameId: candidate.frameId };
  return chrome.tabs.sendMessage(tabId, message, options);
}
async function stripForeignFramesAcrossTab(tabId, candidates, frames) {
  const stripped = [];
  let count = 0;
  const extensionIds = /* @__PURE__ */ new Set();
  const diagnostics = [];
  for (const candidate of candidates) {
    const childFrameUrls = childFrameUrlsOf(candidate.frameId, frames);
    const webNavigationChildCount = childFrameUrls.length;
    try {
      await injectContentScript(tabId, candidate);
      const response = await sendToFrame(
        tabId,
        { target: "content", type: "stripForeignFrames", childFrameUrls },
        candidate
      );
      if (response?.ok === true) {
        diagnostics.push({
          frameId: candidate.frameId,
          documentId: candidate.documentId,
          replied: true,
          windowLength: response.data.windowLength,
          elementCount: response.data.elementCount,
          webNavigationChildCount
        });
        if (response.data.count > 0) {
          stripped.push(candidate);
          count += response.data.count;
          for (const id of response.data.extensionIds) extensionIds.add(id);
        }
      } else {
        diagnostics.push({ frameId: candidate.frameId, documentId: candidate.documentId, replied: false, webNavigationChildCount });
      }
    } catch {
      diagnostics.push({ frameId: candidate.frameId, documentId: candidate.documentId, replied: false, webNavigationChildCount });
    }
  }
  return { stripped, scan: { count, extensionIds: [...extensionIds] }, diagnostics };
}
const RESTORE_TIMEOUT_MS = 4e3;
async function restoreForeignFramesAcrossTab(tabId, candidates) {
  const outcomes = await Promise.all(
    candidates.map(async (candidate) => {
      try {
        const response = await Promise.race([
          sendToFrame(tabId, { target: "content", type: "restoreForeignFrames" }, candidate),
          sleep$2(RESTORE_TIMEOUT_MS).then(() => void 0)
        ]);
        return response?.ok === true;
      } catch {
        return false;
      }
    })
  );
  return { allRestored: outcomes.every(Boolean) };
}
const RELOAD_WAIT_TIMEOUT_MS = 15e3;
function waitForTabLoadComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    timer = setTimeout(finish, Math.max(0, timeoutMs));
  });
}
async function reloadTabAndWait(tabId) {
  try {
    await chrome.tabs.reload(tabId);
  } catch {
  }
  await waitForTabLoadComplete(tabId, RELOAD_WAIT_TIMEOUT_MS);
}
async function attemptStripAndAttach(tabId, frames) {
  const candidates = stripCandidateFrames(frames);
  const { stripped, scan, diagnostics } = await stripForeignFramesAcrossTab(tabId, candidates, frames);
  if (scan.count === 0) {
    return { attempted: false, attached: false, scan, restored: true, sessionSurvived: true, diagnostics };
  }
  let attached = false;
  let allRestored = true;
  try {
    await chrome.debugger.attach({ tabId }, PROTOCOL_VERSION);
    attached = true;
  } catch {
    attached = false;
  } finally {
    allRestored = (await restoreForeignFramesAcrossTab(tabId, stripped)).allRestored;
  }
  let sessionSurvived = true;
  if (attached) {
    try {
      await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: "1" });
    } catch {
      sessionSurvived = false;
    }
  }
  return { attempted: true, attached, scan, restored: allRestored, sessionSurvived, diagnostics };
}
async function attemptReloadStripAndAttach(tabId) {
  await reloadTabAndWait(tabId);
  const frames = await framesOf(tabId);
  return attemptStripAndAttach(tabId, frames);
}
function notifyAttachStripped(tabId, outcome, reloaded, attached) {
  const scannedDocuments = outcome.diagnostics.length;
  const noReplyDocuments = outcome.diagnostics.filter((d) => !d.replied).length;
  const unexplainedChildFrameCount = outcome.diagnostics.filter((d) => d.replied).reduce((max, d) => Math.max(max, (d.windowLength ?? 0) - d.webNavigationChildCount, (d.elementCount ?? 0) - d.webNavigationChildCount), 0);
  void chrome.runtime.sendMessage({
    target: "offscreen",
    type: "attach.stripped",
    tabId,
    strippedCount: outcome.scan.count,
    extensionIds: outcome.scan.extensionIds,
    restored: outcome.restored,
    sessionSurvived: outcome.sessionSurvived,
    reloaded,
    attached,
    // Numbers only (gap (a)), never page content: how many documents were
    // scanned, how many of those never replied at all (the scan didn't
    // run there, distinct from "ran and found nothing"), and the largest
    // window.length/element-count excess over what webNavigation itself
    // reported as that document's children.
    scannedDocuments,
    noReplyDocuments,
    unexplainedChildFrameCount
  }).catch(() => {
  });
}
function describeSessionDropped(url, scan) {
  const where = url ? ` (${url})` : "";
  const who = scan.extensionIds.length > 0 ? scan.extensionIds.map((id) => `extension ${id} (chrome://extensions/?id=${id})`).join(", ") : "another extension's";
  return `Attached tab${where}, but Chrome dropped the debugger session the instant ${who} frame was put back after being temporarily removed to let the debugger attach. Ask the user to set that extension's Site access to "On click" on this page (or leave that extension disabled here), then retry browser_bridge_attach.`;
}
async function holdForeignFramesEnabled() {
  try {
    return (await getSettings()).holdForeignFramesWhileAttached !== false;
  } catch {
    return true;
  }
}
async function armGuardAcrossTab(tabId, candidates, frames) {
  const armed = [];
  for (const candidate of candidates) {
    const childFrameUrls = childFrameUrlsOf(candidate.frameId, frames);
    try {
      await injectContentScript(tabId, candidate);
      const response = await sendToFrame(
        tabId,
        { target: "content", type: "armForeignFrameGuard", childFrameUrls },
        candidate
      );
      if (response?.ok === true) armed.push(candidate);
    } catch {
    }
  }
  return armed;
}
async function disarmGuardAcrossTab(tabId, candidates) {
  await Promise.all(
    candidates.map(async (candidate) => {
      try {
        await Promise.race([
          sendToFrame(tabId, { target: "content", type: "disarmForeignFrameGuard" }, candidate),
          sleep$2(RESTORE_TIMEOUT_MS).then(() => void 0)
        ]);
      } catch {
      }
    })
  );
}
async function guardStatusAcrossTab(tabId, candidates) {
  const ids = /* @__PURE__ */ new Set();
  await Promise.all(
    candidates.map(async (candidate) => {
      try {
        const response = await sendToFrame(tabId, { target: "content", type: "foreignFrameGuardStatus" }, candidate);
        if (response?.ok === true) for (const id of response.data.extensionIds) ids.add(id);
      } catch {
      }
    })
  );
  return { extensionIds: [...ids] };
}
function notifyAttachSessionDropped(tabId, reason, extensionIds, autoReattachAttempted, autoReattachSucceeded) {
  void chrome.runtime.sendMessage({
    target: "offscreen",
    type: "attach.sessionDropped",
    tabId,
    reason,
    extensionIds,
    autoReattachAttempted,
    autoReattachSucceeded
  }).catch(() => {
  });
}
function notifyAttachModeChanged(tabId, mode, reason) {
  void chrome.runtime.sendMessage({
    target: "offscreen",
    type: "attach.modeChanged",
    tabId,
    mode,
    ...reason !== void 0 ? { reason } : {}
  }).catch(() => {
  });
}
class CdpManager {
  // Tabs this worker instance believes are attached. Rebuilt from
  // chrome.debugger.getTargets() on every resync rather than trusted blindly,
  // since a worker restart starts this Set empty while Chrome may still hold
  // real debugger sessions open.
  attached = /* @__PURE__ */ new Set();
  // Limited-mode share fix: tabs sharing without a chrome.debugger session
  // (attachLimited()'s own set, disjoint from `attached` -- a tab is in
  // exactly one of the two, never both). See modeOf()/isShared() below for
  // the combined view most callers actually want.
  limited = /* @__PURE__ */ new Set();
  // The reason attachLimited() fell this tab back, for tabRefOf()'s
  // attachModeReason -- not persisted (best-effort only, rebuilt with a
  // generic message if a worker restart restores `limited` from storage
  // without it; see runResync()'s own comment on that restore).
  limitedReasons = /* @__PURE__ */ new Map();
  // Opportunistic upgrade bookkeeping for maybeUpgrade() -- see
  // UPGRADE_RETRY_INTERVAL_MS's comment above for why this is per-tab.
  lastUpgradeAttempt = /* @__PURE__ */ new Map();
  upgradeInFlight = /* @__PURE__ */ new Set();
  detachListeners = [];
  // M3's network.ts ring buffer needs to drop a tab's buffer the moment it's
  // released — for ANY reason (explicit tabs.release/tabs.releaseAll, or
  // Chrome tearing the session down on its own). Those are two different
  // code paths below (chrome.debugger.onDetach only fires for the latter —
  // calling chrome.debugger.detach() yourself does not raise it), so this is
  // a distinct listener list from detachListeners, fired from both places via
  // notifyReleased(), rather than folding into detachListeners and changing
  // what already-tested consumers (background.ts's tab.changed push) observe.
  releaseListeners = [];
  // Counterpart of releaseListeners for a successful attach() (presence.ts
  // shows the overlay's Stop pill on every attached tab).
  attachListeners = [];
  // The resync kicked off at worker wake (background.ts) runs un-awaited;
  // releaseAll() waits for it so a Stop on a cold-started worker does not
  // run against a not-yet-populated `attached` set.
  resyncInFlight = null;
  // One-off waiters for a specific (tabId, CDP method) pair — backs
  // waitForEvent() below (act.ts's `navigate` settle and the load-event race
  // in settle()). A single onEvent listener services every waiter rather than
  // each caller registering (and having to remember to unregister) its own,
  // which is how a `navigate` action's leaked listener would otherwise keep
  // firing into a stale promise on every subsequent page load of that tab.
  eventWaiters = /* @__PURE__ */ new Set();
  // C2 (`wait_for`'s `network_idle` condition): a MULTI-fire counterpart to
  // eventWaiters above -- a one-shot waiter resolves and forgets itself on
  // the first matching event, which cannot track "how many requests are
  // currently in flight" over time. A subscriber instead gets every matching
  // event on its tab for as long as it stays subscribed (act.ts's
  // actWaitForNetworkIdle unsubscribes once its own wait ends). Reuses the
  // exact same chrome.debugger.onEvent listener below rather than a second
  // listener registration.
  eventSubscribers = /* @__PURE__ */ new Set();
  // Documents the persistent foreign-frame guard is currently armed in, per
  // attached tab -- set once attach() actually succeeds, cleared by
  // disarmGuard() (release, organic detach with no reattach left, kill
  // switch, pause). Presence in this map is also how attach() tells "a
  // plain, no-refusal success" apart from "already armed via the strip
  // step" without a second flag. Keyed by FrameCandidate (frameId +,
  // when webNavigation reported one, documentId) rather than a bare frameId
  // so a prerendered/back-forward-cached document stays reachable by its
  // stable documentId across the frameId renumbering an activation causes.
  guardedFrames = /* @__PURE__ */ new Map();
  // Tabs that have already spent their one automatic re-attach after an
  // organic (Chrome-initiated) detach -- see handleOrganicDetach(). Cleared
  // whenever the tab is disarmed, so a later, fresh user-initiated attach
  // gets its own budget.
  autoReattachedTabs = /* @__PURE__ */ new Set();
  constructor() {
    chrome.debugger.onDetach.addListener((source, reason) => {
      const tabId = source.tabId;
      if (typeof tabId !== "number") return;
      const wasAttached = this.attached.delete(tabId);
      void this.persistDesired();
      if (!wasAttached) return;
      for (const listener of this.detachListeners) listener(tabId, reason);
      this.notifyReleased(tabId);
      void this.handleOrganicDetach(tabId, reason);
    });
    chrome.debugger.onEvent.addListener((source, method, params) => {
      if (method === "Target.attachedToTarget" || method === "Target.detachedFromTarget") return;
      if (this.eventWaiters.size === 0 && this.eventSubscribers.size === 0) return;
      const tabId = source.tabId;
      if (typeof tabId !== "number") return;
      const eventParams = params ?? {};
      for (const waiter of [...this.eventWaiters]) {
        if (waiter.tabId !== tabId || waiter.method !== method) continue;
        this.eventWaiters.delete(waiter);
        waiter.resolve(eventParams);
      }
      for (const sub of this.eventSubscribers) {
        if (sub.tabId !== tabId || !sub.methods.has(method)) continue;
        sub.handler(method, eventParams);
      }
    });
    if (chrome.webNavigation?.onCommitted?.addListener) {
      chrome.webNavigation.onCommitted.addListener((details) => {
        void this.rearmGuardForTab(details.tabId).catch(() => {
        });
      });
    }
  }
  /** Arms the persistent guard across `candidates` when the user's own
   * setting allows it; a no-op (returns `[]`, nothing armed) when it's off.
   * Centralizes the setting check so attach()'s several call sites (the
   * pre-strip early arm, the post-reload re-arm, the plain-success tail)
   * can't disagree about whether the guard runs. */
  async armGuard(tabId, candidates, frames) {
    if (!await holdForeignFramesEnabled()) return [];
    return armGuardAcrossTab(tabId, candidates, frames);
  }
  /** Disarms and restores whatever `armGuard` armed for `tabId`, and clears
   * this tab's auto-reattach budget so a later fresh attach gets its own.
   * A documented no-op when nothing was ever armed for this tab. */
  async disarmGuard(tabId) {
    const candidates = this.guardedFrames.get(tabId);
    this.guardedFrames.delete(tabId);
    this.autoReattachedTabs.delete(tabId);
    if (candidates && candidates.length > 0) await disarmGuardAcrossTab(tabId, candidates);
  }
  /**
   * C: what happens when Chrome drops an already-attached tab's session on
   * its own (chrome.debugger.onDetach), for a tab the persistent guard was
   * watching. The guard's own content-script state is untouched by a
   * debugger detach (it's ordinary page JS, not part of the CDP session),
   * so whatever it's currently holding stays held across this -- there is
   * nothing to disarm/restore yet if a re-attach is about to happen; doing
   * so would just hand the foreign frame straight back and risk re-causing
   * the exact drop being recovered from.
   *
   * At most ONE automatic re-attach per tab (autoReattachedTabs): a tab
   * that keeps dropping is a tab this should stop touching automatically
   * and instead surface plainly, never something to retry forever.
   */
  async handleOrganicDetach(tabId, reason) {
    const guardedCandidates = this.guardedFrames.get(tabId);
    const wasGuarded = guardedCandidates !== void 0;
    if (!wasGuarded) return;
    if (!this.autoReattachedTabs.has(tabId)) {
      this.autoReattachedTabs.add(tabId);
      const status2 = await guardStatusAcrossTab(tabId, guardedCandidates);
      const result = await this.attach(tabId);
      notifyAttachSessionDropped(tabId, reason, status2.extensionIds, true, result.ok);
      if (!result.ok) await this.disarmGuard(tabId);
      return;
    }
    const status = await guardStatusAcrossTab(tabId, guardedCandidates);
    await this.disarmGuard(tabId);
    notifyAttachSessionDropped(tabId, reason, status.extensionIds, false, false);
  }
  /**
   * Re-arms the guard across whatever candidate documents currently exist
   * for `tabId`, if the tab is one the guard is watching at all. A no-op
   * for any other tab (not attached, or attached with the guard off).
   *
   * Why this exists: arming already covers a document that EXISTED at
   * attach time, prerendered or not (armGuard is called with every
   * candidate framesOf() reports, and that includes prerendered/back-
   * forward-cached documents -- see stripCandidateFrames' own comment). It
   * does NOT cover a document that starts prerendering AFTER attach: that
   * document did not exist yet when this tab was last armed, so it isn't in
   * `guardedFrames` and nothing has told its content script to watch for a
   * foreign frame. Re-running armGuard here on every navigation commit in
   * this tab (including a prerendering navigation, and a prerender's own
   * activation swap -- both of which commit like an ordinary navigation
   * from the extension APIs' perspective) closes that gap. armGuardAcrossTab
   * is idempotent per document (foreign-frame-guard.ts's armed flag just
   * re-sweeps on a second arm), so re-arming a document that was already
   * armed costs a wasted round trip, never a second competing observer.
   */
  async rearmGuardForTab(tabId) {
    if (!this.guardedFrames.has(tabId)) return;
    if (!this.attached.has(tabId)) return;
    const frames = await framesOf(tabId);
    const candidates = stripCandidateFrames(frames);
    const armed = await this.armGuard(tabId, candidates, frames);
    if (armed.length > 0) this.guardedFrames.set(tabId, dedupeCandidates([...this.guardedFrames.get(tabId) ?? [], ...armed]));
  }
  /**
   * Resolve the next occurrence of `method` on `tabId`, or `null` if
   * `timeoutMs` elapses first. Used to bound waits on real CDP events
   * (`Page.loadEventFired`) instead of guessing a fixed delay — a page that
   * never navigates just times out cleanly rather than the caller hanging.
   */
  waitForEvent(tabId, method, timeoutMs) {
    return new Promise((resolve) => {
      const waiter = {
        tabId,
        method,
        resolve: (params) => {
          clearTimeout(timer);
          resolve(params);
        }
      };
      const timer = setTimeout(() => {
        this.eventWaiters.delete(waiter);
        resolve(null);
      }, Math.max(0, timeoutMs));
      this.eventWaiters.add(waiter);
    });
  }
  /**
   * C2 (`network_idle`): calls `handler(method, params)` for every one of
   * `methods` seen on `tabId`'s CDP event stream, for as long as the caller
   * keeps the returned unsubscribe function unused -- unlike `waitForEvent`,
   * this never auto-resolves or forgets itself after one event. Only
   * meaningful for a tab attached in FULL mode (Network is one of the
   * domains DOMAINS enables on attach); a limited-mode tab has no
   * chrome.debugger session at all, so no event ever arrives, and the caller
   * (act.ts's actWaitForNetworkIdle) is expected to check `isAttached`
   * itself before subscribing rather than hanging on a subscription that can
   * never fire.
   */
  subscribe(tabId, methods, handler) {
    const entry = { tabId, methods: new Set(methods), handler };
    this.eventSubscribers.add(entry);
    return () => {
      this.eventSubscribers.delete(entry);
    };
  }
  /** Notified on user-closed banner, tab crash, or devtools stealing the target. */
  onDetach(listener) {
    this.detachListeners.push(listener);
  }
  /** Notified whenever a tab stops being attached, regardless of cause —
   * explicit release() (single tab or releaseAll) or an organic onDetach
   * above. For per-tab cleanup that must happen no matter how the tab came
   * off (M3's network.ts ring buffer), rather than something each call site
   * has to remember to trigger. */
  onRelease(listener) {
    this.releaseListeners.push(listener);
  }
  /** Notified after each successful attach(). */
  onAttach(listener) {
    this.attachListeners.push(listener);
  }
  notifyReleased(tabId) {
    for (const listener of this.releaseListeners) listener(tabId);
  }
  /** True only for a real chrome.debugger session ("full" mode) -- callers
   * gating a CDP-only capability (screenshot, evaluate, dialogs, network,
   * console, upload, drag) must keep using this, never isShared(). */
  isAttached(tabId) {
    return this.attached.has(tabId);
  }
  /** Limited-mode share fix: true for the no-debugger fallback only --
   * disjoint from isAttached(). */
  isLimited(tabId) {
    return this.limited.has(tabId);
  }
  /** True whenever the tab is shared AT ALL, full or limited -- the gate
   * for capabilities that never needed chrome.debugger in the first place
   * (dom.snapshot/page.read's content-script walk, cookies, tabs.list's
   * `attached` flag, page.act's outer before/after-diff plumbing). */
  isShared(tabId) {
    return this.attached.has(tabId) || this.limited.has(tabId);
  }
  /** `null` for an unshared tab, else which of the two modes it's in --
   * background.ts's tabRefOf() and act.ts's performAction() branch on this. */
  modeOf(tabId) {
    if (this.attached.has(tabId)) return "full";
    if (this.limited.has(tabId)) return "limited";
    return null;
  }
  /** Human-readable reason for a limited tab (tabRefOf()'s
   * attachModeReason); `undefined` for anything else, including a limited
   * tab restored from storage across a worker restart with no reason on
   * hand. */
  limitedReason(tabId) {
    return this.limitedReasons.get(tabId);
  }
  list() {
    return [...this.attached];
  }
  /** Every shared tab, full or limited -- cookies.ts's origin bound (G3.6.3)
   * and releaseAll() use this instead of list() so a limited tab's cookie
   * access and Stop-button coverage aren't silently narrower than a full
   * tab's. */
  sharedList() {
    return [...this.attached, ...this.limited];
  }
  /**
   * `reloadIfBlocked` (protocol/schema.json's `tabs.attach.reload_if_blocked`,
   * default false) is the last-resort step (4) below: reload the tab and try
   * the whole strip/attach/restore sequence once more when every other path
   * has already failed on Chrome's "different extension" refusal. It
   * DISCARDS UNSAVED PAGE STATE (in-progress form input, an unsubmitted
   * draft), so it is never inferred here -- only ever true when the caller
   * (background.ts's handleTabsAttach, from the gateway's own explicit
   * request) passed it through.
   */
  async attach(tabId, reloadIfBlocked = false) {
    if (this.attached.has(tabId)) return { ok: true, result: { tabId, mode: "full" } };
    let url;
    try {
      url = (await chrome.tabs.get(tabId)).url;
    } catch {
      url = void 0;
    }
    const known = attachabilityOf(url);
    if (!known.attachable) {
      return {
        ok: false,
        error: { code: ERROR_CODES.CDP_ERROR, message: describeAttachFailure(url, "") }
      };
    }
    let chromeMessage = "";
    let attached = false;
    let attemptsUsed = 0;
    for (let attempt = 1; attempt <= MAX_FOREIGN_FRAME_ATTACH_ATTEMPTS; attempt++) {
      attemptsUsed = attempt;
      try {
        await chrome.debugger.attach({ tabId }, PROTOCOL_VERSION);
        attached = true;
        break;
      } catch (error) {
        chromeMessage = describeError(error);
        const isLastAttempt = attempt === MAX_FOREIGN_FRAME_ATTACH_ATTEMPTS;
        if (isLastAttempt || !isForeignExtensionRefusal(chromeMessage)) break;
        notifyAttachRetried(tabId, attempt, MAX_FOREIGN_FRAME_ATTACH_ATTEMPTS);
        await sleep$2(FOREIGN_FRAME_RETRY_BACKOFF_MS[attempt - 1]);
      }
    }
    let armedFrameIds = [];
    if (!attached) {
      const isForeign = isForeignExtensionRefusal(chromeMessage);
      if (!isForeign) {
        return { ok: false, error: { code: ERROR_CODES.CDP_ERROR, message: describeAttachFailure(url, chromeMessage) } };
      }
      const frames = await framesOf(tabId);
      armedFrameIds = await this.armGuard(tabId, stripCandidateFrames(frames), frames);
      let outcome = await attemptStripAndAttach(tabId, frames);
      let reloaded = false;
      if (!outcome.attached && reloadIfBlocked) {
        outcome = await attemptReloadStripAndAttach(tabId);
        reloaded = true;
        const postReloadFrames = await framesOf(tabId);
        armedFrameIds = await this.armGuard(tabId, stripCandidateFrames(postReloadFrames), postReloadFrames);
      }
      notifyAttachStripped(tabId, outcome, reloaded, outcome.attached && outcome.sessionSurvived);
      if (outcome.attached && !outcome.sessionSurvived) {
        await disarmGuardAcrossTab(tabId, armedFrameIds);
        return this.attachLimited(tabId, describeSessionDropped(url, outcome.scan));
      }
      if (outcome.attached) {
        attached = true;
      } else {
        await disarmGuardAcrossTab(tabId, armedFrameIds);
        armedFrameIds = [];
        const finalFrames = reloaded ? await framesOf(tabId) : frames;
        const reloadHint = reloadIfBlocked ? " A reload was also tried (reload_if_blocked) and this still failed." : " You may retry with reload_if_blocked=true as a last resort -- warn the user first that this reloads the page and discards any unsaved input (in-progress form text, an unsubmitted draft).";
        const webNavigationConfirmed = foreignExtensionIds(finalFrames.map((frame) => frame.url), chrome.runtime.id);
        const confirmed = webNavigationConfirmed.length > 0 || outcome.scan.count > 0;
        const preloadedHint = confirmed ? "" : preloadedPageHint(finalFrames);
        const message = describeAttachFailure(
          url,
          chromeMessage,
          finalFrames.map((frame) => frame.url),
          chrome.runtime.id,
          unreportedParentCount(finalFrames),
          summariseFrameRefs(finalFrames),
          outcome.scan,
          { attempts: attemptsUsed },
          outcome.diagnostics
        ) + reloadHint + preloadedHint;
        return this.attachLimited(tabId, message);
      }
    }
    if (armedFrameIds.length === 0) {
      const plainSuccessFrames = await framesOf(tabId);
      armedFrameIds = await this.armGuard(tabId, stripCandidateFrames(plainSuccessFrames), plainSuccessFrames);
    }
    try {
      for (const domain of DOMAINS) {
        await chrome.debugger.sendCommand({ tabId }, `${domain}.enable`);
      }
      await enableFocusEmulation(tabId);
    } catch (error) {
      await chrome.debugger.detach({ tabId }).catch(() => {
      });
      await disarmGuardAcrossTab(tabId, armedFrameIds);
      return {
        ok: false,
        error: {
          code: ERROR_CODES.CDP_ERROR,
          message: `attach(${tabId}) domain enable failed: ${describeError(error)}`
        }
      };
    }
    if (await pausedNow()) {
      await chrome.debugger.detach({ tabId }).catch(() => {
      });
      await disarmGuardAcrossTab(tabId, armedFrameIds);
      return {
        ok: false,
        error: { code: ERROR_CODES.SHARING_PAUSED, message: `sharing was paused while tab ${tabId} was being attached` }
      };
    }
    this.attached.add(tabId);
    this.guardedFrames.set(tabId, armedFrameIds);
    await this.persistDesired();
    if (this.limited.delete(tabId)) {
      this.limitedReasons.delete(tabId);
      await this.persistLimited();
      notifyAttachModeChanged(tabId, "full", "chrome.debugger attach succeeded again");
    }
    await onTabAttached(tabId);
    for (const listener of this.attachListeners) listener(tabId);
    return { ok: true, result: { tabId, mode: "full" } };
  }
  /**
   * Attach every tab in a group. Chrome puts up one "being debugged" banner
   * per tab no matter how we batch the calls — that's the product's intended
   * visibility, not a bug to route around (CLAUDE.md gotchas). All we coalesce
   * is the bookkeeping: one persisted-state write, one partial-failure report,
   * instead of the caller looping tabs.attach itself.
   */
  async attachGroup(groupId, reloadIfBlocked = false) {
    const tabs = await chrome.tabs.query({ groupId });
    const targets = tabs.filter((tab) => typeof tab.id === "number");
    if (targets.length === 0) {
      return { ok: false, error: { code: ERROR_CODES.CDP_ERROR, message: `group ${groupId} has no tabs` } };
    }
    const attempts = await Promise.all(
      targets.map(async (tab) => ({ tabId: tab.id, result: await this.attach(tab.id, reloadIfBlocked) }))
    );
    const tabIds = attempts.filter((a) => a.result.ok).map((a) => a.tabId);
    const failed = attempts.filter((a) => !a.result.ok).map((a) => ({ tabId: a.tabId, message: a.result.error.message }));
    if (tabIds.length === 0) {
      return {
        ok: false,
        error: { code: ERROR_CODES.CDP_ERROR, message: `attach failed for all ${failed.length} tab(s) in group ${groupId}` }
      };
    }
    return { ok: true, result: { tabIds, failed } };
  }
  /** Idempotent: releasing a tab that isn't shared (full or limited) is a
   * no-op, not an error. */
  async release(tabId) {
    const wasAttached = this.attached.delete(tabId);
    const wasLimited = this.limited.delete(tabId);
    this.limitedReasons.delete(tabId);
    this.lastUpgradeAttempt.delete(tabId);
    this.upgradeInFlight.delete(tabId);
    await this.persistDesired();
    await this.persistLimited();
    if (!wasAttached && !wasLimited) return false;
    if (wasAttached) {
      try {
        await chrome.debugger.detach({ tabId });
      } catch {
      }
    }
    await this.disarmGuard(tabId);
    this.notifyReleased(tabId);
    await onTabReleased(tabId);
    return true;
  }
  /** Releases every tab this extension has attached OR limited-shared —
   * including CDP ones Chrome still holds that this worker has not
   * re-learned yet (a cold start's resync still running, or not yet run):
   * waits for an in-flight resync, then adds every attached debugger target
   * to the set before releasing. */
  async releaseAll() {
    await this.resyncInFlight?.catch(() => {
    });
    for (const tabId of await this.liveAttachedTabIds()) this.attached.add(tabId);
    const ids = this.sharedList();
    await Promise.allSettled(ids.map((id) => this.release(id)));
    return ids.length;
  }
  async liveAttachedTabIds() {
    try {
      const live = await chrome.debugger.getTargets();
      return live.filter((t) => t.attached && typeof t.tabId === "number").map((t) => t.tabId);
    } catch {
      return [];
    }
  }
  async send(tabId, method, params) {
    if (!this.attached.has(tabId)) {
      return { ok: false, error: { code: ERROR_CODES.TARGET_NOT_ATTACHED, message: `tab ${tabId} is not attached` } };
    }
    try {
      const result = await chrome.debugger.sendCommand({ tabId }, method, params);
      return { ok: true, result: result ?? {} };
    } catch (error) {
      return {
        ok: false,
        error: { code: ERROR_CODES.CDP_ERROR, message: `${method}(${tabId}) failed: ${describeError(error)}` }
      };
    }
  }
  /**
   * Reconcile desired state (persisted across worker restarts) against what
   * Chrome actually has attached right now. Called on every worker wake and
   * whenever the offscreen document's WebSocket comes back up, so a dead
   * worker or a dropped/reconnected socket never leaves the gateway's view of
   * "attached tabs" stale.
   */
  async resyncAttachments() {
    const run = this.runResync();
    this.resyncInFlight = run;
    try {
      return await run;
    } finally {
      if (this.resyncInFlight === run) this.resyncInFlight = null;
    }
  }
  async runResync() {
    const stored = await chrome.storage.session.get([DESIRED_ATTACH_KEY, LIMITED_ATTACH_KEY]);
    const desired = new Set(stored[DESIRED_ATTACH_KEY] ?? []);
    const desiredLimited = new Set(stored[LIMITED_ATTACH_KEY] ?? []);
    for (const tabId of desiredLimited) {
      try {
        await chrome.tabs.get(tabId);
        this.limited.add(tabId);
      } catch {
      }
    }
    let live = [];
    try {
      live = await chrome.debugger.getTargets();
    } catch {
      live = [];
    }
    const adopted = [];
    for (const target of live) {
      if (target.attached && typeof target.tabId === "number") {
        if (!this.attached.has(target.tabId)) adopted.push(target.tabId);
        this.attached.add(target.tabId);
        desired.add(target.tabId);
      }
    }
    for (const tabId of adopted) await enableFocusEmulation(tabId);
    if (await pausedNow()) {
      for (const tabId of this.sharedList()) await this.release(tabId);
      desired.clear();
      desiredLimited.clear();
      adopted.length = 0;
    }
    for (const tabId of adopted) for (const listener of this.attachListeners) listener(tabId);
    for (const tabId of desired) {
      if (this.attached.has(tabId)) continue;
      const result = await this.attach(tabId);
      if (!result.ok || result.result.mode !== "full") desired.delete(tabId);
    }
    await chrome.storage.session.set({ [DESIRED_ATTACH_KEY]: [...desired] });
    await this.persistLimited();
    reconcileAttachedTabs(this.sharedList());
    return { attachedTabIds: this.sharedList() };
  }
  async persistDesired() {
    await chrome.storage.session.set({ [DESIRED_ATTACH_KEY]: this.list() });
  }
  /** Limited-mode share fix counterpart of persistDesired() -- see
   * LIMITED_ATTACH_KEY's own comment for why this is a separate key. */
  async persistLimited() {
    await chrome.storage.session.set({ [LIMITED_ATTACH_KEY]: [...this.limited] });
  }
  /**
   * Limited-mode share fix (spec item 1): falls a tab back to a no-debugger
   * "limited" share instead of failing it outright, when attach() has
   * already exhausted every CDP-only path (retry, strip-and-attach, the
   * opt-in reload) and confirmed WHY: another extension's frame is present
   * somewhere in this tab's WebContents. Never called speculatively -- only
   * from the two attach() call sites that already know this for certain
   * (the confirmed foreign-frame refusal, and the "attached then Chrome
   * dropped it restoring the frame" case) -- an unconfirmed CDP_ERROR is
   * passed through as a real failure instead, same as before this fix.
   */
  async attachLimited(tabId, reason) {
    if (await pausedNow()) {
      return { ok: false, error: { code: ERROR_CODES.SHARING_PAUSED, message: `sharing was paused while tab ${tabId} was being attached` } };
    }
    const wasAlreadyLimited = this.limited.has(tabId);
    this.limited.add(tabId);
    this.limitedReasons.set(tabId, reason);
    this.autoReattachedTabs.delete(tabId);
    await this.persistLimited();
    if (!wasAlreadyLimited) {
      notifyAttachModeChanged(tabId, "limited", reason);
      await onTabAttached(tabId);
      for (const listener of this.attachListeners) listener(tabId);
    }
    return { ok: true, result: { tabId, mode: "limited", reason } };
  }
  /**
   * Opportunistic upgrade (spec item 4): quietly retries the real
   * chrome.debugger.attach() for a limited tab, at most once per
   * UPGRADE_RETRY_INTERVAL_MS (never a hot loop, never awaited by the
   * action that triggered it -- see act.ts's call site). A no-op for a tab
   * that isn't currently limited, or one already mid-upgrade-attempt.
   * Success (attach() itself reports mode "full") lifts the tab out of the
   * limited set and re-arms the CDP guard exactly like any other successful
   * attach; failure just leaves the tab limited for the next call to retry.
   */
  maybeUpgrade(tabId) {
    if (!this.limited.has(tabId)) return;
    if (this.upgradeInFlight.has(tabId)) return;
    const last = this.lastUpgradeAttempt.get(tabId) ?? 0;
    if (Date.now() - last < UPGRADE_RETRY_INTERVAL_MS) return;
    this.lastUpgradeAttempt.set(tabId, Date.now());
    this.upgradeInFlight.add(tabId);
    void (async () => {
      try {
        await this.attach(tabId);
      } catch {
      } finally {
        this.upgradeInFlight.delete(tabId);
      }
    })();
  }
}
const cdp = new CdpManager();
function cdpOnlyGate(tabId, capability) {
  if (cdp.isAttached(tabId)) return null;
  if (cdp.isLimited(tabId)) {
    const { code, message } = formatRefusal("limited_mode_capability_unavailable", { capability });
    return { code: code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, message };
  }
  return { code: ERROR_CODES.TARGET_NOT_ATTACHED, message: `tab ${tabId} is not attached` };
}
const FOCUS_STEAL_WINDOW_MS = 1500;
const lastActionByTab = /* @__PURE__ */ new Map();
function noteAction(tabId, kind) {
  lastActionByTab.set(tabId, { kind, at: Date.now() });
}
function notifyFocusStolen(tabId, kind, msSinceAction, via) {
  void chrome.runtime.sendMessage({ target: "offscreen", type: "focus.stolen", tabId, actionKind: kind, msSinceAction, via }).catch(() => {
  });
}
function checkTab(tabId, via) {
  if (!cdp.isShared(tabId)) return;
  const last = lastActionByTab.get(tabId);
  if (!last) return;
  const elapsed = Date.now() - last.at;
  if (elapsed <= FOCUS_STEAL_WINDOW_MS) notifyFocusStolen(tabId, last.kind, elapsed, via);
}
chrome.tabs?.onActivated?.addListener(({ tabId }) => checkTab(tabId, "tab"));
if (chrome.windows?.onFocusChanged?.addListener) {
  chrome.windows.onFocusChanged.addListener((windowId) => {
    if (windowId === chrome.windows.WINDOW_ID_NONE) return;
    void chrome.tabs.query({ windowId, active: true }).then((tabs) => {
      for (const tab of tabs) if (typeof tab.id === "number") checkTab(tab.id, "window");
    }).catch(() => {
    });
  });
}
function codeForCancelReason(reason) {
  return reason === "pause" || reason === "stop" ? ERROR_CODES.SHARING_PAUSED : ERROR_CODES.TARGET_NOT_ATTACHED;
}
const ops = /* @__PURE__ */ new Map();
const opsByTab$1 = /* @__PURE__ */ new Map();
let opCounter$1 = 0;
function registerCancellable(tabId, kind, onCancel) {
  opCounter$1 += 1;
  const opId = `${kind}-${opCounter$1}`;
  const entry = { tabId, kind, cancelled: false, reason: null, onCancel };
  ops.set(opId, entry);
  let set = opsByTab$1.get(tabId);
  if (!set) opsByTab$1.set(tabId, set = /* @__PURE__ */ new Set());
  set.add(opId);
  return {
    opId,
    tabId,
    isCancelled: () => entry.cancelled,
    cancelReason: () => entry.reason,
    unregister: () => {
      ops.delete(opId);
      const forTab = opsByTab$1.get(tabId);
      if (forTab) {
        forTab.delete(opId);
        if (forTab.size === 0) opsByTab$1.delete(tabId);
      }
    }
  };
}
async function fireCancel(entry, reason) {
  if (entry.cancelled) return;
  entry.cancelled = true;
  entry.reason = reason;
  try {
    await entry.onCancel(reason);
  } catch {
  }
}
async function cancelOpsForTab(tabId, reason) {
  const set = opsByTab$1.get(tabId);
  if (!set || set.size === 0) return 0;
  let count = 0;
  for (const opId of [...set]) {
    const entry = ops.get(opId);
    if (!entry || entry.cancelled) continue;
    await fireCancel(entry, reason);
    count += 1;
  }
  return count;
}
async function cancelAllOps(reason) {
  let count = 0;
  for (const tabId of [...opsByTab$1.keys()]) count += await cancelOpsForTab(tabId, reason);
  return count;
}
cdp.onRelease((tabId) => {
  void cancelOpsForTab(tabId, "tab-released");
});
const SETTINGS_STORAGE_KEY = "settings";
if (typeof chrome !== "undefined" && chrome.storage?.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    const change = changes[SETTINGS_STORAGE_KEY];
    if (!change) return;
    const before = change.oldValue?.paused === true;
    const after = change.newValue;
    if (before || after?.paused !== true) return;
    const reason = after?.pauseReason === "stop-button" ? "stop" : "pause";
    void cancelAllOps(reason);
  });
}
const IN_FLIGHT_KEY = "cancel.inFlight";
async function readMarkers() {
  try {
    const stored = await chrome.storage.session.get(IN_FLIGHT_KEY);
    return stored[IN_FLIGHT_KEY] ?? [];
  } catch {
    return [];
  }
}
async function writeMarkers(markers) {
  try {
    await chrome.storage.session.set({ [IN_FLIGHT_KEY]: markers });
  } catch {
  }
}
async function markInFlight(opId, tabId, kind, detail) {
  const markers = await readMarkers();
  markers.push({ opId, tabId, kind, at: Date.now(), ...detail ? { detail } : {} });
  await writeMarkers(markers);
}
async function clearInFlight(opId) {
  const markers = await readMarkers();
  const next = markers.filter((m) => m.opId !== opId);
  if (next.length !== markers.length) await writeMarkers(next);
}
async function recoverFromPreviousLife() {
  const markers = await readMarkers();
  if (markers.length === 0) return { found: 0, recovered: 0 };
  let recovered = 0;
  for (const marker of markers) {
    if (cdp.isAttached(marker.tabId) && marker.kind === "drag") {
      const x = marker.detail?.x ?? 0;
      const y = marker.detail?.y ?? 0;
      await cdp.send(marker.tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }).catch(() => {
      });
      recovered += 1;
    }
  }
  await writeMarkers([]);
  return { found: markers.length, recovered };
}
const injectedFrames = /* @__PURE__ */ new Map();
function isKnownInjected(tabId, frameId) {
  return injectedFrames.get(tabId)?.has(frameId) ?? false;
}
function markInjected(tabId, frameId) {
  let frames = injectedFrames.get(tabId);
  if (!frames) {
    frames = /* @__PURE__ */ new Set();
    injectedFrames.set(tabId, frames);
  }
  frames.add(frameId);
}
function forgetFrame(tabId, frameId) {
  injectedFrames.get(tabId)?.delete(frameId);
}
function forgetTab(tabId) {
  injectedFrames.delete(tabId);
}
function forgetTabInjectionCache(tabId) {
  forgetTab(tabId);
}
if (typeof chrome !== "undefined" && chrome.webNavigation?.onCommitted?.addListener) {
  chrome.webNavigation.onCommitted.addListener((details) => forgetFrame(details.tabId, details.frameId));
}
if (typeof chrome !== "undefined" && chrome.tabs?.onRemoved?.addListener) {
  chrome.tabs.onRemoved.addListener((tabId) => forgetTab(tabId));
}
cdp.onRelease((tabId) => forgetTab(tabId));
const CONTENT_SCRIPT_ERROR_CODE = ERROR_CODES.CONTENT_SCRIPT_ERROR;
async function contentRequest(tabId, message, frameId = 0) {
  if (!isKnownInjected(tabId, frameId)) {
    try {
      await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["content.js"] });
      markInjected(tabId, frameId);
    } catch (error) {
      return {
        ok: false,
        code: CONTENT_SCRIPT_ERROR_CODE,
        error: `content script could not be injected on tab ${tabId} frame ${frameId} (chrome://, Web Store and PDF viewer pages refuse this, and a frame that navigated away between discovery and injection reports the same way): ${describeError(error)}`
      };
    }
  }
  let response;
  try {
    response = await chrome.tabs.sendMessage(tabId, message, { frameId });
  } catch (error) {
    forgetFrame(tabId, frameId);
    return {
      ok: false,
      code: CONTENT_SCRIPT_ERROR_CODE,
      error: `content script did not respond on tab ${tabId} frame ${frameId}: ${describeError(error)}`
    };
  }
  if (!response || response.ok !== true) {
    return {
      ok: false,
      code: CONTENT_SCRIPT_ERROR_CODE,
      error: response?.error ?? `content script returned no data on tab ${tabId} frame ${frameId}`
    };
  }
  return { ok: true, data: response.data };
}
async function contentRequestWithTimeout(tabId, message, timeoutMs, frameId = 0) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      resolve({
        ok: false,
        code: ERROR_CODES.TIMEOUT,
        error: `no response from content script/user on tab ${tabId} within ${timeoutMs}ms`
      });
    }, Math.max(0, timeoutMs));
  });
  try {
    return await Promise.race([contentRequest(tabId, message, frameId), timeout]);
  } finally {
    clearTimeout(timer);
  }
}
const PIERCE_SEPARATOR = ">>>";
function splitTopLevel(text, hop) {
  const parts = [];
  let current = "";
  let quote = null;
  let bracketDepth = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (quote) {
      if (ch === "\\" && i + 1 < text.length) {
        current += ch + text[i + 1];
        i += 2;
        continue;
      }
      current += ch;
      if (ch === quote) quote = null;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      i += 1;
      continue;
    }
    if (ch === "[") {
      bracketDepth += 1;
      current += ch;
      i += 1;
      continue;
    }
    if (ch === "]") {
      if (bracketDepth > 0) bracketDepth -= 1;
      current += ch;
      i += 1;
      continue;
    }
    if (bracketDepth === 0 && text.startsWith(hop, i)) {
      parts.push(current);
      current = "";
      i += hop.length;
      continue;
    }
    current += ch;
    i += 1;
  }
  if (quote) return { ok: false, error: `unterminated ${quote === '"' ? "double" : "single"}-quoted string in "${text}"` };
  if (bracketDepth > 0) return { ok: false, error: `unterminated '[' attribute selector in "${text}"` };
  parts.push(current);
  return { ok: true, parts };
}
function splitPierceSelector(selector) {
  const split = splitTopLevel(selector, PIERCE_SEPARATOR);
  if (!split.ok) return split;
  return { ok: true, parts: split.parts.map((part) => part.trim()) };
}
function describeChoice(selector, matchCount, chosenReason) {
  if (matchCount <= 1) return void 0;
  const which = chosenReason === "visible-in-viewport" ? "the first one that is visible in the viewport" : chosenReason === "first-visible" ? "the first visible one (none were in the viewport)" : "the first one in document order";
  return `selector "${selector}" matched ${matchCount} elements; used ${which}`;
}
const FRAME_HOP = "|>>";
function isBlank(segment) {
  return segment.trim() === "";
}
function parseFrameSelector(selector) {
  if (isBlank(selector)) return { ok: false, error: "empty selector" };
  const split = splitTopLevel(selector, FRAME_HOP);
  if (!split.ok) return { ok: false, error: split.error };
  const segments = split.parts;
  if (segments.length === 1) {
    const pierceError = validatePierceSegment(segments[0]);
    if (pierceError) return { ok: false, error: pierceError };
    return { ok: true, path: { segments } };
  }
  if (segments[0] === "" || segments[segments.length - 1] === "") {
    return { ok: false, error: `frame selector cannot start or end with '${FRAME_HOP}'` };
  }
  for (let i = 0; i < segments.length; i++) {
    if (segments[i] === "") {
      return { ok: false, error: `empty frame segment between '${FRAME_HOP}' hops` };
    }
    const pierceError = validatePierceSegment(segments[i]);
    if (pierceError) return { ok: false, error: `frame segment ${i}: ${pierceError}` };
  }
  return { ok: true, path: { segments } };
}
function validatePierceSegment(segment) {
  const split = splitPierceSelector(segment);
  if (!split.ok) return split.error;
  const emptyIndex = split.parts.findIndex((part) => part === "");
  if (emptyIndex === -1) return void 0;
  return `empty selector part in "${segment}"`;
}
function formatFrameSelector(path) {
  return path.segments.join(FRAME_HOP);
}
function isFrameQualified(selector) {
  const result = parseFrameSelector(selector);
  return result.ok && result.path.segments.length > 1;
}
function canonicalizeOrigin(origin) {
  if (origin === "null") return origin;
  try {
    const u = new URL(origin);
    const host = u.hostname.replace(/\.+$/, "");
    const port = u.port ? `:${u.port}` : "";
    return `${u.protocol}//${host}${port}`;
  } catch {
    return origin;
  }
}
function isOriginAllowed(origin, policy) {
  if (!origin || origin === "null") return false;
  const canonical = canonicalizeOrigin(origin);
  const denied = policy.denied.map(canonicalizeOrigin);
  const granted = policy.granted.map(canonicalizeOrigin);
  if (denied.includes(canonical)) return false;
  if (granted.includes(canonical)) return true;
  return policy.defaultFull;
}
class IndexAssigner {
  selectorToIdx = /* @__PURE__ */ new Map();
  next;
  newIndexMap = {};
  newIndexMeta = {};
  constructor(existingIndexMap, nextIdx) {
    let maxIdx = 0;
    for (const [rawIdx, selector] of Object.entries(existingIndexMap ?? {})) {
      const idx = Number(rawIdx);
      if (!Number.isFinite(idx)) continue;
      this.selectorToIdx.set(selector, idx);
      if (idx > maxIdx) maxIdx = idx;
    }
    this.next = typeof nextIdx === "number" && nextIdx > maxIdx ? Math.floor(nextIdx) : maxIdx + 1;
  }
  /** The next idx this assigner would mint for a genuinely new selector —
   * exposed so a caller chaining several assigners (frames.ts's one shared
   * instance across every frame of one `aggregateSnapshot` call) can report
   * how far the counter advanced, or so a caller minting idx OUTSIDE this
   * assigner (none today) could stay clear of it. */
  get nextIdx() {
    return this.next;
  }
  assign(selector, meta) {
    const existing = this.selectorToIdx.get(selector);
    if (existing !== void 0) return { idx: existing, isNew: false };
    const idx = this.next++;
    this.selectorToIdx.set(selector, idx);
    this.newIndexMap[idx] = selector;
    this.newIndexMeta[idx] = meta;
    return { idx, isNew: true };
  }
}
const FRAME_MARKER_START = "\0FRAME-START\0";
const FRAME_MARKER_END = "\0FRAME-END\0";
const frameTreeEpoch = /* @__PURE__ */ new Map();
function bumpEpoch(tabId) {
  frameTreeEpoch.set(tabId, (frameTreeEpoch.get(tabId) ?? 0) + 1);
}
function frameEpoch(tabId) {
  return frameTreeEpoch.get(tabId) ?? 0;
}
if (typeof chrome !== "undefined" && chrome.webNavigation) {
  chrome.webNavigation.onCommitted.addListener((details) => bumpEpoch(details.tabId));
  chrome.webNavigation.onBeforeNavigate.addListener((details) => bumpEpoch(details.tabId));
}
if (typeof chrome !== "undefined" && chrome.tabs?.onRemoved?.addListener) {
  chrome.tabs.onRemoved.addListener((tabId) => frameTreeCache.delete(tabId));
}
const frameTreeCache = /* @__PURE__ */ new Map();
async function getFrameTree(tabId) {
  const epoch = frameEpoch(tabId);
  const cached = frameTreeCache.get(tabId);
  if (cached && cached.epoch === epoch) {
    return cached.frames;
  }
  const frames = await chrome.webNavigation.getAllFrames({ tabId });
  const mapped = (frames ?? []).map((f) => ({ frameId: f.frameId, parentFrameId: f.parentFrameId, url: f.url }));
  frameTreeCache.set(tabId, { epoch, frames: mapped });
  return mapped;
}
function resolveAbsolute(base, ref) {
  try {
    return new URL(ref, base).href;
  } catch {
    return void 0;
  }
}
function frameOriginOf(url, parentOrigin) {
  if (url === "about:blank" || url === "about:srcdoc" || url === "") return parentOrigin;
  try {
    const u = new URL(url);
    if (u.protocol === "http:" || u.protocol === "https:") return canonicalizeOrigin(u.origin);
    return "null";
  } catch {
    return "null";
  }
}
function matchChildFrame(frames, parentFrameId, parentUrl, boundary) {
  const children = frames.filter((f) => f.parentFrameId === parentFrameId);
  if (children.length === 0) {
    return { ok: false, reason: "chrome.webNavigation.getAllFrames reports no child frame under this parent" };
  }
  if (!boundary.src) {
    return { ok: false, reason: "iframe has no src attribute (srcdoc/about:blank) — cannot be matched to a Chrome frameId by URL" };
  }
  const resolved = resolveAbsolute(parentUrl, boundary.src);
  if (!resolved) {
    return { ok: false, reason: `iframe src "${boundary.src}" did not resolve to an absolute URL` };
  }
  const matches = children.filter((c) => c.url === resolved);
  if (matches.length === 0) {
    return { ok: false, reason: `no child frame with url ${resolved} among ${children.length} candidate(s) under this parent` };
  }
  if (matches.length > 1) {
    return { ok: false, reason: `ambiguous: ${matches.length} child frames share url ${resolved}` };
  }
  return { ok: true, frameId: matches[0].frameId };
}
const TOP_SHARE = 0.6;
const FRAME_BUDGET_FLOOR_BYTES = 256;
const MAX_FRAME_DEPTH = 8;
const FRAME_ANCHOR_TIMEOUT_MS = 1e4;
function splitFrameBudget(remaining, areas) {
  const n = areas.length;
  if (n === 0) return [];
  const flooredTotal = FRAME_BUDGET_FLOOR_BYTES * n;
  if (flooredTotal >= remaining) {
    const equal = Math.max(1, Math.floor(remaining / n));
    return areas.map(() => equal);
  }
  const totalArea = areas.reduce((a, b) => a + b, 0) || n;
  const extra = remaining - flooredTotal;
  return areas.map((a) => FRAME_BUDGET_FLOOR_BYTES + Math.floor(extra * ((a || 1) / totalArea)));
}
const IDX_LINE = /^(\s*)\[(\d+)\]/;
function newAccumulator() {
  return { treeLines: [], indexMap: {}, indexMeta: {}, truncated: false, redactions: 0, findMatches: [] };
}
function mergeIndex(raw, chain, assigner, merge) {
  const indexMap = raw.indexMap ?? {};
  const indexMeta = raw.indexMeta ?? {};
  const localIdx = Object.keys(indexMap).map(Number).sort((a, b) => a - b);
  const remap = /* @__PURE__ */ new Map();
  for (const oldIdx of localIdx) {
    const local = indexMap[oldIdx];
    const qualified = chain.length === 0 ? local : formatFrameSelector({ segments: [...chain, local] });
    const meta = indexMeta[oldIdx] ?? { role: "", name: "" };
    const { idx: newIdx } = assigner.assign(qualified, meta);
    remap.set(oldIdx, newIdx);
    merge.indexMap[newIdx] = qualified;
    merge.indexMeta[newIdx] = meta;
  }
  merge.truncated = merge.truncated || raw.truncated === true;
  merge.redactions += raw.redactions ?? 0;
  return remap;
}
function renumberTree(tree, remap) {
  if (!tree || tree.length === 0) return [];
  return tree.split("\n").map((line) => {
    const m = IDX_LINE.exec(line);
    if (!m) return line;
    const mapped = remap.get(Number(m[2]));
    return mapped === void 0 ? line : `${m[1]}[${mapped}]${line.slice(m[0].length)}`;
  });
}
function snapshotMessage(budgetBytes, extras, selector) {
  return {
    target: "content",
    type: "snapshot",
    budgetBytes,
    ...selector ? { selector } : {},
    grantedOrigins: extras.grantedOrigins,
    deniedOrigins: extras.deniedOrigins,
    defaultFull: extras.defaultFull,
    ...extras.find ? { find: extras.find } : {},
    ...extras.dialogOnly ? { dialogOnly: true } : {},
    ...extras.viewportOnly ? { viewportOnly: true } : {},
    ...extras.interactiveOnly ? { interactiveOnly: true } : {}
  };
}
async function mergeFrame(tabId, frameId, chain, frameOrigin, budgetBytes, raw, assigner, depth, policy, requestExtras, merge, gaps, frameOrigins) {
  const prefix = chain.join(FRAME_HOP);
  frameOrigins[prefix] = frameOrigin;
  const boundaries = raw.frameBoundaries ?? [];
  let ownRaw = raw;
  let mappedChildren = boundaries.map(() => void 0);
  let childBudgets = boundaries.map(() => 0);
  if (boundaries.length > 0 && depth < MAX_FRAME_DEPTH) {
    const frames = await getFrameTree(tabId);
    const mappedAreas = [];
    const mappedIndexes = [];
    boundaries.forEach((b, i) => {
      if (b.blocked) {
        gaps.push({ selector: b.selector, name: b.name, reason: "not_granted", origin: b.origin });
        return;
      }
      const m = matchChildFrame(frames, frameId, raw.url, b);
      if (!m.ok) {
        gaps.push({ selector: b.selector, name: b.name, reason: m.reason });
        return;
      }
      const childUrl = frames.find((f) => f.frameId === m.frameId)?.url ?? "";
      const childOrigin = frameOriginOf(childUrl, frameOrigin);
      if (!isOriginAllowed(childOrigin, policy)) {
        gaps.push({ selector: b.selector, name: b.name, reason: "not_granted", origin: childOrigin });
        return;
      }
      mappedChildren[i] = { frameId: m.frameId, origin: childOrigin };
      mappedAreas.push(b.area);
      mappedIndexes.push(i);
    });
    if (mappedAreas.length > 0) {
      const ownBudget = Math.max(1, Math.floor(budgetBytes * TOP_SHARE));
      const remaining = budgetBytes - ownBudget;
      const perMapped = splitFrameBudget(remaining, mappedAreas);
      mappedIndexes.forEach((boundaryIdx, i) => {
        childBudgets[boundaryIdx] = perMapped[i];
      });
      const refetch = await contentRequest(tabId, snapshotMessage(ownBudget, requestExtras), frameId);
      if (refetch.ok && refetch.data) ownRaw = refetch.data;
    }
  }
  const remap = mergeIndex(ownRaw, chain, assigner, merge);
  for (const m of ownRaw.findMatches ?? []) {
    const newIdx = remap.get(m.idx);
    if (newIdx !== void 0) merge.findMatches.push({ ...m, idx: newIdx });
  }
  const renumbered = renumberTree(ownRaw.tree, remap);
  let boundaryCursor = 0;
  const fetched = /* @__PURE__ */ new Set();
  const out = [];
  for (const line of renumbered) {
    out.push(line);
    if (/iframe "/.test(line)) {
      const i = boundaryCursor++;
      const child = mappedChildren[i];
      const boundary = boundaries[i];
      if (child && boundary) {
        fetched.add(i);
        await fetchChild(tabId, child.frameId, [...chain, boundary.selector], child.origin, childBudgets[i], assigner, depth + 1, policy, requestExtras, out, merge, gaps, frameOrigins);
      }
    }
  }
  for (let i = 0; i < mappedChildren.length; i++) {
    if (fetched.has(i)) continue;
    const child = mappedChildren[i];
    const boundary = boundaries[i];
    if (!child || !boundary) continue;
    await fetchChild(tabId, child.frameId, [...chain, boundary.selector], child.origin, childBudgets[i], assigner, depth + 1, policy, requestExtras, out, merge, gaps, frameOrigins);
  }
  merge.treeLines.push(...out);
}
async function fetchChild(tabId, frameId, chain, frameOrigin, budgetBytes, assigner, depth, policy, requestExtras, parentLines, merge, gaps, frameOrigins) {
  const childSelector = chain[chain.length - 1] ?? "(unknown frame)";
  const result = await contentRequest(tabId, snapshotMessage(budgetBytes, requestExtras), frameId);
  if (!result.ok || !result.data) {
    gaps.push({ selector: childSelector, name: childSelector, reason: result.error ?? "snapshot request failed" });
    return;
  }
  const childMerge = newAccumulator();
  await mergeFrame(tabId, frameId, chain, frameOrigin, budgetBytes, result.data, assigner, depth, policy, requestExtras, childMerge, gaps, frameOrigins);
  for (const key of Object.keys(childMerge.indexMap)) merge.indexMap[Number(key)] = childMerge.indexMap[Number(key)];
  for (const key of Object.keys(childMerge.indexMeta)) merge.indexMeta[Number(key)] = childMerge.indexMeta[Number(key)];
  merge.truncated = merge.truncated || childMerge.truncated;
  merge.redactions += childMerge.redactions;
  merge.findMatches.push(...childMerge.findMatches);
  parentLines.push(`  ${FRAME_MARKER_START}${frameOrigin}\0${chain.join(FRAME_HOP)}`);
  for (const line of childMerge.treeLines) parentLines.push(`  ${line}`);
  parentLines.push(`  ${FRAME_MARKER_END}`);
}
async function aggregateSnapshot(tabId, budgetBytes, selector, policy = { granted: [], denied: [], defaultFull: false }, extra) {
  const requestExtras = {
    grantedOrigins: policy.granted,
    deniedOrigins: policy.denied,
    defaultFull: policy.defaultFull,
    find: extra?.find,
    dialogOnly: extra?.dialogOnly,
    viewportOnly: extra?.viewportOnly,
    interactiveOnly: extra?.interactiveOnly
  };
  const top = await contentRequest(tabId, snapshotMessage(budgetBytes, requestExtras, selector), 0);
  if (!top.ok || !top.data) {
    return { ok: false, code: top.code ?? CONTENT_SCRIPT_ERROR_CODE, error: top.error ?? `content script returned no data on tab ${tabId}` };
  }
  const assigner = new IndexAssigner(extra?.existingIndexMap, extra?.startIndex && extra.startIndex > 0 ? extra.startIndex : void 0);
  const gaps = [];
  const merge = newAccumulator();
  const frameOrigins = {};
  const topOrigin = frameOriginOf(top.data.url ?? "", "null");
  await mergeFrame(tabId, 0, [], topOrigin, budgetBytes, top.data, assigner, 0, policy, requestExtras, merge, gaps, frameOrigins);
  const { frameBoundaries: _ignored, indexMap: _im, indexMeta: _imeta, tree: _tree, truncated: _tr, redactions: _red, findMatches: _fm, ...pageMeta } = top.data;
  return {
    ok: true,
    data: {
      tree: merge.treeLines.join("\n"),
      truncated: merge.truncated,
      ...extra?.find ? { findMatches: merge.findMatches } : {},
      redactions: merge.redactions,
      indexMap: merge.indexMap,
      indexMeta: merge.indexMeta,
      frameGaps: gaps,
      frameOrigins,
      pageMeta
    }
  };
}
async function resolveFrameTarget(tabId, selector) {
  const parsed = parseFrameSelector(selector);
  if (!parsed.ok) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: `invalid selector ${selector}: ${parsed.error}` };
  }
  const { segments } = parsed.path;
  if (segments.length === 1) {
    return { ok: true, result: { frameId: 0, localSelector: segments[0], offsetX: 0, offsetY: 0, frameOrigin: "null" } };
  }
  if (segments.length - 1 > MAX_FRAME_DEPTH) {
    const { code, message } = formatRefusal("frame_target_unresolved", { selector });
    return { ok: false, code, error: message };
  }
  let currentFrameId = 0;
  let offsetX = 0;
  let offsetY = 0;
  const tab = await chrome.tabs.get(tabId).catch(() => void 0);
  let parentOrigin = tab?.url ? frameOriginOf(tab.url, "null") : "null";
  let frameOrigin = parentOrigin;
  for (let i = 0; i < segments.length - 1; i++) {
    const anchorSelector = segments[i];
    const anchor = await contentRequestWithTimeout(
      tabId,
      { target: "content", type: "resolveFrameAnchor", selector: anchorSelector, scroll: false },
      FRAME_ANCHOR_TIMEOUT_MS,
      currentFrameId
    );
    if (!anchor.ok || !anchor.data) {
      const { code, message } = formatRefusal("frame_action_unsupported", { selector });
      return { ok: false, code, error: message };
    }
    const data = anchor.data;
    if (!data.found || data.notAFrame || !data.contentBoxOffset) {
      const { code, message } = formatRefusal("frame_target_unresolved", { selector });
      return { ok: false, code, error: message };
    }
    offsetX += data.contentBoxOffset.x;
    offsetY += data.contentBoxOffset.y;
    const frames = await getFrameTree(tabId);
    const match = matchChildFrame(frames, currentFrameId, data.src ?? "", {
      src: data.src ?? null
    });
    if (!match.ok) {
      const { code, message } = formatRefusal("frame_action_unsupported", { selector });
      return { ok: false, code, error: message };
    }
    const childUrl = frames.find((f) => f.frameId === match.frameId)?.url ?? "";
    frameOrigin = frameOriginOf(childUrl, parentOrigin);
    parentOrigin = frameOrigin;
    currentFrameId = match.frameId;
  }
  return {
    ok: true,
    result: { frameId: currentFrameId, localSelector: segments[segments.length - 1], offsetX, offsetY, frameOrigin }
  };
}
const MAX_GLIDE_MS = 900;
const CURSOR_ARRIVAL_SLACK_MS = 150;
const CURSOR_WAIT_CEILING_MS = MAX_GLIDE_MS + CURSOR_ARRIVAL_SLACK_MS;
const HIDE_ACK_TIMEOUT_MS = 250;
const MARKS_ACK_TIMEOUT_MS = 300;
const CAPTURE_LABELS = {
  "dom.snapshot": "viewing",
  "dom.inspect": "inspecting",
  "page.read": "reading",
  "page.screenshot": "looking",
  annotate: "asking"
};
async function sendPresence(tabId, message) {
  try {
    await contentRequest(tabId, message);
  } catch {
  }
}
const opsByTab = /* @__PURE__ */ new Map();
let opCounter = 0;
function presenceBegin(tabId, label, ttlMs) {
  opCounter += 1;
  const opId = `${Date.now().toString(36)}-${opCounter}`;
  const begun = (async () => {
    try {
      const settings = await getSettings();
      if (!settings.showActivityGlow) return false;
      if (!cdp.isAttached(tabId)) return false;
      let ops2 = opsByTab.get(tabId);
      if (!ops2) opsByTab.set(tabId, ops2 = /* @__PURE__ */ new Set());
      ops2.add(opId);
      await sendPresence(tabId, {
        target: "content",
        type: "presence.begin",
        opId,
        label,
        ...ttlMs !== void 0 ? { ttlMs } : {}
      });
      return true;
    } catch {
      return false;
    }
  })();
  return { tabId, opId, begun };
}
async function presenceEnd(op) {
  try {
    if (!await op.begun) return;
    const ops2 = opsByTab.get(op.tabId);
    if (!ops2 || !ops2.delete(op.opId)) return;
    if (ops2.size === 0) opsByTab.delete(op.tabId);
    await sendPresence(op.tabId, { target: "content", type: "presence.end", opId: op.opId });
  } catch {
  }
}
function withCeiling$1(work, ms, onTimeout) {
  let timer;
  const ceiling = new Promise((resolve) => {
    timer = setTimeout(() => resolve(onTimeout), ms);
  });
  return Promise.race([work, ceiling]).finally(() => clearTimeout(timer));
}
async function effectivePointerAnimationMode(tabId, configured) {
  if (configured !== "normal") return configured;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!tab.active) return "fast";
    const win = await chrome.windows.get(tab.windowId);
    return win.focused ? "normal" : "fast";
  } catch {
    return configured;
  }
}
function presenceCursor(tabId, x, y, label, click) {
  const work = (async () => {
    try {
      const settings = await getSettings();
      if (!settings.showPresenceCursor) return;
      if (!cdp.isAttached(tabId)) return;
      const mode = await effectivePointerAnimationMode(tabId, settings.pointerAnimation);
      await contentRequestWithTimeout(
        tabId,
        { target: "content", type: "presence.cursor", x, y, label, click, mode },
        CURSOR_WAIT_CEILING_MS
      );
    } catch {
    }
  })();
  return withCeiling$1(work, CURSOR_WAIT_CEILING_MS, void 0);
}
function presenceHideForCapture(tabId) {
  const work = (async () => {
    try {
      const result = await contentRequestWithTimeout(
        tabId,
        { target: "content", type: "presence.hideForCapture" },
        HIDE_ACK_TIMEOUT_MS
      );
      return result.ok && result.data?.hidden === true;
    } catch {
      return false;
    }
  })();
  return withCeiling$1(work, HIDE_ACK_TIMEOUT_MS, false);
}
async function presenceRestoreAfterCapture(tabId) {
  await sendPresence(tabId, { target: "content", type: "presence.restoreAfterCapture" });
}
function presenceShowMarks(tabId, marks) {
  const work = (async () => {
    try {
      const result = await contentRequestWithTimeout(
        tabId,
        { target: "content", type: "presence.marks.show", marks },
        MARKS_ACK_TIMEOUT_MS
      );
      return result.ok;
    } catch {
      return false;
    }
  })();
  return withCeiling$1(work, MARKS_ACK_TIMEOUT_MS, false);
}
async function presenceHideMarks(tabId) {
  await sendPresence(tabId, { target: "content", type: "presence.marks.hide" });
}
async function handleStopPressed(now = Date.now) {
  await setSettings({ paused: true, pauseReason: "stop-button", pausedAt: now() });
  await send({ target: "offscreen", type: "stoppedFromPage" });
  const released = await cdp.releaseAll();
  return { released };
}
const STOP_COMMAND = "stop-hermes";
async function stopShortcut() {
  try {
    const commands = await chrome.commands.getAll();
    return commands.find((c) => c.name === STOP_COMMAND)?.shortcut ?? "";
  } catch {
    return "";
  }
}
async function presenceAttached(tabId) {
  try {
    const settings = await getSettings();
    if (!settings.showActivityGlow && !settings.showPresenceCursor) return;
    const shortcut = await stopShortcut();
    if (!cdp.isAttached(tabId)) return;
    await sendPresence(tabId, { target: "content", type: "presence.attached", stopShortcut: shortcut });
  } catch {
  }
}
cdp.onAttach((tabId) => void presenceAttached(tabId));
chrome.tabs?.onUpdated?.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "complete" && cdp.isAttached(tabId)) void presenceAttached(tabId);
});
chrome.commands?.onCommand?.addListener((command) => {
  if (command === STOP_COMMAND) void handleStopPressed();
});
cdp.onRelease((tabId) => {
  opsByTab.delete(tabId);
  void sendPresence(tabId, { target: "content", type: "presence.teardown" });
});
const MIN_CAPTURE_PX = 64;
function pageGeometry(metrics) {
  const visual = metrics.cssVisualViewport ?? {};
  const layout = metrics.cssLayoutViewport ?? {};
  const viewportWidth = layout.clientWidth ?? visual.clientWidth ?? 0;
  const viewportHeight = layout.clientHeight ?? visual.clientHeight ?? 0;
  const pageX = layout.pageX ?? visual.pageX ?? 0;
  const pageY = layout.pageY ?? visual.pageY ?? 0;
  return {
    pageX,
    pageY,
    viewportWidth,
    viewportHeight,
    // A page shorter than the viewport still has the viewport's area to show.
    contentWidth: Math.max(metrics.cssContentSize?.width ?? 0, pageX + viewportWidth),
    contentHeight: Math.max(metrics.cssContentSize?.height ?? 0, pageY + viewportHeight)
  };
}
function placeClip(viewportRect, geometry) {
  const xs = clampSpan(viewportRect.x + geometry.pageX, viewportRect.width, geometry.contentWidth);
  const ys = clampSpan(viewportRect.y + geometry.pageY, viewportRect.height, geometry.contentHeight);
  if (!xs || !ys) return null;
  const gx = growSpan(xs[0], xs[1], geometry.contentWidth);
  const gy = growSpan(ys[0], ys[1], geometry.contentHeight);
  const clip = { x: gx[0], y: gy[0], width: gx[1], height: gy[1] };
  return {
    clip,
    viewportClip: { x: clip.x - geometry.pageX, y: clip.y - geometry.pageY, width: clip.width, height: clip.height },
    beyondViewport: clip.x < geometry.pageX || clip.y < geometry.pageY || clip.x + clip.width > geometry.pageX + geometry.viewportWidth || clip.y + clip.height > geometry.pageY + geometry.viewportHeight,
    grown: gx[1] !== xs[1] || gy[1] !== ys[1]
  };
}
function captureAreaClip(geometry, full) {
  return full ? { x: 0, y: 0, width: geometry.contentWidth, height: geometry.contentHeight } : { x: geometry.pageX, y: geometry.pageY, width: geometry.viewportWidth, height: geometry.viewportHeight };
}
const MIN_SCALE = 0.25;
const MAX_SCALE = 1;
const DEFAULT_SCALE = 1;
const DEFAULT_JPEG_QUALITY = 70;
function resolveScale(scale) {
  if (typeof scale !== "number" || !Number.isFinite(scale)) return DEFAULT_SCALE;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}
function resolveFormat(format) {
  return format === "png" ? "png" : "jpeg";
}
function resolveQuality(quality) {
  if (typeof quality !== "number" || !Number.isFinite(quality)) return DEFAULT_JPEG_QUALITY;
  return Math.min(100, Math.max(1, Math.round(quality)));
}
function mimeOf(format) {
  return format === "png" ? "image/png" : "image/jpeg";
}
function base64ToBytes$1(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
function bytesToBase64$1(bytes) {
  let binary = "";
  const chunk = 32768;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
async function decodeImageDims(base64, mime) {
  try {
    const bitmap = await createImageBitmap(new Blob([base64ToBytes$1(base64).buffer], { type: mime }));
    const dims = { width: bitmap.width, height: bitmap.height };
    bitmap.close();
    return dims;
  } catch {
    return void 0;
  }
}
async function reencodeScaled(base64, mime, scale, outFormat, quality) {
  try {
    const bitmap = await createImageBitmap(new Blob([base64ToBytes$1(base64).buffer], { type: mime }));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      bitmap.close();
      return void 0;
    }
    ctx.drawImage(bitmap, 0, 0, width, height);
    bitmap.close();
    const outMime = mimeOf(outFormat);
    const blob = await canvas.convertToBlob(
      outFormat === "jpeg" ? { type: outMime, quality: (quality ?? DEFAULT_JPEG_QUALITY) / 100 } : { type: outMime }
    );
    const buf = new Uint8Array(await blob.arrayBuffer());
    return { b64: bytesToBase64$1(buf), width, height };
  } catch {
    return void 0;
  }
}
const MAX_MARKS = 150;
function prioritizeMarks(boxes, viewport) {
  const w = viewport?.width ?? Number.POSITIVE_INFINITY;
  const h = viewport?.height ?? Number.POSITIVE_INFINITY;
  const inViewport = (b) => b.x + b.width > 0 && b.y + b.height > 0 && b.x < w && b.y < h;
  const visible = [];
  const rest = [];
  for (const b of boxes) (inViewport(b) ? visible : rest).push(b);
  return [...visible, ...rest].slice(0, MAX_MARKS).map((b) => ({ idx: b.idx, x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }));
}
function clampSpan(start, length, limit) {
  const lo = Math.max(0, start);
  const hi = Math.min(limit, start + Math.max(0, length));
  if (hi < lo || lo > limit) return null;
  return [lo, hi - lo];
}
function growSpan(start, length, limit) {
  if (length >= MIN_CAPTURE_PX) return [start, length];
  const size = Math.min(MIN_CAPTURE_PX, limit);
  let lo = start + length / 2 - size / 2;
  lo = Math.min(Math.max(0, lo), limit - size);
  return [lo, size];
}
const CAPABILITY_POWER_KEYS = {
  upload: ["allowFileUpload", "allowFileUploadFromAgent"],
  evaluate: ["allowEvaluate"],
  console: ["allowConsoleRead"],
  cookies_write: ["allowCookieWrite"],
  http_auth: ["allowHttpAuth"],
  downloads: ["allowDownloadsRead"],
  dialog: ["allowDialogAccept"]
};
function powerEnabled(value) {
  return value === true;
}
function powerDenialFor(capability, policy) {
  const keys = CAPABILITY_POWER_KEYS[capability];
  if (keys.some((key) => powerEnabled(policy[key]))) return null;
  const settingNames = keys.map((key) => `'${key}'`).join(" or ");
  const { code, message } = formatRefusal("power_capability_disabled", { capability, settingNames });
  return { code, message };
}
async function checkPower(capability, settings) {
  const effective = settings ?? await getSettings();
  return powerDenialFor(capability, powerPolicyOf(effective));
}
function assertLocalFileUploadAllowed(settings) {
  return checkSinglePower("allowFileUpload", "upload (local path)", settings);
}
function assertAgentFileUploadAllowed(settings) {
  return checkSinglePower("allowFileUploadFromAgent", "upload (bytes from agent)", settings);
}
async function checkSinglePower(key, label, settings) {
  const effective = settings ?? await getSettings();
  const policy = powerPolicyOf(effective);
  if (powerEnabled(policy[key])) return null;
  const { code, message } = formatRefusal("power_capability_disabled", {
    capability: label,
    settingNames: `'${key}'`
  });
  return { code, message };
}
function assertEvaluateAllowed(settings) {
  return checkPower("evaluate", settings);
}
function assertConsoleReadAllowed(settings) {
  return checkPower("console", settings);
}
function assertCookieWriteAllowed(settings) {
  return checkPower("cookies_write", settings);
}
function assertHttpAuthAllowed(settings) {
  return checkPower("http_auth", settings);
}
function assertDownloadsReadAllowed(settings) {
  return checkPower("downloads", settings);
}
function assertDialogAcceptAllowed(settings) {
  return checkPower("dialog", settings);
}
const HEADFUL_DIALOG_NOTE = "a dialog is open and waiting for the user; ask them to answer it, or use browser_bridge_dialog";
const TIMING = {
  // No native dialog exists to read (hasBrowserHandler: false) — nothing is
  // lost by resolving it at once, and every extra millisecond is a
  // millisecond Runtime.evaluate stays wedged for no visible reason.
  immediateMs: 0
};
const MAX_BUFFERED_PER_TAB = 20;
const buffers$2 = /* @__PURE__ */ new Map();
const open = /* @__PURE__ */ new Map();
let dialogCounter = 0;
function nextDialogId() {
  dialogCounter += 1;
  return `dlg_${Date.now().toString(36)}_${dialogCounter}`;
}
function bufferFor$2(tabId) {
  let buffer = buffers$2.get(tabId);
  if (!buffer) {
    buffer = [];
    buffers$2.set(tabId, buffer);
  }
  return buffer;
}
function pushRecord(tabId, record) {
  const buffer = bufferFor$2(tabId);
  buffer.push(record);
  while (buffer.length > MAX_BUFFERED_PER_TAB) buffer.shift();
}
function clearDialogBuffer(tabId) {
  buffers$2.delete(tabId);
}
function drainDialogs(tabId) {
  const buffer = buffers$2.get(tabId);
  buffers$2.delete(tabId);
  return buffer ?? [];
}
async function currentSettings() {
  return getSettings();
}
function clearDismissTimer(tracked) {
  if (tracked.dismissTimer !== null) {
    clearTimeout(tracked.dismissTimer);
    tracked.dismissTimer = null;
  }
}
async function resolveDialog(tabId, accept, promptText) {
  const tracked = open.get(tabId);
  if (!tracked || tracked.resolved) return false;
  tracked.resolved = true;
  clearDismissTimer(tracked);
  open.delete(tabId);
  const result = await cdp.send(tabId, "Page.handleJavaScriptDialog", {
    accept,
    ...promptText !== void 0 ? { promptText } : {}
  });
  return result.ok;
}
function scheduleAutoDismiss(tabId, tracked, allowDismiss) {
  if (!allowDismiss) return;
  if (tracked.hasBrowserHandler) return;
  tracked.dismissTimer = setTimeout(() => {
    void resolveDialog(tabId, false);
  }, TIMING.immediateMs);
}
function asRecord$2(value) {
  return value && typeof value === "object" ? value : {};
}
function dialogTypeOf(raw) {
  return raw === "confirm" || raw === "prompt" || raw === "beforeunload" ? raw : "alert";
}
function onJavascriptDialogOpening(tabId, params) {
  const rawMessage = typeof params.message === "string" ? params.message : "";
  const rawDefault = typeof params.defaultPrompt === "string" ? params.defaultPrompt : "";
  const tracked = {
    id: nextDialogId(),
    type: dialogTypeOf(params.type),
    message: rawMessage,
    defaultPrompt: rawDefault,
    url: typeof params.url === "string" ? params.url : "",
    hasBrowserHandler: params.hasBrowserHandler !== false,
    resolved: false,
    dismissTimer: null
  };
  open.set(tabId, tracked);
  void finishRecordingDialog(tabId, tracked, rawMessage, rawDefault);
}
async function finishRecordingDialog(tabId, tracked, rawMessage, rawDefault) {
  const settings = await currentSettings();
  const policy = redactionPolicyOf(settings);
  tracked.message = redactText(rawMessage, policy).text;
  tracked.defaultPrompt = redactText(rawDefault, policy).text;
  pushRecord(tabId, {
    id: tracked.id,
    type: tracked.type,
    message: tracked.message,
    defaultPrompt: tracked.defaultPrompt,
    url: tracked.url,
    hasBrowserHandler: tracked.hasBrowserHandler,
    ...tracked.hasBrowserHandler ? { note: HEADFUL_DIALOG_NOTE } : {}
  });
  if (!tracked.resolved) {
    scheduleAutoDismiss(tabId, tracked, powerPolicyOf(settings).allowDialogDismiss);
  }
}
function onJavascriptDialogClosed(tabId) {
  const tracked = open.get(tabId);
  if (!tracked) return;
  tracked.resolved = true;
  clearDismissTimer(tracked);
  open.delete(tabId);
}
function isTopLevelFrame(params) {
  const frame = asRecord$2(params.frame);
  return frame.parentId === void 0 || frame.parentId === null;
}
function onDebuggerEvent$2(source, method, rawParams) {
  const tabId = source.tabId;
  if (typeof tabId !== "number" || !cdp.isAttached(tabId)) return;
  const params = asRecord$2(rawParams);
  if (method === "Page.javascriptDialogOpening") {
    onJavascriptDialogOpening(tabId, params);
    return;
  }
  if (method === "Page.javascriptDialogClosed") {
    onJavascriptDialogClosed(tabId);
    return;
  }
  if (method === "Page.frameNavigated" && isTopLevelFrame(params)) {
    clearDialogBuffer(tabId);
  }
}
chrome.debugger.onEvent.addListener(onDebuggerEvent$2);
cdp.onRelease((tabId) => {
  buffers$2.delete(tabId);
  const tracked = open.get(tabId);
  if (tracked) clearDismissTimer(tracked);
  open.delete(tabId);
});
async function handleDialogWire(msg) {
  const limitedRefusal = cdpOnlyGate(msg.tabId, "page.dialog");
  if (limitedRefusal) {
    return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
  }
  const tracked = open.get(msg.tabId);
  if (!tracked || tracked.resolved || tracked.id !== msg.dialogId) {
    const { code, message } = formatRefusal("dialog_not_found");
    return { ok: false, code, error: message };
  }
  if (msg.accept) {
    const cancelHandle = registerCancellable(msg.tabId, "dialog-accept", () => {
    });
    try {
      const settings = await currentSettings();
      const refusal = await assertDialogAcceptAllowed(settings);
      if (refusal) {
        return { ok: false, code: refusal.code, error: refusal.message };
      }
      if (msg.ackMessage === void 0 || msg.ackMessage !== tracked.message) {
        const { code, message } = formatRefusal("dialog_ack_mismatch");
        return { ok: false, code, error: message };
      }
      if (cancelHandle.isCancelled()) {
        const reason = cancelHandle.cancelReason();
        return {
          ok: false,
          code: reason ? codeForCancelReason(reason) : ERROR_CODES.SHARING_PAUSED,
          error: "the accept was cancelled before it reached the page (pause/Stop landed first); the dialog is untouched and still waiting for the user — it was NOT accepted or dismissed"
        };
      }
    } finally {
      cancelHandle.unregister();
    }
  }
  try {
    const resolved = await resolveDialog(msg.tabId, msg.accept, msg.promptText);
    if (!resolved) {
      const { code, message } = formatRefusal("dialog_not_found");
      return { ok: false, code, error: message };
    }
    return { ok: true, data: { resolved: true, dialogId: msg.dialogId } };
  } catch (error) {
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: describeError(error) };
  }
}
function basename(rawPath) {
  const parts = rawPath.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1];
}
const BANNED_SEGMENTS = /* @__PURE__ */ new Set([
  ".ssh",
  ".aws",
  ".gnupg",
  ".gnupg-agent",
  ".docker",
  ".kube",
  ".azure",
  ".gcloud",
  ".netrc",
  ".mozilla",
  "keychains",
  "user data"
  // Chrome/Edge/Brave profile root ("...\Google\Chrome\User Data")
]);
function normSegments(p) {
  return p.replace(/\\/g, "/").split("/").filter((s) => s.length > 0);
}
function normSegmentForCompare(segment, isFirst) {
  if (isFirst && /^[A-Za-z]:$/.test(segment)) return segment.toLowerCase();
  return segment;
}
function isUnderRoot(pathSegments, rootSegments) {
  if (rootSegments.length === 0) return false;
  if (pathSegments.length < rootSegments.length) return false;
  for (let i = 0; i < rootSegments.length; i++) {
    if (normSegmentForCompare(pathSegments[i], i === 0) !== normSegmentForCompare(rootSegments[i], i === 0)) {
      return false;
    }
  }
  return true;
}
const WINDOWS_DRIVE_ABSOLUTE = /^[A-Za-z]:[\\/]/;
function isAbsolutePath(rawPath) {
  return rawPath.startsWith("/") || WINDOWS_DRIVE_ABSOLUTE.test(rawPath);
}
const CONTROL_CHARS = /[\x00-\x1f\x7f]/;
function pathDenial(reasonId, params = {}) {
  const { code, message } = formatRefusal(reasonId, params);
  return { ok: false, code, message };
}
function validateLocalUploadPath(rawPath, uploadRoots) {
  if (CONTROL_CHARS.test(rawPath)) {
    return pathDenial("upload_path_control_char", { basename: basename(rawPath) });
  }
  if (!isAbsolutePath(rawPath)) {
    return pathDenial("upload_path_not_absolute");
  }
  const roots = uploadRoots.split(/\r?\n/).map((r) => r.trim()).filter((r) => r.length > 0);
  if (roots.length === 0) {
    return pathDenial("upload_roots_empty");
  }
  const rawSegments = rawPath.replace(/\\/g, "/").split("/");
  if (rawSegments.some((s) => s === "..")) {
    return pathDenial("upload_path_traversal", { basename: basename(rawPath) });
  }
  const pathSegments = normSegments(rawPath);
  const hiddenOrBanned = pathSegments.find((s) => s.startsWith(".") || BANNED_SEGMENTS.has(s.toLowerCase()));
  if (hiddenOrBanned !== void 0) {
    return pathDenial("upload_path_hidden_segment", { basename: basename(rawPath), segment: hiddenOrBanned });
  }
  const underAnyRoot = roots.some((root) => isUnderRoot(pathSegments, normSegments(root)));
  if (!underAnyRoot) {
    return pathDenial("upload_path_outside_roots", { basename: basename(rawPath) });
  }
  return { ok: true };
}
const FALLBACK_MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const EXECUTABLE_SIGNATURES = [
  { name: "Windows PE (MZ)", bytes: [77, 90] },
  { name: "ELF", bytes: [127, 69, 76, 70] },
  { name: "Mach-O 32-bit", bytes: [254, 237, 250, 206] },
  { name: "Mach-O 64-bit", bytes: [254, 237, 250, 207] },
  { name: "Mach-O 32-bit (reverse)", bytes: [206, 250, 237, 254] },
  { name: "Mach-O 64-bit (reverse)", bytes: [207, 250, 237, 254] },
  { name: "Mach-O fat binary", bytes: [202, 254, 186, 190] }
];
const KNOWN_SIGNATURES = [
  { mimePrefixes: ["image/png"], bytes: [137, 80, 78, 71], name: "PNG" },
  { mimePrefixes: ["image/jpeg", "image/jpg"], bytes: [255, 216, 255], name: "JPEG" },
  { mimePrefixes: ["image/gif"], bytes: [71, 73, 70, 56], name: "GIF" },
  { mimePrefixes: ["application/pdf"], bytes: [37, 80, 68, 70], name: "PDF" }
];
function startsWithBytes(bytes, signature) {
  if (bytes.length < signature.length) return false;
  for (let i = 0; i < signature.length; i++) {
    if (bytes[i] !== signature[i]) return false;
  }
  return true;
}
function sniffMismatch(bytes, declaredMime) {
  for (const sig of EXECUTABLE_SIGNATURES) {
    if (startsWithBytes(bytes, sig.bytes)) {
      return { mismatch: true, reason: `decoded bytes are a ${sig.name} executable, refused regardless of declared mimeType ${JSON.stringify(declaredMime)}` };
    }
  }
  const mimeLower = declaredMime.toLowerCase();
  const known = KNOWN_SIGNATURES.find((k) => k.mimePrefixes.some((p) => mimeLower.startsWith(p)));
  if (known && !startsWithBytes(bytes, known.bytes)) {
    return { mismatch: true, reason: `declared mimeType ${JSON.stringify(declaredMime)} claims ${known.name}, but the decoded bytes' own signature does not match` };
  }
  return { mismatch: false };
}
function decodeBase64(b64) {
  try {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}
function maxUploadBytesOf(policy) {
  return policy.maxUploadBytes > 0 ? policy.maxUploadBytes : FALLBACK_MAX_UPLOAD_BYTES;
}
async function handleUploadBytes(msg) {
  const settings = await getSettings();
  const powerDenial = await assertAgentFileUploadAllowed(settings);
  if (powerDenial) {
    return { ok: false, code: powerDenial.code, error: powerDenial.message };
  }
  const bytes = decodeBase64(msg.contentBase64);
  if (!bytes) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "page.upload: contentBase64 is not valid base64" };
  }
  const cap = maxUploadBytesOf(settings);
  if (bytes.length > cap) {
    return {
      ok: false,
      code: ERROR_CODES.INVALID_PARAMS,
      error: `page.upload: decoded size ${bytes.length} bytes exceeds this device's maxUploadBytes (${cap}) — refused, never truncated`
    };
  }
  const sniffed = sniffMismatch(bytes, msg.mimeType);
  if (sniffed.mismatch) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: `page.upload: ${sniffed.reason}` };
  }
  let execResult;
  try {
    execResult = await chrome.scripting.executeScript({
      target: { tabId: msg.tabId },
      world: "MAIN",
      func: (selector, filename, mimeType, byteArray) => {
        const el = document.querySelector(selector);
        if (!el || !(el instanceof HTMLInputElement) || el.type !== "file") {
          return { found: false };
        }
        try {
          const bytesLocal = new Uint8Array(byteArray);
          const file = new File([bytesLocal], filename, { type: mimeType });
          const dt = new DataTransfer();
          dt.items.add(file);
          el.files = dt.files;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { found: true };
        } catch (err) {
          return { found: false, error: String(err) };
        }
      },
      args: [msg.selector, msg.filename, msg.mimeType, Array.from(bytes)]
    });
  } catch (error) {
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: `page.upload: could not inject into tab ${msg.tabId}: ${describeError(error)}` };
  }
  const injected = execResult[0]?.result;
  if (!injected?.found) {
    return {
      ok: false,
      code: ERROR_CODES.INVALID_PARAMS,
      error: `page.upload: selector ${JSON.stringify(msg.selector)} did not resolve to an <input type=file> in tab ${msg.tabId}${injected?.error ? ` (${injected.error})` : ""}`
    };
  }
  const readBack = await contentRequest(msg.tabId, {
    target: "content",
    type: "readFileInput",
    selector: msg.selector
  });
  const fileInfo = readBack.ok && readBack.data?.found && readBack.data.name !== void 0 ? { name: readBack.data.name, size: readBack.data.size ?? 0, type: readBack.data.type ?? "" } : void 0;
  return { ok: true, data: fileInfo ? { fileInfo } : {} };
}
const COMMITTING_NAMES = [
  "Finish",
  "Submit",
  "Send",
  "Pay",
  "Purchase",
  "Buy",
  "Place order",
  "Delete",
  "Remove",
  "Confirm",
  "Publish",
  "Transfer",
  "Deploy",
  "Power off"
];
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
const COMMITTING_NAME_PATTERNS = COMMITTING_NAMES.map(
  (name) => new RegExp(`\\b${escapeRegExp(name)}\\b`, "i")
);
function isCommittingName(name) {
  if (!name) return false;
  return COMMITTING_NAME_PATTERNS.some((re) => re.test(name));
}
function isCommittingAction(action) {
  return action === "submit";
}
function isCommittingTarget(action, name) {
  return isCommittingAction(action) || isCommittingName(name);
}
const REPLAY_JPEG_QUALITY = 50;
const REPLAY_SCALE = 0.5;
const REPLAY_CAPTURE_CEILING_MS = 4e3;
function withCeiling(work, ms, onTimeout) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      resolve(onTimeout);
    }, ms);
    work.then(
      (v) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(v);
      },
      () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(onTimeout);
      }
    );
  });
}
function describeReplayTarget(ctx, policy) {
  if (ctx.hit) {
    const name = redactCaption(ctx.hit.name, policy);
    return name ? `${ctx.hit.role} "${name}"` : ctx.hit.role;
  }
  if (ctx.action === "fill" && ctx.fieldSelectors && ctx.fieldSelectors.length > 0) {
    return ctx.fieldSelectors.map((s) => redactCaption(s, policy)).join(", ");
  }
  if (ctx.selector) return redactCaption(ctx.selector, policy);
  return void 0;
}
function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 32768;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
async function downscale(base64, mime) {
  try {
    const bitmap = await createImageBitmap(new Blob([base64ToBytes(base64).buffer], { type: mime }));
    const width = Math.max(1, Math.round(bitmap.width * REPLAY_SCALE));
    const height = Math.max(1, Math.round(bitmap.height * REPLAY_SCALE));
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext("2d");
    if (!context) {
      bitmap.close();
      return void 0;
    }
    context.drawImage(bitmap, 0, 0, width, height);
    bitmap.close();
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: REPLAY_JPEG_QUALITY / 100 });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    return { b64: bytesToBase64(bytes), bytes: bytes.byteLength };
  } catch {
    return void 0;
  }
}
async function captureFullMode(tabId) {
  await presenceHideForCapture(tabId);
  let shot;
  try {
    shot = await cdp.send(tabId, "Page.captureScreenshot", {
      format: "jpeg",
      quality: REPLAY_JPEG_QUALITY
    });
  } finally {
    void presenceRestoreAfterCapture(tabId);
  }
  if (!shot.ok) return void 0;
  return downscale(shot.result.data, "image/jpeg");
}
async function captureLimitedMode(tabId) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return void 0;
  }
  if (!tab.active || typeof tab.windowId !== "number") return void 0;
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: REPLAY_JPEG_QUALITY });
    const b64 = dataUrl.startsWith("data:") ? dataUrl.slice(dataUrl.indexOf(",") + 1) : dataUrl;
    return downscale(b64, "image/jpeg");
  } catch {
    return void 0;
  }
}
async function captureAndStore(ctx, outcome) {
  const settings = await getSettings();
  if (!settings.recordReplay) return;
  if (!cdp.isShared(ctx.tabId)) return;
  const frame = cdp.isAttached(ctx.tabId) ? await captureFullMode(ctx.tabId) : await captureLimitedMode(ctx.tabId);
  if (!frame) return;
  const policy = redactionPolicyOf(settings);
  const target = describeReplayTarget(ctx, policy);
  const record = {
    time: Date.now(),
    tabId: ctx.tabId,
    kind: ctx.action,
    outcome,
    image: frame.b64,
    bytes: frame.bytes,
    ...target ? { target } : {}
  };
  await getReplayStore().addFrame(record, settings.replayRetention);
}
async function recordReplayFrame(ctx, outcome) {
  try {
    await withCeiling(captureAndStore(ctx, outcome), REPLAY_CAPTURE_CEILING_MS, void 0);
  } catch {
  }
}
const DEFAULT_TIMEOUT_MS$2 = 1e4;
const HOVER_DEFAULT_MS = 250;
const HOVER_MAX_MS = 5e3;
const DIFF_SNAPSHOT_BUDGET_BYTES = 8192;
const DEFAULT_KEY_DELAY_MS = 20;
const SETTLE_POLL_MS = 150;
const SETTLE_INITIAL_DELAY_MS = 50;
const MIN_QUIET_MS = 100;
const MAX_QUIET_MS = 150;
const ADAPTIVE_QUIET_FULL_BUDGET_MS = MAX_QUIET_MS * 10;
function adaptiveQuietWindowMs(budgetMs) {
  if (budgetMs <= 0) return MIN_QUIET_MS;
  const scaled = budgetMs / ADAPTIVE_QUIET_FULL_BUDGET_MS * MAX_QUIET_MS;
  return Math.max(MIN_QUIET_MS, Math.min(MAX_QUIET_MS, scaled));
}
const ANNOTATE_DEFAULT_TIMEOUT_MS = 12e4;
const FINGERPRINT_EXPRESSION = `(function () {
  let n = document.documentElement.outerHTML.length;
  const scopes = [document];
  while (scopes.length) {
    for (const el of scopes.pop().querySelectorAll("*")) {
      const root = el.shadowRoot;
      if (root) { n += root.innerHTML.length; scopes.push(root); }
    }
  }
  return n + "|" + document.title + "|" + location.href;
})()`;
function actOk(result) {
  return { ok: true, result };
}
function actFail(code, message) {
  return { ok: false, error: { code, message } };
}
function sleep$1(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}
function remainingUntil(deadline) {
  return Math.max(0, deadline - Date.now());
}
async function evaluate(tabId, expression) {
  const sent = await cdp.send(tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true
  });
  if (!sent.ok) return actFail(sent.error.code, sent.error.message);
  if (sent.result.exceptionDetails) {
    const detail = sent.result.exceptionDetails.exception?.description ?? sent.result.exceptionDetails.text ?? "evaluation threw";
    return actFail(ERROR_CODES.CDP_ERROR, detail);
  }
  return actOk(sent.result.result?.value);
}
function shiftRect(rect, frame) {
  if (!frame) return rect;
  return { x: rect.x + frame.offsetX, y: rect.y + frame.offsetY, width: rect.width, height: rect.height };
}
function shiftPoint(x, y, frame) {
  return frame ? [x + frame.offsetX, y + frame.offsetY] : [x, y];
}
function frameIdOf(frame) {
  return frame?.frameId ?? 0;
}
function localSelectorOf(selector, frame) {
  return frame?.localSelector ?? selector;
}
function noteChoice(ctx, selector, matchCount, chosenReason) {
  if (matchCount === void 0 || chosenReason === void 0) return;
  const note = describeChoice(selector, matchCount, chosenReason);
  if (note) ctx.notes.push(note);
}
function contentFail(result, what) {
  return actFail(result.code ?? ERROR_CODES.CONTENT_SCRIPT_ERROR, result.error ?? `${what} failed`);
}
function invalidSelector(selector, detail) {
  return actFail(ERROR_CODES.INVALID_PARAMS, `invalid selector ${selector}: ${detail}`);
}
async function resolveRect(tabId, selector, ctx, frame, scroll = true) {
  const local = localSelectorOf(selector, frame);
  const result = await contentRequest(
    tabId,
    { target: "content", type: "resolveTarget", selector: local, scroll },
    frameIdOf(frame)
  );
  if (!result.ok || !result.data) return contentFail(result, "resolveTarget");
  const data = result.data;
  if (!data.found) {
    if (data.invalid) return invalidSelector(selector, data.invalid);
    return actFail(ERROR_CODES.CDP_ERROR, `selector not found: ${selector}`);
  }
  noteChoice(ctx, selector, data.matchCount, data.chosenReason);
  return actOk(shiftRect(data.rect, frame));
}
function centerOf(rect) {
  return [rect.x + rect.width / 2, rect.y + rect.height / 2];
}
async function resolveEditableRect(tabId, selector, ctx, frame) {
  const local = localSelectorOf(selector, frame);
  const result = await contentRequest(
    tabId,
    { target: "content", type: "resolveEditableTarget", selector: local, scroll: true },
    frameIdOf(frame)
  );
  if (!result.ok || !result.data) return contentFail(result, "resolveEditableTarget");
  const data = result.data;
  if (!data.found) {
    if (data.invalid) return invalidSelector(selector, data.invalid);
    return actFail(ERROR_CODES.CDP_ERROR, `selector not found: ${selector}`);
  }
  noteChoice(ctx, selector, data.matchCount, data.chosenReason);
  return actOk({ ...data, rect: shiftRect(data.rect, frame) });
}
async function readTypedField(tabId, selector, frame) {
  const result = await contentRequest(
    tabId,
    { target: "content", type: "readField", selector: localSelectorOf(selector, frame) },
    frameIdOf(frame)
  );
  if (!result.ok || !result.data || !result.data.found) return void 0;
  return result.data.value;
}
async function readUploadedFile(tabId, selector, frame) {
  const result = await contentRequest(
    tabId,
    { target: "content", type: "readFileInput", selector: localSelectorOf(selector, frame) },
    frameIdOf(frame)
  );
  if (!result.ok || !result.data?.found || result.data.notFileInput || result.data.name === void 0) return void 0;
  return { name: result.data.name, size: result.data.size ?? 0, type: result.data.type ?? "" };
}
const VIEWPORT_EXPRESSION = "({width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio, scrollX: window.scrollX, scrollY: window.scrollY})";
function viewportsDiffer(expected, live) {
  return Math.round(expected.width) !== Math.round(live.width) || Math.round(expected.height) !== Math.round(live.height) || Math.round(expected.scrollX) !== Math.round(live.scrollX) || Math.round(expected.scrollY) !== Math.round(live.scrollY) || // dpr is a float (e.g. 1, 1.5, 2); compare to 2dp to absorb float noise
  // without forgiving a real zoom-level change.
  Math.round(expected.dpr * 100) !== Math.round(live.dpr * 100);
}
function describeViewport(v) {
  return `${v.width}x${v.height} @${v.dpr}x, scroll (${v.scrollX}, ${v.scrollY})`;
}
async function checkViewportStaleness(tabId, expected) {
  if (!expected) return null;
  const live = await evaluate(tabId, VIEWPORT_EXPRESSION);
  if (!live.ok || !live.result) return null;
  if (!viewportsDiffer(expected, live.result)) return null;
  const { code, message } = formatRefusal("viewport_mismatch", {
    expectedViewport: describeViewport(expected),
    actualViewport: describeViewport(live.result)
  });
  return actFail(code, message);
}
function quadArea(q) {
  let area = 0;
  for (let i = 0; i < 4; i++) {
    const x1 = q[i * 2];
    const y1 = q[i * 2 + 1];
    const x2 = q[(i + 1) % 4 * 2];
    const y2 = q[(i + 1) % 4 * 2 + 1];
    area += x1 * y2 - x2 * y1;
  }
  return Math.abs(area) / 2;
}
function quadCenter(q) {
  const xs = [q[0], q[2], q[4], q[6]];
  const ys = [q[1], q[3], q[5], q[7]];
  return [(xs[0] + xs[1] + xs[2] + xs[3]) / 4, (ys[0] + ys[1] + ys[2] + ys[3]) / 4];
}
function quadOnScreen(q, viewportWidth, viewportHeight) {
  const xs = [q[0], q[2], q[4], q[6]];
  const ys = [q[1], q[3], q[5], q[7]];
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return maxX > 0 && minX < viewportWidth && maxY > 0 && minY < viewportHeight;
}
async function viewportDimensions(tabId) {
  const live = await evaluate(tabId, VIEWPORT_EXPRESSION);
  if (!live.ok || !live.result) return null;
  return { width: live.result.width, height: live.result.height };
}
async function contentQuadCenter(tabId, backendNodeId) {
  const quads = await cdp.send(tabId, "DOM.getContentQuads", { backendNodeId });
  if (!quads.ok) return actFail(quads.error.code, quads.error.message);
  const viewport = await viewportDimensions(tabId);
  const candidates = (quads.result.quads || []).filter((q) => Array.isArray(q) && q.length === 8).map((q) => ({ q, area: quadArea(q) })).filter(({ area }) => area > 0).filter(({ q }) => !viewport || quadOnScreen(q, viewport.width, viewport.height));
  if (candidates.length === 0) {
    const { code, message } = formatRefusal("no_hit_testable_target");
    return actFail(code, message);
  }
  let largest = candidates[0];
  for (const c of candidates) if (c.area > largest.area) largest = c;
  return actOk(quadCenter(largest.q));
}
async function fetchFrameOriginTree(tabId) {
  const sent = await cdp.send(tabId, "Page.getFrameTree", {});
  if (!sent.ok || !sent.result?.frameTree?.frame?.id) return null;
  const frames = /* @__PURE__ */ new Map();
  const flatten = (node, parentId) => {
    if (!node?.frame?.id) return;
    frames.set(node.frame.id, { url: node.frame.url ?? "", parentId });
    for (const child of node.childFrames ?? []) flatten(child, node.frame.id);
  };
  flatten(sent.result.frameTree, null);
  return { rootId: sent.result.frameTree.frame.id, frames };
}
function originOfCdpFrame(tree, frameId) {
  const seen = /* @__PURE__ */ new Set();
  const resolve = (id) => {
    if (seen.has(id)) return "null";
    seen.add(id);
    const node = tree.frames.get(id);
    if (!node) return "null";
    const parentOrigin = node.parentId ? resolve(node.parentId) : "null";
    return frameOriginOf(node.url, parentOrigin);
  };
  return resolve(frameId);
}
async function hitTestAtPoint(tabId, x, y) {
  const sent = await cdp.send(tabId, "DOM.getNodeForLocation", {
    x,
    y,
    includeUserAgentShadowDOM: true
  });
  if (!sent.ok || sent.result?.backendNodeId === void 0 || !sent.result.frameId) return null;
  return sent.result;
}
async function frameOwnerHasRenderedArea(tabId, backendNodeId) {
  const sent = await cdp.send(tabId, "DOM.getBoxModel", { backendNodeId });
  if (!sent.ok || !sent.result?.model) return true;
  return sent.result.model.width > 0 && sent.result.model.height > 0;
}
function rawFrameInfo(tree, frameId) {
  const url = tree.frames.get(frameId)?.url ?? "";
  try {
    const u = new URL(url);
    if (u.protocol === "chrome-extension:") return { isExtensionOverlay: true, displayOrigin: `${u.protocol}//${u.host}` };
  } catch {
  }
  return { isExtensionOverlay: false, displayOrigin: "" };
}
function resolveAbsoluteUrl(base, ref) {
  try {
    return new URL(ref, base).href;
  } catch {
    return void 0;
  }
}
function resolveFrameOwnerNode(node, outerFrameId, tree) {
  const tag = node.nodeName.toLowerCase();
  if (tag !== "iframe" && tag !== "frame") return "not-owner";
  if (node.frameId && tree.frames.has(node.frameId)) return node.frameId;
  const src = attrValue(node.attributes, "src");
  const parent = tree.frames.get(outerFrameId);
  if (!src || !parent) return null;
  const resolved = resolveAbsoluteUrl(parent.url, src);
  if (!resolved) return null;
  const candidates = [];
  for (const [id, f] of tree.frames) {
    if (f.parentId === outerFrameId && f.url === resolved) candidates.push(id);
  }
  return candidates.length === 1 ? candidates[0] : null;
}
async function resolveLandingFrameId(tabId, hit, tree) {
  const outerFrameId = hit.frameId;
  const described = await cdp.send(tabId, "DOM.describeNode", {
    backendNodeId: hit.backendNodeId,
    pierce: true
  });
  if (!described.ok || !described.result?.node) return outerFrameId;
  const resolved = resolveFrameOwnerNode(described.result.node, outerFrameId, tree);
  if (resolved === "not-owner") return outerFrameId;
  if (resolved === null) return null;
  const hasArea = await frameOwnerHasRenderedArea(tabId, hit.backendNodeId);
  return hasArea ? resolved : outerFrameId;
}
function authorizeLandingFrame(tree, ctx, landingFrameId, deniedReason) {
  if (landingFrameId === tree.rootId) return actOk(void 0);
  const origin = originOfCdpFrame(tree, landingFrameId);
  const topOrigin = originOfCdpFrame(tree, tree.rootId);
  if (origin !== "null" && canonicalizeOrigin(origin) === canonicalizeOrigin(topOrigin)) return actOk(void 0);
  if (isOriginAllowed(origin, ctx.originPolicy)) return actOk(void 0);
  if (origin === "null") {
    const raw = rawFrameInfo(tree, landingFrameId);
    if (raw.isExtensionOverlay) {
      const { code: code2, message: message2 } = formatRefusal("pixel_route_shadowed_by_extension", { origin: raw.displayOrigin });
      return actFail(code2, message2);
    }
  }
  const { code, message } = formatRefusal(deniedReason, { origin });
  return actFail(code, message);
}
async function authorizeDispatchPoint(tabId, x, y, ctx) {
  if (ctx.frameTreeCache === void 0) {
    ctx.frameTreeCache = await fetchFrameOriginTree(tabId);
  }
  const tree = ctx.frameTreeCache;
  const hit = await hitTestAtPoint(tabId, x, y);
  if (!hit || !tree) {
    const { code, message } = formatRefusal("pixel_route_hit_test_failed");
    return actFail(code, message);
  }
  const landingFrameId = await resolveLandingFrameId(tabId, hit, tree);
  if (landingFrameId === null) {
    const { code, message } = formatRefusal("pixel_route_hit_test_failed");
    return actFail(code, message);
  }
  return authorizeLandingFrame(tree, ctx, landingFrameId, "pixel_route_frame_denied");
}
const FOCUSED_ELEMENT_WALK_EXPRESSION = `(function () {
  let el = document.activeElement;
  while (el && el.tagName && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    let inner;
    try {
      inner = el.contentWindow && el.contentWindow.document ? el.contentWindow.document.activeElement : undefined;
    } catch (e) {
      inner = undefined;
    }
    if (!inner) break;
    el = inner;
  }
  return el;
})()`;
async function authorizeFocusedFrame(tabId, ctx) {
  if (ctx.frameTreeCache === void 0) {
    ctx.frameTreeCache = await fetchFrameOriginTree(tabId);
  }
  const tree = ctx.frameTreeCache;
  if (!tree) {
    const { code, message } = formatRefusal("focused_frame_unresolved");
    return actFail(code, message);
  }
  const evaluated = await cdp.send(tabId, "Runtime.evaluate", {
    expression: FOCUSED_ELEMENT_WALK_EXPRESSION,
    returnByValue: false
  });
  if (!evaluated.ok) {
    const { code, message } = formatRefusal("focused_frame_unresolved");
    return actFail(code, message);
  }
  const objectId = evaluated.result?.result?.objectId;
  if (!objectId) return actOk(void 0);
  const described = await cdp.send(tabId, "DOM.describeNode", { objectId });
  if (!described.ok || !described.result?.node) {
    const { code, message } = formatRefusal("focused_frame_unresolved");
    return actFail(code, message);
  }
  const resolved = resolveFrameOwnerNode(described.result.node, tree.rootId, tree);
  if (resolved === "not-owner") return actOk(void 0);
  if (resolved === null) {
    const { code, message } = formatRefusal("focused_frame_unresolved");
    return actFail(code, message);
  }
  return authorizeLandingFrame(tree, ctx, resolved, "focused_frame_denied");
}
const TAG_ROLE = {
  a: "link",
  button: "button",
  select: "combobox",
  textarea: "textbox",
  option: "option",
  img: "img"
};
function attrValue(attributes, name) {
  if (!attributes) return void 0;
  for (let i = 0; i + 1 < attributes.length; i += 2) {
    if (attributes[i] === name) return attributes[i + 1];
  }
  return void 0;
}
function coarseRole(node) {
  const tag = node.nodeName.toLowerCase();
  if (tag === "input") {
    const type = (attrValue(node.attributes, "type") || "text").toLowerCase();
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (type === "submit" || type === "button" || type === "reset") return "button";
    return "textbox";
  }
  return TAG_ROLE[tag] ?? tag;
}
function nodeMatchesExpect(expect, node) {
  if (coarseRole(node) !== expect.role) return false;
  const ariaLabel = attrValue(node.attributes, "aria-label");
  if (ariaLabel === void 0) return true;
  return normalizeName(ariaLabel) === normalizeName(expect.name);
}
async function resolveViaBackendNode(tabId, backendNodeId, expect) {
  const described = await cdp.send(tabId, "DOM.describeNode", { backendNodeId });
  if (!described.ok || !described.result?.node) return { ok: false, stale: true };
  if (expect && !nodeMatchesExpect(expect, described.result.node)) {
    const { code, message } = formatRefusal("element_mismatch");
    return {
      ok: false,
      stale: false,
      error: { code, message }
    };
  }
  const scrolled = await cdp.send(tabId, "DOM.scrollIntoViewIfNeeded", { backendNodeId });
  if (!scrolled.ok) return { ok: false, stale: true };
  const quads = await contentQuadCenter(tabId, backendNodeId);
  if (!quads.ok) {
    if (quads.error.code === ERROR_CODES.NO_HIT_TESTABLE_TARGET) return { ok: false, stale: false, error: quads.error };
    return { ok: false, stale: true };
  }
  return { ok: true, point: quads.result };
}
const axDomainEnabled = /* @__PURE__ */ new Map();
cdp.onRelease((tabId) => axDomainEnabled.delete(tabId));
async function queryAXTreeForExpect(tabId, expect) {
  const cachedEnabled = axDomainEnabled.get(tabId);
  if (cachedEnabled === false) return void 0;
  if (cachedEnabled !== true) {
    const enabled = await cdp.send(tabId, "Accessibility.enable", {});
    axDomainEnabled.set(tabId, enabled.ok);
    if (!enabled.ok) return void 0;
  }
  const queried = await cdp.send(tabId, "Accessibility.queryAXTree", {
    accessibleName: expect.name,
    role: expect.role
  });
  if (!queried.ok) return void 0;
  const candidates = (queried.result.nodes ?? []).filter(
    (n) => !n.ignored && typeof n.backendDOMNodeId === "number"
  );
  if (candidates.length !== 1) return void 0;
  return candidates[0].backendDOMNodeId;
}
async function resolveViaAccessibilityTree(tabId, expect, ctx) {
  const backendNodeId = await queryAXTreeForExpect(tabId, expect);
  if (backendNodeId === void 0) return void 0;
  const handle = await resolveViaBackendNode(tabId, backendNodeId, expect);
  if (handle.ok) {
    ctx.notes.push("resolved via accessibility tree fallback (selector and node handle both failed)");
    ctx.resolvedBackendNodeId = backendNodeId;
    return actOk(handle.point);
  }
  if (handle.stale) return void 0;
  return actFail(handle.error.code, handle.error.message);
}
async function describeAxNode(tabId, backendNodeId) {
  const enabled = await cdp.send(tabId, "Accessibility.enable", {});
  if (!enabled.ok) return void 0;
  const tree = await cdp.send(tabId, "Accessibility.getPartialAXTree", {
    backendNodeId,
    fetchRelatives: false
  });
  if (!tree.ok) return void 0;
  const node = (tree.result.nodes ?? []).find((n) => !n.ignored);
  if (!node) return void 0;
  return { role: node.role?.value ?? "", name: node.name?.value ?? "" };
}
function boundingBoxOf(quads) {
  if (!quads || quads.length === 0) return void 0;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const q of quads) {
    if (!Array.isArray(q) || q.length !== 8) continue;
    for (let i = 0; i < 4; i++) {
      const x = q[i * 2];
      const y = q[i * 2 + 1];
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
  }
  return minX === Infinity ? void 0 : { minX, minY, maxX, maxY };
}
function boxContains(outer, inner) {
  return outer.minX <= inner.minX + 0.5 && outer.minY <= inner.minY + 0.5 && outer.maxX >= inner.maxX - 0.5 && outer.maxY >= inner.maxY - 0.5;
}
async function nearMissRelation(tabId, hitBackendNodeId, targetBackendNodeId) {
  if (targetBackendNodeId === void 0 || targetBackendNodeId === hitBackendNodeId) return void 0;
  const [hitQuads, targetQuads] = await Promise.all([
    cdp.send(tabId, "DOM.getContentQuads", { backendNodeId: hitBackendNodeId }),
    cdp.send(tabId, "DOM.getContentQuads", { backendNodeId: targetBackendNodeId })
  ]);
  if (!hitQuads.ok || !targetQuads.ok) return void 0;
  const hitBox = boundingBoxOf(hitQuads.result.quads);
  const targetBox = boundingBoxOf(targetQuads.result.quads);
  if (!hitBox || !targetBox) return void 0;
  if (boxContains(targetBox, hitBox)) return "descendant";
  if (boxContains(hitBox, targetBox)) return "ancestor";
  return void 0;
}
async function captureFullModeHit(tabId, x, y, ctx, expect) {
  const hitPoint = await hitTestAtPoint(tabId, x, y);
  if (!hitPoint || hitPoint.backendNodeId === void 0) return;
  const hitBackendNodeId = hitPoint.backendNodeId;
  const described = await describeAxNode(tabId, hitBackendNodeId);
  if (!described) return;
  let settings;
  try {
    settings = await getSettings();
  } catch {
    return;
  }
  const policy = redactionPolicyOf(settings);
  const role = described.role;
  const name = redactText(described.name, policy).text;
  const matched = expect ? elementMatchesExpect(expect, { role, name }) : ctx.resolvedBackendNodeId !== void 0 ? ctx.resolvedBackendNodeId === hitBackendNodeId : true;
  ctx.hitInfo = { role, name, matched };
  if (!matched) {
    const relation = await nearMissRelation(tabId, hitBackendNodeId, ctx.resolvedBackendNodeId);
    ctx.notes.push(
      relation ? `hit near-miss: the event landed on ${role} "${name}", ${relation === "descendant" ? "inside" : "an ancestor of"} the intended target — see result.hit` : `hit mismatch: the event landed on ${role} "${name}", not the intended target — see result.hit`
    );
  }
}
async function resolvePoint(tabId, params, ctx) {
  const inTopFrame = frameIdOf(ctx.sourceFrame) === 0;
  let handleId = inTopFrame ? params.backendNodeId : void 0;
  let handleSource = handleId !== void 0 ? "backendNodeId" : void 0;
  if (inTopFrame && handleId === void 0 && params.selector) {
    const onDemand = await resolveNodeHandleOnDemand(tabId, params.selector);
    if (onDemand !== void 0) {
      handleId = onDemand;
      handleSource = "on-demand";
    }
  }
  let handleFailed = false;
  if (handleId !== void 0) {
    const handle = await resolveViaBackendNode(tabId, handleId, params.expect);
    if (handle.ok) {
      ctx.notes.push(`resolved via node handle (${handleSource})`);
      ctx.resolvedBackendNodeId = handleId;
      return actOk(handle.point);
    }
    if (!handle.stale) return actFail(handle.error.code, handle.error.message);
    handleFailed = true;
    if (handleSource === "backendNodeId") {
      ctx.notes.push("node handle stale; fell back to selector re-resolution");
    }
  }
  if (params.selector) {
    const rect = await resolveRect(tabId, params.selector, ctx, ctx.sourceFrame);
    if (rect.ok) return actOk(centerOf(rect.result));
    if (inTopFrame && params.expect) {
      const axPoint = await resolveViaAccessibilityTree(tabId, params.expect, ctx);
      if (axPoint) return axPoint;
    }
    return rect;
  }
  if (inTopFrame && handleFailed && params.expect) {
    const axPoint = await resolveViaAccessibilityTree(tabId, params.expect, ctx);
    if (axPoint) return axPoint;
  }
  if (params.xy) {
    const stale = await checkViewportStaleness(tabId, params.expectViewport);
    if (stale) return stale;
    return actOk(params.xy);
  }
  return actFail(ERROR_CODES.INVALID_PARAMS, `${params.action} requires a selector or xy`);
}
async function dispatchClick(tabId, x, y, ctx) {
  const guard = await authorizeDispatchPoint(tabId, x, y, ctx);
  if (!guard.ok) return guard;
  const moved = await cdp.send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  if (!moved.ok) return actFail(moved.error.code, moved.error.message);
  const pressed = await cdp.send(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    x,
    y,
    button: "left",
    clickCount: 1
  });
  if (!pressed.ok) return actFail(pressed.error.code, pressed.error.message);
  const released = await cdp.send(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x,
    y,
    button: "left",
    clickCount: 1
  });
  if (!released.ok) return actFail(released.error.code, released.error.message);
  return actOk(void 0);
}
function normalizeName(name) {
  return name.trim().replace(/\s+/g, " ");
}
function elementMatchesExpect(expect, actual) {
  if (!actual) return false;
  if (actual.role !== expect.role) return false;
  return normalizeName(actual.name) === normalizeName(expect.name);
}
async function resolveLiveElement(tabId, selector, frame) {
  const result = await contentRequest(
    tabId,
    { target: "content", type: "resolveElement", selector: localSelectorOf(selector, frame) },
    frameIdOf(frame)
  );
  if (!result.ok) {
    return actFail(result.code ?? ERROR_CODES.CONTENT_SCRIPT_ERROR, result.error ?? "resolveElement failed");
  }
  const data = result.data ?? null;
  if (data && "invalid" in data) return invalidSelector(selector, data.invalid);
  return actOk(data);
}
async function checkElementExpectation(tabId, selector, expect, whatFailed, frame) {
  if (!expect || !selector) return null;
  const live = await resolveLiveElement(tabId, selector, frame);
  if (!live.ok) {
    return { ok: false, code: live.error.code, error: live.error.message };
  }
  if (elementMatchesExpect(expect, live.result)) return null;
  const { code, message } = formatRefusal("element_mismatch");
  return {
    ok: false,
    code,
    error: `${message} (${whatFailed})`,
    // Structured detail alongside the message above — see offscreen.ts's
    // requireOk/bridgeError and client.ts's onMessage for how (and how far)
    // this rides the wire today.
    data: { expected: expect, actual: live.result }
  };
}
async function checkExpectedElement(params, sourceFrame, destFrame) {
  if (params.action === "wait_for") return null;
  const source = await checkElementExpectation(params.tabId, params.selector, params.expect, "at that index", sourceFrame);
  if (source) return source;
  if (params.action !== "drag") return null;
  return checkElementExpectation(params.tabId, params.toSelector, params.expectTo, "at the drag destination", destFrame);
}
const NAMED_KEYS = {
  enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: "\r" },
  tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 },
  escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 },
  esc: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 },
  backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 },
  delete: { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46, nativeVirtualKeyCode: 46 },
  space: { key: " ", code: "Space", windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 32, text: " " },
  arrowup: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38, nativeVirtualKeyCode: 38 },
  up: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38, nativeVirtualKeyCode: 38 },
  arrowdown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40, nativeVirtualKeyCode: 40 },
  down: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40, nativeVirtualKeyCode: 40 },
  arrowleft: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37, nativeVirtualKeyCode: 37 },
  left: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37, nativeVirtualKeyCode: 37 },
  arrowright: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39, nativeVirtualKeyCode: 39 },
  right: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39, nativeVirtualKeyCode: 39 },
  home: { key: "Home", code: "Home", windowsVirtualKeyCode: 36, nativeVirtualKeyCode: 36 },
  end: { key: "End", code: "End", windowsVirtualKeyCode: 35, nativeVirtualKeyCode: 35 },
  pageup: { key: "PageUp", code: "PageUp", windowsVirtualKeyCode: 33, nativeVirtualKeyCode: 33 },
  pagedown: { key: "PageDown", code: "PageDown", windowsVirtualKeyCode: 34, nativeVirtualKeyCode: 34 }
};
const MODIFIER_BIT = {
  alt: 1,
  ctrl: 2,
  control: 2,
  meta: 4,
  cmd: 4,
  command: 4,
  shift: 8
};
function resolveSingleKey(token) {
  const named = NAMED_KEYS[token.toLowerCase()];
  if (named) return named;
  if (token.length === 1) {
    if (/[a-zA-Z]/.test(token)) {
      const upper = token.toUpperCase();
      return { key: token, code: `Key${upper}`, windowsVirtualKeyCode: upper.charCodeAt(0), nativeVirtualKeyCode: upper.charCodeAt(0), text: token };
    }
    if (/[0-9]/.test(token)) {
      const vk2 = 48 + Number(token);
      return { key: token, code: `Digit${token}`, windowsVirtualKeyCode: vk2, nativeVirtualKeyCode: vk2, text: token };
    }
    const vk = token.charCodeAt(0);
    return { key: token, code: token, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, text: token };
  }
  return { key: token, code: token, windowsVirtualKeyCode: 0, nativeVirtualKeyCode: 0 };
}
function parseKeyChord(chord) {
  const parts = chord.split("+").map((part) => part.trim()).filter(Boolean);
  const keyToken = parts.length > 0 ? parts[parts.length - 1] : chord;
  let modifiers = 0;
  for (const part of parts.slice(0, -1)) {
    modifiers |= MODIFIER_BIT[part.toLowerCase()] ?? 0;
  }
  return { descriptor: resolveSingleKey(keyToken), modifiers };
}
async function dispatchKeyChord(tabId, chord, ctx) {
  const guard = await authorizeFocusedFrame(tabId, ctx);
  if (!guard.ok) return guard;
  const { descriptor, modifiers } = parseKeyChord(chord);
  const base = {
    key: descriptor.key,
    code: descriptor.code,
    windowsVirtualKeyCode: descriptor.windowsVirtualKeyCode,
    nativeVirtualKeyCode: descriptor.nativeVirtualKeyCode,
    modifiers
  };
  if (descriptor.text && modifiers === 0) {
    base.text = descriptor.text;
    base.unmodifiedText = descriptor.text;
  }
  const down = await cdp.send(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...base });
  if (!down.ok) return actFail(down.error.code, down.error.message);
  const up = await cdp.send(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
  if (!up.ok) return actFail(up.error.code, up.error.message);
  return actOk(void 0);
}
function presenceLabelFor(action) {
  switch (action) {
    case "click":
      return "clicking";
    case "type":
      return "typing";
    case "select":
      return "selecting";
    case "submit":
      return "submitting";
    case "scroll":
      return "scrolling";
    case "key":
      return "pressing key";
    case "navigate":
      return "navigating";
    case "wait_for":
      return "waiting";
    case "hover":
      return "hovering";
    case "drag":
      return "dragging";
    case "upload":
      return "uploading";
    case "fill":
      return "filling";
    case "back":
      return "going back";
    case "forward":
      return "going forward";
    case "reload":
      return "reloading";
    default:
      return String(action);
  }
}
async function actClick(tabId, params, ctx) {
  const point = await resolvePoint(tabId, params, ctx);
  if (!point.ok) return point;
  await presenceCursor(tabId, point.result[0], point.result[1], "clicking", true);
  const outcome = await dispatchClick(tabId, point.result[0], point.result[1], ctx);
  if (outcome.ok) await captureFullModeHit(tabId, point.result[0], point.result[1], ctx, params.expect);
  return outcome;
}
async function actHover(tabId, params, ctx) {
  const point = await resolvePoint(tabId, params, ctx);
  if (!point.ok) return point;
  const requested = params.hoverMs ?? HOVER_DEFAULT_MS;
  const hoverMs = Math.min(Math.max(requested, 0), HOVER_MAX_MS);
  if (hoverMs !== requested) {
    ctx.notes.push(`hover_ms ${requested} is out of range (0-${HOVER_MAX_MS}); clamped to ${hoverMs}`);
  }
  const guard = await authorizeDispatchPoint(tabId, point.result[0], point.result[1], ctx);
  if (!guard.ok) return guard;
  await presenceCursor(tabId, point.result[0], point.result[1], "hovering", false);
  const moved = await cdp.send(tabId, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: point.result[0],
    y: point.result[1]
  });
  if (!moved.ok) return actFail(moved.error.code, moved.error.message);
  await captureFullModeHit(tabId, point.result[0], point.result[1], ctx, params.expect);
  await sleep$1(hoverMs);
  return actOk(void 0);
}
async function resolveUploadNode(tabId, params) {
  if (params.backendNodeId !== void 0) {
    const described = await cdp.send(tabId, "DOM.describeNode", { backendNodeId: params.backendNodeId });
    if (described.ok && described.result?.node) return actOk(params.backendNodeId);
  }
  if (params.selector) {
    const handle = await resolveNodeHandleOnDemand(tabId, params.selector);
    if (handle !== void 0) return actOk(handle);
    return actFail(ERROR_CODES.CDP_ERROR, `upload: selector ${JSON.stringify(params.selector)} did not resolve to a CDP node`);
  }
  return actFail(ERROR_CODES.INVALID_PARAMS, "upload requires a selector (or a resolved backendNodeId) — xy has no element to pick a file for");
}
async function actUpload(tabId, params, ctx) {
  const settings = await getSettings();
  const powerDenial = await assertLocalFileUploadAllowed(settings);
  if (powerDenial) return actFail(powerDenial.code, powerDenial.message);
  const filePath = params.filePath;
  if (!filePath) return actFail(ERROR_CODES.INVALID_PARAMS, "upload requires filePath");
  const pathCheck = validateLocalUploadPath(filePath, settings.uploadRoots);
  if (!pathCheck.ok) return actFail(pathCheck.code ?? ERROR_CODES.UPLOAD_PATH_DENIED, pathCheck.message ?? "upload: path refused");
  let fileUrlAccessAllowed;
  try {
    fileUrlAccessAllowed = await chrome.extension.isAllowedFileSchemeAccess();
  } catch {
    fileUrlAccessAllowed = true;
  }
  if (!fileUrlAccessAllowed) {
    const { code, message } = formatRefusal("upload_file_url_access_disabled");
    return actFail(code, message);
  }
  const node = await resolveUploadNode(tabId, params);
  if (!node.ok) return node;
  const set = await cdp.send(tabId, "DOM.setFileInputFiles", { files: [filePath], backendNodeId: node.result });
  if (!set.ok) return actFail(set.error.code, set.error.message);
  if (params.selector) ctx.uploadReadbackSelector = params.selector;
  return actOk(void 0);
}
const DRAG_WAYPOINT_COUNT = 12;
const DRAG_WAYPOINT_DELAY_MS = 16;
function lerp(a, b, t) {
  return a + (b - a) * t;
}
async function resolveDragSource(tabId, params, ctx) {
  if (params.selector) {
    const result = await contentRequest(
      tabId,
      { target: "content", type: "resolveTarget", selector: localSelectorOf(params.selector, ctx.sourceFrame), scroll: true },
      frameIdOf(ctx.sourceFrame)
    );
    if (!result.ok || !result.data) return contentFail(result, "resolveTarget");
    const data = result.data;
    if (!data.found) {
      if (data.invalid) return invalidSelector(params.selector, data.invalid);
      return actFail(ERROR_CODES.CDP_ERROR, `selector not found: ${params.selector}`);
    }
    noteChoice(ctx, params.selector, data.matchCount, data.chosenReason);
    return actOk({ point: centerOf(shiftRect(data.rect, ctx.sourceFrame)), draggable: data.draggable === true });
  }
  if (params.xy) {
    const stale = await checkViewportStaleness(tabId, params.expectViewport);
    if (stale) return stale;
    return actOk({ point: params.xy, draggable: false });
  }
  return actFail(ERROR_CODES.INVALID_PARAMS, "drag requires a source: idx, selector, or xy");
}
async function resolveDragDestination(tabId, params, ctx) {
  if (params.toSelector) {
    const rect = await resolveRect(tabId, params.toSelector, ctx, ctx.destFrame);
    if (!rect.ok) return rect;
    return actOk(centerOf(rect.result));
  }
  if (params.toXy) {
    const stale = await checkViewportStaleness(tabId, params.expectToViewport);
    if (stale) return stale;
    return actOk(params.toXy);
  }
  return actFail(ERROR_CODES.INVALID_PARAMS, "drag requires a destination: to_idx, to_selector, or to_xy");
}
async function tryReleaseDragButton(tabId, x, y) {
  try {
    const result = await cdp.send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    return result.ok;
  } catch {
    return false;
  }
}
async function actDrag(tabId, params, ctx) {
  const mode = params.dragMode ?? "auto";
  if (mode === "html5") {
    const { code, message } = formatRefusal("drag_html5_unsupported");
    return actFail(code, message);
  }
  const src = await resolveDragSource(tabId, params, ctx);
  if (!src.ok) return src;
  const [srcX, srcY] = src.result.point;
  ctx.sourceDraggable = src.result.draggable;
  const srcGuard = await authorizeDispatchPoint(tabId, srcX, srcY, ctx);
  if (!srcGuard.ok) return srcGuard;
  await presenceCursor(tabId, srcX, srcY, "dragging", false);
  const moved = await cdp.send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: srcX, y: srcY });
  if (!moved.ok) return actFail(moved.error.code, moved.error.message);
  const pressed = await cdp.send(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: srcX,
    y: srcY,
    button: "left",
    clickCount: 1
  });
  if (!pressed.ok) return actFail(pressed.error.code, pressed.error.message);
  if (ctx.cancelHandle) await markInFlight(ctx.cancelHandle.opId, tabId, "drag", { x: srcX, y: srcY });
  let released = false;
  let lastX = srcX;
  let lastY = srcY;
  let cancelledReason = null;
  let waypointsCompleted = 0;
  const runDrag = async () => {
    const dst = await resolveDragDestination(tabId, params, ctx);
    if (!dst.ok) return dst;
    const [dstX, dstY] = dst.result;
    for (let i = 1; i <= DRAG_WAYPOINT_COUNT; i++) {
      if (ctx.cancelHandle?.isCancelled()) {
        cancelledReason = ctx.cancelHandle.cancelReason();
        break;
      }
      const t = i / DRAG_WAYPOINT_COUNT;
      const x = lerp(srcX, dstX, t);
      const y = lerp(srcY, dstY, t);
      const wpGuard = await authorizeDispatchPoint(tabId, x, y, ctx);
      if (!wpGuard.ok) return wpGuard;
      lastX = x;
      lastY = y;
      void presenceCursor(tabId, x, y, "dragging", false);
      const wp = await cdp.send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 });
      if (!wp.ok) return actFail(wp.error.code, wp.error.message);
      waypointsCompleted = i;
      await sleep$1(DRAG_WAYPOINT_DELAY_MS);
    }
    if (cancelledReason) {
      return actFail(codeForCancelReason(cancelledReason), "drag cancelled");
    }
    const midDrag = await checkViewportStaleness(tabId, params.expectToViewport);
    if (midDrag) return midDrag;
    const dropGuard = await authorizeDispatchPoint(tabId, dstX, dstY, ctx);
    if (!dropGuard.ok) return dropGuard;
    lastX = dstX;
    lastY = dstY;
    const releaseResult = await cdp.send(tabId, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: dstX,
      y: dstY,
      button: "left",
      clickCount: 1
    });
    if (!releaseResult.ok) return actFail(releaseResult.error.code, releaseResult.error.message);
    released = true;
    return actOk(void 0);
  };
  let outcome;
  try {
    outcome = await runDrag();
  } catch (error) {
    outcome = actFail(ERROR_CODES.CDP_ERROR, describeError(error));
  }
  let recovered = false;
  if (!released) {
    recovered = await tryReleaseDragButton(tabId, lastX, lastY);
    if (!recovered && !outcome.ok && !cancelledReason) {
      outcome = actFail(
        outcome.error.code,
        `${outcome.error.message} — the left mouse button may still be held down on this tab (the automatic release attempt also failed); a real click there may behave unexpectedly (e.g. extend a selection or continue an unrelated drag) until it is released, for example by clicking anywhere on the page`
      );
    }
  }
  if (cancelledReason) {
    const releaseNote = released || recovered ? "button released" : "button may still be held down";
    outcome = actFail(
      codeForCancelReason(cancelledReason),
      `drag cancelled (${cancelledReason}) after ${waypointsCompleted} of ${DRAG_WAYPOINT_COUNT} waypoints; ${releaseNote}`
    );
  }
  if (ctx.cancelHandle) await clearInFlight(ctx.cancelHandle.opId);
  return outcome;
}
async function dispatchCharsAsKeys(tabId, text, delayMs, ctx) {
  for (const ch of Array.from(text)) {
    const sent = await dispatchKeyChord(tabId, ch === "\n" ? "enter" : ch, ctx);
    if (!sent.ok) return sent;
    if (delayMs > 0) await sleep$1(delayMs);
  }
  return actOk(void 0);
}
async function actType(tabId, params, ctx) {
  let point;
  if (params.selector) {
    const target = await resolveEditableRect(tabId, params.selector, ctx, ctx.sourceFrame);
    if (!target.ok) return target;
    const data = target.result;
    if (!data.editable || data.disabled || data.readOnly) {
      const { code, message } = formatRefusal("field_not_editable", { selector: params.selector });
      return actFail(code, message);
    }
    if (data.retargeted) {
      ctx.notes.push(`typed into the nearest editable ancestor of selector "${params.selector}", not the matched wrapper itself`);
    }
    point = centerOf(data.rect);
    ctx.typeReadbackSelector = params.selector;
  } else if (params.xy) {
    const stale = await checkViewportStaleness(tabId, params.expectViewport);
    if (stale) return stale;
    point = params.xy;
  } else {
    return actFail(ERROR_CODES.INVALID_PARAMS, "type requires a selector or xy");
  }
  await presenceCursor(tabId, point[0], point[1], "typing", true);
  const focused = await dispatchClick(tabId, point[0], point[1], ctx);
  if (!focused.ok) return focused;
  const mode = params.typeMode ?? "replace";
  if (mode === "replace") {
    const selectAll = await dispatchKeyChord(tabId, "ctrl+a", ctx);
    if (!selectAll.ok) return selectAll;
    const cleared = await dispatchKeyChord(tabId, "backspace", ctx);
    if (!cleared.ok) return cleared;
  } else {
    const positioned = await dispatchKeyChord(tabId, mode === "append" ? "ctrl+end" : "ctrl+home", ctx);
    if (!positioned.ok) return positioned;
  }
  const text = params.text ?? "";
  const dispatch = params.dispatch ?? "insert_text";
  if (dispatch === "keys") {
    const typed = await dispatchCharsAsKeys(tabId, text, params.keyDelayMs ?? DEFAULT_KEY_DELAY_MS, ctx);
    if (!typed.ok) return typed;
  } else {
    const focusGuard = await authorizeFocusedFrame(tabId, ctx);
    if (!focusGuard.ok) return focusGuard;
    const inserted = await cdp.send(tabId, "Input.insertText", { text });
    if (!inserted.ok) return actFail(inserted.error.code, inserted.error.message);
  }
  const shouldPressEnter = params.pressEnter ?? text.endsWith("\n");
  if (shouldPressEnter) {
    const entered = await dispatchKeyChord(tabId, "enter", ctx);
    if (!entered.ok) return entered;
  }
  return actOk(void 0);
}
async function redactedOptionList(options) {
  try {
    const policy = redactionPolicyOf(await getSettings());
    return options.map((o) => `"${redactText(o, policy).text}"`).join(", ");
  } catch {
    return options.map((o) => `"${o}"`).join(", ");
  }
}
async function actSelect(tabId, params, ctx) {
  if (!params.selector) return actFail(ERROR_CODES.INVALID_PARAMS, "select requires a selector");
  const result = await contentRequest(
    tabId,
    {
      target: "content",
      type: "selectOption",
      selector: localSelectorOf(params.selector, ctx.sourceFrame),
      text: params.text ?? "",
      // D5: exactly one of `text` (legacy)/`optionText` is ever meaningful to
      // the content script's selectOption -- see its own doc comment.
      optionText: params.optionText
    },
    frameIdOf(ctx.sourceFrame)
  );
  if (!result.ok || !result.data) return contentFail(result, "selectOption");
  const outcome = result.data;
  if (!outcome.matched) {
    if (outcome.reason === "invalid_selector") return invalidSelector(params.selector, outcome.detail ?? "");
    if ((outcome.reason === "no_matching_option" || outcome.reason === "ambiguous_option") && outcome.options) {
      const list = await redactedOptionList(outcome.options);
      return actFail(
        ERROR_CODES.CDP_ERROR,
        `select failed on ${params.selector}: ${outcome.reason} for option_text "${params.optionText ?? ""}"; options were: ${list}`
      );
    }
    return actFail(ERROR_CODES.CDP_ERROR, `select failed on ${params.selector}: ${outcome.reason}`);
  }
  noteChoice(ctx, params.selector, outcome.matchCount, outcome.chosenReason);
  return actOk(void 0);
}
async function actSubmit(tabId, params, ctx) {
  const result = await contentRequest(
    tabId,
    { target: "content", type: "submitTarget", ...params.selector ? { selector: localSelectorOf(params.selector, ctx.sourceFrame) } : {} },
    frameIdOf(ctx.sourceFrame)
  );
  if (!result.ok || !result.data) return contentFail(result, "submitTarget");
  const outcome = result.data;
  if (outcome.kind === "none") {
    if (outcome.invalid && params.selector) return invalidSelector(params.selector, outcome.invalid);
    return actFail(
      ERROR_CODES.CDP_ERROR,
      `submit: no form or submit control found${params.selector ? ` for ${params.selector}` : ""}`
    );
  }
  if (params.selector) noteChoice(ctx, params.selector, outcome.matchCount, outcome.chosenReason);
  if (outcome.kind === "control") {
    const [x, y] = shiftPoint(outcome.x, outcome.y, ctx.sourceFrame);
    await presenceCursor(tabId, x, y, "submitting", true);
    return dispatchClick(tabId, x, y, ctx);
  }
  return actOk(void 0);
}
async function actFill(tabId, params, ctx) {
  const fields = params.fields ?? [];
  if (fields.length === 0) return actFail(ERROR_CODES.INVALID_PARAMS, "fill requires at least one field");
  const result = await contentRequest(
    tabId,
    {
      target: "content",
      type: "fillFields",
      fields: fields.map((f) => ({ selector: localSelectorOf(f.selector, ctx.sourceFrame), value: f.value }))
    },
    frameIdOf(ctx.sourceFrame)
  );
  if (!result.ok || !result.data) return contentFail(result, "fillFields");
  const outcome = result.data;
  if (outcome.failedIndex !== void 0) {
    const field = fields[outcome.failedIndex];
    if (outcome.reason === "invalid_selector") return invalidSelector(field?.selector ?? "", outcome.detail ?? "");
    if (outcome.reason === "no_matching_option" || outcome.reason === "ambiguous_option") {
      const { code: code2, message: message2 } = formatRefusal("fill_option_ambiguous", { field_index: String(outcome.failedIndex) });
      const optionsNote = outcome.options && outcome.options.length > 0 ? ` options: ${outcome.options.join(", ")}` : "";
      return actFail(code2 ?? ERROR_CODES.FILL_OPTION_AMBIGUOUS, `${message2}${optionsNote}`);
    }
    const { code, message } = formatRefusal("fill_field_invalid", { field_index: String(outcome.failedIndex) });
    const detailNote = outcome.detail ? ` (${outcome.detail})` : "";
    return actFail(code ?? ERROR_CODES.FILL_FIELD_INVALID, `${message}${detailNote} [reason: ${outcome.reason}]`);
  }
  ctx.notes.push(`filled ${outcome.completed} field${outcome.completed === 1 ? "" : "s"}`);
  return actOk(void 0);
}
function isScrollMeasureRaw(value) {
  if (!value || typeof value !== "object") return false;
  const v = value;
  return (v.kind === "page" || v.kind === "container") && typeof v.heightBefore === "number" && typeof v.countBefore === "number" && typeof v.before === "object" && typeof v.after === "object" && typeof v.max === "object";
}
function isScrollMeasureOnly(value) {
  if (!value || typeof value !== "object") return false;
  const v = value;
  return typeof v.height === "number" && typeof v.count === "number";
}
const SCROLL_ACT_FUNCTION = `function (mode, deltaX, deltaY) {
  function findContainer(start) {
    var el = start;
    while (el && el.nodeType === 1 && el !== document.documentElement && el !== document.body) {
      var cs = window.getComputedStyle(el);
      var scrollableY = (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight > el.clientHeight;
      var scrollableX = (cs.overflowX === "auto" || cs.overflowX === "scroll") && el.scrollWidth > el.clientWidth;
      if (scrollableY || scrollableX) return el;
      el = el.parentElement;
    }
    return null;
  }
  var container = (this && this.nodeType === 1) ? findContainer(this) : null;
  var el = container || (document.scrollingElement || document.documentElement);
  var round = function (n) { return Math.round(n); };
  var before = { top: round(el.scrollTop), left: round(el.scrollLeft) };
  var maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
  var maxLeft = Math.max(0, el.scrollWidth - el.clientWidth);
  var heightBefore = el.scrollHeight;
  var countBefore = el.getElementsByTagName("*").length;
  if (mode === "top") el.scrollTo({ top: 0, left: el.scrollLeft });
  else if (mode === "bottom") el.scrollTo({ top: maxTop, left: el.scrollLeft });
  else if (mode === "next_page") el.scrollBy(0, el.clientHeight);
  else el.scrollBy(deltaX, deltaY);
  var after = { top: round(el.scrollTop), left: round(el.scrollLeft) };
  return {
    kind: container ? "container" : "page",
    before: before,
    after: after,
    max: { top: round(maxTop), left: round(maxLeft) },
    heightBefore: heightBefore,
    countBefore: countBefore
  };
}`;
const SCROLL_MEASURE_FUNCTION = `function () {
  var el = (this && this.nodeType === 1) ? this : (document.scrollingElement || document.documentElement);
  return { height: el.scrollHeight, count: el.getElementsByTagName("*").length };
}`;
async function scrollDispatchAndMeasure(tabId, hit, mode, deltaX, deltaY) {
  if (hit?.backendNodeId !== void 0) {
    const resolved = await cdp.send(tabId, "DOM.resolveNode", { backendNodeId: hit.backendNodeId });
    const objectId = resolved.ok ? resolved.result?.object?.objectId : void 0;
    if (objectId) {
      const called = await cdp.send(tabId, "Runtime.callFunctionOn", {
        functionDeclaration: SCROLL_ACT_FUNCTION,
        objectId,
        arguments: [{ value: mode }, { value: deltaX }, { value: deltaY }],
        returnByValue: true
      });
      if (called.ok) {
        const raw2 = called.result.result?.value;
        return actOk({ raw: isScrollMeasureRaw(raw2) ? raw2 : void 0, objectId });
      }
    }
  }
  const evaluated = await cdp.send(tabId, "Runtime.evaluate", {
    expression: `(${SCROLL_ACT_FUNCTION})(${JSON.stringify(mode)}, ${JSON.stringify(deltaX)}, ${JSON.stringify(deltaY)})`,
    returnByValue: true
  });
  if (!evaluated.ok) return actFail(evaluated.error.code, evaluated.error.message);
  const raw = evaluated.result.result?.value;
  return actOk({ raw: isScrollMeasureRaw(raw) ? raw : void 0, objectId: void 0 });
}
async function measureScrollOnce(tabId, objectId) {
  if (objectId) {
    const called = await cdp.send(tabId, "Runtime.callFunctionOn", {
      functionDeclaration: SCROLL_MEASURE_FUNCTION,
      objectId,
      returnByValue: true
    });
    if (!called.ok) return void 0;
    const raw2 = called.result.result?.value;
    return isScrollMeasureOnly(raw2) ? raw2 : void 0;
  }
  const evaluated = await cdp.send(tabId, "Runtime.evaluate", {
    expression: `(${SCROLL_MEASURE_FUNCTION})()`,
    returnByValue: true
  });
  if (!evaluated.ok) return void 0;
  const raw = evaluated.result.result?.value;
  return isScrollMeasureOnly(raw) ? raw : void 0;
}
async function waitForGrowth(tabId, heightBefore, countBefore, budgetMs, measure, settleFrameId) {
  const start = Date.now();
  if (budgetMs <= 0) return { heightAfter: heightBefore, contentGrew: false, waitedMs: 0 };
  const deadline = start + budgetMs;
  let lastHeight = heightBefore;
  let grew = false;
  while (Date.now() < deadline) {
    const m = await measure();
    if (m) {
      lastHeight = m.height;
      if (m.height > heightBefore || m.count > countBefore) {
        grew = true;
        break;
      }
    }
    await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
  }
  if (grew) {
    const remaining = remainingUntil(deadline);
    if (remaining > 0) {
      await contentRequestWithTimeout(
        tabId,
        { target: "content", type: "settleQuiet", timeoutMs: remaining },
        remaining,
        settleFrameId ?? 0
      );
    }
    const finalMeasure = await measure();
    if (finalMeasure) lastHeight = finalMeasure.height;
  }
  return { heightAfter: lastHeight, contentGrew: grew, waitedMs: Date.now() - start };
}
function buildScrollActResult(raw, growth) {
  const moved = raw.after.top !== raw.before.top || raw.after.left !== raw.before.left;
  return {
    target: raw.kind,
    before: raw.before,
    after: raw.after,
    max: raw.max,
    moved,
    at_top: raw.after.top <= 2,
    at_bottom: growth.contentGrew ? false : raw.after.top >= raw.max.top - 2,
    content_grew: growth.contentGrew,
    height_before: raw.heightBefore,
    height_after: growth.heightAfter,
    waited_ms: growth.waitedMs
  };
}
async function scrollAtPoint(tabId, x, y, mode, deltaX, deltaY, ctx, waitForGrowthMs, settleFrameId) {
  if (ctx.frameTreeCache === void 0) {
    ctx.frameTreeCache = await fetchFrameOriginTree(tabId);
  }
  const tree = ctx.frameTreeCache;
  const hit = await hitTestAtPoint(tabId, x, y);
  if (hit && tree) {
    const landingFrameId = await resolveLandingFrameId(tabId, hit, tree);
    if (landingFrameId !== null && landingFrameId !== tree.rootId) {
      const raw = rawFrameInfo(tree, landingFrameId);
      if (!raw.isExtensionOverlay) {
        const guard = authorizeLandingFrame(tree, ctx, landingFrameId, "scroll_frame_denied");
        if (!guard.ok) return guard;
      }
    }
  }
  const dispatched = await scrollDispatchAndMeasure(tabId, hit, mode, deltaX, deltaY);
  if (!dispatched.ok) return dispatched;
  if (!dispatched.result.raw) return actOk(void 0);
  const growth = await waitForGrowth(
    tabId,
    dispatched.result.raw.heightBefore,
    dispatched.result.raw.countBefore,
    waitForGrowthMs,
    () => measureScrollOnce(tabId, dispatched.result.objectId),
    settleFrameId
  );
  return actOk(buildScrollActResult(dispatched.result.raw, growth));
}
const MAX_WAIT_FOR_GROWTH_MS = 1e4;
const DEFAULT_WAIT_FOR_GROWTH_MS = 1500;
function clampWaitForGrowthMs(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_WAIT_FOR_GROWTH_MS;
  return Math.min(Math.max(0, Math.round(value)), MAX_WAIT_FOR_GROWTH_MS);
}
function resolveScrollModeAndDelta(params) {
  if (params.to === "top" || params.to === "bottom" || params.to === "next_page") {
    return { mode: params.to, deltaX: 0, deltaY: 0 };
  }
  if (params.xy) return { mode: "delta", deltaX: params.xy[0], deltaY: params.xy[1] };
  return { mode: "next_page", deltaX: 0, deltaY: 0 };
}
async function actScroll(tabId, params, ctx) {
  const waitForGrowthMs = clampWaitForGrowthMs(params.waitForGrowthMs);
  if (params.selector) {
    const hasScrollRequest = params.to !== void 0 || params.xy !== void 0;
    if (!hasScrollRequest) {
      const rect = await resolveRect(tabId, params.selector, ctx, ctx.sourceFrame);
      if (!rect.ok) return rect;
      const [x, y] = centerOf(rect.result);
      void presenceCursor(tabId, x, y, "scrolling", false);
      const measured = await scrollAtPoint(tabId, x, y, "delta", 0, 0, ctx, waitForGrowthMs, ctx.sourceFrame?.frameId);
      if (!measured.ok) return measured;
      if (measured.result) ctx.scrollResult = measured.result;
      return actOk(void 0);
    }
    const { mode: mode2, deltaX: deltaX2, deltaY: deltaY2 } = resolveScrollModeAndDelta(params);
    return scrollSelectorContainer(tabId, params.selector, params.backendNodeId, mode2, deltaX2, deltaY2, waitForGrowthMs, ctx);
  }
  const { mode, deltaX, deltaY } = resolveScrollModeAndDelta(params);
  const metrics = await cdp.send(
    tabId,
    "Page.getLayoutMetrics"
  );
  const width = metrics.ok ? metrics.result.cssLayoutViewport?.clientWidth ?? 800 : 800;
  const height = metrics.ok ? metrics.result.cssLayoutViewport?.clientHeight ?? 600 : 600;
  void presenceCursor(tabId, width / 2, height / 2, "scrolling", false);
  const outcome = await scrollAtPoint(tabId, width / 2, height / 2, mode, deltaX, deltaY, ctx, waitForGrowthMs, ctx.sourceFrame?.frameId);
  if (!outcome.ok) return outcome;
  if (outcome.result) ctx.scrollResult = outcome.result;
  return actOk(void 0);
}
async function scrollSelectorContainer(tabId, selector, gatewayBackendNodeId, mode, deltaX, deltaY, waitForGrowthMs, ctx) {
  const rect = await resolveRect(tabId, selector, ctx, ctx.sourceFrame, false);
  if (!rect.ok) return rect;
  const [x, y] = centerOf(rect.result);
  void presenceCursor(tabId, x, y, "scrolling", false);
  const inTopFrame = frameIdOf(ctx.sourceFrame) === 0;
  let backendNodeId = inTopFrame ? gatewayBackendNodeId : void 0;
  if (backendNodeId === void 0 && inTopFrame && !selector.includes(">>>")) {
    backendNodeId = await resolveNodeHandleOnDemand(tabId, selector);
  }
  if (backendNodeId !== void 0) {
    const dispatched = await scrollDispatchAndMeasure(tabId, { backendNodeId }, mode, deltaX, deltaY);
    if (!dispatched.ok) return dispatched;
    if (dispatched.result.raw) {
      const growth = await waitForGrowth(
        tabId,
        dispatched.result.raw.heightBefore,
        dispatched.result.raw.countBefore,
        waitForGrowthMs,
        () => measureScrollOnce(tabId, dispatched.result.objectId),
        ctx.sourceFrame?.frameId
      );
      ctx.scrollResult = buildScrollActResult(dispatched.result.raw, growth);
    }
    return actOk(void 0);
  }
  const outcome = await scrollAtPoint(tabId, x, y, mode, deltaX, deltaY, ctx, waitForGrowthMs, ctx.sourceFrame?.frameId);
  if (!outcome.ok) return outcome;
  if (outcome.result) ctx.scrollResult = outcome.result;
  return actOk(void 0);
}
async function actKey(tabId, params, ctx) {
  if (!params.text) return actFail(ERROR_CODES.INVALID_PARAMS, "key requires the chord/key name in `text`");
  if (params.selector || params.xy) {
    const point = await resolvePoint(tabId, params, ctx);
    if (!point.ok) return point;
    await presenceCursor(tabId, point.result[0], point.result[1], "pressing key", true);
    const focused = await dispatchClick(tabId, point.result[0], point.result[1], ctx);
    if (!focused.ok) return focused;
  }
  return dispatchKeyChord(tabId, params.text, ctx);
}
async function actNavigate(tabId, params, timeoutMs) {
  if (!params.url) return actFail(ERROR_CODES.INVALID_PARAMS, "navigate requires a url");
  const navigated = await cdp.send(tabId, "Page.navigate", { url: params.url });
  if (!navigated.ok) return actFail(navigated.error.code, navigated.error.message);
  if (navigated.result.errorText) {
    return actFail(ERROR_CODES.CDP_ERROR, `navigate to ${params.url} failed: ${navigated.result.errorText}`);
  }
  const loaded = await cdp.waitForEvent(tabId, "Page.loadEventFired", timeoutMs);
  if (!loaded) {
    return actFail(ERROR_CODES.TIMEOUT, `navigation to ${params.url} did not fire a load event within ${timeoutMs}ms`);
  }
  return actOk(void 0);
}
async function actWaitFor(tabId, params, timeoutMs, ctx) {
  if (params.condition) return actWaitForCondition(tabId, params.condition, ctx);
  return actWaitForLegacy(tabId, params, timeoutMs);
}
async function actWaitForLegacy(tabId, params, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  const negate = params.selector?.startsWith("!") ?? false;
  const selector = params.selector ? negate ? params.selector.slice(1) : params.selector : void 0;
  if (!selector && !params.text) {
    return actFail(ERROR_CODES.INVALID_PARAMS, "wait_for requires a selector or text");
  }
  const message = selector ? { target: "content", type: "selectorPresent", selector } : { target: "content", type: "textPresent", text: params.text ?? "" };
  for (; ; ) {
    const checked = await contentRequest(tabId, message);
    if (!checked.ok || !checked.data) return contentFail(checked, message.type);
    if (checked.data.invalid && selector) return invalidSelector(selector, checked.data.invalid);
    const present = Boolean(checked.data.present);
    if (present !== negate) return actOk(void 0);
    if (Date.now() >= deadline) {
      const what = selector ? `selector "${selector}" to ${negate ? "disappear" : "appear"}` : `text "${params.text}"`;
      return actFail(ERROR_CODES.TIMEOUT, `wait_for: ${what} not satisfied within ${timeoutMs}ms`);
    }
    await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
  }
}
const WAIT_FOR_CONDITION_DEFAULT_MS = 5e3;
const WAIT_FOR_CONDITION_MAX_MS = 3e4;
const NETWORK_IDLE_QUIET_MS = 500;
const NETWORK_IDLE_POLL_MS = 100;
function clampConditionTimeoutMs(ms) {
  return Math.max(0, Math.min(WAIT_FOR_CONDITION_MAX_MS, ms ?? WAIT_FOR_CONDITION_DEFAULT_MS));
}
function describeWaitForCondition(condition) {
  switch (condition.type) {
    case "text_appears":
      return `text_appears "${condition.text ?? ""}"`;
    case "text_gone":
      return `text_gone "${condition.text ?? ""}"`;
    case "element_visible":
      return `element_visible ${condition.selector ?? ""}`;
    case "element_gone":
      return `element_gone ${condition.selector ?? ""}`;
    case "url_matches":
      return `url_matches ${condition.pattern ?? ""}`;
    case "network_idle":
      return "network_idle";
    default:
      return `unknown condition ${String(condition.type)}`;
  }
}
function urlMatcher(pattern) {
  const m = /^\/(.*)\/([a-z]*)$/.exec(pattern);
  if (m) {
    try {
      const re = new RegExp(m[1], m[2]);
      return (url) => re.test(url);
    } catch {
    }
  }
  return (url) => url.includes(pattern);
}
async function actWaitForCondition(tabId, condition, ctx) {
  const conditionTimeoutMs = clampConditionTimeoutMs(condition.timeoutMs);
  const deadline = Date.now() + conditionTimeoutMs;
  const startedAt = Date.now();
  const label = describeWaitForCondition(condition);
  const finish = (met, note) => {
    ctx.waitForResult = { met, waited_ms: Date.now() - startedAt, condition: label };
    if (note) ctx.notes.push(note);
    if (!met) {
      ctx.notes.push(`wait_for: ${label} not satisfied within ${conditionTimeoutMs}ms`);
    }
    return actOk(void 0);
  };
  switch (condition.type) {
    case "text_appears":
    case "text_gone": {
      if (!condition.text) {
        return actFail(ERROR_CODES.INVALID_PARAMS, `condition.type '${condition.type}' needs condition.text`);
      }
      const negate = condition.type === "text_gone";
      for (; ; ) {
        const checked = await contentRequest(tabId, {
          target: "content",
          type: "textPresent",
          text: condition.text,
          caseInsensitive: true
        });
        if (checked.ok && checked.data) {
          const present = Boolean(checked.data.present);
          if (present !== negate) return finish(true);
        }
        if (Date.now() >= deadline) return finish(false);
        await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
      }
    }
    case "element_visible":
    case "element_gone": {
      if (!condition.selector) {
        return actFail(ERROR_CODES.INVALID_PARAMS, `condition.type '${condition.type}' needs condition.selector`);
      }
      const negate = condition.type === "element_gone";
      for (; ; ) {
        const checked = await contentRequest(tabId, {
          target: "content",
          type: "selectorPresent",
          selector: condition.selector
        });
        if (checked.ok && checked.data) {
          if (checked.data.invalid) return invalidSelector(condition.selector, checked.data.invalid);
          const present = Boolean(checked.data.present);
          if (present !== negate) return finish(true);
        }
        if (Date.now() >= deadline) return finish(false);
        await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
      }
    }
    case "url_matches": {
      if (!condition.pattern) {
        return actFail(ERROR_CODES.INVALID_PARAMS, "condition.type 'url_matches' needs condition.pattern");
      }
      const matches = urlMatcher(condition.pattern);
      for (; ; ) {
        const tab = await tabUrlTitle(tabId);
        if (matches(tab.url)) return finish(true);
        if (Date.now() >= deadline) return finish(false);
        await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
      }
    }
    case "network_idle":
      return actWaitForNetworkIdle(tabId, deadline, finish);
    default:
      return actFail(ERROR_CODES.INVALID_PARAMS, `unknown wait_for condition type: ${String(condition.type)}`);
  }
}
async function actWaitForNetworkIdle(tabId, deadline, finish) {
  if (!cdp.isAttached(tabId)) {
    const remaining = remainingUntil(deadline);
    const result = await contentRequestWithTimeout(
      tabId,
      { target: "content", type: "settleQuiet", timeoutMs: remaining },
      remaining,
      0
    );
    const settled = Boolean(result.ok && result.data?.settled === true);
    return finish(settled, "network_idle is not observable in limited (no-debugger) mode; waited for the DOM to go quiet instead");
  }
  const inFlight = /* @__PURE__ */ new Set();
  let lastActivityAt = Date.now();
  const unsubscribe = cdp.subscribe(
    tabId,
    ["Network.requestWillBeSent", "Network.loadingFinished", "Network.loadingFailed"],
    (method, eventParams) => {
      const requestId = typeof eventParams.requestId === "string" ? eventParams.requestId : void 0;
      if (!requestId) return;
      if (method === "Network.requestWillBeSent") inFlight.add(requestId);
      else inFlight.delete(requestId);
      lastActivityAt = Date.now();
    }
  );
  try {
    for (; ; ) {
      const quietFor = Date.now() - lastActivityAt;
      if (inFlight.size === 0 && quietFor >= NETWORK_IDLE_QUIET_MS) return finish(true);
      if (Date.now() >= deadline) return finish(false);
      await sleep$1(Math.min(NETWORK_IDLE_POLL_MS, remainingUntil(deadline)));
    }
  } finally {
    unsubscribe();
  }
}
function limitedActRequest(tabId, kind, params, frame) {
  return contentRequest(tabId, { target: "content", type: "limitedAct", kind, ...params }, frameIdOf(frame));
}
function limitedNotFound(data, selector) {
  if (data.invalid) return invalidSelector(selector, data.invalid);
  return actFail(ERROR_CODES.CDP_ERROR, `selector not found: ${selector}`);
}
async function limitedActClick(tabId, params, ctx) {
  if (!params.selector) {
    return actFail(
      ERROR_CODES.INVALID_PARAMS,
      "click by bare xy needs chrome.debugger, and this tab is currently shared in limited mode -- target the element by idx/selector instead"
    );
  }
  const selector = localSelectorOf(params.selector, ctx.sourceFrame);
  const result = await limitedActRequest(tabId, "click", { selector }, ctx.sourceFrame);
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(click)");
  if (!result.data.found) return limitedNotFound(result.data, params.selector);
  if (result.data.rect) {
    const [x, y] = centerOf(shiftRect(result.data.rect, ctx.sourceFrame));
    await presenceCursor(tabId, x, y, "clicking", true);
  }
  noteChoice(ctx, params.selector, result.data.matchCount, result.data.chosenReason);
  if (result.data.description) {
    ctx.hitInfo = { role: result.data.description.role, name: result.data.description.name, matched: true };
  }
  return actOk(void 0);
}
async function limitedActHover(tabId, params, ctx) {
  if (!params.selector) {
    return actFail(
      ERROR_CODES.INVALID_PARAMS,
      "hover by bare xy needs chrome.debugger, and this tab is currently shared in limited mode -- target the element by idx/selector instead"
    );
  }
  const selector = localSelectorOf(params.selector, ctx.sourceFrame);
  const result = await limitedActRequest(tabId, "hover", { selector }, ctx.sourceFrame);
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(hover)");
  if (!result.data.found) return limitedNotFound(result.data, params.selector);
  if (result.data.rect) {
    const [x, y] = centerOf(shiftRect(result.data.rect, ctx.sourceFrame));
    await presenceCursor(tabId, x, y, "hovering", false);
  }
  noteChoice(ctx, params.selector, result.data.matchCount, result.data.chosenReason);
  if (result.data.description) {
    ctx.hitInfo = { role: result.data.description.role, name: result.data.description.name, matched: true };
  }
  const requested = params.hoverMs ?? HOVER_DEFAULT_MS;
  const hoverMs = Math.min(Math.max(requested, 0), HOVER_MAX_MS);
  if (hoverMs !== requested) ctx.notes.push(`hover_ms ${requested} is out of range (0-${HOVER_MAX_MS}); clamped to ${hoverMs}`);
  await sleep$1(hoverMs);
  return actOk(void 0);
}
async function limitedActType(tabId, params, ctx) {
  if (!params.selector) {
    return actFail(
      ERROR_CODES.INVALID_PARAMS,
      "type by bare xy needs chrome.debugger, and this tab is currently shared in limited mode -- target the field by idx/selector instead"
    );
  }
  const selector = localSelectorOf(params.selector, ctx.sourceFrame);
  const result = await limitedActRequest(
    tabId,
    "type",
    { selector, text: params.text ?? "", typeMode: params.typeMode ?? "replace" },
    ctx.sourceFrame
  );
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(type)");
  if (!result.data.found) return limitedNotFound(result.data, params.selector);
  if (!result.data.editable || result.data.disabled || result.data.readOnly) {
    const { code, message } = formatRefusal("field_not_editable", { selector: params.selector });
    return actFail(code, message);
  }
  if (result.data.retargeted) {
    ctx.notes.push(`typed into the nearest editable ancestor of selector "${params.selector}", not the matched wrapper itself`);
  }
  ctx.typeReadbackSelector = params.selector;
  return actOk(void 0);
}
async function limitedActSelect(tabId, params, ctx) {
  return actSelect(tabId, params, ctx);
}
async function limitedActSubmit(tabId, params, ctx) {
  const result = await limitedActRequest(
    tabId,
    "submit",
    params.selector ? { selector: localSelectorOf(params.selector, ctx.sourceFrame) } : {},
    ctx.sourceFrame
  );
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(submit)");
  if (!result.data.found) {
    if (result.data.invalid && params.selector) return invalidSelector(params.selector, result.data.invalid);
    return actFail(ERROR_CODES.CDP_ERROR, `submit: no form or submit control found${params.selector ? ` for ${params.selector}` : ""}`);
  }
  if (params.selector) noteChoice(ctx, params.selector, result.data.matchCount, result.data.chosenReason);
  return actOk(void 0);
}
function resolveLimitedScrollTo(params) {
  if (params.to === "top" || params.to === "bottom" || params.to === "next_page") {
    return { to: params.to, dx: 0, dy: 0 };
  }
  if (params.xy) return { to: void 0, dx: params.xy[0], dy: params.xy[1] };
  return { to: "next_page", dx: 0, dy: 0 };
}
async function limitedActScroll(tabId, params, ctx) {
  const waitForGrowthMs = clampWaitForGrowthMs(params.waitForGrowthMs);
  if (params.selector) {
    const selector = localSelectorOf(params.selector, ctx.sourceFrame);
    const hasScrollRequest = params.to !== void 0 || params.xy !== void 0;
    const to2 = params.to === "top" || params.to === "bottom" || params.to === "next_page" ? params.to : void 0;
    const dx2 = hasScrollRequest ? params.xy?.[0] : void 0;
    const dy2 = hasScrollRequest ? params.xy?.[1] : void 0;
    const result2 = await limitedActRequest(tabId, "scroll", { selector, to: to2, dx: dx2, dy: dy2, waitForGrowthMs }, ctx.sourceFrame);
    if (!result2.ok || !result2.data) return contentFail(result2, "limitedAct(scroll)");
    if (!result2.data.found) return limitedNotFound(result2.data, params.selector);
    if (result2.data.scroll) ctx.scrollResult = result2.data.scroll;
    return actOk(void 0);
  }
  const { to, dx, dy } = resolveLimitedScrollTo(params);
  const result = await limitedActRequest(tabId, "scroll", { dx, dy, to, waitForGrowthMs }, ctx.sourceFrame);
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(scroll)");
  if (result.data.found && result.data.scroll) ctx.scrollResult = result.data.scroll;
  return actOk(void 0);
}
async function limitedActKey(tabId, params, ctx) {
  if (!params.text) return actFail(ERROR_CODES.INVALID_PARAMS, "key requires the chord/key name in `text`");
  const selector = params.selector ? localSelectorOf(params.selector, ctx.sourceFrame) : void 0;
  const result = await limitedActRequest(tabId, "key", { selector, keyName: params.text }, ctx.sourceFrame);
  if (!result.ok || !result.data) return contentFail(result, "limitedAct(key)");
  if (params.selector && !result.data.found) return limitedNotFound(result.data, params.selector);
  return actOk(void 0);
}
async function limitedActNavigate(tabId, params, timeoutMs) {
  if (!params.url) return actFail(ERROR_CODES.INVALID_PARAMS, "navigate requires a url");
  try {
    await chrome.tabs.update(tabId, { url: params.url });
  } catch (error) {
    return actFail(ERROR_CODES.CDP_ERROR, `navigate to ${params.url} failed: ${describeError(error)}`);
  }
  const loaded = await waitForTabComplete(tabId, timeoutMs);
  if (!loaded) return actFail(ERROR_CODES.TIMEOUT, `navigation to ${params.url} did not settle within ${timeoutMs}ms`);
  return actOk(void 0);
}
async function actHistory(tabId, kind, timeoutMs) {
  try {
    if (kind === "back") await chrome.tabs.goBack(tabId);
    else if (kind === "forward") await chrome.tabs.goForward(tabId);
    else await chrome.tabs.reload(tabId);
  } catch (error) {
    return actFail(ERROR_CODES.CDP_ERROR, `${kind} failed: ${describeError(error)}`);
  }
  await waitForTabComplete(tabId, timeoutMs);
  return actOk(void 0);
}
function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve(ok);
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") finish(true);
    };
    chrome.tabs.onUpdated.addListener(listener);
    const timer = setTimeout(() => finish(false), Math.max(0, timeoutMs));
    void chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") finish(true);
    }).catch(() => finish(false));
  });
}
function refuseLimitedOnly(capability) {
  const { code, message } = formatRefusal("limited_mode_capability_unavailable", { capability });
  return actFail(code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, message);
}
async function performAction(tabId, params, timeoutMs, ctx) {
  const limited = !cdp.isAttached(tabId);
  noteAction(tabId, params.action);
  switch (params.action) {
    case "click":
      return limited ? limitedActClick(tabId, params, ctx) : actClick(tabId, params, ctx);
    case "type":
      return limited ? limitedActType(tabId, params, ctx) : actType(tabId, params, ctx);
    case "select":
      return limited ? limitedActSelect(tabId, params, ctx) : actSelect(tabId, params, ctx);
    case "submit":
      return limited ? limitedActSubmit(tabId, params, ctx) : actSubmit(tabId, params, ctx);
    case "scroll":
      return limited ? limitedActScroll(tabId, params, ctx) : actScroll(tabId, params, ctx);
    case "key":
      return limited ? limitedActKey(tabId, params, ctx) : actKey(tabId, params, ctx);
    case "navigate":
      return limited ? limitedActNavigate(tabId, params, timeoutMs) : actNavigate(tabId, params, timeoutMs);
    case "wait_for":
      return actWaitFor(tabId, params, timeoutMs, ctx);
    case "hover":
      return limited ? limitedActHover(tabId, params, ctx) : actHover(tabId, params, ctx);
    case "drag":
      return limited ? refuseLimitedOnly("drag") : actDrag(tabId, params, ctx);
    case "upload":
      return limited ? refuseLimitedOnly("upload") : actUpload(tabId, params, ctx);
    // fill (speedimprovements.md A2): CDP-free, so it is mode-agnostic like
    // wait_for above -- there is no limitedActFill because actFill never
    // touches chrome.debugger in the first place.
    case "fill":
      return actFill(tabId, params, ctx);
    // back/forward/reload (spec item 2): mode-agnostic, chrome.tabs only --
    // same function either way, see actHistory's own comment.
    case "back":
      return actHistory(tabId, "back", timeoutMs);
    case "forward":
      return actHistory(tabId, "forward", timeoutMs);
    case "reload":
      return actHistory(tabId, "reload", timeoutMs);
    default:
      return actFail(ERROR_CODES.INVALID_PARAMS, `unknown action: ${String(params.action)}`);
  }
}
async function settle(tabId, timeoutMs, cancelHandle, frameId) {
  const startedAt = Date.now();
  const deadline = Date.now() + timeoutMs;
  const remaining = remainingUntil(deadline);
  if (remaining > 0 && !cancelHandle?.isCancelled()) {
    const requests = [
      contentRequestWithTimeout(tabId, { target: "content", type: "settleQuiet", timeoutMs: remaining }, remaining, 0)
    ];
    if (frameId !== void 0 && frameId !== 0) {
      requests.push(contentRequestWithTimeout(tabId, { target: "content", type: "settleQuiet", timeoutMs: remaining }, remaining, frameId));
    }
    const results = await Promise.all(requests);
    if (results.every((r) => r.ok && typeof r.data?.settled === "boolean")) return { settledMs: Date.now() - startedAt };
  }
  if (cancelHandle?.isCancelled()) return { settledMs: Date.now() - startedAt };
  await settleViaFingerprint(tabId, deadline, cancelHandle);
  return { settledMs: Date.now() - startedAt };
}
async function settleViaFingerprint(tabId, deadline, cancelHandle) {
  const quietWindow = adaptiveQuietWindowMs(remainingUntil(deadline));
  await sleep$1(Math.min(SETTLE_INITIAL_DELAY_MS, remainingUntil(deadline)));
  let lastFingerprint;
  let lastChangeAt = Date.now();
  while (remainingUntil(deadline) > 0) {
    if (!cdp.isAttached(tabId)) return;
    if (cancelHandle?.isCancelled()) return;
    const fingerprint = await evaluate(tabId, FINGERPRINT_EXPRESSION);
    const now = Date.now();
    if (!fingerprint.ok) {
      lastFingerprint = void 0;
      lastChangeAt = now;
    } else if (fingerprint.result === lastFingerprint) {
      if (now - lastChangeAt >= quietWindow) return;
    } else {
      lastFingerprint = fingerprint.result;
      lastChangeAt = now;
    }
    await sleep$1(Math.min(SETTLE_POLL_MS, remainingUntil(deadline)));
  }
}
async function resolveDocumentRootId(tabId) {
  const doc = await cdp.send(tabId, "DOM.getDocument", { depth: 0 });
  return doc.ok ? doc.result?.root?.nodeId : void 0;
}
async function resolveBackendNodeIdForSelector(tabId, rootId, selector) {
  const found = await cdp.send(tabId, "DOM.querySelector", { nodeId: rootId, selector });
  if (!found.ok || typeof found.result?.nodeId !== "number") return void 0;
  const described = await cdp.send(tabId, "DOM.describeNode", { nodeId: found.result.nodeId });
  const backendNodeId = described.ok ? described.result?.node?.backendNodeId : void 0;
  return typeof backendNodeId === "number" ? backendNodeId : void 0;
}
async function resolveNodeHandleOnDemand(tabId, selector) {
  if (selector.includes(">>>") || isFrameQualified(selector)) return void 0;
  const rootId = await resolveDocumentRootId(tabId);
  if (typeof rootId !== "number") return void 0;
  return resolveBackendNodeIdForSelector(tabId, rootId, selector);
}
const NODE_HANDLE_BUDGET_MS = 150;
const NODE_HANDLE_MAX_ENTRIES = 100;
const NODE_HANDLE_CONCURRENCY = 16;
async function resolveBackendNodeIds(tabId, indexMap) {
  const out = {};
  if (!indexMap) return { nodeMap: out, skipped: 0 };
  const eligible = Object.entries(indexMap).filter(([, selector]) => !selector.includes(">>>") && !isFrameQualified(selector));
  const capped = eligible.slice(0, NODE_HANDLE_MAX_ENTRIES);
  let skipped = eligible.length - capped.length;
  if (capped.length === 0) return { nodeMap: out, skipped };
  const resolvedRootId = await resolveDocumentRootId(tabId);
  if (typeof resolvedRootId !== "number") return { nodeMap: out, skipped: eligible.length };
  const rootId = resolvedRootId;
  const deadline = Date.now() + NODE_HANDLE_BUDGET_MS;
  let cursor = 0;
  const claim = () => {
    if (Date.now() >= deadline) return void 0;
    if (cursor >= capped.length) return void 0;
    return capped[cursor++];
  };
  async function worker() {
    for (let entry = claim(); entry; entry = claim()) {
      const [idxStr, selector] = entry;
      const backendNodeId = await resolveBackendNodeIdForSelector(tabId, rootId, selector);
      if (typeof backendNodeId === "number") out[Number(idxStr)] = backendNodeId;
    }
  }
  const workerCount = Math.min(NODE_HANDLE_CONCURRENCY, capped.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  skipped += capped.length - cursor;
  return { nodeMap: out, skipped };
}
async function captureSnapshot(tabId, diffAgainst, existingIndexMap, startIndex) {
  const result = await contentRequest(tabId, {
    target: "content",
    type: "snapshot",
    budgetBytes: DIFF_SNAPSHOT_BUDGET_BYTES,
    ...diffAgainst !== void 0 ? { diffAgainst } : {},
    ...existingIndexMap !== void 0 ? { existingIndexMap } : {},
    ...startIndex !== void 0 ? { startIndex } : {}
  });
  return result.ok ? result.data : void 0;
}
async function captureActScreenshotAfter(tabId, opts) {
  const format = resolveFormat(opts.format);
  const scale = resolveScale(opts.scale);
  const quality = format === "jpeg" ? resolveQuality(void 0) : void 0;
  if (!cdp.isAttached(tabId)) {
    if (opts.region) {
      return { note: "screenshot_after: limited mode captures the whole viewport only; region was ignored" };
    }
    try {
      const tab = await chrome.tabs.get(tabId);
      if (!tab.active || typeof tab.windowId !== "number") {
        return { note: "screenshot_after: tab is not the active tab of its window; limited mode can only capture the focused tab" };
      }
      const hidden2 = await presenceHideForCapture(tabId);
      let dataUrl;
      try {
        const captureOpts = { format };
        if (quality !== void 0) captureOpts.quality = quality;
        dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, captureOpts);
      } finally {
        void presenceRestoreAfterCapture(tabId);
      }
      const png_b64 = dataUrl.startsWith("data:") ? dataUrl.slice(dataUrl.indexOf(",") + 1) : dataUrl;
      const image = await decodeImageDims(png_b64, mimeOf(format));
      return {
        shot: { png_b64, format, scale: 1, image },
        ...hidden2 ? {} : { note: "screenshot_after: the presence overlay could not be confirmed hidden; it may appear in the image" }
      };
    } catch (error) {
      return { note: `screenshot_after failed: ${describeError(error)}` };
    }
  }
  const metrics = await cdp.send(tabId, "Page.getLayoutMetrics");
  let cdpClip;
  let notes;
  if (opts.region && metrics.ok) {
    const placed = placeClip(opts.region, pageGeometry(metrics.result));
    if (placed) {
      cdpClip = { ...placed.clip, scale };
      if (placed.grown) notes = `screenshot_after: clip grown to the minimum capture size`;
    } else {
      return { note: "screenshot_after: region lies entirely outside the page" };
    }
  } else if (scale !== 1 && metrics.ok) {
    cdpClip = { ...captureAreaClip(pageGeometry(metrics.result), false), scale };
  }
  const hidden = await presenceHideForCapture(tabId);
  try {
    const shot = await cdp.send(tabId, "Page.captureScreenshot", {
      format,
      ...quality !== void 0 ? { quality } : {},
      ...cdpClip ? { clip: cdpClip } : {}
    });
    if (!shot.ok) return { note: `screenshot_after failed: ${shot.error.message}` };
    const image = await decodeImageDims(shot.result.data, mimeOf(format));
    return {
      shot: { png_b64: shot.result.data, format, scale, image },
      note: [notes, hidden ? void 0 : "screenshot_after: the presence overlay could not be confirmed hidden; it may appear in the image"].filter(Boolean).join("; ") || void 0
    };
  } finally {
    void presenceRestoreAfterCapture(tabId);
  }
}
async function captureScopedSnapshotAfter(tabId, scope) {
  const result = await contentRequest(tabId, {
    target: "content",
    type: "snapshot",
    budgetBytes: DIFF_SNAPSHOT_BUDGET_BYTES,
    ...scope.selector !== void 0 ? { selector: scope.selector } : {},
    ...scope.dialogOnly ? { dialogOnly: true } : {},
    ...scope.viewportOnly ? { viewportOnly: true } : {}
  });
  return result.ok ? result.data : void 0;
}
async function tabUrlTitle(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url ?? "", title: tab.title ?? "" };
  } catch {
    return { url: "", title: "" };
  }
}
function hitIdxFromIndexMeta(indexMeta, hit) {
  if (!indexMeta) return void 0;
  let found;
  for (const [key, meta] of Object.entries(indexMeta)) {
    if (meta.role !== hit.role || normalizeName(meta.name) !== normalizeName(hit.name)) continue;
    if (found !== void 0) return void 0;
    found = Number(key);
  }
  return found;
}
function buildActResult(before, after, fallback, notes = [], dialogs = []) {
  const url = after?.url ?? fallback.url;
  const title = after?.title ?? fallback.title;
  const body = after?.diff ?? after?.tree ?? "";
  const diff = notes.length > 0 ? [...notes.map((n) => `note: ${n}`), ...body ? [body] : []].join("\n") : body;
  const indexMapField = after?.tree !== void 0 && after?.indexMap !== void 0 ? { indexMap: after.indexMap } : {};
  const indexMetaField = after?.tree !== void 0 && after?.indexMeta !== void 0 ? { indexMeta: after.indexMeta } : {};
  const viewportField = after?.tree !== void 0 && after?.viewport !== void 0 ? { viewport: after.viewport } : {};
  const dialogsField = dialogs.length > 0 ? { dialogs } : {};
  if (before?.tree !== void 0 && after?.tree !== void 0) {
    return {
      changed: before.tree !== after.tree,
      diff,
      url,
      title,
      ...indexMapField,
      ...indexMetaField,
      ...viewportField,
      ...dialogsField
    };
  }
  if (after?.tree !== void 0) {
    return {
      changed: true,
      diff,
      url,
      title,
      ...indexMapField,
      ...indexMetaField,
      ...viewportField,
      ...dialogsField
    };
  }
  const urlChanged = before?.url !== void 0 && before.url !== url;
  return { changed: urlChanged || before === void 0, diff, url, title, ...dialogsField };
}
function releasedMidCall(tabId) {
  return {
    ok: false,
    code: ERROR_CODES.TARGET_NOT_ATTACHED,
    error: `tab ${tabId} was released while this call was running (the user may have pressed Stop); nothing captured after that point is returned`
  };
}
async function resolveActFrames(msg) {
  let sourceFrame;
  let destFrame;
  if (msg.selector && isFrameQualified(msg.selector)) {
    const resolved = await resolveFrameTarget(msg.tabId, msg.selector);
    if (!resolved.ok) return { ok: false, result: { ok: false, code: resolved.code, error: resolved.error } };
    const denial = checkAuthorizedFrameOrigin(msg.selector, resolved.result.frameOrigin, msg.frameOrigin);
    if (denial) return { ok: false, result: denial };
    sourceFrame = resolved.result;
  }
  if (msg.action === "drag" && msg.toSelector && isFrameQualified(msg.toSelector)) {
    const resolved = await resolveFrameTarget(msg.tabId, msg.toSelector);
    if (!resolved.ok) return { ok: false, result: { ok: false, code: resolved.code, error: resolved.error } };
    const denial = checkAuthorizedFrameOrigin(msg.toSelector, resolved.result.frameOrigin, msg.toFrameOrigin);
    if (denial) return { ok: false, result: denial };
    destFrame = resolved.result;
  }
  return { ok: true, sourceFrame, destFrame };
}
function checkAuthorizedFrameOrigin(selector, liveOrigin, authorizedOrigin) {
  if (authorizedOrigin === void 0) {
    const { code, message } = formatRefusal("frame_origin_not_authorized", { selector });
    return { ok: false, code, error: message };
  }
  if (canonicalizeOrigin(liveOrigin) !== canonicalizeOrigin(authorizedOrigin)) {
    const { code, message } = formatRefusal("frame_origin_changed", { selector });
    return { ok: false, code, error: message };
  }
  return null;
}
const CLASSIFY_AX_CHAIN_DEPTH = 6;
async function axChainAt(tabId, backendNodeId) {
  const enabled = await cdp.send(tabId, "Accessibility.enable", {});
  if (enabled.ok) {
    const chain = await cdp.send(tabId, "Accessibility.getAXNodeAndAncestors", { backendNodeId });
    if (chain.ok && Array.isArray(chain.result?.nodes) && chain.result.nodes.length > 0) {
      return chain.result.nodes.filter((n) => !n.ignored).slice(0, CLASSIFY_AX_CHAIN_DEPTH).map((n) => ({ role: n.role?.value ?? "", name: n.name?.value ?? "" }));
    }
  }
  const single = await describeAxNode(tabId, backendNodeId);
  return single ? [single] : [];
}
async function classifyActTarget(msg, sourceFrame) {
  if (isCommittingAction(msg.action)) {
    return { ok: true, data: { role: "", name: "", committing: true } };
  }
  let described = [];
  if (msg.selector) {
    const live = await resolveLiveElement(msg.tabId, msg.selector, sourceFrame);
    if (!live.ok) return { ok: false, code: live.error.code, error: live.error.message };
    if (!live.result) {
      return { ok: false, code: ERROR_CODES.CDP_ERROR, error: "classify_only: the selector resolved to no live element" };
    }
    described = [live.result];
  } else if (msg.xy) {
    if (!cdp.isAttached(msg.tabId)) {
      return {
        ok: false,
        code: ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE,
        error: "classify_only: an xy target cannot be hit-tested in limited (no-debugger) mode"
      };
    }
    const hit = await hitTestAtPoint(msg.tabId, msg.xy[0], msg.xy[1]);
    described = hit && hit.backendNodeId !== void 0 ? await axChainAt(msg.tabId, hit.backendNodeId) : [];
    if (described.length === 0) {
      const { code, message } = formatRefusal("classify_target_unresolved");
      return { ok: false, code, error: message };
    }
  } else {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "classify_only needs a selector or xy target" };
  }
  const committing = described.some((d) => isCommittingTarget(msg.action, d.name));
  let name = "";
  try {
    name = redactText(described[0].name, redactionPolicyOf(await getSettings())).text;
  } catch {
    name = "";
  }
  return { ok: true, data: { role: described[0].role, name, committing } };
}
async function handlePageAct(msg) {
  if (!cdp.isShared(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  if (cdp.isLimited(msg.tabId)) cdp.maybeUpgrade(msg.tabId);
  const frames = await resolveActFrames(msg);
  if (!frames.ok) return frames.result;
  if (msg.classifyOnly === true) return classifyActTarget(msg, frames.sourceFrame);
  clearDialogBuffer(msg.tabId);
  const timeoutMs = msg.timeoutMs ?? DEFAULT_TIMEOUT_MS$2;
  let op;
  const cancelHandle = registerCancellable(msg.tabId, `act:${msg.action}`, () => {
    void contentRequest(msg.tabId, { target: "content", type: "settleQuiet.cancel" }).catch(() => {
    });
  });
  try {
    const mismatch = await checkExpectedElement(msg, frames.sourceFrame, frames.destFrame);
    if (mismatch) return mismatch;
    op = presenceBegin(msg.tabId, presenceLabelFor(msg.action));
    const before = await captureSnapshot(msg.tabId);
    const originPolicy = {
      granted: msg.grantedOrigins ?? [],
      denied: msg.deniedOrigins ?? [],
      defaultFull: msg.defaultFull === true
    };
    const ctx = {
      notes: [],
      cancelHandle,
      sourceFrame: frames.sourceFrame,
      destFrame: frames.destFrame,
      originPolicy
    };
    const cdpStart = performance.now();
    let outcome;
    try {
      outcome = await performAction(msg.tabId, msg, timeoutMs, ctx);
    } catch (error) {
      return { ok: false, code: ERROR_CODES.CDP_ERROR, error: describeError(error) };
    }
    const cdpMs = Math.round((performance.now() - cdpStart) * 10) / 10;
    if (!outcome.ok) {
      return { ok: false, code: outcome.error.code, error: outcome.error.message };
    }
    const settleStart = performance.now();
    const settleOutcome = await settle(msg.tabId, timeoutMs, cancelHandle, ctx.sourceFrame?.frameId ?? ctx.destFrame?.frameId);
    const settleMs = Math.round((performance.now() - settleStart) * 10) / 10;
    if (cancelHandle.isCancelled()) {
      ctx.notes.push(
        `settle was interrupted (${cancelHandle.cancelReason()}) before confirming the page had gone quiet; the action above already ran — this only affects how fresh the diff below is`
      );
    }
    const dialogs = drainDialogs(msg.tabId);
    if (!cdp.isShared(msg.tabId)) return releasedMidCall(msg.tabId);
    forgetTabInjectionCache(msg.tabId);
    const after = await captureSnapshot(msg.tabId, before?.tree, msg.existingIndexMap, msg.startIndex);
    if (!cdp.isShared(msg.tabId)) return releasedMidCall(msg.tabId);
    const fallback = await tabUrlTitle(msg.tabId);
    const result = buildActResult(before, after, fallback, ctx.notes, dialogs);
    result.timing = { cdp_ms: cdpMs, settle_ms: settleMs };
    result.settledMs = settleOutcome.settledMs;
    if (ctx.waitForResult) result.waitFor = ctx.waitForResult;
    if (ctx.typeReadbackSelector) {
      const fieldValue = await readTypedField(msg.tabId, ctx.typeReadbackSelector, ctx.sourceFrame);
      if (fieldValue !== void 0) result.fieldValue = fieldValue;
    }
    if (ctx.uploadReadbackSelector) {
      const fileInfo = await readUploadedFile(msg.tabId, ctx.uploadReadbackSelector, ctx.sourceFrame);
      if (fileInfo !== void 0) result.fileInfo = fileInfo;
    }
    if (ctx.scrollResult) result.scroll = ctx.scrollResult;
    if (ctx.hitInfo) {
      const idx = hitIdxFromIndexMeta(after?.indexMeta, ctx.hitInfo);
      result.hit = {
        role: ctx.hitInfo.role,
        name: ctx.hitInfo.name,
        matched: ctx.hitInfo.matched,
        ...idx !== void 0 ? { idx } : {}
      };
    }
    if (msg.snapshotAfter === true && after?.tree !== void 0) {
      result.snapshotAfter = { tree: after.tree, url: after.url ?? result.url, title: after.title ?? result.title };
    } else if (msg.snapshotAfter && typeof msg.snapshotAfter === "object" && cdp.isShared(msg.tabId)) {
      const scoped = await captureScopedSnapshotAfter(msg.tabId, msg.snapshotAfter);
      if (scoped?.tree !== void 0 && !(msg.snapshotAfter.dialogOnly && scoped.dialogMissing)) {
        result.snapshotAfter = { tree: scoped.tree, url: scoped.url ?? result.url, title: scoped.title ?? result.title };
      }
    }
    if (msg.screenshotAfter && cdp.isShared(msg.tabId)) {
      const opts = msg.screenshotAfter === true ? {} : { scale: msg.screenshotAfter.scale, region: msg.screenshotAfter.region, format: msg.screenshotAfter.format };
      const { shot, note } = await captureActScreenshotAfter(msg.tabId, opts);
      if (shot) result.screenshotAfter = shot;
      if (note) result.diff = result.diff ? `note: ${note}
${result.diff}` : `note: ${note}`;
    }
    if (msg.action === "drag" && !result.changed && ctx.sourceDraggable && (msg.dragMode ?? "auto") === "auto") {
      const note = "note: no visible change; the source element is draggable, and this build's auto mode only drives the pointer path (see G0.1.6) — if the page listens for native HTML5 drag events specifically, this drag may not have been recognised";
      result.diff = result.diff ? `${note}
${result.diff}` : note;
    }
    result.committing = isCommittingTarget(msg.action, ctx.hitInfo?.name ?? msg.expect?.name);
    void recordReplayFrame(
      {
        tabId: msg.tabId,
        action: msg.action,
        selector: msg.selector,
        fieldSelectors: msg.fields?.map((f) => f.selector),
        hit: ctx.hitInfo ? { role: ctx.hitInfo.role, name: ctx.hitInfo.name } : void 0
      },
      "ok"
    );
    return { ok: true, data: result };
  } finally {
    if (op) void presenceEnd(op);
    cancelHandle.unregister();
  }
}
async function handleAnnotate(msg) {
  if (!cdp.isAttached(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  const timeoutMs = msg.timeoutMs ?? ANNOTATE_DEFAULT_TIMEOUT_MS;
  const op = presenceBegin(msg.tabId, CAPTURE_LABELS.annotate, timeoutMs + 5e3);
  let result;
  try {
    result = await contentRequestWithTimeout(
      msg.tabId,
      { target: "content", type: "annotate", question: msg.question, candidates: msg.candidates ?? [] },
      timeoutMs
    );
  } finally {
    void presenceEnd(op);
  }
  if (!result.ok) {
    void contentRequest(msg.tabId, { target: "content", type: "annotate.cancel" }).catch(() => {
    });
    return { ok: false, code: result.code ?? ERROR_CODES.TIMEOUT, error: result.error ?? "annotate failed" };
  }
  const data = result.data;
  if (!data || data.cancelled || data.choice === null || data.choice === void 0) {
    return { ok: false, code: ERROR_CODES.APPROVAL_DENIED, error: "user cancelled the annotate prompt" };
  }
  return { ok: true, data: { choice: data.choice } };
}
const VIEWPORT_MIN_WIDTH = 320;
const VIEWPORT_MAX_WIDTH = 3840;
const VIEWPORT_MIN_HEIGHT = 240;
const VIEWPORT_MAX_HEIGHT = 8e3;
function validateViewport(raw) {
  if (raw === void 0 || raw === null) return void 0;
  if (typeof raw !== "object") {
    return { error: "viewport must be an object with numeric width and height" };
  }
  const { width, height } = raw;
  if (typeof width !== "number" || typeof height !== "number" || !Number.isFinite(width) || !Number.isFinite(height)) {
    return { error: "viewport.width and viewport.height must be finite numbers" };
  }
  const w = Math.round(width);
  const h = Math.round(height);
  if (w < VIEWPORT_MIN_WIDTH || w > VIEWPORT_MAX_WIDTH || h < VIEWPORT_MIN_HEIGHT || h > VIEWPORT_MAX_HEIGHT) {
    return {
      error: `viewport must have width ${VIEWPORT_MIN_WIDTH}-${VIEWPORT_MAX_WIDTH} and height ${VIEWPORT_MIN_HEIGHT}-${VIEWPORT_MAX_HEIGHT} (got ${w}x${h})`,
      outOfRange: true
    };
  }
  return { width: w, height: h };
}
const tabQueues = /* @__PURE__ */ new Map();
function runExclusive(tabId, fn) {
  const tail = tabQueues.get(tabId) ?? Promise.resolve();
  const result = tail.then(fn, fn);
  tabQueues.set(
    tabId,
    result.then(
      () => void 0,
      () => void 0
    )
  );
  return result;
}
async function withViewportOverride(tabId, viewport, settleAfter, body) {
  return runExclusive(tabId, async () => {
    const applied = await cdp.send(tabId, "Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 0,
      mobile: false
    });
    try {
      if (applied.ok) {
        await settleAfter();
      }
      const result = await body();
      return { result, overrideApplied: applied.ok };
    } finally {
      await cdp.send(tabId, "Emulation.clearDeviceMetricsOverride").catch(() => {
      });
    }
  });
}
const DEFAULT_TIMEOUT_MS$1 = 3e4;
const DEFAULT_MAX_BYTES = 2 * 1024 * 1024;
async function pageFetchImpl(args) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), args.timeoutMs);
  try {
    const response = await fetch(args.url, {
      method: args.method,
      headers: args.headers,
      body: args.body === null ? void 0 : args.body,
      credentials: args.credentials,
      redirect: "follow",
      signal: controller.signal
    });
    const headers = {};
    response.headers.forEach((value, key) => {
      headers[key] = value;
    });
    let truncated = false;
    const chunks = [];
    let total = 0;
    const reader = response.body ? response.body.getReader() : null;
    if (reader) {
      for (; ; ) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || value.length === 0) continue;
        const remaining = args.maxBytes - total;
        if (remaining <= 0) {
          truncated = true;
          void reader.cancel().catch(() => {
          });
          break;
        }
        const slice = value.length > remaining ? value.subarray(0, remaining) : value;
        chunks.push(slice);
        total += slice.length;
        if (slice.length < value.length) {
          truncated = true;
          void reader.cancel().catch(() => {
          });
          break;
        }
      }
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    const contentType = (headers["content-type"] ?? "").toLowerCase();
    const looksTextual = contentType === "" || /^text\//.test(contentType) || /(json|xml|javascript|ecmascript|x-www-form-urlencoded|svg)/.test(contentType);
    let bodyText = null;
    if (looksTextual) {
      try {
        bodyText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        bodyText = null;
      }
    }
    if (bodyText !== null) {
      return { status: response.status, headers, body: bodyText, bodyEncoding: "utf-8", truncated };
    }
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return { status: response.status, headers, body: btoa(binary), bodyEncoding: "base64", truncated };
  } catch (error) {
    const err = error;
    return {
      __hermesFetchError: true,
      kind: err?.name === "AbortError" ? "timeout" : "network",
      message: err?.message ?? "fetch failed"
    };
  } finally {
    clearTimeout(timer);
  }
}
function isFailure(value) {
  return Boolean(value) && value.__hermesFetchError === true;
}
function validateUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return `page.fetch: not a valid absolute URL: ${JSON.stringify(url)}`;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `page.fetch: unsupported URL scheme ${JSON.stringify(parsed.protocol)} (only http/https)`;
  }
  return void 0;
}
async function handlePageFetch(msg) {
  if (!cdp.isAttached(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  if (!msg.url) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "page.fetch requires a url" };
  }
  const urlError = validateUrl(msg.url);
  if (urlError) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: urlError };
  }
  const args = {
    url: msg.url,
    method: (msg.method ?? "GET").toUpperCase(),
    headers: msg.headers ?? {},
    body: msg.body ?? null,
    credentials: msg.credentials ?? "include",
    timeoutMs: msg.timeoutMs ?? DEFAULT_TIMEOUT_MS$1,
    maxBytes: msg.maxBytes ?? DEFAULT_MAX_BYTES
  };
  const expression = `(${pageFetchImpl.toString()})(${JSON.stringify(args)})`;
  const sent = await cdp.send(msg.tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true
  });
  if (!sent.ok) {
    return { ok: false, code: sent.error.code, error: sent.error.message };
  }
  if (sent.result.exceptionDetails) {
    const detail = sent.result.exceptionDetails.exception?.description ?? sent.result.exceptionDetails.text ?? "page.fetch evaluation threw";
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: detail };
  }
  const value = sent.result.result?.value;
  if (isFailure(value)) {
    const code = value.kind === "timeout" ? ERROR_CODES.TIMEOUT : ERROR_CODES.CDP_ERROR;
    return { ok: false, code, error: value.message };
  }
  if (!value) {
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: "page.fetch produced no result" };
  }
  return {
    ok: true,
    data: {
      status: value.status,
      headers: value.headers,
      body: value.body,
      bodyEncoding: value.bodyEncoding,
      truncated: value.truncated
    }
  };
}
function toWireCookie(cookie) {
  return {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path,
    // chrome.cookies.Cookie.expirationDate is seconds-since-epoch and absent
    // for session cookies; the wire schema types `expiry` as a plain
    // integer with no null/undefined variant, so a session cookie reports 0
    // rather than omitting the field.
    expiry: cookie.expirationDate ?? 0,
    httpOnly: cookie.httpOnly,
    secure: cookie.secure,
    sameSite: cookie.sameSite,
    session: cookie.session
  };
}
function cookieKey(cookie) {
  return `${cookie.domain}\0${cookie.path}\0${cookie.name}`;
}
function scrubValueFromMessage(message, value) {
  if (value.length < 3) return message;
  return message.split(value).join("[withheld]");
}
function redactUrlForLog(url) {
  const q = url.indexOf("?");
  return q === -1 ? url : `${url.slice(0, q)}?<redacted>`;
}
async function handleCookiesGet(msg) {
  const seen = /* @__PURE__ */ new Set();
  const cookies = [];
  for (const url of msg.urls) {
    let matched;
    try {
      matched = await chrome.cookies.getAll({ url });
    } catch (error) {
      return {
        ok: false,
        code: ERROR_CODES.INVALID_PARAMS,
        error: `cookies.get failed for ${redactUrlForLog(url)}: ${describeError(error)}`
      };
    }
    for (const cookie of matched) {
      const key = cookieKey(cookie);
      if (seen.has(key)) continue;
      seen.add(key);
      cookies.push(toWireCookie(cookie));
    }
  }
  return { ok: true, data: { cookies } };
}
async function attachedOrigins() {
  const origins = /* @__PURE__ */ new Set();
  for (const tabId of cdp.sharedList()) {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      continue;
    }
    if (!tab.url) continue;
    try {
      origins.add(new URL(tab.url).origin);
    } catch {
    }
  }
  return origins;
}
function validateSetParams(msg) {
  if (msg.sameSite === "none") {
    return 'cookies.set: sameSite "none" is not a valid chrome.cookies.SameSiteStatus value — use "no_restriction" instead (with secure:true and an https:// url)';
  }
  if (msg.sameSite === "no_restriction") {
    if (msg.secure !== true) {
      return 'cookies.set: sameSite:"no_restriction" requires secure:true, or chrome.cookies.set fails';
    }
    let scheme = "";
    try {
      scheme = new URL(msg.url).protocol;
    } catch {
      return `cookies.set: not a valid absolute URL: ${JSON.stringify(msg.url)}`;
    }
    if (scheme !== "https:") {
      return 'cookies.set: sameSite:"no_restriction" requires an https:// url, or chrome.cookies.set fails';
    }
  }
  return "";
}
async function handleCookiesSet(msg) {
  const settings = await getSettings();
  const powerDenial = await assertCookieWriteAllowed(settings);
  if (powerDenial) {
    return { ok: false, code: powerDenial.code, error: powerDenial.message };
  }
  let targetOrigin;
  try {
    targetOrigin = new URL(msg.url).origin;
  } catch {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: `cookies.set: not a valid absolute URL: ${JSON.stringify(msg.url)}` };
  }
  const attached = await attachedOrigins();
  if (!attached.has(targetOrigin)) {
    const { code, message } = formatRefusal("cookie_write_origin_unattached", { origin: targetOrigin });
    return { ok: false, code, error: message };
  }
  const paramError = validateSetParams(msg);
  if (paramError) {
    return { ok: false, code: ERROR_CODES.COOKIE_SAME_SITE_INVALID, error: paramError };
  }
  const details = {
    url: msg.url,
    name: msg.name,
    value: msg.value,
    path: msg.path,
    domain: msg.domain,
    secure: msg.secure,
    httpOnly: msg.httpOnly,
    sameSite: msg.sameSite,
    expirationDate: msg.expirationDate,
    partitionKey: msg.partitionKey
  };
  let result;
  try {
    result = await chrome.cookies.set(details);
  } catch (error) {
    return {
      ok: false,
      code: ERROR_CODES.INVALID_PARAMS,
      error: scrubValueFromMessage(`cookies.set failed for ${redactUrlForLog(msg.url)}: ${describeError(error)}`, msg.value)
    };
  }
  if (!result) {
    return {
      ok: false,
      code: ERROR_CODES.INVALID_PARAMS,
      error: `cookies.set failed for ${redactUrlForLog(msg.url)}: chrome.cookies.set returned null (details were individually valid but jointly rejected for this url)`
    };
  }
  const shaped = {
    name: result.name,
    domain: result.domain,
    path: result.path,
    httpOnly: result.httpOnly,
    secure: result.secure,
    sameSite: result.sameSite,
    expires: result.expirationDate ?? 0
  };
  return { ok: true, data: shaped };
}
const MAX_ENTRIES_PER_TAB$1 = 200;
const MAX_POST_DATA_BYTES = 8192;
const MAX_HEAVY_BYTES_PER_TAB$1 = 1 * 1024 * 1024;
const SENSITIVE_HEADER_NAMES = /* @__PURE__ */ new Set(["cookie", "authorization"]);
const REDACTED_HEADER_VALUE = "<redacted>";
const buffers$1 = /* @__PURE__ */ new Map();
const heavyBytesByTab$1 = /* @__PURE__ */ new Map();
const pendingByKey = /* @__PURE__ */ new Map();
function pendingKey(tabId, requestId) {
  return `${tabId}\0${requestId}`;
}
function bufferFor$1(tabId) {
  let buffer = buffers$1.get(tabId);
  if (!buffer) {
    buffer = [];
    buffers$1.set(tabId, buffer);
  }
  return buffer;
}
function evictOldest$1(tabId, buffer) {
  const evicted = buffer.shift();
  if (!evicted) return;
  pendingByKey.delete(pendingKey(tabId, evicted.requestId));
  heavyBytesByTab$1.set(tabId, Math.max(0, (heavyBytesByTab$1.get(tabId) ?? 0) - evicted.heavyBytes));
}
function push$1(tabId, entry) {
  const buffer = bufferFor$1(tabId);
  buffer.push(entry);
  heavyBytesByTab$1.set(tabId, (heavyBytesByTab$1.get(tabId) ?? 0) + entry.heavyBytes);
  while (buffer.length > MAX_ENTRIES_PER_TAB$1 || (heavyBytesByTab$1.get(tabId) ?? 0) > MAX_HEAVY_BYTES_PER_TAB$1) {
    if (buffer.length === 0) break;
    evictOldest$1(tabId, buffer);
  }
}
function asRecord$1(value) {
  return value && typeof value === "object" ? value : {};
}
function sanitizeRequestHeaders(rawHeaders) {
  const record = asRecord$1(rawHeaders);
  const keys = Object.keys(record);
  if (keys.length === 0) return void 0;
  const sanitized = {};
  for (const name of keys) {
    const value = record[name];
    if (typeof value !== "string") continue;
    sanitized[name] = SENSITIVE_HEADER_NAMES.has(name.toLowerCase()) ? REDACTED_HEADER_VALUE : value;
  }
  return sanitized;
}
function capturePostData(rawPostData) {
  if (typeof rawPostData !== "string" || rawPostData.length === 0) return { postDataTruncated: false };
  if (rawPostData.length <= MAX_POST_DATA_BYTES) return { postData: rawPostData, postDataTruncated: false };
  return { postData: rawPostData.slice(0, MAX_POST_DATA_BYTES), postDataTruncated: true };
}
function heavyByteSize$1(requestHeaders, postData) {
  let size = postData?.length ?? 0;
  if (requestHeaders) {
    for (const [name, value] of Object.entries(requestHeaders)) size += name.length + value.length;
  }
  return size;
}
function onDebuggerEvent$1(source, method, rawParams) {
  const tabId = source.tabId;
  if (typeof tabId !== "number" || !cdp.isAttached(tabId)) return;
  const params = asRecord$1(rawParams);
  if (method === "Network.requestWillBeSent") {
    const requestId = String(params.requestId ?? "");
    if (!requestId) return;
    const request = asRecord$1(params.request);
    const requestHeaders = sanitizeRequestHeaders(request.headers);
    const { postData, postDataTruncated } = capturePostData(request.postData);
    const entry = {
      requestId,
      method: typeof request.method === "string" ? request.method : "GET",
      url: typeof request.url === "string" ? request.url : "",
      status: null,
      resourceType: typeof params.type === "string" ? params.type : "Other",
      failed: false,
      ...requestHeaders ? { requestHeaders } : {},
      ...postData !== void 0 ? { postData } : {},
      ...postDataTruncated ? { postDataTruncated: true } : {},
      heavyBytes: heavyByteSize$1(requestHeaders, postData)
    };
    push$1(tabId, entry);
    pendingByKey.set(pendingKey(tabId, requestId), entry);
    return;
  }
  if (method === "Network.responseReceived") {
    const requestId = String(params.requestId ?? "");
    const entry = pendingByKey.get(pendingKey(tabId, requestId));
    if (!entry) return;
    const response = asRecord$1(params.response);
    if (typeof response.status === "number") entry.status = response.status;
    if (typeof params.type === "string") entry.resourceType = params.type;
    pendingByKey.delete(pendingKey(tabId, requestId));
    return;
  }
  if (method === "Network.loadingFailed") {
    const requestId = String(params.requestId ?? "");
    const entry = pendingByKey.get(pendingKey(tabId, requestId));
    if (!entry) return;
    entry.failed = true;
    pendingByKey.delete(pendingKey(tabId, requestId));
  }
}
chrome.debugger.onEvent.addListener(onDebuggerEvent$1);
cdp.onRelease((tabId) => {
  buffers$1.delete(tabId);
  heavyBytesByTab$1.delete(tabId);
  const prefix = `${tabId}\0`;
  for (const key of pendingByKey.keys()) {
    if (key.startsWith(prefix)) pendingByKey.delete(key);
  }
});
function getNetworkLog(tabId, filter, limit, includeBodies = false) {
  const buffer = buffers$1.get(tabId) ?? [];
  const filtered = filter ? buffer.filter((entry) => entry.url.toLowerCase().includes(filter.toLowerCase())) : buffer;
  const bounded = Math.max(0, Math.min(limit, filtered.length));
  return filtered.slice(filtered.length - bounded).reverse().map(({ method, url, status, resourceType, failed, requestHeaders, postData, postDataTruncated }) => {
    const base = { method, url, status, resourceType, failed };
    if (includeBodies) {
      if (requestHeaders) base.requestHeaders = requestHeaders;
      if (postData !== void 0) base.postData = postData;
      if (postDataTruncated) base.postDataTruncated = true;
    }
    return base;
  });
}
async function handleNetworkLog(msg) {
  const limitedRefusal = cdpOnlyGate(msg.tabId, "network.log");
  if (limitedRefusal) {
    return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
  }
  const entries = getNetworkLog(msg.tabId, msg.filter, msg.limit ?? 50, msg.bodies ?? false);
  return { ok: true, data: { entries } };
}
const STORE_KEY = "httpAuthCredentials";
const LAST_RESULT_KEY = "httpAuthLastResult";
const DEFAULT_TTL_MS = 15 * 60 * 1e3;
const MIN_TTL_MS = 60 * 1e3;
const MAX_TTL_MS = 4 * 60 * 60 * 1e3;
function clampTtl(ttlMs) {
  if (typeof ttlMs !== "number" || !Number.isFinite(ttlMs) || ttlMs <= 0) return DEFAULT_TTL_MS;
  return Math.min(MAX_TTL_MS, Math.max(MIN_TTL_MS, Math.round(ttlMs)));
}
function normalizeOrigin(raw) {
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin;
  } catch {
    return null;
  }
}
async function readStore() {
  const stored = await chrome.storage.session.get(STORE_KEY);
  const raw = stored[STORE_KEY] ?? {};
  const now = Date.now();
  let pruned = false;
  const live = {};
  for (const [origin, cred] of Object.entries(raw)) {
    if (cred && cred.expiresAt > now) {
      live[origin] = cred;
    } else {
      pruned = true;
    }
  }
  if (pruned) await writeStore(live);
  return live;
}
async function writeStore(store) {
  await chrome.storage.session.set({ [STORE_KEY]: store });
  await syncListener(store);
}
async function readLastResult(origin) {
  const stored = await chrome.storage.session.get(LAST_RESULT_KEY);
  const raw = stored[LAST_RESULT_KEY] ?? {};
  return raw[origin]?.result ?? null;
}
async function writeLastResult(origin, result) {
  const stored = await chrome.storage.session.get(LAST_RESULT_KEY);
  const raw = stored[LAST_RESULT_KEY] ?? {};
  raw[origin] = { result, at: Date.now() };
  await chrome.storage.session.set({ [LAST_RESULT_KEY]: raw });
}
async function clearLastResult(origin) {
  const stored = await chrome.storage.session.get(LAST_RESULT_KEY);
  const raw = stored[LAST_RESULT_KEY] ?? {};
  if (!(origin in raw)) return;
  delete raw[origin];
  await chrome.storage.session.set({ [LAST_RESULT_KEY]: raw });
}
function webRequestAvailable() {
  return typeof chrome !== "undefined" && Boolean(chrome.webRequest?.onAuthRequired);
}
async function syncListener(store) {
  if (!webRequestAvailable()) return;
  const shouldBeRegistered = Object.keys(store).length > 0;
  const isRegistered = chrome.webRequest.onAuthRequired.hasListener(onAuthRequired);
  if (shouldBeRegistered && !isRegistered) {
    chrome.webRequest.onAuthRequired.addListener(onAuthRequired, { urls: ["<all_urls>"] }, ["asyncBlocking"]);
    chrome.webRequest.onCompleted.addListener(onRequestSettled, { urls: ["<all_urls>"] });
    chrome.webRequest.onErrorOccurred.addListener(onRequestSettled, { urls: ["<all_urls>"] });
  } else if (!shouldBeRegistered && isRegistered) {
    chrome.webRequest.onAuthRequired.removeListener(onAuthRequired);
    chrome.webRequest.onCompleted.removeListener(onRequestSettled);
    chrome.webRequest.onErrorOccurred.removeListener(onRequestSettled);
  }
}
async function resyncAuthListener() {
  await syncListener(await readStore());
}
function notifyReplay(origin) {
  void chrome.runtime.sendMessage({ target: "offscreen", type: "auth.replayed", origin }).catch(() => {
  });
}
const MAX_TRACKED_REQUESTS = 256;
const answeredRequests = /* @__PURE__ */ new Map();
function rememberAnswered(requestId, origin) {
  if (!answeredRequests.has(requestId) && answeredRequests.size >= MAX_TRACKED_REQUESTS) {
    const oldest = answeredRequests.keys().next().value;
    if (oldest !== void 0) answeredRequests.delete(oldest);
  }
  answeredRequests.set(requestId, origin);
}
function forgetRequest(requestId) {
  answeredRequests.delete(requestId);
}
async function handleRejectedCredential(origin) {
  const store = await readStore();
  if (origin in store) {
    delete store[origin];
    await writeStore(store);
  }
  await writeLastResult(origin, "rejected");
}
function onAuthRequired(details, asyncCallback) {
  if (!asyncCallback) return;
  if (details.isProxy) {
    asyncCallback({});
    return;
  }
  const origin = normalizeOrigin(details.url);
  if (!origin) {
    asyncCallback({});
    return;
  }
  const previouslyAnsweredOrigin = answeredRequests.get(details.requestId);
  if (previouslyAnsweredOrigin !== void 0) {
    forgetRequest(details.requestId);
    asyncCallback({});
    void handleRejectedCredential(previouslyAnsweredOrigin);
    return;
  }
  void readStore().then((store) => {
    const cred = store[origin];
    if (!cred) {
      asyncCallback({});
      return;
    }
    rememberAnswered(details.requestId, origin);
    asyncCallback({ authCredentials: { username: cred.username, password: cred.password } });
    notifyReplay(origin);
  });
}
function onRequestSettled(details) {
  forgetRequest(details.requestId);
}
async function handleAuthArm(msg) {
  const denial = await assertHttpAuthAllowed(await getSettings());
  if (denial) return { ok: false, code: denial.code, error: denial.message };
  const origin = normalizeOrigin(msg.origin);
  if (!origin) return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: `not a valid http(s) origin: ${msg.origin}` };
  if (!msg.username) return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "username is required" };
  if (!msg.password) return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "password is required" };
  const now = Date.now();
  const ttlMs = clampTtl(msg.ttlMs);
  const store = await readStore();
  store[origin] = { origin, username: msg.username, password: msg.password, armedAt: now, expiresAt: now + ttlMs };
  await writeStore(store);
  await clearLastResult(origin);
  return { ok: true, data: { origin, expiresAt: store[origin].expiresAt } };
}
async function handleAuthDisarm(msg) {
  const origin = normalizeOrigin(msg.origin) ?? msg.origin;
  const store = await readStore();
  const removed = origin in store;
  if (removed) {
    delete store[origin];
    await writeStore(store);
  }
  await clearLastResult(origin);
  return { ok: true, data: { removed } };
}
async function handleAuthListArmed() {
  const store = await readStore();
  const armed = Object.values(store).map((cred) => ({ origin: cred.origin, expiresAt: cred.expiresAt }));
  return { ok: true, data: { armed } };
}
async function handleAuthStatus(msg) {
  const denial = await assertHttpAuthAllowed(await getSettings());
  if (denial) return { ok: false, code: denial.code, error: denial.message };
  const origin = normalizeOrigin(msg.origin) ?? msg.origin;
  const store = await readStore();
  const cred = store[origin];
  const lastResult = await readLastResult(origin);
  const data = {
    armed: Boolean(cred),
    origin,
    expiresAt: cred?.expiresAt ?? null
  };
  if (lastResult) data.lastResult = lastResult;
  return { ok: true, data };
}
const MAX_ENTRIES_PER_TAB = 500;
const MAX_TEXT_BYTES = 4096;
const RAW_CAPTURE_CHARS = MAX_TEXT_BYTES * REDACT_THEN_TRUNCATE_PREFIX_MULTIPLIER;
const MAX_HEAVY_BYTES_PER_TAB = 1 * 1024 * 1024;
const MAX_STACK_FRAMES = 10;
const buffers = /* @__PURE__ */ new Map();
const heavyBytesByTab = /* @__PURE__ */ new Map();
function bufferFor(tabId) {
  let buffer = buffers.get(tabId);
  if (!buffer) {
    buffer = [];
    buffers.set(tabId, buffer);
  }
  return buffer;
}
function evictOldest(tabId, buffer) {
  const evicted = buffer.shift();
  if (!evicted) return;
  heavyBytesByTab.set(tabId, Math.max(0, (heavyBytesByTab.get(tabId) ?? 0) - evicted.heavyBytes));
}
function push(tabId, entry) {
  const buffer = bufferFor(tabId);
  buffer.push(entry);
  heavyBytesByTab.set(tabId, (heavyBytesByTab.get(tabId) ?? 0) + entry.heavyBytes);
  while (buffer.length > MAX_ENTRIES_PER_TAB || (heavyBytesByTab.get(tabId) ?? 0) > MAX_HEAVY_BYTES_PER_TAB) {
    if (buffer.length === 0) break;
    evictOldest(tabId, buffer);
  }
}
function asRecord(value) {
  return value && typeof value === "object" ? value : {};
}
const MAX_PREVIEW_PROPERTIES = 10;
const MAX_PREVIEW_SUMMARY_LENGTH = 200;
const SECRET_KEY_SINGLE_TERMS = /* @__PURE__ */ new Set([
  "token",
  "secret",
  "password",
  "passwd",
  "pwd",
  "apikey",
  "authorization",
  "auth",
  "session",
  "bearer",
  "jwt",
  "cookie",
  "credential",
  "credentials",
  "passphrase",
  "signature"
]);
const SECRET_KEY_COMPOUND_TERMS = [
  ["api", "key"],
  ["access", "token"],
  ["refresh", "token"],
  ["client", "secret"],
  ["secret", "key"],
  ["private", "key"]
];
function keySegments(name) {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2").toLowerCase().split(/[^a-z0-9]+/).filter((segment) => segment.length > 0);
}
function isSecretKeyName(name) {
  const segments = keySegments(name);
  if (segments.some((segment) => SECRET_KEY_SINGLE_TERMS.has(segment))) return true;
  for (const [first, second] of SECRET_KEY_COMPOUND_TERMS) {
    for (let i = 0; i < segments.length - 1; i++) {
      if (segments[i] === first && segments[i + 1] === second) return true;
    }
  }
  return false;
}
function renderPreviewPropertyValue(raw) {
  const prop = asRecord(raw);
  if (prop.type === "accessor") return "<accessor>";
  return typeof prop.value === "string" ? prop.value : "<value>";
}
function finishPreviewSummary(parts, open2, close, showOverflow) {
  let body = parts.join(", ");
  if (showOverflow) body = parts.length > 0 ? `${body}, …` : "…";
  const summary = `${open2}${body}${close}`;
  if (summary.length <= MAX_PREVIEW_SUMMARY_LENGTH) return summary;
  const cut = trimTrailingPartialToken(summary.slice(0, MAX_PREVIEW_SUMMARY_LENGTH - 1));
  return `${cut}…`;
}
function renderPreview(raw) {
  const preview = asRecord(raw);
  const overflowFlag = preview.overflow === true;
  if (Array.isArray(preview.entries)) {
    const entries = preview.entries;
    const capped2 = entries.slice(0, MAX_PREVIEW_PROPERTIES);
    const parts2 = capped2.map((rawEntry) => {
      const entry = asRecord(rawEntry);
      const hasKey = "key" in entry;
      const keyProp = asRecord(entry.key);
      const keyIsSecretName = hasKey && keyProp.type === "string" && typeof keyProp.value === "string" && isSecretKeyName(keyProp.value);
      const value = keyIsSecretName ? secretRedactionMarker() : renderPreviewPropertyValue(entry.value);
      return hasKey ? `${renderPreviewPropertyValue(entry.key)} => ${value}` : value;
    });
    return finishPreviewSummary(parts2, "{", "}", overflowFlag || entries.length > capped2.length);
  }
  if (!Array.isArray(preview.properties)) return void 0;
  const properties = preview.properties;
  const isArray = preview.subtype === "array";
  const capped = properties.slice(0, MAX_PREVIEW_PROPERTIES);
  const parts = capped.map((rawProp) => {
    const prop = asRecord(rawProp);
    const name = typeof prop.name === "string" ? prop.name : "";
    const value = isSecretKeyName(name) ? secretRedactionMarker() : renderPreviewPropertyValue(rawProp);
    return isArray ? value : `${name}: ${value}`;
  });
  return finishPreviewSummary(parts, isArray ? "[" : "{", isArray ? "]" : "}", overflowFlag || properties.length > capped.length);
}
function previewArg(raw) {
  const obj = asRecord(raw);
  if ("unserializableValue" in obj) return String(obj.unserializableValue);
  if ("value" in obj) {
    const value = obj.value;
    if (value === null) return "null";
    if (typeof value === "string") return value;
    return JSON.stringify(value);
  }
  const preview = renderPreview(obj.preview);
  if (preview !== void 0) return preview;
  if (typeof obj.description === "string") return obj.description;
  return typeof obj.type === "string" ? `<${obj.type}>` : "<value>";
}
function joinArgs(args) {
  if (!Array.isArray(args)) return "";
  return args.map(previewArg).join(" ");
}
const CONSOLE_TYPE_LEVEL = {
  error: "error",
  assert: "error",
  warning: "warning",
  debug: "verbose",
  trace: "verbose"
};
function levelForConsoleType(type) {
  if (typeof type === "string" && type in CONSOLE_TYPE_LEVEL) return CONSOLE_TYPE_LEVEL[type];
  return "info";
}
function levelForLogEntry(level) {
  return level === "verbose" || level === "info" || level === "warning" || level === "error" ? level : "info";
}
function extractStack(stackTrace) {
  const frames = asRecord(stackTrace).callFrames;
  if (!Array.isArray(frames) || frames.length === 0) return void 0;
  return frames.slice(0, MAX_STACK_FRAMES).map((raw) => {
    const frame = asRecord(raw);
    return {
      functionName: typeof frame.functionName === "string" ? frame.functionName : "",
      url: typeof frame.url === "string" ? frame.url : "",
      lineNumber: typeof frame.lineNumber === "number" ? frame.lineNumber : 0,
      columnNumber: typeof frame.columnNumber === "number" ? frame.columnNumber : 0
    };
  });
}
function heavyByteSize(entry) {
  let size = entry.text.length;
  if (entry.stack) {
    for (const frame of entry.stack) size += frame.functionName.length + frame.url.length;
  }
  return size;
}
function capRawCapture(text) {
  return text.length > RAW_CAPTURE_CHARS ? text.slice(0, RAW_CAPTURE_CHARS) : text;
}
function track(tabId, entry) {
  const tracked = { ...entry, heavyBytes: heavyByteSize(entry) };
  push(tabId, tracked);
}
function onDebuggerEvent(source, method, rawParams) {
  const tabId = source.tabId;
  if (typeof tabId !== "number" || !cdp.isAttached(tabId)) return;
  const params = asRecord(rawParams);
  const ts = Date.now();
  if (method === "Runtime.consoleAPICalled") {
    track(tabId, {
      level: levelForConsoleType(params.type),
      source: "console-api",
      text: capRawCapture(joinArgs(params.args)),
      stack: extractStack(params.stackTrace),
      ts
    });
    return;
  }
  if (method === "Runtime.exceptionThrown") {
    const details = asRecord(params.exceptionDetails);
    const exceptionPreview = "exception" in details ? previewArg(details.exception) : "";
    const text = [typeof details.text === "string" ? details.text : "", exceptionPreview].filter(Boolean).join(": ");
    track(tabId, {
      level: "error",
      source: "exception",
      text: capRawCapture(text || "uncaught exception"),
      url: typeof details.url === "string" ? details.url : void 0,
      lineNumber: typeof details.lineNumber === "number" ? details.lineNumber : void 0,
      columnNumber: typeof details.columnNumber === "number" ? details.columnNumber : void 0,
      stack: extractStack(details.stackTrace),
      ts
    });
    return;
  }
  if (method === "Log.entryAdded") {
    const entry = asRecord(params.entry);
    track(tabId, {
      level: levelForLogEntry(entry.level),
      source: "log",
      text: capRawCapture(typeof entry.text === "string" ? entry.text : ""),
      url: typeof entry.url === "string" ? entry.url : void 0,
      lineNumber: typeof entry.lineNumber === "number" ? entry.lineNumber : void 0,
      stack: extractStack(entry.stackTrace),
      ts
    });
  }
}
chrome.debugger.onEvent.addListener(onDebuggerEvent);
cdp.onRelease((tabId) => {
  buffers.delete(tabId);
  heavyBytesByTab.delete(tabId);
});
function redactEntry(entry, policy) {
  let hits = 0;
  const pass = (text) => {
    const step1 = redactText(text, policy);
    hits += step1.count;
    const step2 = redactSecrets(step1.text);
    hits += step2.count;
    return step2.text;
  };
  const textResult = redactThenTruncate(entry.text, MAX_TEXT_BYTES, policy);
  hits += textResult.count;
  const redacted = { ...entry, text: textResult.text };
  if (redacted.url) redacted.url = pass(redacted.url);
  if (redacted.stack) redacted.stack = redacted.stack.map((frame) => ({ ...frame, url: pass(frame.url) }));
  return { entry: redacted, hits };
}
function getConsoleEntries(tabId, levelFilter, textFilter, limit, since) {
  const buffer = buffers.get(tabId) ?? [];
  let filtered = buffer;
  if (typeof since === "number") filtered = filtered.filter((e) => e.ts >= since);
  if (levelFilter) {
    const wanted = levelFilter.toLowerCase();
    filtered = filtered.filter((e) => e.level === wanted);
  }
  if (textFilter) {
    const needle = textFilter.toLowerCase();
    filtered = filtered.filter((e) => e.text.toLowerCase().includes(needle));
  }
  const bounded = Math.max(0, Math.min(limit, filtered.length));
  return filtered.slice(filtered.length - bounded).reverse().map(({ level, source, text, url, lineNumber, columnNumber, stack, ts }) => ({
    level,
    source,
    text,
    ...url !== void 0 ? { url } : {},
    ...lineNumber !== void 0 ? { lineNumber } : {},
    ...columnNumber !== void 0 ? { columnNumber } : {},
    ...stack ? { stack } : {},
    ts
  }));
}
async function handleConsoleEntries(msg) {
  const settings = await getSettings();
  const denial = await assertConsoleReadAllowed(settings);
  if (denial) {
    return { ok: false, code: denial.code, error: denial.message };
  }
  const limitedRefusal = cdpOnlyGate(msg.tabId, "console.entries");
  if (limitedRefusal) {
    return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
  }
  const policy = redactionPolicyOf(settings);
  const raw = getConsoleEntries(msg.tabId, msg.levelFilter, msg.textFilter, msg.limit ?? 50, msg.since);
  const entries = [];
  for (const one of raw) {
    const { entry } = redactEntry(one, policy);
    entries.push(entry);
  }
  return { ok: true, data: { entries } };
}
const MAX_EXPRESSION_CHARS = 4096;
const MAX_RESULT_CHARS = 32 * 1024;
const DEFAULT_TIMEOUT_MS = 1e4;
const MAX_TIMEOUT_MS = 3e4;
const WALL_CLOCK_GRACE_MS = 500;
function clampTimeout(requested) {
  const value = typeof requested === "number" && requested > 0 ? requested : DEFAULT_TIMEOUT_MS;
  return Math.min(value, MAX_TIMEOUT_MS);
}
function terminateBestEffort(tabId) {
  void cdp.send(tabId, "Runtime.terminateExecution", {}).catch(() => {
  });
}
function previewOf(remote) {
  if (!remote) return { text: "undefined" };
  if ("unserializableValue" in remote && remote.unserializableValue !== void 0) {
    return { text: String(remote.unserializableValue), type: remote.type, subtype: remote.subtype };
  }
  if ("value" in remote) {
    const value = remote.value;
    if (value === null) return { text: "null", type: remote.type, subtype: remote.subtype };
    if (typeof value === "string") return { text: value, type: remote.type, subtype: remote.subtype };
    try {
      return { text: JSON.stringify(value), type: remote.type, subtype: remote.subtype };
    } catch {
      return { text: String(value), type: remote.type, subtype: remote.subtype };
    }
  }
  if (typeof remote.description === "string" && remote.description) {
    return { text: remote.description, type: remote.type, subtype: remote.subtype };
  }
  return { text: remote.type ? `<${remote.type}>` : "<value>", type: remote.type, subtype: remote.subtype };
}
async function handlePageEvaluate(msg) {
  const settings = await getSettings();
  const denial = await assertEvaluateAllowed(settings);
  if (denial) {
    return { ok: false, code: denial.code, error: denial.message };
  }
  const limitedRefusal = cdpOnlyGate(msg.tabId, "evaluate");
  if (limitedRefusal) {
    return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
  }
  const expression = typeof msg.expression === "string" ? msg.expression : "";
  if (!expression) {
    return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "page.evaluate requires a non-empty expression" };
  }
  if (expression.length > MAX_EXPRESSION_CHARS) {
    return {
      ok: false,
      code: ERROR_CODES.EVAL_EXPRESSION_TOO_LARGE,
      error: `expression is ${expression.length} chars, over the ${MAX_EXPRESSION_CHARS}-char cap`
    };
  }
  const world = msg.world ?? "main";
  if (world !== "main") {
    return {
      ok: false,
      code: ERROR_CODES.EVAL_WORLD_UNSUPPORTED,
      error: `world '${world}' is not supported; only 'main' ships today (see docs/security.md §20)`
    };
  }
  const timeoutMs = clampTimeout(msg.timeoutMs);
  const awaitPromise = msg.awaitPromise ?? true;
  let resolveCancelled;
  const cancelHandle = registerCancellable(msg.tabId, "evaluate", (reason) => {
    terminateBestEffort(msg.tabId);
    resolveCancelled?.(reason);
  });
  const attempt = cdp.send(msg.tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    // always true on the wire — see file header
    awaitPromise,
    timeout: timeoutMs,
    // CDP-native bound; see file header for why it isn't sufficient alone
    userGesture: false
  });
  let timeoutTimer;
  const timeoutPromise = new Promise((resolve) => {
    timeoutTimer = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs + WALL_CLOCK_GRACE_MS);
  });
  const cancelPromise = new Promise((resolve) => {
    resolveCancelled = (reason) => resolve({ kind: "cancelled", reason });
  });
  let outcome;
  try {
    outcome = await Promise.race([
      attempt.then((sent2) => ({ kind: "sent", sent: sent2 })),
      timeoutPromise,
      cancelPromise
    ]);
  } finally {
    clearTimeout(timeoutTimer);
    cancelHandle.unregister();
  }
  if (outcome.kind === "cancelled") {
    return {
      ok: false,
      code: codeForCancelReason(outcome.reason),
      error: `evaluate was cancelled (${outcome.reason}) before it finished; Runtime.terminateExecution was requested best-effort on tab ${msg.tabId} — the expression may still be running if a dialog or a frozen main thread kept the termination itself from taking effect`
    };
  }
  if (outcome.kind === "timeout") {
    terminateBestEffort(msg.tabId);
    return {
      ok: false,
      code: ERROR_CODES.TIMEOUT,
      error: `evaluate exceeded its ${timeoutMs}ms timeout; the expression may still be running in the page (a dialog it opened, an infinite loop, or a pending promise) — this call gave up waiting rather than hang every subsequent browser_bridge_* call on tab ${msg.tabId}`
    };
  }
  const sent = outcome.sent;
  if (!sent.ok) {
    return { ok: false, code: sent.error.code, error: sent.error.message };
  }
  if (sent.result.exceptionDetails) {
    const detail = sent.result.exceptionDetails.exception?.description ?? sent.result.exceptionDetails.text ?? "evaluate threw";
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: detail };
  }
  const preview = previewOf(sent.result.result);
  const policy = redactionPolicyOf(settings);
  const { text: redacted, truncated } = redactThenTruncate(preview.text, MAX_RESULT_CHARS, policy);
  return {
    ok: true,
    data: {
      result: redacted,
      resultType: preview.type,
      resultSubtype: preview.subtype,
      truncated
    }
  };
}
const CORRELATION_WINDOW_MS = 1e4;
const MAX_ATTRIBUTION_ENTRIES = 200;
const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;
const MAX_WAIT_MS = 12e4;
const WAIT_POLL_MS = 500;
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
function safeOrigin$2(url) {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}
const recentActivity = /* @__PURE__ */ new Map();
const attribution = /* @__PURE__ */ new Map();
function pruneAttribution() {
  while (attribution.size > MAX_ATTRIBUTION_ENTRIES) {
    const oldestKey = attribution.keys().next().value;
    if (oldestKey === void 0) break;
    attribution.delete(oldestKey);
  }
}
function noteActivity(tabId) {
  recentActivity.set(tabId, Date.now());
}
function correlateTabId() {
  const now = Date.now();
  let bestTab;
  let bestTs = -Infinity;
  for (const [tabId, ts] of recentActivity) {
    if (now - ts > CORRELATION_WINDOW_MS) continue;
    if (!cdp.isAttached(tabId)) continue;
    if (ts >= bestTs) {
      bestTab = tabId;
      bestTs = ts;
    }
  }
  return bestTab;
}
function attributeIfUnseen(downloadId) {
  if (attribution.has(downloadId)) return;
  const tabId = correlateTabId();
  if (tabId === void 0) return;
  attribution.set(downloadId, { tabId, correlatedAt: Date.now() });
  pruneAttribution();
}
chrome.downloads.onCreated.addListener((item) => {
  attributeIfUnseen(item.id);
});
chrome.downloads.onChanged.addListener((delta) => {
  attributeIfUnseen(delta.id);
});
cdp.onRelease((tabId) => {
  recentActivity.delete(tabId);
  cancelDownloadWaitsForTab(tabId);
});
const activeWaits = /* @__PURE__ */ new Map();
let waitCounter = 0;
function registerDownloadWait(tabId) {
  const id = `dl-wait-${++waitCounter}`;
  const cancelHandle = registerCancellable(tabId, "download-wait", () => {
    cancelDownloadWait(id);
  });
  activeWaits.set(id, { tabId, cancelled: false, cancelHandle });
  return id;
}
function isWaitCancelled(id) {
  return activeWaits.get(id)?.cancelled ?? false;
}
function releaseDownloadWait(id) {
  activeWaits.get(id)?.cancelHandle.unregister();
  activeWaits.delete(id);
}
function cancelDownloadWait(waitId) {
  const wait = activeWaits.get(waitId);
  if (!wait) return false;
  wait.cancelled = true;
  return true;
}
function cancelDownloadWaitsForTab(tabId) {
  let count = 0;
  for (const wait of activeWaits.values()) {
    if (wait.tabId === tabId && !wait.cancelled) {
      wait.cancelled = true;
      count += 1;
    }
  }
  return count;
}
function redactQuery(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.search) parsed.search = "";
    return parsed.toString();
  } catch {
    return rawUrl;
  }
}
function shape(item) {
  return {
    id: item.id,
    filename: item.filename,
    url: redactQuery(item.finalUrl || item.url || ""),
    mime: item.mime ?? "",
    bytesReceived: item.bytesReceived ?? 0,
    totalBytes: item.totalBytes ?? 0,
    state: item.state ?? "unknown",
    startTime: item.startTime ?? "",
    ...item.endTime ? { endTime: item.endTime } : {},
    danger: item.danger ?? "unknown"
  };
}
async function matchingDownloads(tabId, filter, limit) {
  let tabOrigin = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    tabOrigin = safeOrigin$2(tab.url ?? "");
  } catch {
  }
  const items = await chrome.downloads.search({ orderBy: ["-startTime"], limit: Math.max(limit * 4, 100) });
  const filterLower = filter?.toLowerCase();
  const matched = items.filter((item) => {
    const itemUrl = item.finalUrl || item.url || "";
    const itemOrigin = safeOrigin$2(itemUrl);
    const originMatches = Boolean(tabOrigin) && itemOrigin === tabOrigin;
    const correlated = attribution.get(item.id)?.tabId === tabId;
    if (!originMatches && !correlated) return false;
    if (filterLower) {
      const haystack = `${item.filename} ${itemUrl}`.toLowerCase();
      if (!haystack.includes(filterLower)) return false;
    }
    return true;
  });
  return matched.slice(0, limit).map(shape);
}
function clampWaitMs(requested) {
  return Math.max(0, Math.min(requested ?? 0, MAX_WAIT_MS));
}
function clampLimit(requested) {
  return Math.max(1, Math.min(requested ?? DEFAULT_LIMIT, MAX_LIMIT));
}
async function handleDownloadsSearch(msg) {
  const denial = await assertDownloadsReadAllowed();
  if (denial) return { ok: false, code: denial.code, error: denial.message };
  const limitedRefusal = cdpOnlyGate(msg.tabId, "downloads.search");
  if (limitedRefusal) {
    return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
  }
  const limit = clampLimit(msg.limit);
  const waitMs = clampWaitMs(msg.waitMs);
  if (waitMs === 0) {
    const downloads = await matchingDownloads(msg.tabId, msg.filter, limit);
    return { ok: true, data: { downloads } };
  }
  const waitId = registerDownloadWait(msg.tabId);
  try {
    const deadline = Date.now() + waitMs;
    for (; ; ) {
      const downloads = await matchingDownloads(msg.tabId, msg.filter, limit);
      const complete = downloads.some((d) => d.state === "complete" && d.bytesReceived > 0);
      if (complete) return { ok: true, data: { downloads, timedOut: false } };
      if (isWaitCancelled(waitId)) return { ok: true, data: { downloads, timedOut: false, cancelled: true } };
      if (Date.now() >= deadline) return { ok: true, data: { downloads, timedOut: true } };
      await sleep(Math.min(WAIT_POLL_MS, deadline - Date.now()));
    }
  } finally {
    releaseDownloadWait(waitId);
  }
}
function safeOrigin$1(url) {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}
function tabRefOf$1(tab) {
  return {
    tabId: tab.id ?? -1,
    windowId: tab.windowId,
    groupId: tab.groupId,
    url: tab.url ?? "",
    origin: safeOrigin$1(tab.url ?? ""),
    title: tab.title ?? "",
    active: tab.active,
    attached: typeof tab.id === "number" && cdp.isAttached(tab.id)
  };
}
const NO_GROUP = -1;
function pushToOffscreen$1(message) {
  void chrome.runtime.sendMessage(message).catch(() => {
  });
}
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: "normal" });
  return tab;
}
async function syncAttached(tabIds) {
  const refs = [];
  for (const tabId of tabIds) {
    let ref;
    try {
      ref = tabRefOf$1(await chrome.tabs.get(tabId));
    } catch {
      ref = { tabId, attached: cdp.isAttached(tabId) };
    }
    refs.push(ref);
    pushToOffscreen$1({ target: "offscreen", type: "tab.changed", tab: ref, change: "updated" });
  }
  return refs;
}
async function applyPause(fields) {
  await setSettings(fields);
  await chrome.runtime.sendMessage({ target: "offscreen", type: "pause.changed" });
}
async function resumeForUserShare() {
  try {
    const settings = await getSettings();
    if (!settings.paused) return null;
    const prior = { paused: true, pauseReason: settings.pauseReason, pausedAt: settings.pausedAt };
    await applyPause({ paused: false, pauseReason: null, pausedAt: null });
    return prior;
  } catch {
    return null;
  }
}
async function restorePause(prior) {
  if (!prior) return;
  try {
    await applyPause(prior);
  } catch {
  }
}
async function attachOneTab(tabId) {
  const prior = await resumeForUserShare();
  const result = await cdp.attach(tabId);
  if (!result.ok) {
    await restorePause(prior);
    return { ok: false, code: result.error.code, error: result.error.message };
  }
  return { ok: true, data: { attached: await syncAttached([tabId]) } };
}
async function attachTabsGroup(groupId) {
  const prior = await resumeForUserShare();
  const result = await cdp.attachGroup(groupId);
  if (!result.ok) {
    await restorePause(prior);
    return { ok: false, code: result.error.code, error: result.error.message };
  }
  return { ok: true, data: { attached: await syncAttached(result.result.tabIds), failed: result.result.failed } };
}
async function handleTabsShareTab(msg) {
  return attachOneTab(msg.tabId);
}
async function handleTabsShareActive(msg) {
  const tab = await activeTab();
  if (!tab || typeof tab.id !== "number") {
    return {
      ok: false,
      code: ERROR_CODES.INVALID_PARAMS,
      error: "couldn't find the tab you're looking at — switch to it, then try Share again"
    };
  }
  if (msg.group) {
    if (typeof tab.groupId !== "number" || tab.groupId === NO_GROUP) {
      return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "this tab isn't in a tab group" };
    }
    return attachTabsGroup(tab.groupId);
  }
  return attachOneTab(tab.id);
}
const MENU_ID_TAB = "hermes-share-tab";
const MENU_ID_GROUP = "hermes-share-group";
function createMenuItem(props) {
  chrome.contextMenus.create(props, () => {
    void chrome.runtime.lastError;
  });
}
async function refreshGroupMenuVisibility(tab) {
  const target = tab ?? await activeTab();
  const grouped = typeof target?.groupId === "number" && target.groupId !== NO_GROUP;
  chrome.contextMenus.update(MENU_ID_GROUP, { visible: grouped }, () => {
    void chrome.runtime.lastError;
  });
}
function registerShareContextMenus() {
  chrome.contextMenus.removeAll(() => {
    void chrome.runtime.lastError;
    createMenuItem({ id: MENU_ID_TAB, title: "Share this tab with Hermes", contexts: ["page"] });
    createMenuItem({ id: MENU_ID_GROUP, title: "Share this tab's group with Hermes", contexts: ["page"], visible: false });
    void refreshGroupMenuVisibility();
  });
}
function notifyShareIssue(tab, title, message) {
  chrome.notifications.create(`hermes-share-error-${tab.id ?? "unknown"}-${Date.now()}`, {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title,
    message,
    priority: 1
  });
}
function describeFailures(failed) {
  return failed.map((f) => f.message).join("; ");
}
async function shareFromContextMenu(tab, group) {
  if (!tab || typeof tab.id !== "number") return;
  if (group) {
    if (typeof tab.groupId !== "number" || tab.groupId === NO_GROUP) {
      notifyShareIssue(tab, "Hermes couldn't share that tab", "this tab isn't in a tab group");
      return;
    }
    const result2 = await attachTabsGroup(tab.groupId);
    if (!result2.ok) {
      notifyShareIssue(tab, "Hermes couldn't share that tab group", result2.error ?? "attach failed");
      return;
    }
    const data = result2.data;
    const failed = data?.failed ?? [];
    if (failed.length > 0) {
      const attachedCount = data?.attached.length ?? 0;
      notifyShareIssue(
        tab,
        "Hermes only partly shared that group",
        `Shared ${attachedCount} of ${attachedCount + failed.length} tabs. Couldn't share: ${describeFailures(failed)}`
      );
    }
    return;
  }
  const result = await attachOneTab(tab.id);
  if (!result.ok) notifyShareIssue(tab, "Hermes couldn't share that tab", result.error ?? "attach failed");
}
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === MENU_ID_TAB) void shareFromContextMenu(tab, false);
  else if (info.menuItemId === MENU_ID_GROUP) void shareFromContextMenu(tab, true);
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
  void chrome.tabs.get(tabId).then((tab) => refreshGroupMenuVisibility(tab)).catch(() => {
  });
});
chrome.tabs.onUpdated.addListener((_tabId, _changeInfo, tab) => {
  if (tab.active) void refreshGroupMenuVisibility(tab);
});
const COMMIT_WAIT_MS = 3e3;
const COMMIT_POLL_MS = 100;
const AGENT_OPENED_TAB_IDS_KEY = "openTab.agentOpenedTabIds";
async function readAgentOpenedTabIds() {
  const stored = await chrome.storage.session.get(AGENT_OPENED_TAB_IDS_KEY);
  const raw = stored[AGENT_OPENED_TAB_IDS_KEY];
  return new Set(Array.isArray(raw) ? raw.filter((id) => typeof id === "number") : []);
}
async function writeAgentOpenedTabIds(ids) {
  await chrome.storage.session.set({ [AGENT_OPENED_TAB_IDS_KEY]: [...ids] });
}
async function markAgentOpenedTab(tabId) {
  const ids = await readAgentOpenedTabIds();
  ids.add(tabId);
  await writeAgentOpenedTabIds(ids);
}
async function forgetAgentOpenedTab(tabId) {
  const ids = await readAgentOpenedTabIds();
  if (!ids.delete(tabId)) return;
  await writeAgentOpenedTabIds(ids);
}
if (typeof chrome !== "undefined" && chrome.tabs?.onRemoved?.addListener) {
  chrome.tabs.onRemoved.addListener((tabId) => {
    void forgetAgentOpenedTab(tabId);
  });
}
function isHttpUrl(raw) {
  try {
    const scheme = new URL(raw).protocol.toLowerCase();
    return scheme === "http:" || scheme === "https:";
  } catch {
    return false;
  }
}
async function waitForCommit(tabId) {
  const deadline = Date.now() + COMMIT_WAIT_MS;
  let last;
  while (Date.now() < deadline) {
    try {
      last = await chrome.tabs.get(tabId);
    } catch {
      return void 0;
    }
    const url = last.url ?? "";
    if (url && url !== "about:blank" && last.status !== "loading") return last;
    await new Promise((resolve) => setTimeout(resolve, COMMIT_POLL_MS));
  }
  return last;
}
async function handleTabsCreate(msg, toTabRef) {
  if (!isHttpUrl(msg.url)) {
    return {
      ok: false,
      code: ERROR_CODES.URL_SCHEME_BLOCKED,
      error: `refusing to open ${msg.url} — only http and https URLs can be opened`
    };
  }
  let created;
  try {
    created = await chrome.tabs.create({
      url: msg.url,
      // Background by default. An agent that steals focus every time it opens
      // a tab makes the browser unusable for the person sitting in front of it.
      active: msg.active === true,
      ...typeof msg.windowId === "number" ? { windowId: msg.windowId } : {}
    });
  } catch (error) {
    return { ok: false, code: ERROR_CODES.INTERNAL_ERROR, error: `could not open a tab: ${describeError(error)}` };
  }
  const tabId = created.id;
  if (typeof tabId !== "number") {
    return { ok: false, code: ERROR_CODES.INTERNAL_ERROR, error: "Chrome created a tab with no id" };
  }
  await markAgentOpenedTab(tabId);
  const settled = await waitForCommit(tabId);
  if (!settled) {
    return { ok: false, code: ERROR_CODES.INTERNAL_ERROR, error: `tab ${tabId} was closed before it finished opening` };
  }
  if (msg.attach === false) {
    return { ok: true, data: { tab: toTabRef(settled), attached: false } };
  }
  const attached = await cdp.attach(tabId);
  if (!attached.ok) {
    return {
      ok: true,
      data: {
        tab: toTabRef(settled),
        attached: false,
        attachError: attached.error.message
      }
    };
  }
  let final = settled;
  try {
    final = await chrome.tabs.get(tabId);
  } catch {
  }
  return { ok: true, data: { tab: toTabRef(final), attached: true } };
}
async function handleTabsClose(msg) {
  const tabIds = Array.isArray(msg.tabIds) ? msg.tabIds.filter((id) => typeof id === "number" && Number.isFinite(id)) : [];
  const closed = [];
  const refused = [];
  const agentOpened = await readAgentOpenedTabIds();
  for (const tabId of tabIds) {
    if (!agentOpened.has(tabId)) {
      refused.push({
        tabId,
        code: ERROR_CODES.TAB_NOT_AGENT_OPENED,
        error: `tab ${tabId} was not opened by this extension via tabs.create — refusing to close it`
      });
      continue;
    }
    try {
      await chrome.tabs.get(tabId);
    } catch {
      continue;
    }
    try {
      await cdp.release(tabId);
    } catch {
    }
    try {
      await chrome.tabs.remove(tabId);
      closed.push(tabId);
    } catch {
    }
  }
  return { ok: true, data: { closed, refused } };
}
const OFFSCREEN_PATH = "offscreen.html";
const KEEPALIVE_ALARM = "bridge-keepalive";
const KEEPALIVE_PERIOD_MINUTES = 0.5;
const NOTIFIED_APPROVALS_KEY = "approvals.notified";
const APPROVAL_NOTIFICATION_PREFIX = "hermes-approval-";
const BADGE = {
  connected: { text: "●", color: "#1a7f37" },
  connecting: { text: "…", color: "#9a6700" },
  pairing: { text: "…", color: "#9a6700" },
  disconnected: { text: "", color: "#6e7781" },
  error: { text: "!", color: "#cf222e" }
};
const APPROVAL_BADGE_COLOR = "#8250df";
let latestBridgeStatus = null;
let latestPendingApprovalCount = 0;
async function ensureOffscreen() {
  const existing = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT]
  });
  if (existing.length > 0) return;
  try {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_PATH,
      reasons: [chrome.offscreen.Reason.WORKERS],
      justification: "Maintains the persistent WebSocket connection to the Hermes gateway and the bridge's RPC state."
    });
  } catch (error) {
    if (!String(error).includes("Only a single offscreen")) throw error;
  }
}
const PAUSED_BADGE = { text: "⏸", color: "#9a6700" };
function paintBadge() {
  if (latestPendingApprovalCount > 0) {
    void chrome.action.setBadgeText({ text: String(latestPendingApprovalCount) });
    void chrome.action.setBadgeBackgroundColor({ color: APPROVAL_BADGE_COLOR });
    void chrome.action.setTitle({
      title: `Hermes Browser Bridge: ${latestPendingApprovalCount} approval${latestPendingApprovalCount === 1 ? "" : "s"} waiting`
    });
    return;
  }
  const status = latestBridgeStatus;
  if (status?.paused) {
    void chrome.action.setBadgeText({ text: PAUSED_BADGE.text });
    void chrome.action.setBadgeBackgroundColor({ color: PAUSED_BADGE.color });
    void chrome.action.setTitle({ title: "Hermes Browser Bridge: sharing paused — nothing is being read or driven" });
    return;
  }
  const shared = sharedTabCount();
  if (shared > 0) {
    void chrome.action.setBadgeText({ text: String(shared) });
    void chrome.action.setBadgeBackgroundColor({ color: SHARED_BADGE_COLOR });
    void chrome.action.setTitle({
      title: `Hermes Browser Bridge: sharing ${shared} tab${shared === 1 ? "" : "s"} with Hermes`
    });
    return;
  }
  const badge = (status && BADGE[status.state]) ?? BADGE.disconnected;
  void chrome.action.setBadgeText({ text: badge.text });
  void chrome.action.setBadgeBackgroundColor({ color: badge.color });
  const detail = status?.lastError ? ` — ${status.lastError}` : "";
  void chrome.action.setTitle({ title: `Hermes Browser Bridge: ${status?.state ?? "disconnected"}${detail}` });
}
onSharedCountChanged(paintBadge);
async function handleApprovalsChanged(pendingCount) {
  latestPendingApprovalCount = Math.max(0, pendingCount);
  paintBadge();
  if (latestPendingApprovalCount === 0) {
    await chrome.storage.session.set({ [NOTIFIED_APPROVALS_KEY]: [] });
    return;
  }
  const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "approvals.list" }).catch(() => void 0);
  const approvals = response?.ok ? response.data?.approvals ?? [] : [];
  const liveIds = new Set(approvals.map((approval) => approval.approvalId));
  const stored = await chrome.storage.session.get(NOTIFIED_APPROVALS_KEY);
  const notified = new Set(stored[NOTIFIED_APPROVALS_KEY] ?? []);
  for (const approval of approvals) {
    if (notified.has(approval.approvalId)) continue;
    notified.add(approval.approvalId);
    void chrome.notifications.create(`${APPROVAL_NOTIFICATION_PREFIX}${approval.approvalId}`, {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "Hermes wants your approval",
      message: `${approval.origin || "A site"} wants to ${approval.capability} — open the popup to decide.`,
      priority: 2
    });
  }
  const pruned = [...notified].filter((id) => liveIds.has(id));
  await chrome.storage.session.set({ [NOTIFIED_APPROVALS_KEY]: pruned });
}
function safeOrigin(url) {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}
function tabRefOf(tab) {
  const tabId = tab.id;
  const mode = typeof tabId === "number" ? cdp.modeOf(tabId) : null;
  return {
    tabId: tabId ?? -1,
    windowId: tab.windowId,
    groupId: tab.groupId,
    url: tab.url ?? "",
    origin: safeOrigin(tab.url ?? ""),
    title: tab.title ?? "",
    active: tab.active,
    attached: mode !== null,
    ...mode === "limited" ? { attachMode: "limited", attachModeReason: cdp.limitedReason(tabId) } : {},
    ...mode === "full" ? { attachMode: "full" } : {}
  };
}
async function listTabs(groupId) {
  const tabs = await chrome.tabs.query(typeof groupId === "number" ? { groupId } : {});
  return { tabs: tabs.map(tabRefOf) };
}
async function tabRefsFor(tabIds) {
  const refs = [];
  for (const tabId of tabIds) {
    try {
      refs.push(tabRefOf(await chrome.tabs.get(tabId)));
    } catch {
      refs.push({ tabId, attached: cdp.isShared(tabId) });
    }
  }
  return refs;
}
function pushToOffscreen(message) {
  void chrome.runtime.sendMessage(message).catch(() => {
  });
}
function notifyTabChanged(tab, change) {
  if (typeof tab.id !== "number" || !cdp.isShared(tab.id)) return;
  pushToOffscreen({ target: "offscreen", type: "tab.changed", tab: tabRefOf(tab), change });
}
async function collectInteractiveBoxes(tabId, originPolicy) {
  const result = await contentRequest(tabId, {
    target: "content",
    type: "boxes",
    grantedOrigins: originPolicy?.granted,
    deniedOrigins: originPolicy?.denied,
    defaultFull: originPolicy?.defaultFull
  });
  return result.ok ? result.data : void 0;
}
async function withMarks(tabId, wantMarks, originPolicy, capture) {
  if (!wantMarks) return capture([]);
  const contentBoxes = await collectInteractiveBoxes(tabId, originPolicy);
  const marks = prioritizeMarks(contentBoxes?.boxes ?? [], contentBoxes?.viewport);
  if (marks.length === 0) return capture(marks);
  await presenceShowMarks(tabId, marks);
  try {
    return await capture(marks);
  } finally {
    void presenceHideMarks(tabId);
  }
}
async function handleTabsAttach(msg) {
  const reloadIfBlocked = msg.reloadIfBlocked === true;
  if (typeof msg.groupId === "number") {
    const result = await cdp.attachGroup(msg.groupId, reloadIfBlocked);
    if (!result.ok) return { ok: false, code: result.error.code, error: result.error.message };
    return { ok: true, data: { attached: await tabRefsFor(result.result.tabIds), failed: result.result.failed } };
  }
  if (typeof msg.tabId === "number") {
    const result = await cdp.attach(msg.tabId, reloadIfBlocked);
    if (!result.ok) return { ok: false, code: result.error.code, error: result.error.message };
    return { ok: true, data: { attached: await tabRefsFor([msg.tabId]) } };
  }
  return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "tabs.attach requires tabId or groupId" };
}
async function handleDomSnapshot(msg) {
  const viewportCheck = validateViewport(msg.viewport);
  if (viewportCheck && "error" in viewportCheck) {
    return {
      ok: false,
      code: viewportCheck.outOfRange ? ERROR_CODES.VIEWPORT_OUT_OF_RANGE : ERROR_CODES.INVALID_PARAMS,
      error: viewportCheck.error
    };
  }
  if (viewportCheck && !cdp.isAttached(msg.tabId)) {
    const { code, message } = formatRefusal("limited_mode_capability_unavailable", {
      capability: "dom.snapshot with viewport (the emulated-viewport override needs chrome.debugger)"
    });
    return { ok: false, code: code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, error: message };
  }
  if (!viewportCheck) return handleDomSnapshotBody(msg);
  const { result, overrideApplied } = await withViewportOverride(
    msg.tabId,
    viewportCheck,
    () => settle(msg.tabId, 1500).then(() => void 0),
    () => handleDomSnapshotBody(msg)
  );
  if (result.ok && result.data && overrideApplied) {
    result.data.viewportUsed = viewportCheck;
  }
  return result;
}
async function handleDomSnapshotBody(msg) {
  if (!cdp.isShared(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  const op = presenceBegin(msg.tabId, CAPTURE_LABELS["dom.snapshot"]);
  try {
    const originPolicy = {
      granted: msg.grantedOrigins ?? [],
      denied: msg.deniedOrigins ?? [],
      defaultFull: msg.defaultFull === true
    };
    const aggregated = await aggregateSnapshot(msg.tabId, msg.budgetBytes ?? 4096, msg.selector, originPolicy, {
      find: msg.find,
      startIndex: msg.startIndex,
      dialogOnly: msg.dialogOnly,
      viewportOnly: msg.viewportOnly,
      interactiveOnly: msg.interactiveOnly,
      // speedimprovements.md B4 (stable element refs): forwarded unchanged to
      // aggregateSnapshot's own shared IndexAssigner — see that function's
      // doc comment.
      existingIndexMap: msg.existingIndexMap
    });
    if (!aggregated.ok || !aggregated.data) {
      return { ok: false, code: aggregated.code, error: aggregated.error };
    }
    const { frameGaps, pageMeta, ...rest } = aggregated.data;
    const result = {
      ok: true,
      data: { ...pageMeta, ...rest, ...frameGaps.length > 0 ? { frameGaps } : {} }
    };
    if (!cdp.isShared(msg.tabId)) return releasedMidCall(msg.tabId);
    if (result.ok && result.data?.indexMap && cdp.isAttached(msg.tabId)) {
      const cdpStart = performance.now();
      const { nodeMap, skipped } = await resolveBackendNodeIds(msg.tabId, result.data.indexMap);
      const cdpMs = Math.round((performance.now() - cdpStart) * 10) / 10;
      if (Object.keys(nodeMap).length > 0) result.data.nodeMap = nodeMap;
      if (skipped > 0) result.data.nodeMapSkipped = skipped;
      result.data.timing = { cdp_ms: cdpMs };
      if (!cdp.isShared(msg.tabId)) return releasedMidCall(msg.tabId);
    }
    return result;
  } finally {
    void presenceEnd(op);
  }
}
async function handleDomInspect(msg) {
  if (!cdp.isShared(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  if (msg.question === "listeners") {
    const limitedRefusal = cdpOnlyGate(msg.tabId, "inspect(listeners)");
    if (limitedRefusal) {
      return { ok: false, code: limitedRefusal.code, error: limitedRefusal.message };
    }
  }
  const op = presenceBegin(msg.tabId, CAPTURE_LABELS["dom.inspect"]);
  try {
    const result = await contentRequest(msg.tabId, {
      target: "content",
      type: "inspect",
      question: msg.question,
      selector: msg.selector,
      x: msg.x,
      y: msg.y,
      props: msg.props,
      existingIndexMap: msg.existingIndexMap,
      nextIdx: msg.nextIdx,
      grantedOrigins: msg.grantedOrigins,
      deniedOrigins: msg.deniedOrigins,
      defaultFull: msg.defaultFull
    });
    if (!cdp.isShared(msg.tabId)) return releasedMidCall(msg.tabId);
    if (msg.question === "listeners" && result.ok && msg.selector) {
      return await attachListenerData(msg.tabId, msg.selector, result);
    }
    return result;
  } finally {
    void presenceEnd(op);
  }
}
async function attachListenerData(tabId, selfSelector, contentResult) {
  if (!contentResult.ok) return contentResult;
  const data = contentResult.data;
  const chain = [
    { selector: selfSelector, ...data.self },
    ...data.ancestors ?? []
  ];
  const events = {};
  for (let i = 0; i < chain.length; i++) {
    const node = chain[i];
    const backendNodeId = await resolveNodeHandleOnDemand(tabId, node.selector);
    if (typeof backendNodeId !== "number") continue;
    const resolved = await cdp.send(tabId, "DOM.resolveNode", { backendNodeId });
    const objectId = resolved.ok ? resolved.result?.object?.objectId : void 0;
    if (!objectId) continue;
    const listeners = await cdp.send(tabId, "DOMDebugger.getEventListeners", {
      objectId,
      depth: 0
    });
    if (!listeners.ok) continue;
    const types = new Set((listeners.result?.listeners ?? []).map((l) => l.type).filter((t) => typeof t === "string"));
    if (i === 0) {
      for (const type of types) events[type] = { onElement: true };
    } else {
      for (const type of types) {
        if (type !== "click" && type !== "keydown") continue;
        if (events[type]?.onElement) continue;
        const entry = events[type] ?? { onElement: false, delegatedFrom: [] };
        entry.delegatedFrom = entry.delegatedFrom ?? [];
        if (typeof node.idx === "number") {
          entry.delegatedFrom.push({ level: i, idx: node.idx, role: node.role ?? "generic", name: node.name ?? "" });
        }
        events[type] = entry;
      }
    }
  }
  return {
    ok: true,
    data: { question: "listeners", self: data.self, events, checkedAncestorLevels: chain.length - 1 }
  };
}
async function handlePageRead(msg) {
  if (!cdp.isShared(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  const op = presenceBegin(msg.tabId, CAPTURE_LABELS["page.read"]);
  try {
    const result = await contentRequest(msg.tabId, {
      target: "content",
      type: "read",
      selector: msg.selector,
      format: msg.format ?? "markdown"
    });
    return cdp.isShared(msg.tabId) ? result : releasedMidCall(msg.tabId);
  } finally {
    void presenceEnd(op);
  }
}
async function handlePageScreenshot(msg) {
  const viewportCheck = validateViewport(msg.viewport);
  if (viewportCheck && "error" in viewportCheck) {
    return {
      ok: false,
      code: viewportCheck.outOfRange ? ERROR_CODES.VIEWPORT_OUT_OF_RANGE : ERROR_CODES.INVALID_PARAMS,
      error: viewportCheck.error
    };
  }
  if (viewportCheck && !cdp.isAttached(msg.tabId)) {
    const { code, message } = formatRefusal("limited_mode_capability_unavailable", {
      capability: "page.screenshot with viewport (the emulated-viewport override needs chrome.debugger)"
    });
    return { ok: false, code: code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, error: message };
  }
  if (!viewportCheck) return handlePageScreenshotBody(msg);
  const { result, overrideApplied } = await withViewportOverride(
    msg.tabId,
    viewportCheck,
    () => settle(msg.tabId, 1500).then(() => void 0),
    () => handlePageScreenshotBody(msg)
  );
  if (result.ok && result.data && overrideApplied) {
    result.data.viewportUsed = viewportCheck;
  }
  return result;
}
async function handlePageScreenshotBody(msg) {
  if (!cdp.isShared(msg.tabId)) {
    return { ok: false, code: ERROR_CODES.TARGET_NOT_ATTACHED, error: `tab ${msg.tabId} is not attached` };
  }
  const originPolicy = {
    granted: msg.grantedOrigins ?? [],
    denied: msg.deniedOrigins ?? [],
    defaultFull: msg.defaultFull === true
  };
  if (!cdp.isAttached(msg.tabId)) {
    if (msg.selector || msg.region || msg.full) {
      const { code, message } = formatRefusal("limited_mode_capability_unavailable", {
        capability: "page.screenshot with selector/region/full (clipped capture needs chrome.debugger)"
      });
      return { ok: false, code: code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, error: message };
    }
    const op2 = presenceBegin(msg.tabId, CAPTURE_LABELS["page.screenshot"]);
    try {
      const result = await captureScreenshotLimited(msg.tabId, msg, originPolicy);
      return cdp.isShared(msg.tabId) ? result : releasedMidCall(msg.tabId);
    } finally {
      void presenceEnd(op2);
    }
  }
  const op = presenceBegin(msg.tabId, CAPTURE_LABELS["page.screenshot"]);
  try {
    const result = await captureScreenshot(msg, originPolicy);
    return cdp.isAttached(msg.tabId) ? result : releasedMidCall(msg.tabId);
  } finally {
    void presenceEnd(op);
  }
}
async function captureScreenshotLimited(tabId, msg, originPolicy) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return { ok: false, code: ERROR_CODES.CDP_ERROR, error: `tab ${tabId} no longer exists` };
  }
  if (!tab.active || typeof tab.windowId !== "number") {
    const { code, message } = formatRefusal("limited_mode_capability_unavailable", {
      capability: "page.screenshot (this tab is not the active tab of its window; chrome.tabs.captureVisibleTab can only capture whichever tab is actually focused)"
    });
    return { ok: false, code: code ?? ERROR_CODES.LIMITED_MODE_CAPABILITY_UNAVAILABLE, error: message };
  }
  const format = resolveFormat(msg.format);
  const scale = resolveScale(msg.scale);
  const quality = format === "jpeg" ? resolveQuality(msg.quality) : void 0;
  const notes = ["limited mode: whole-viewport capture only (no clip/selector/full-page)"];
  return withMarks(tabId, Boolean(msg.marks), originPolicy, async (marks) => {
    try {
      const captureOpts = { format };
      if (quality !== void 0) captureOpts.quality = quality;
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, captureOpts);
      let png_b64 = dataUrl.startsWith("data:") ? dataUrl.slice(dataUrl.indexOf(",") + 1) : dataUrl;
      let image = await decodeImageDims(png_b64, mimeOf(format));
      let effectiveScale = 1;
      if (scale !== 1) {
        const scaled = await reencodeScaled(png_b64, mimeOf(format), scale, format, quality);
        if (scaled) {
          png_b64 = scaled.b64;
          image = { width: scaled.width, height: scaled.height };
          effectiveScale = scale;
        } else {
          notes.push("scale could not be applied (re-encode failed); returning the unscaled capture");
        }
      }
      return {
        ok: true,
        data: {
          png_b64,
          format,
          scale: effectiveScale,
          image,
          boxes: [],
          ...marks.length > 0 ? { marks } : {},
          note: notes.join("; ")
        }
      };
    } catch (error) {
      return { ok: false, code: ERROR_CODES.CDP_ERROR, error: `captureVisibleTab failed: ${describeError(error)}` };
    }
  });
}
async function captureScreenshot(msg, originPolicy) {
  noteAction(msg.tabId, "screenshot");
  const format = resolveFormat(msg.format);
  const scale = resolveScale(msg.scale);
  const quality = format === "jpeg" ? resolveQuality(msg.quality) : void 0;
  const notes = [];
  let target;
  if (msg.selector) {
    let frameId = 0;
    let localSelector = msg.selector;
    let offsetX = 0;
    let offsetY = 0;
    if (isFrameQualified(msg.selector)) {
      const resolvedFrame = await resolveFrameTarget(msg.tabId, msg.selector);
      if (!resolvedFrame.ok) {
        return { ok: false, code: resolvedFrame.code, error: resolvedFrame.error };
      }
      frameId = resolvedFrame.result.frameId;
      localSelector = resolvedFrame.result.localSelector;
      offsetX = resolvedFrame.result.offsetX;
      offsetY = resolvedFrame.result.offsetY;
    }
    const resolved = await contentRequest(
      msg.tabId,
      { target: "content", type: "resolveTarget", selector: localSelector, scroll: true },
      frameId
    );
    if (!resolved.ok || !resolved.data) {
      return {
        ok: false,
        code: resolved.code ?? ERROR_CODES.CONTENT_SCRIPT_ERROR,
        error: resolved.error ?? "resolveTarget failed"
      };
    }
    const found = resolved.data;
    if (!found.found) {
      if (found.invalid) {
        return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: `invalid selector ${msg.selector}: ${found.invalid}` };
      }
      return { ok: false, code: ERROR_CODES.CDP_ERROR, error: `selector not found: ${msg.selector}` };
    }
    target = { x: found.rect.x + offsetX, y: found.rect.y + offsetY, width: found.rect.width, height: found.rect.height };
    const choice = describeChoice(msg.selector, found.matchCount, found.chosenReason);
    if (choice) notes.push(choice);
  } else if (msg.region) {
    const { x, y, width, height } = msg.region;
    if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
      return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "region needs finite x, y and a positive width and height" };
    }
    target = msg.region;
  }
  const metrics = await cdp.send(
    msg.tabId,
    "Page.getLayoutMetrics"
  );
  let placed;
  if (target) {
    if (!metrics.ok) return { ok: false, code: metrics.error.code, error: `cannot place the clip: ${metrics.error.message}` };
    placed = placeClip(target, pageGeometry(metrics.result));
    if (!placed) return { ok: false, code: ERROR_CODES.INVALID_PARAMS, error: "region lies entirely outside the page" };
    if (placed.grown) notes.push(`clip grown to the ${MIN_CAPTURE_PX}x${MIN_CAPTURE_PX} CSS px minimum capture size`);
  }
  let cdpClip;
  if (placed) {
    cdpClip = { ...placed.clip, scale };
  } else if (scale !== 1 && metrics.ok) {
    cdpClip = { ...captureAreaClip(pageGeometry(metrics.result), Boolean(msg.full)), scale };
  }
  return withMarks(msg.tabId, Boolean(msg.marks), originPolicy, async (marks) => {
    const hidden = await presenceHideForCapture(msg.tabId);
    if (!hidden) notes.push("the presence overlay could not be confirmed hidden; it may appear in the image");
    let shot;
    try {
      shot = await cdp.send(msg.tabId, "Page.captureScreenshot", {
        format,
        ...quality !== void 0 ? { quality } : {},
        captureBeyondViewport: Boolean(msg.full) || Boolean(placed?.beyondViewport),
        ...cdpClip ? { clip: cdpClip } : {}
      });
    } finally {
      void presenceRestoreAfterCapture(msg.tabId);
    }
    if (!shot.ok) return { ok: false, code: shot.error.code, error: shot.error.message };
    const image = await decodeImageDims(shot.result.data, mimeOf(format));
    const contentBoxes = await collectInteractiveBoxes(msg.tabId, originPolicy);
    const viewportInfo = contentBoxes?.viewport ? {
      ...contentBoxes.viewport,
      ...metrics.ok ? { scrollX: pageGeometry(metrics.result).pageX, scrollY: pageGeometry(metrics.result).pageY } : {}
    } : void 0;
    return {
      ok: true,
      data: {
        png_b64: shot.result.data,
        format,
        scale,
        image,
        width: metrics.ok ? metrics.result.cssLayoutViewport?.clientWidth : void 0,
        height: metrics.ok ? metrics.result.cssLayoutViewport?.clientHeight : void 0,
        viewport: viewportInfo,
        boxes: contentBoxes?.boxes ?? [],
        ...marks.length > 0 ? { marks } : {},
        ...placed ? { clip: placed.viewportClip } : {},
        ...notes.length > 0 ? { note: notes.join("; ") } : {}
      }
    };
  });
}
async function handleStorageGetSettings() {
  try {
    return { ok: true, data: await getSettings() };
  } catch (error) {
    return { ok: false, error: describeError(error) };
  }
}
async function handleStorageGetCredentials() {
  try {
    return { ok: true, data: await getCredentials() };
  } catch (error) {
    return { ok: false, error: describeError(error) };
  }
}
async function handleStorageSetCredentials(credentials) {
  try {
    await setCredentials(credentials);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: describeError(error) };
  }
}
async function handleStorageClearCredentials() {
  try {
    await clearCredentials();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: describeError(error) };
  }
}
chrome.runtime.onInstalled.addListener(() => {
  void ensureOffscreen();
  void chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
  void cdp.resyncAttachments();
  registerShareContextMenus();
});
chrome.runtime.onStartup.addListener(() => {
  void ensureOffscreen();
  void chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
  void cdp.resyncAttachments();
  registerShareContextMenus();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === KEEPALIVE_ALARM) void ensureOffscreen();
});
cdp.onDetach((tabId) => {
  pushToOffscreen({ target: "offscreen", type: "tab.changed", tab: { tabId, attached: false }, change: "updated" });
});
chrome.tabs.onUpdated.addListener((_tabId, _changeInfo, tab) => notifyTabChanged(tab, "updated"));
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!cdp.isShared(tabId)) return;
  pushToOffscreen({ target: "offscreen", type: "tab.changed", tab: { tabId, attached: false }, change: "removed" });
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (!cdp.isShared(tabId)) return;
  void chrome.tabs.get(tabId).then((tab) => notifyTabChanged(tab, "activated")).catch(() => {
  });
});
if (chrome.tabGroups) {
  chrome.tabGroups.onUpdated.addListener((group) => {
    void chrome.tabs.query({ groupId: group.id }).then((tabs) => {
      for (const tab of tabs) notifyTabChanged(tab, "grouped");
    });
  });
}
chrome.notifications.onClicked.addListener((notificationId) => {
  if (!notificationId.startsWith(APPROVAL_NOTIFICATION_PREFIX)) return;
  void chrome.notifications.clear(notificationId);
  void chrome.action.openPopup().catch(() => {
  });
});
chrome.debugger.onEvent.addListener((source, method) => {
  if (method !== "Page.loadEventFired") return;
  const tabId = source.tabId;
  if (typeof tabId !== "number" || !cdp.isAttached(tabId)) return;
  void chrome.tabs.get(tabId).then((tab) => pushToOffscreen({ target: "offscreen", type: "page.loadFired", tabId, url: tab.url ?? "" })).catch(() => {
  });
});
chrome.runtime.onMessage.addListener(
  (message, sender, sendResponse) => {
    if (message?.target !== "background") return false;
    switch (message.type) {
      case "viewport.resized":
        if (sender.tab) notifyTabChanged(sender.tab, "updated");
        sendResponse({ ok: true });
        return false;
      case "status.changed":
        latestBridgeStatus = message.status;
        paintBadge();
        sendResponse({ ok: true });
        return false;
      case "approvals.changed":
        void handleApprovalsChanged(message.pendingCount).then(() => sendResponse({ ok: true }));
        return true;
      case "tabs.list":
        void listTabs(message.groupId).then((data) => sendResponse({ ok: true, data }));
        return true;
      case "platform.info":
        void chrome.runtime.getPlatformInfo().then((info) => sendResponse({
          ok: true,
          data: { os: info.os, version: chrome.runtime.getManifest().version }
        })).catch((error) => sendResponse({ ok: false, error: String(error) }));
        return true;
      case "storage.getSettings":
        void handleStorageGetSettings().then(sendResponse);
        return true;
      case "storage.getCredentials":
        void handleStorageGetCredentials().then(sendResponse);
        return true;
      case "storage.setCredentials":
        void handleStorageSetCredentials(message.credentials).then(sendResponse);
        return true;
      case "storage.clearCredentials":
        void handleStorageClearCredentials().then(sendResponse);
        return true;
      case "tabs.attach":
        void handleTabsAttach(message).then(sendResponse);
        return true;
      case "tabs.create":
        void handleTabsCreate(message, tabRefOf).then(sendResponse);
        return true;
      case "tabs.shareTab":
        void handleTabsShareTab(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "tabs.shareActive":
        void handleTabsShareActive(message).then(sendResponse);
        return true;
      case "tabs.release":
        void cdp.release(message.tabId).then((released) => sendResponse({ ok: true, data: { released: released ? 1 : 0 } }));
        return true;
      case "tabs.close":
        void handleTabsClose(message).then(sendResponse);
        return true;
      case "presence.stop":
        void handleStopPressed().then((data) => sendResponse({ ok: true, data })).catch((error) => sendResponse({ ok: false, error: describeError(error) }));
        return true;
      case "tabs.releaseAll":
        void cdp.releaseAll().then((released) => sendResponse({ ok: true, data: { released } }));
        return true;
      case "cdp.resync":
        void cdp.resyncAttachments().then((data) => sendResponse({ ok: true, data }));
        return true;
      case "dom.snapshot":
        void handleDomSnapshot(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "dom.inspect":
        void handleDomInspect(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.screenshot":
        void handlePageScreenshot(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.read":
        void handlePageRead(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.act":
        if (typeof message.tabId === "number") noteActivity(message.tabId);
        void handlePageAct(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "annotate":
        void handleAnnotate(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.fetch":
        void handlePageFetch(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "cookies.get":
        void handleCookiesGet(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "cookies.set":
        void handleCookiesSet(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "network.log":
        void handleNetworkLog(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.dialog":
        void handleDialogWire(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "console.entries":
        void handleConsoleEntries(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.evaluate":
        void handlePageEvaluate(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "downloads.search":
        void handleDownloadsSearch(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "page.upload":
        void handleUploadBytes(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "auth.status":
        void handleAuthStatus(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "auth.arm":
        void handleAuthArm(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "auth.disarm":
        void handleAuthDisarm(message).then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      case "auth.listArmed":
        void handleAuthListArmed().then(sendResponse).catch(
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      default:
        return false;
    }
  }
);
void ensureOffscreen();
void cdp.resyncAttachments().then(() => recoverFromPreviousLife());
void resyncAuthListener();
