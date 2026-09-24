import { P as PROTOCOL_VERSION, H as HEARTBEAT_INTERVAL_MS, E as ERROR_CODES, f as formatRefusal } from "./chunks/refusals.js";
import { p as pausedErrorMessage } from "./chunks/pause.js";
import { r as redactionPolicyOf, p as powerPolicyOf, l as leaseSecondsOf } from "./chunks/storage.js";
async function call(type, extra = {}) {
  let response;
  try {
    response = await chrome.runtime.sendMessage({ target: "background", type, ...extra });
  } catch (error) {
    throw new Error(`could not reach the extension's service worker for ${type}: ${String(error)}`);
  }
  if (!response || response.ok !== true) {
    throw new Error(`${type} failed: ${response?.error ?? "no response from the extension's service worker"}`);
  }
  return response.data;
}
async function getSettings() {
  return call("storage.getSettings");
}
async function getCredentials() {
  return call("storage.getCredentials");
}
async function setCredentials(creds) {
  await call("storage.setCredentials", { credentials: creds });
}
async function clearCredentials() {
  await call("storage.clearCredentials");
}
async function getDeviceInfo() {
  const response = await chrome.runtime.sendMessage({
    target: "background",
    type: "platform.info"
  });
  const data = response?.ok ? response.data : void 0;
  return {
    os: typeof data?.os === "string" && data.os ? data.os : "unknown",
    version: typeof data?.version === "string" && data.version ? data.version : "0.0.0"
  };
}
const RECONNECT_BASE_MS = 1e3;
const RECONNECT_MAX_MS = 6e4;
const REQUEST_TIMEOUT_MS = 3e4;
const CLOSE_WATCHDOG_MS = 5e3;
const CONNECT_TIMEOUT_MS = 15e3;
function bridgeError$1(message, code) {
  const err = new Error(message);
  err.code = code;
  return err;
}
class BridgeClient {
  /** Grants as of the last successful device.hello, kept current by
   * recordGrant() when the user changes one. Display only — see
   * BridgeStatus's comment for why the gateway remains the only enforcement
   * point. */
  grants = [];
  /** The gateway's `default_mode`: what an origin with no entry in `grants`
   * actually gets. "" when the gateway never reported one. */
  defaultMode = "";
  ws = null;
  // BUG FIX: the open+hello watchdog for the CURRENT connect attempt. Armed
  // in _connect() right after the socket is created, cleared the instant the
  // attempt settles one way or another (closeSocket() clears it centrally,
  // plus an explicit clear on the hello-success path, which never calls
  // closeSocket()).
  connectTimeoutTimer = null;
  /** Set once `open` fires, so a timeout can distinguish "never reached the
   *  gateway" from "connected, but the handshake never completed" — those
   *  have completely different causes and the wrong message sends you
   *  debugging the network when the socket was fine. */
  socketOpened = false;
  // Whether the in-flight connect attempt is a first-time pairing (a code was
  // supplied) or a reconnect of an already-paired device — read by
  // cancelConnect() to decide whether the "your code may already be spent"
  // warning applies.
  connectingWithPairCode = false;
  // ASK 2 (popup UI): the last credentials presence this client actually
  // observed a successful read of. status() falls back to this instead of
  // asserting `paired: false` when a storage read merely FAILS (service
  // worker asleep, chrome.storage hiccup) — a transient read error must
  // never make a still-paired device flash "not paired" in the popup, which
  // would invite the user to burn a pairing code they don't need.
  lastKnownPaired = false;
  outSeq = 0;
  expectedInSeq = 1;
  nextRequestId = 1;
  pending = /* @__PURE__ */ new Map();
  heartbeatTimer = null;
  reconnectTimer = null;
  closeWatchdogTimer = null;
  // Bumped each time _connect() creates a new socket. Every listener bound to
  // a socket closes over the epoch it was created with, so a late event from
  // a socket that's since been superseded (e.g. the close watchdog gave up on
  // a dead one and a fresh connection is already up) is a no-op instead of
  // nulling out this.ws / clearing the session out from under the live one.
  socketEpoch = 0;
  reconnectAttempts = 0;
  session = "";
  deliberateClose = false;
  // Set for a permanent hello failure (bad/revoked credential, protocol
  // mismatch) so the normal close->reconnect loop doesn't retry forever
  // against a cause retrying can never fix. Cleared on the next explicit
  // connect() (e.g. the user re-pairs or presses Reconnect).
  terminal = false;
  // In-flight connect() guard: without it, two overlapping calls (e.g. a
  // popup double-click) can both race past the `this.ws` check below before
  // either has assigned a socket, opening two of them.
  connectPromise = null;
  state = "disconnected";
  lastError = "";
  connectedAt = null;
  lastHeartbeatAt = null;
  deviceId = "";
  attachedTabIds = [];
  // Mirrored into every heartbeat's `pending_approvals` (protocol/schema.json
  // device.heartbeat, M2) by offscreen.ts, which is the only thing that
  // knows the pending set. Kept here rather than read back from the gateway
  // so `browser_bridge_status` never goes stale between approval events.
  pendingApprovalsCount = 0;
  // Written out as explicit fields + a constructor body rather than
  // TS parameter properties (`constructor(private readonly x: ...)`) —
  // node:test runs these .ts sources through Node's built-in strip-only
  // TypeScript support (no full transform), which errors on that syntax
  // (ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX). Behaviorally identical.
  onInbound;
  onStatusChange;
  constructor(onInbound, onStatusChange) {
    this.onInbound = onInbound;
    this.onStatusChange = onStatusChange;
  }
  /**
   * Never throws: this is polled every couple of seconds by the popup and
   * fed to onStatusChange() after every state transition (emitStatus()), so
   * a rejection here would either hang a sendResponse (offscreen.ts's
   * `status.get`) or become an unhandled rejection that silently drops a
   * status push. If settings/credentials can't be read (service worker
   * asleep and failed to wake, chrome.storage.local threw), that's folded
   * into `lastError` — fail closed (`paired: false`) rather than guess.
   */
  async status() {
    let settings = null;
    let credentials = null;
    let storageError = "";
    let paired = this.lastKnownPaired;
    try {
      settings = await getSettings();
    } catch (error) {
      storageError = `settings unavailable: ${error instanceof Error ? error.message : String(error)}`;
    }
    try {
      credentials = await getCredentials();
      paired = credentials !== null;
      this.lastKnownPaired = paired;
    } catch (error) {
      storageError = storageError || `credentials unavailable: ${error instanceof Error ? error.message : String(error)}`;
    }
    return {
      state: this.state,
      gatewayUrl: settings?.gatewayUrl ?? "",
      deviceName: settings?.deviceName ?? "",
      deviceId: credentials?.deviceId ?? "",
      paired,
      lastError: this.lastError || storageError,
      connectedAt: this.connectedAt,
      lastHeartbeatAt: this.lastHeartbeatAt,
      reconnectAttempts: this.reconnectAttempts,
      attachedTabIds: this.attachedTabIds,
      paused: Boolean(settings?.paused),
      grants: this.grants,
      defaultMode: this.defaultMode
    };
  }
  /** Take the grants table off a hello or heartbeat result.
   *
   * Called from BOTH, deliberately. A grant can change with no extension
   * involvement at all: answering a `request`-mode approval "always" writes
   * one gateway-side (hermes_plugin/approvals.py), and so does the CLI. A
   * picker fed only by hello therefore displayed a stale mode for the rest of
   * the connection — and did so most visibly in the one moment it has to be
   * right, immediately after the user granted standing access. Absorbing on
   * every heartbeat bounds staleness to one interval and self-heals however
   * the grant changed.
   *
   * Defensive about shape because this is wire data: a gateway that sends
   * neither field leaves the picker exactly as it behaved before, blank rather
   * than guessing. A gateway that sends `grants` but not `default_mode` keeps
   * the last known default rather than blanking a value it did not contradict.
   */
  absorbGrants(result) {
    if (Array.isArray(result.grants)) {
      this.grants = result.grants.filter((grant) => typeof grant?.origin === "string" && typeof grant?.mode === "string").map((grant) => ({ origin: String(grant.origin), mode: String(grant.mode) }));
    }
    if (typeof result.default_mode === "string") this.defaultMode = result.default_mode;
  }
  /** Mirror a grant the user just set, so the picker shows the new mode on the
   * next 2s poll instead of snapping back to the old one until the next
   * reconnect. Called by offscreen.ts only after the gateway's grant.set
   * actually resolved — never optimistically, or the popup would show a mode
   * the gateway rejected. */
  recordGrant(origin, mode) {
    const existing = this.grants.findIndex((grant) => grant.origin === origin);
    if (existing >= 0) this.grants[existing] = { origin, mode };
    else this.grants = [...this.grants, { origin, mode }];
  }
  /** Open a connection. A pairing code is used once; afterwards the stored token is. */
  async connect(pairCode) {
    if (this.connectPromise) {
      return this.connectPromise;
    }
    this.connectPromise = this._connect(pairCode).finally(() => {
      this.connectPromise = null;
    });
    return this.connectPromise;
  }
  async _connect(pairCode) {
    this.deliberateClose = false;
    this.terminal = false;
    this.clearReconnect();
    this.clearConnectTimeout();
    this.connectingWithPairCode = Boolean(pairCode);
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      if (!pairCode) return;
      this.closeSocket("reconnecting with a pairing code");
    }
    let settings;
    let credentials;
    try {
      settings = await getSettings();
      credentials = await getCredentials();
    } catch (error) {
      this.fail(
        `cannot read extension settings — the service worker may be unreachable: ${error instanceof Error ? error.message : String(error)}`
      );
      return;
    }
    if (!pairCode && !credentials) {
      this.fail("not paired — enter a pairing code from `hermes browser-bridge pair`");
      return;
    }
    this.setState(pairCode ? "pairing" : "connecting");
    let socket;
    try {
      socket = new WebSocket(settings.gatewayUrl);
    } catch (error) {
      this.fail(`invalid gateway URL: ${String(error)}`);
      return;
    }
    this.ws = socket;
    this.socketOpened = false;
    const epoch = ++this.socketEpoch;
    const isCurrent = () => epoch === this.socketEpoch;
    this.armConnectTimeout(epoch, settings.gatewayUrl);
    socket.addEventListener("open", () => {
      if (!isCurrent()) return;
      this.socketOpened = true;
      void this.sendHello(pairCode).catch((error) => {
        if (!isCurrent()) return;
        const detail = error instanceof Error ? error.message : String(error);
        this.fail(`handshake failed inside the extension: ${detail}`);
        this.closeSocket("handshake threw");
      });
    });
    socket.addEventListener("message", (event) => {
      if (!isCurrent()) return;
      void this.onMessage(event.data);
    });
    socket.addEventListener("close", (event) => {
      if (!isCurrent()) return;
      this.onClose(event.code, event.reason);
    });
    socket.addEventListener("error", () => {
      if (!isCurrent()) return;
      this.lastError = this.lastError || "socket error";
    });
  }
  /**
   * ASK 1 (Cancel during pairing): aborts an in-flight connect()/pairing
   * attempt. Only meaningful while `state` is "connecting" or "pairing" —
   * outside that window there is nothing in flight to cancel, so this is a
   * no-op rather than an error (mirrors disconnect()'s own tolerance).
   *
   * Closes the REAL socket (not just a UI flag): `deliberateClose = true`
   * routes this through the exact same "don't schedule a reconnect" path
   * disconnect() uses, and closeSocket() (called via the connect-timeout
   * machinery's own helper below, reused here rather than duplicated) tears
   * down the socket, rejects any pending requests, and clears the
   * connect-attempt watchdog — so a cancelled attempt can never later fire a
   * stale timeout and clobber whatever state came after it.
   *
   * Never clears credentials (ASK 2's invariant) — cancelling mid-pairing
   * before a token was ever issued has none to clear, and cancelling a
   * reconnect of an already-paired device must leave its stored token
   * exactly alone; the device stays paired, just not connected.
   *
   * The one thing this DOES report: if a pairing code was in flight, the
   * gateway may already have redeemed it (state.redeem_pair_code marks it
   * consumed the moment device.hello's request frame is processed — before
   * this client ever sees a response back). Cancelling doesn't and can't
   * un-consume it server-side, so the honest instruction is "get a fresh
   * one", not "try the same code again".
   */
  cancelConnect() {
    if (this.state !== "connecting" && this.state !== "pairing") return;
    const wasPairing = this.connectingWithPairCode;
    this.deliberateClose = true;
    this.terminal = false;
    this.clearConnectTimeout();
    this.clearReconnect();
    this.closeSocket("cancelled by user");
    this.connectPromise = null;
    this.lastError = wasPairing ? "Pairing cancelled. The code you entered may already be spent — the gateway marks a code used the moment it's submitted, even if you cancel before hearing back. Generate a fresh one with `hermes browser-bridge pair` before trying again." : "Connection attempt cancelled.";
    this.setState("disconnected");
  }
  disconnect(reason = "user requested") {
    this.deliberateClose = true;
    this.clearReconnect();
    if (this.ws?.readyState === WebSocket.OPEN) {
      void this.notify("device.goodbye", { reason });
    }
    this.closeSocket(reason);
    this.setState("disconnected");
  }
  async unpair() {
    this.disconnect("unpaired");
    try {
      await clearCredentials();
    } catch (error) {
      this.lastError = `disconnected, but failed to clear the stored device credentials: ${error instanceof Error ? error.message : String(error)}`;
    }
    this.deviceId = "";
    this.emitStatus();
  }
  /**
   * Called by offscreen.ts's `redaction.changed` handler right after Options
   * saves a new policy. Deliberately a `state.report` push, not a reconnect
   * (see lib/messages.ts's `redaction.changed` doc comment) — a policy change
   * must take effect at the gateway immediately, but must never interrupt an
   * otherwise-healthy connection to do it. Silently a no-op while
   * disconnected: `notify()` just drops the frame on the floor, which is
   * fine, since the next successful `device.hello` already carries the
   * current policy (sendHello() reads it fresh, not from any cache here).
   */
  async reportRedactionPolicy() {
    const settings = await getSettings();
    this.notify("state.report", { redaction: redactionPolicyOf(settings) });
  }
  /**
   * Called by offscreen.ts's `powers.changed` handler right after Options
   * saves a new power policy. Mirrors `reportRedactionPolicy` above exactly
   * — a `state.report` push, never a reconnect — silently a no-op while
   * disconnected (the next successful `device.hello` already carries the
   * current policy, read fresh, not from any cache here).
   */
  async reportPowerPolicy() {
    const settings = await getSettings();
    this.notify("state.report", { powers: powerPolicyOf(settings) });
  }
  /**
   * Called by offscreen.ts's `defaultMode.changed` handler right after
   * Options saves a new default access mode. Mirrors `reportRedactionPolicy`/
   * `reportPowerPolicy` above exactly — a `state.report` push, never a
   * reconnect — silently a no-op while disconnected (the next successful
   * `device.hello` already carries the current setting, read fresh, not from
   * any cache here).
   */
  async reportDefaultMode() {
    const settings = await getSettings();
    this.notify("state.report", { device_default_mode: settings.defaultAccessMode });
  }
  /**
   * Called by offscreen.ts's `lease.changed` handler right after Options
   * saves a new tab lease setting. Mirrors `reportDefaultMode` above exactly
   * — a `state.report` push, never a reconnect — silently a no-op while
   * disconnected (the next successful `device.hello` already carries the
   * current setting, read fresh, not from any cache here).
   */
  async reportLeaseSeconds() {
    const settings = await getSettings();
    this.notify("state.report", { device_lease_seconds: leaseSecondsOf(settings) });
  }
  /**
   * speedimprovements.md H1: called by offscreen.ts's `commitMode.changed`
   * handler right after Options saves a new committing-actions mode.
   * Mirrors `reportDefaultMode`/`reportLeaseSeconds` above exactly — a
   * `state.report` push, never a reconnect — silently a no-op while
   * disconnected (the next successful `device.hello` already carries the
   * current setting, read fresh, not from any cache here).
   */
  async reportCommitMode() {
    const settings = await getSettings();
    this.notify("state.report", { device_commit_mode: settings.commitMode });
  }
  /** Extension → gateway request. Resolves with the result object. */
  async request(method, params = {}) {
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("bridge is not connected");
    }
    const id = this.nextRequestId++;
    const frame = {
      jsonrpc: "2.0",
      id,
      seq: ++this.outSeq,
      ts: Date.now(),
      method,
      params,
      ...this.session ? { session: this.session } : {}
    };
    socket.send(JSON.stringify(frame));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer });
    });
  }
  /** Returns whether the frame was actually put on the wire. `false` (socket
   * missing/not open) is not an error to throw — every existing caller fires
   * a notification best-effort — but offscreen.ts's approval relay needs to
   * tell a real send from a silent no-op: claiming an approval response went
   * out when it didn't would leave the popup believing a decision reached
   * the gateway when nothing did. */
  notify(method, params = {}) {
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(
      JSON.stringify({
        jsonrpc: "2.0",
        seq: ++this.outSeq,
        ts: Date.now(),
        method,
        params,
        ...this.session ? { session: this.session } : {}
      })
    );
    return true;
  }
  // -- internals ---------------------------------------------------------
  async sendHello(pairCode) {
    let settings;
    let credentials;
    try {
      settings = await getSettings();
      credentials = await getCredentials();
    } catch (error) {
      this.fail(
        `cannot read extension settings during handshake — the service worker may be unreachable: ${error instanceof Error ? error.message : String(error)}`
      );
      this.closeSocket("settings unavailable during handshake");
      return;
    }
    const device = await getDeviceInfo().catch(() => ({ os: "unknown", version: "0.0.0" }));
    const params = {
      protocol_version: PROTOCOL_VERSION,
      device: {
        device_name: settings.deviceName || `chrome-${device.os}`,
        platform: device.os,
        browser: "chrome",
        extension_version: device.version,
        capabilities: ["offscreen", "cdp", "tabgroups"]
      },
      // ASK 3: sent on hello (protocol/schema.json's device.hello.paused) so
      // a reconnect that happens while paused (Chrome sleep/resume, a socket
      // drop) doesn't briefly report itself as live/sharing before the next
      // heartbeat catches up.
      paused: Boolean(settings.paused),
      // Reported so the gateway's belt-and-braces redaction re-check
      // (hermes_plugin/tools.py's `_gateway_redact`) honours the same
      // per-kind policy the extension is already applying, rather than
      // re-redacting a kind the user just switched off. Sent on every hello,
      // not only when it changes, so a reconnecting device never leaves the
      // gateway holding a stale policy from before the drop.
      redaction: redactionPolicyOf(settings),
      // Reported so the gateway's device_power_policy table (fail-closed,
      // unlike redaction above) starts a reconnecting device off with the
      // capabilities the user actually has switched on in Options, rather
      // than falling back to "never heard from this device" == everything
      // disabled for the length of one heartbeat interval.
      powers: powerPolicyOf(settings),
      // Reported so the gateway's device_default_mode table (hermes_plugin/
      // state.py) starts a reconnecting device off with the default access
      // mode actually set in Options, rather than falling back to whatever
      // this device last reported (or config.yaml's fleet-wide default_mode,
      // if it has never reported one at all).
      device_default_mode: settings.defaultAccessMode,
      // Reported so the gateway's device_lease_seconds table (hermes_plugin/
      // state.py) starts a reconnecting device off with the tab lease
      // duration actually set in Options, rather than falling back to
      // whatever this device last reported (or config.yaml's fleet-wide
      // lease_seconds, if it has never reported one at all).
      device_lease_seconds: leaseSecondsOf(settings),
      // speedimprovements.md H1: reported so the gateway's device_commit_mode
      // table (hermes_plugin/state.py) starts a reconnecting device off with
      // the committing-actions mode actually set in Options, rather than
      // falling back to whatever this device last reported (or "auto", if
      // it has never reported one at all).
      device_commit_mode: settings.commitMode
    };
    if (pairCode) {
      params.pair_code = pairCode;
    } else if (credentials) {
      params.device_id = credentials.deviceId;
      params.device_token = credentials.deviceToken;
    }
    try {
      const result = await this.request("device.hello", params);
      this.clearConnectTimeout();
      this.session = String(result.session ?? "");
      this.deviceId = String(result.device_id ?? "");
      this.absorbGrants(result);
      let credentialSaveError = "";
      if (typeof result.device_token === "string" && result.device_token) {
        try {
          await setCredentials({ deviceId: this.deviceId, deviceToken: result.device_token });
        } catch (error) {
          credentialSaveError = `connected, but failed to save the device token locally — this device will need to re-pair on the next reconnect: ${error instanceof Error ? error.message : String(error)}`;
        }
      }
      this.connectedAt = Date.now();
      this.reconnectAttempts = 0;
      this.lastError = credentialSaveError;
      this.setState("connected");
      this.startHeartbeat(Number(result.heartbeat_interval_ms) || HEARTBEAT_INTERVAL_MS);
    } catch (error) {
      const code = error instanceof Error ? error.code : void 0;
      if (code === ERROR_CODES.TOKEN_INVALID || code === ERROR_CODES.TOKEN_REVOKED) {
        try {
          await clearCredentials();
        } catch {
        }
        this.deviceId = "";
        this.terminal = true;
        this.fail(
          code === ERROR_CODES.TOKEN_REVOKED ? "this device's pairing was revoked — pair again to reconnect" : "pairing token was rejected — pair again to reconnect"
        );
        this.closeSocket("hello rejected");
        return;
      }
      if (code === ERROR_CODES.PROTOCOL_MISMATCH) {
        this.terminal = true;
        this.fail("extension is out of date for this gateway — update the extension");
        this.closeSocket("protocol mismatch");
        return;
      }
      this.fail(String(error instanceof Error ? error.message : error));
      this.closeSocket("hello failed");
    }
  }
  async onMessage(raw) {
    let frame;
    try {
      frame = JSON.parse(raw);
    } catch {
      return;
    }
    const seq = frame.seq;
    if (typeof seq === "number") {
      if (seq !== this.expectedInSeq && this.state === "connected") {
        this.notify("conn.resync", { expected_seq: this.expectedInSeq, got_seq: seq });
      }
      this.expectedInSeq = seq + 1;
    }
    const asResponse = frame;
    if (asResponse.id !== void 0 && !("method" in frame)) {
      const pending = this.pending.get(Number(asResponse.id));
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(Number(asResponse.id));
      if (asResponse.error) {
        pending.reject(bridgeError$1(asResponse.error.message, asResponse.error.code));
      } else {
        pending.resolve(asResponse.result ?? {});
      }
      return;
    }
    const asRequest = frame;
    if (!asRequest.method) return;
    if (asRequest.method === "device.offline") {
      await this.unpair();
      this.fail("gateway revoked this device — pair again to reconnect");
      return;
    }
    if (asRequest.id === void 0) {
      return;
    }
    try {
      const result = await this.onInbound(asRequest.method, asRequest.params ?? {});
      this.respond(asRequest.id, { result });
    } catch (error) {
      const code = error.code;
      const data = error.data;
      this.respond(asRequest.id, {
        error: {
          code: typeof code === "number" ? code : ERROR_CODES.INTERNAL_ERROR,
          message: error instanceof Error ? error.message : String(error),
          ...data !== void 0 && data !== null ? { data } : {}
        }
      });
    }
  }
  respond(id, payload) {
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(
      JSON.stringify({
        jsonrpc: "2.0",
        id,
        seq: ++this.outSeq,
        ts: Date.now(),
        ...this.session ? { session: this.session } : {},
        ...payload
      })
    );
  }
  startHeartbeat(intervalMs) {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      void getSettings().then((settings) => ({
        paused: settings.paused,
        redaction: redactionPolicyOf(settings),
        powers: powerPolicyOf(settings),
        defaultMode: settings.defaultAccessMode,
        leaseSeconds: leaseSecondsOf(settings),
        commitMode: settings.commitMode
      })).catch(() => ({
        paused: void 0,
        redaction: void 0,
        powers: void 0,
        defaultMode: void 0,
        leaseSeconds: void 0,
        commitMode: void 0
      })).then(
        ({ paused, redaction, powers, defaultMode, leaseSeconds, commitMode }) => this.request("device.heartbeat", {
          attached: this.attachedTabIds.map((tabId) => ({ tabId, attached: true })),
          pending_approvals: this.pendingApprovalsCount,
          uptime_ms: this.connectedAt ? Date.now() - this.connectedAt : 0,
          paused: Boolean(paused),
          ...redaction ? { redaction } : {},
          ...powers ? { powers } : {},
          // Same read-fresh-omit-on-failure treatment as redaction/powers
          // above: a getSettings() failure this tick just leaves the
          // gateway's device_default_mode table exactly as it was, never a
          // reason to skip the heartbeat itself.
          ...defaultMode ? { device_default_mode: defaultMode } : {},
          // Same treatment as defaultMode above, but checked against
          // `undefined` specifically (not truthiness): leaseSeconds is 0
          // for a legitimate "unlimited" report, which must still be sent
          // — only a getSettings() failure (leaseSeconds === undefined)
          // omits this field.
          ...leaseSeconds !== void 0 ? { device_lease_seconds: leaseSeconds } : {},
          // speedimprovements.md H1: same read-fresh-omit-on-failure
          // treatment as defaultMode/leaseSeconds above.
          ...commitMode ? { device_commit_mode: commitMode } : {}
        })
      ).then((result) => {
        this.lastHeartbeatAt = Date.now();
        this.absorbGrants(result);
        this.emitStatus();
      }).catch(() => {
        this.closeSocket("heartbeat failed");
      });
    }, intervalMs);
  }
  stopHeartbeat() {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }
  closeSocket(reason) {
    this.clearConnectTimeout();
    this.stopHeartbeat();
    for (const [, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error(`connection closed: ${reason}`));
    }
    this.pending.clear();
    const socket = this.ws;
    const epoch = this.socketEpoch;
    this.ws = null;
    this.session = "";
    this.outSeq = 0;
    this.expectedInSeq = 1;
    if (socket && socket.readyState !== WebSocket.CLOSED) {
      try {
        socket.close(1e3, reason.slice(0, 120));
      } catch {
      }
      this.armCloseWatchdog(reason, epoch);
    }
  }
  armCloseWatchdog(reason, epoch) {
    this.clearCloseWatchdog();
    this.closeWatchdogTimer = setTimeout(() => {
      this.closeWatchdogTimer = null;
      if (epoch !== this.socketEpoch) return;
      if (this.deliberateClose || this.terminal || this.ws !== null || this.state === "disconnected") return;
      if (!this.lastError) this.lastError = `${reason} (no close event received)`;
      this.setState("disconnected");
      this.scheduleReconnect();
    }, CLOSE_WATCHDOG_MS);
  }
  clearCloseWatchdog() {
    if (this.closeWatchdogTimer !== null) {
      clearTimeout(this.closeWatchdogTimer);
      this.closeWatchdogTimer = null;
    }
  }
  /** BUG FIX: the actionable message for a connect attempt that never
   * finished opening (or never got a hello response) within
   * CONNECT_TIMEOUT_MS — names the URL that was tried and the three things
   * worth checking, rather than a bare "connection failed". */
  connectTimeoutMessage(gatewayUrl) {
    return `could not reach the gateway at ${gatewayUrl} within ${CONNECT_TIMEOUT_MS / 1e3}s. Check: 1) the gateway is running, 2) the URL is correct, 3) this machine can reach that host and port (firewall/VLAN).`;
  }
  armConnectTimeout(epoch, gatewayUrl) {
    this.clearConnectTimeout();
    this.connectTimeoutTimer = setTimeout(() => {
      this.connectTimeoutTimer = null;
      if (epoch !== this.socketEpoch) return;
      if (this.state !== "connecting" && this.state !== "pairing") return;
      this.lastError = this.socketOpened ? `connected to ${gatewayUrl}, but the handshake did not complete within 15s. The network path is fine — this is a bridge-side fault. Open the offscreen document's console (chrome://extensions -> Inspect views: offscreen.html) for the underlying error.` : this.connectTimeoutMessage(gatewayUrl);
      this.terminal = false;
      this.closeSocket("connect timeout");
    }, CONNECT_TIMEOUT_MS);
  }
  clearConnectTimeout() {
    if (this.connectTimeoutTimer !== null) {
      clearTimeout(this.connectTimeoutTimer);
      this.connectTimeoutTimer = null;
    }
  }
  onClose(code, reason) {
    this.stopHeartbeat();
    this.clearCloseWatchdog();
    this.ws = null;
    this.session = "";
    this.connectedAt = null;
    if (this.deliberateClose) {
      this.setState("disconnected");
      return;
    }
    if (this.terminal) {
      return;
    }
    if (!this.lastError) {
      this.lastError = reason || `connection closed (${code})`;
    }
    this.setState("disconnected");
    this.scheduleReconnect();
  }
  scheduleReconnect() {
    if (this.reconnectTimer !== null) return;
    void getCredentials().then((credentials) => {
      if (!credentials) return;
      this.reconnectAttempts += 1;
      const backoff = Math.min(RECONNECT_BASE_MS * 2 ** (this.reconnectAttempts - 1), RECONNECT_MAX_MS);
      const jitter = Math.random() * backoff * 0.3;
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        void this.connect();
      }, backoff + jitter);
    }).catch((error) => {
      this.lastError = `cannot check pairing status to schedule a reconnect: ${error instanceof Error ? error.message : String(error)}`;
      this.emitStatus();
    });
  }
  clearReconnect() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
  fail(message) {
    this.lastError = message;
    this.setState("error");
  }
  /** Public escape hatch for failures that happen entirely outside a
   * connect()/request() cycle — currently only offscreen.ts's module-load
   * auto-connect check, when it can't even read settings/credentials to
   * decide whether to auto-connect at all. Surfaces the same way `fail()`
   * does (lastError + "error" state + a status push) rather than leaving
   * the popup stuck on "Starting…" forever with no explanation. */
  reportError(message) {
    this.fail(message);
  }
  setState(state) {
    this.state = state;
    this.emitStatus();
  }
  emitStatus() {
    void this.status().then((status) => this.onStatusChange(status));
  }
}
let latestStatus = null;
let cachedPaused = false;
let cachedPauseState = {};
function isPaused() {
  return cachedPaused;
}
async function refreshPausedFromSettings() {
  try {
    const settings = await getSettings();
    cachedPaused = Boolean(settings.paused);
    cachedPauseState = { pauseReason: settings.pauseReason ?? null, pausedAt: settings.pausedAt ?? null };
  } catch {
  }
}
const PAUSABLE_METHODS = /* @__PURE__ */ new Set([
  "dom.snapshot",
  "page.screenshot",
  "page.read",
  "page.act",
  "annotate",
  "page.fetch",
  "cookies.get",
  // Mutating (chrome.cookies.set), same reasoning as page.act/tabs.create: a
  // paused user must not have their cookie jar written to.
  "cookies.set",
  "network.log",
  "downloads.search",
  // G1.3 tier 2: assigning bytes to a page's file input is page-content-
  // touching exactly the same way page.act is — a paused user must not have
  // the agent uploading anything on their behalf either. Tier 1 (page.act's
  // upload action) is already covered by "page.act" above.
  "page.upload",
  // G1.4: accepting/dismissing a dialog interacts with the page the same way
  // page.act does — a paused user must not have the agent resolving a
  // dialog on their behalf either.
  "page.dialog",
  // G2.4.7: console output is page content exactly like a network body or a
  // cookie value — a paused user must not have it read out from under them.
  "console.entries",
  // G1.5: evaluate reads and can write page state directly — a paused user
  // must not have arbitrary code running against their page any more than
  // page.act may act on it.
  "page.evaluate",
  // Opening a tab is the most side-effectful thing the gateway can ask for:
  // a paused user must not find new tabs appearing.
  "tabs.create",
  // A gateway-originated attach while paused would re-share what the user
  // just stopped. The user starts sharing through share.ts instead.
  "tabs.attach"
]);
function handleStatusChange(status) {
  const wasConnected = latestStatus?.state === "connected";
  latestStatus = status;
  if (status.state === "connected" && !wasConnected) {
    void delegate({ target: "background", type: "cdp.resync" }).then((result) => {
      if (result.ok) client.attachedTabIds = result.data?.attachedTabIds ?? [];
    });
  }
  if (wasConnected && status.state !== "connected") {
    clearAllPendingApprovals();
  }
  void chrome.runtime.sendMessage({ target: "background", type: "status.changed", status }).catch(() => {
  });
}
const client = new BridgeClient(handleInbound, handleStatusChange);
const pendingApprovals = /* @__PURE__ */ new Map();
function notifyApprovalsChanged() {
  client.pendingApprovalsCount = pendingApprovals.size;
  const message = {
    target: "background",
    type: "approvals.changed",
    pendingCount: pendingApprovals.size
  };
  void chrome.runtime.sendMessage(message).catch(() => {
  });
}
function removePendingApprovalEntry(approvalId, notify = true) {
  const entry = pendingApprovals.get(approvalId);
  if (!entry) return void 0;
  clearTimeout(entry.timer);
  pendingApprovals.delete(approvalId);
  if (notify) notifyApprovalsChanged();
  return entry.approval;
}
function clearAllPendingApprovals() {
  if (pendingApprovals.size === 0) return;
  for (const entry of pendingApprovals.values()) clearTimeout(entry.timer);
  pendingApprovals.clear();
  notifyApprovalsChanged();
}
function addPendingApproval(approval) {
  removePendingApprovalEntry(approval.approvalId, false);
  const delay = Math.max(0, approval.expiresAt - Date.now());
  const timer = setTimeout(() => {
    pendingApprovals.delete(approval.approvalId);
    notifyApprovalsChanged();
  }, delay);
  pendingApprovals.set(approval.approvalId, { approval, timer });
  notifyApprovalsChanged();
}
function listPendingApprovals() {
  const now = Date.now();
  const live = [];
  for (const [approvalId, entry] of pendingApprovals) {
    if (entry.approval.expiresAt <= now) {
      removePendingApprovalEntry(approvalId);
      continue;
    }
    live.push(entry.approval);
  }
  return live;
}
function respondToApproval(approvalId, choice) {
  const approval = removePendingApprovalEntry(approvalId);
  if (!approval) {
    return { ok: false, error: `approval ${approvalId} is unknown or already resolved` };
  }
  const sent = client.notify("approval.respond", { approval_id: approvalId, choice });
  if (!sent) {
    return { ok: false, error: `bridge is not connected -- approval ${approvalId} was already resolved (denied) when the connection dropped` };
  }
  return { ok: true };
}
function bridgeError(code, message, data) {
  const error = new Error(message);
  error.code = code;
  if (data !== void 0) error.data = data;
  return error;
}
function requireOk(result, fallbackCode, fallback) {
  if (!result.ok) throw bridgeError(result.code ?? fallbackCode, result.error ?? fallback, result.data);
  return result.data ?? {};
}
function mergeAttached(existing, added) {
  return [.../* @__PURE__ */ new Set([...existing, ...added])];
}
async function handleInbound(method, params) {
  if (!PAUSABLE_METHODS.has(method)) return runTimed(method, params);
  if (isPaused()) throw pausedError();
  let result;
  try {
    result = await runTimed(method, params);
  } catch (error) {
    if (isPaused()) throw pausedError();
    throw error;
  }
  if (isPaused()) throw pausedError();
  return result;
}
async function runTimed(method, params) {
  const start = performance.now();
  const result = await dispatchInbound(method, params);
  const totalMs = Math.round((performance.now() - start) * 10) / 10;
  const existingTiming = result.timing && typeof result.timing === "object" ? result.timing : {};
  return { ...result, timing: { ...existingTiming, total_ms: totalMs } };
}
function pausedError() {
  return bridgeError(ERROR_CODES.SHARING_PAUSED, pausedErrorMessage(cachedPauseState));
}
async function dispatchInbound(method, params) {
  switch (method) {
    case "kill.switch": {
      typeof params.reason === "string" ? params.reason : void 0;
      const result = await delegate({ target: "background", type: "tabs.releaseAll" });
      client.attachedTabIds = [];
      return { released: result.ok ? result.data?.released ?? 0 : 0 };
    }
    case "tabs.list": {
      const result = await delegate({
        target: "background",
        type: "tabs.list",
        groupId: typeof params.group_id === "number" ? params.group_id : void 0
      });
      return { tabs: requireOk(result, ERROR_CODES.INTERNAL_ERROR, "tabs.list failed").tabs ?? [] };
    }
    case "tabs.attach": {
      const result = await delegate({
        target: "background",
        type: "tabs.attach",
        tabId: typeof params.tabId === "number" ? params.tabId : void 0,
        groupId: typeof params.groupId === "number" ? params.groupId : void 0,
        // G4238: discards unsaved page state, so only ever true when the
        // gateway explicitly asked for it (protocol/schema.json's
        // reload_if_blocked) -- never inferred or defaulted to true here.
        reloadIfBlocked: params.reload_if_blocked === true
      });
      const data = requireOk(result, ERROR_CODES.CDP_ERROR, "attach failed");
      const attached = data.attached ?? [];
      client.attachedTabIds = mergeAttached(client.attachedTabIds, attached.map((tab) => tab.tabId));
      return { attached };
    }
    case "tabs.create": {
      const result = await delegate({
        target: "background",
        type: "tabs.create",
        url: String(params.url ?? ""),
        attach: params.attach !== false,
        active: params.active === true,
        ...typeof params.windowId === "number" ? { windowId: params.windowId } : {}
      });
      const data = requireOk(result, ERROR_CODES.INTERNAL_ERROR, "tabs.create failed");
      if (data.attached && data.tab?.tabId !== void 0) {
        client.attachedTabIds = mergeAttached(client.attachedTabIds, [data.tab.tabId]);
      }
      return { tab: data.tab, attached: data.attached };
    }
    case "tabs.release": {
      const hasTabId = typeof params.tabId === "number" && Number.isFinite(params.tabId);
      if (!hasTabId) {
        const result2 = await delegate({ target: "background", type: "tabs.releaseAll" });
        const data2 = requireOk(result2, ERROR_CODES.CDP_ERROR, "release all failed");
        client.attachedTabIds = [];
        return { released: data2.released ?? 0 };
      }
      const tabId = params.tabId;
      const result = await delegate({ target: "background", type: "tabs.release", tabId });
      const data = requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "release failed");
      client.attachedTabIds = client.attachedTabIds.filter((id) => id !== tabId);
      return { released: data.released ?? 0 };
    }
    case "tabs.close": {
      const tabIds = Array.isArray(params.tabIds) ? params.tabIds.filter((id) => typeof id === "number") : [];
      const result = await delegate({ target: "background", type: "tabs.close", tabIds });
      const data = requireOk(result, ERROR_CODES.INTERNAL_ERROR, "tabs.close failed");
      const closed = data.closed ?? [];
      const refused = data.refused ?? [];
      if (closed.length) {
        client.attachedTabIds = client.attachedTabIds.filter((id) => !closed.includes(id));
      }
      return { closed, refused };
    }
    case "dom.snapshot": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "dom.snapshot",
        tabId,
        budgetBytes: typeof params.budget_bytes === "number" ? params.budget_bytes : void 0,
        selector: typeof params.selector === "string" ? params.selector : void 0,
        // G2.2.15 (protocol/schema.json's dom.snapshot.granted_origins/
        // denied_origins/default_full) -- not unpacked here means not
        // reaching background.ts's handleDomSnapshot at all, the same
        // "silently dropped" warning as every other field in this file: it
        // built its OriginPolicy straight from msg.grantedOrigins et al.
        // (background.ts), which was always undefined on the real wire path
        // before this fix, so aggregateSnapshot always ran with
        // lib/origin-policy.ts's fail-closed DENY_ALL default regardless of
        // what the device's grants table actually said -- every embedded
        // cross-origin (or differently-origined) frame showed up only as a
        // not_granted placeholder, even when its origin WAS granted 'full'.
        grantedOrigins: Array.isArray(params.granted_origins) ? params.granted_origins.filter((o) => typeof o === "string") : void 0,
        deniedOrigins: Array.isArray(params.denied_origins) ? params.denied_origins.filter((o) => typeof o === "string") : void 0,
        defaultFull: typeof params.default_full === "boolean" ? params.default_full : void 0,
        // speedimprovements.md B1/B3 (protocol/schema.json's dom.snapshot
        // find/start_index/dialog_only/viewport_only) -- unread here means
        // silently dropped before it ever reaches background.ts, the same
        // rule every other field on this call already follows (see the
        // G2.2.15 comment above and tests/lib/offscreen-param-drift.test.ts).
        find: params.find && typeof params.find === "object" && typeof params.find.query === "string" ? {
          query: params.find.query,
          role: typeof params.find.role === "string" ? params.find.role : void 0
        } : void 0,
        startIndex: typeof params.start_index === "number" ? params.start_index : void 0,
        dialogOnly: typeof params.dialog_only === "boolean" ? params.dialog_only : void 0,
        viewportOnly: typeof params.viewport_only === "boolean" ? params.viewport_only : void 0,
        // speedimprovements.md G6 (protocol/schema.json's dom.snapshot
        // interactive_only) — same "not unpacked here means silently
        // dropped" rule as every other field on this call.
        interactiveOnly: typeof params.interactive_only === "boolean" ? params.interactive_only : void 0,
        // speedimprovements.md B4 (stable element refs, protocol/schema.json's
        // dom.snapshot.existing_index_map) -- not unpacked here means not
        // reaching background.ts's aggregateSnapshot at all, the same
        // "silently dropped" rule as every other field on this call.
        existingIndexMap: (() => {
          const raw = params.existing_index_map;
          if (!raw || typeof raw !== "object") return void 0;
          const out = {};
          for (const [k, v] of Object.entries(raw)) {
            if (typeof v === "string") out[Number(k)] = v;
          }
          return out;
        })(),
        // speedimprovements.md G2 (protocol/schema.json's dom.snapshot.viewport)
        // -- not unpacked here means silently dropped before background.ts's
        // handleDomSnapshot ever sees it, the same rule every field on this
        // call follows (tests/lib/offscreen-param-drift.test.ts).
        viewport: params.viewport && typeof params.viewport === "object" ? {
          width: Number(params.viewport.width),
          height: Number(params.viewport.height)
        } : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "dom.snapshot failed");
    }
    case "dom.inspect": {
      const tabId = Number(params.tabId);
      const question = params.question;
      if (question !== "scrollables" && question !== "at_point" && question !== "visibility" && question !== "expanded" && question !== "options" && question !== "form_state" && question !== "element" && question !== "listeners" && question !== "style") {
        throw bridgeError(ERROR_CODES.INVALID_INSPECT_QUESTION, `dom.inspect: unsupported question ${JSON.stringify(question)}`);
      }
      const props = Array.isArray(params.props) ? params.props.filter((p) => typeof p === "string") : void 0;
      const existingIndexMap = (() => {
        const raw = params.existing_index_map;
        if (!raw || typeof raw !== "object") return void 0;
        const out = {};
        for (const [k, v] of Object.entries(raw)) {
          if (typeof v === "string") out[Number(k)] = v;
        }
        return out;
      })();
      const result = await delegate({
        target: "background",
        type: "dom.inspect",
        tabId,
        question,
        selector: typeof params.selector === "string" ? params.selector : void 0,
        x: typeof params.x === "number" ? params.x : void 0,
        y: typeof params.y === "number" ? params.y : void 0,
        props,
        existingIndexMap,
        nextIdx: typeof params.next_idx === "number" ? params.next_idx : void 0,
        grantedOrigins: Array.isArray(params.granted_origins) ? params.granted_origins.filter((o) => typeof o === "string") : void 0,
        deniedOrigins: Array.isArray(params.denied_origins) ? params.denied_origins.filter((o) => typeof o === "string") : void 0,
        defaultFull: typeof params.default_full === "boolean" ? params.default_full : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "dom.inspect failed");
    }
    case "page.screenshot": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "page.screenshot",
        tabId,
        full: Boolean(params.full),
        region: params.region,
        selector: typeof params.selector === "string" ? params.selector : void 0,
        // D2 (speedimprovements.md): not unpacked here means silently
        // dropped before background.ts's handlePageScreenshot ever sees it —
        // the same "not read means silently dropped" rule the G2.2.15 drift
        // guard already enforces for dom.snapshot's origin-policy triple.
        scale: typeof params.scale === "number" ? params.scale : void 0,
        format: params.format === "png" ? "png" : params.format === "jpeg" ? "jpeg" : void 0,
        quality: typeof params.quality === "number" ? params.quality : void 0,
        // D1: draws/removes idx badges (background.ts's `withMarks`).
        marks: Boolean(params.marks),
        // G2.2 permission-model fix, same shape as dom.snapshot's own triple.
        grantedOrigins: Array.isArray(params.granted_origins) ? params.granted_origins.filter((o) => typeof o === "string") : void 0,
        deniedOrigins: Array.isArray(params.denied_origins) ? params.denied_origins.filter((o) => typeof o === "string") : void 0,
        defaultFull: typeof params.default_full === "boolean" ? params.default_full : void 0,
        // speedimprovements.md G2 (protocol/schema.json's page.screenshot.viewport)
        // -- same shape/rule as dom.snapshot's own field above.
        viewport: params.viewport && typeof params.viewport === "object" ? {
          width: Number(params.viewport.width),
          height: Number(params.viewport.height)
        } : void 0
      });
      return requireOk(result, ERROR_CODES.CDP_ERROR, "page.screenshot failed");
    }
    case "page.read": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "page.read",
        tabId,
        selector: typeof params.selector === "string" ? params.selector : void 0,
        format: params.format === "text" ? "text" : "markdown"
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "page.read failed");
    }
    case "page.act": {
      if (typeof params.idx === "number") {
        throw bridgeError(
          ERROR_CODES.INVALID_PARAMS,
          "page.act received `idx` — the gateway must resolve idx to a selector before sending (M2 contract)"
        );
      }
      const tabId = Number(params.tabId);
      const xy = Array.isArray(params.xy) && params.xy.length === 2 && params.xy.every((n) => typeof n === "number") ? [params.xy[0], params.xy[1]] : void 0;
      const expectParam = params.expect;
      const expect = expectParam && typeof expectParam.role === "string" && typeof expectParam.name === "string" ? { role: expectParam.role, name: expectParam.name } : void 0;
      const expectViewportParam = params.expectViewport;
      const expectViewport = expectViewportParam && typeof expectViewportParam.width === "number" && typeof expectViewportParam.height === "number" && typeof expectViewportParam.dpr === "number" && typeof expectViewportParam.scrollX === "number" && typeof expectViewportParam.scrollY === "number" ? {
        width: expectViewportParam.width,
        height: expectViewportParam.height,
        dpr: expectViewportParam.dpr,
        scrollX: expectViewportParam.scrollX,
        scrollY: expectViewportParam.scrollY
      } : void 0;
      const result = await delegate({
        target: "background",
        type: "page.act",
        tabId,
        action: params.action,
        selector: typeof params.selector === "string" ? params.selector : void 0,
        xy,
        text: typeof params.text === "string" ? params.text : void 0,
        url: typeof params.url === "string" ? params.url : void 0,
        timeoutMs: typeof params.timeout_ms === "number" ? params.timeout_ms : void 0,
        expect,
        expectViewport,
        // G2.6.2 (protocol/schema.json's page.act.backendNodeId): the CDP
        // node handle the gateway recorded alongside `selector`, when it has
        // one. Not unpacked here means not reaching act.ts at all (this
        // file's header warning, the recorded `bodies` regression), so this
        // gets its own line even though only idx-targeted calls carry it.
        backendNodeId: typeof params.backendNodeId === "number" ? params.backendNodeId : void 0,
        // `hover`'s dwell (protocol/schema.json's page.act.hover_ms). A field
        // not unpacked here never reaches act.ts at all — see the `bodies`
        // regression this file's header warns about — so this is threaded
        // through even though only one action reads it.
        hoverMs: typeof params.hover_ms === "number" ? params.hover_ms : void 0,
        // `type`'s params (protocol/schema.json's `type_mode`/`press_enter`/
        // `dispatch`/`key_delay_ms`, G1.6.2) -- not unpacked here means not
        // reaching act.ts at all (this file's header warning, the recorded
        // `bodies` regression), so every one of these four gets its own line
        // even though only `type` reads them.
        typeMode: params.type_mode === "replace" || params.type_mode === "append" || params.type_mode === "prepend" ? params.type_mode : void 0,
        pressEnter: typeof params.press_enter === "boolean" ? params.press_enter : void 0,
        dispatch: params.dispatch === "insert_text" || params.dispatch === "keys" ? params.dispatch : void 0,
        keyDelayMs: typeof params.key_delay_ms === "number" ? params.key_delay_ms : void 0,
        // `scroll`'s own params (protocol/schema.json's `to`/
        // `wait_for_growth_ms`, the infinite-scroll/pagination contract) --
        // not unpacked here means not reaching act.ts at all, same "silently
        // dropped" warning as every other per-action field above.
        to: params.to === "top" || params.to === "bottom" || params.to === "next_page" ? params.to : void 0,
        waitForGrowthMs: typeof params.wait_for_growth_ms === "number" ? params.wait_for_growth_ms : void 0,
        // `wait_for`'s structured `condition` (C2, protocol/schema.json's
        // page.act.condition) -- not unpacked here means not reaching act.ts
        // at all, same "silently dropped" warning as every other per-action
        // field above. A malformed/unrecognised `type` degrades to
        // `undefined` (the legacy selector/text wait_for path), never a
        // crash.
        condition: (() => {
          const c = params.condition;
          if (!c || typeof c.type !== "string") return void 0;
          const type = c.type;
          if (type !== "text_appears" && type !== "text_gone" && type !== "element_visible" && type !== "element_gone" && type !== "url_matches" && type !== "network_idle") {
            return void 0;
          }
          return {
            type,
            text: typeof c.text === "string" ? c.text : void 0,
            selector: typeof c.selector === "string" ? c.selector : void 0,
            pattern: typeof c.pattern === "string" ? c.pattern : void 0,
            timeoutMs: typeof c.timeout_ms === "number" ? c.timeout_ms : void 0
          };
        })(),
        // `drag`'s own params (protocol/schema.json's `toSelector`/`toXy`/
        // `expectTo`/`expectToViewport`/`mode`, G1.2) -- same "not unpacked
        // here means not reaching act.ts at all" rule as every field above.
        toSelector: typeof params.toSelector === "string" ? params.toSelector : void 0,
        toXy: Array.isArray(params.toXy) && params.toXy.length === 2 && params.toXy.every((n) => typeof n === "number") ? [params.toXy[0], params.toXy[1]] : void 0,
        expectTo: (() => {
          const expectToParam = params.expectTo;
          return expectToParam && typeof expectToParam.role === "string" && typeof expectToParam.name === "string" ? { role: expectToParam.role, name: expectToParam.name } : void 0;
        })(),
        expectToViewport: (() => {
          const p = params.expectToViewport;
          return p && typeof p.width === "number" && typeof p.height === "number" && typeof p.dpr === "number" && typeof p.scrollX === "number" && typeof p.scrollY === "number" ? { width: p.width, height: p.height, dpr: p.dpr, scrollX: p.scrollX, scrollY: p.scrollY } : void 0;
        })(),
        dragMode: params.mode === "auto" || params.mode === "pointer" || params.mode === "html5" ? params.mode : void 0,
        // `upload` only (G1.3 tier 1, protocol/schema.json's page.act.filePath)
        // -- not unpacked here means not reaching act.ts at all, same "silently
        // dropped" warning as every other per-action field above.
        filePath: typeof params.filePath === "string" ? params.filePath : void 0,
        // G2.2.13 TOCTOU fix (protocol/schema.json's page.act.frameOrigin/
        // toFrameOrigin) -- same "not unpacked here means not reaching act.ts
        // at all" rule as every field above; act.ts fails closed when a
        // frame-qualified selector/toSelector has no matching origin here.
        frameOrigin: typeof params.frameOrigin === "string" ? params.frameOrigin : void 0,
        toFrameOrigin: typeof params.toFrameOrigin === "string" ? params.toFrameOrigin : void 0,
        // G2.2.14 (protocol/schema.json's page.act.granted_origins/
        // denied_origins/default_full) -- same shape and same "not unpacked
        // here means not reaching act.ts at all" rule as every field above.
        // Sent on every page.act call (unlike frameOrigin/toFrameOrigin,
        // which only cover an already-frame-qualified selector), because a
        // bare xy or a plain selector can still land ON a pixel Chrome
        // routes into a different, unauthorized frame -- see act.ts's
        // authorizeDispatchPoint.
        grantedOrigins: Array.isArray(params.granted_origins) ? params.granted_origins.filter((o) => typeof o === "string") : void 0,
        deniedOrigins: Array.isArray(params.denied_origins) ? params.denied_origins.filter((o) => typeof o === "string") : void 0,
        defaultFull: typeof params.default_full === "boolean" ? params.default_full : void 0,
        // `fill` only (protocol/schema.json's page.act.fields,
        // speedimprovements.md A2) -- same "not unpacked here means not
        // reaching act.ts at all" rule as every per-action field above. Each
        // entry needs a string `selector` (the gateway already resolved any
        // idx) and a `value` of either type fill accepts (string or
        // boolean); anything else is dropped rather than forwarded
        // half-shaped.
        fields: Array.isArray(params.fields) ? params.fields.filter(
          (f) => typeof f === "object" && f !== null && typeof f.selector === "string" && (typeof f.value === "string" || typeof f.value === "boolean")
        ).map((f) => ({ selector: f.selector, value: f.value })) : void 0,
        // `select` only (speedimprovements.md D5, protocol/schema.json's
        // page.act.option_text) -- not unpacked here means not reaching
        // act.ts at all, same "silently dropped" warning as every other
        // per-action field above.
        optionText: typeof params.option_text === "string" ? params.option_text : void 0,
        // A4 follow-up (protocol/schema.json's page.act.snapshot_after): the
        // scoped object shape (`{selector, dialog_only, viewport_only}` --
        // `root` already resolved to a plain `selector` gateway-side, same as
        // browser_bridge_snapshot's own `root`) now reaches act.ts verbatim,
        // reusing the B3 scoped-snapshot code path -- see ActParams.snapshotAfter's
        // own doc comment. Plain `true` still means "the whole page", exactly
        // as before.
        snapshotAfter: (() => {
          if (params.snapshot_after === true) return true;
          if (typeof params.snapshot_after === "object" && params.snapshot_after !== null) {
            const s = params.snapshot_after;
            return {
              selector: typeof s.selector === "string" ? s.selector : void 0,
              dialogOnly: typeof s.dialog_only === "boolean" ? s.dialog_only : void 0,
              viewportOnly: typeof s.viewport_only === "boolean" ? s.viewport_only : void 0
            };
          }
          return void 0;
        })(),
        // speedimprovements.md B4 (stable element refs, protocol/schema.json's
        // page.act.existing_index_map/start_index) -- used only by this
        // call's after-action walk (act.ts's captureSnapshot) -- not
        // unpacked here means not reaching act.ts at all, same "silently
        // dropped" rule as every other field above.
        existingIndexMap: (() => {
          const raw = params.existing_index_map;
          if (!raw || typeof raw !== "object") return void 0;
          const out = {};
          for (const [k, v] of Object.entries(raw)) {
            if (typeof v === "string") out[Number(k)] = v;
          }
          return out;
        })(),
        startIndex: typeof params.start_index === "number" ? params.start_index : void 0,
        // speedimprovements.md H1: classify-only pre-check (see ActParams's
        // own doc comment) -- strictly `true`, never truthy, so a malformed
        // value can only ever mean "really act", which the gateway gates
        // before sending anyway.
        classifyOnly: params.classify_only === true ? true : void 0,
        // speedimprovements.md G5 (protocol/schema.json's page.act.screenshot_after)
        // -- not unpacked here means not reaching act.ts at all, same
        // "silently dropped" rule as every other field above.
        screenshotAfter: (() => {
          if (params.screenshot_after === true) return true;
          if (typeof params.screenshot_after === "object" && params.screenshot_after !== null) {
            const s = params.screenshot_after;
            const region = s.region && typeof s.region === "object" ? {
              x: Number(s.region.x),
              y: Number(s.region.y),
              width: Number(s.region.width),
              height: Number(s.region.height)
            } : void 0;
            return {
              scale: typeof s.scale === "number" ? s.scale : void 0,
              region,
              format: s.format === "png" ? "png" : s.format === "jpeg" ? "jpeg" : void 0
            };
          }
          return void 0;
        })()
      });
      return requireOk(result, ERROR_CODES.CDP_ERROR, "page.act failed");
    }
    case "annotate": {
      const tabId = Number(params.tabId);
      const candidates = Array.isArray(params.candidates) ? params.candidates.filter((c) => typeof c === "number") : void 0;
      const result = await delegate({
        target: "background",
        type: "annotate",
        tabId,
        question: typeof params.question === "string" ? params.question : "",
        candidates
      });
      return requireOk(result, ERROR_CODES.TIMEOUT, "annotate failed");
    }
    case "page.fetch": {
      const tabId = Number(params.tabId);
      const headers = params.headers && typeof params.headers === "object" && !Array.isArray(params.headers) ? params.headers : void 0;
      const stringHeaders = headers ? Object.fromEntries(Object.entries(headers).filter((entry) => typeof entry[1] === "string")) : void 0;
      const credentials = params.credentials === "include" || params.credentials === "same-origin" || params.credentials === "omit" ? params.credentials : void 0;
      const result = await delegate({
        target: "background",
        type: "page.fetch",
        tabId,
        url: typeof params.url === "string" ? params.url : "",
        method: typeof params.method === "string" ? params.method : void 0,
        headers: stringHeaders,
        body: typeof params.body === "string" ? params.body : void 0,
        credentials,
        timeoutMs: typeof params.timeout_ms === "number" ? params.timeout_ms : void 0,
        maxBytes: typeof params.max_bytes === "number" ? params.max_bytes : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "page.fetch failed");
    }
    case "cookies.get": {
      const urls = Array.isArray(params.urls) ? params.urls.filter((u) => typeof u === "string") : [];
      const result = await delegate({ target: "background", type: "cookies.get", urls });
      return requireOk(result, ERROR_CODES.INVALID_PARAMS, "cookies.get failed");
    }
    case "cookies.set": {
      const sameSite = params.sameSite === "no_restriction" || params.sameSite === "lax" || params.sameSite === "strict" || params.sameSite === "unspecified" ? params.sameSite : void 0;
      const result = await delegate({
        target: "background",
        type: "cookies.set",
        url: typeof params.url === "string" ? params.url : "",
        name: typeof params.name === "string" ? params.name : "",
        value: typeof params.value === "string" ? params.value : "",
        path: typeof params.path === "string" ? params.path : void 0,
        domain: typeof params.domain === "string" ? params.domain : void 0,
        secure: typeof params.secure === "boolean" ? params.secure : void 0,
        httpOnly: typeof params.httpOnly === "boolean" ? params.httpOnly : void 0,
        sameSite,
        expirationDate: typeof params.expirationDate === "number" ? params.expirationDate : void 0,
        // Not typed against chrome.cookies here on purpose — this file must
        // never reference the chrome.cookies namespace at all, even in a
        // type-only position (see no-restricted-apis.test.ts's structural
        // guard: an offscreen document supports chrome.runtime only).
        // messages.ts's BridgeMessage carries the real
        // chrome.cookies.CookiePartitionKey type for the background.ts side.
        partitionKey: params.partitionKey && typeof params.partitionKey === "object" ? params.partitionKey : void 0
      });
      return requireOk(result, ERROR_CODES.INVALID_PARAMS, "cookies.set failed");
    }
    case "network.log": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "network.log",
        tabId,
        filter: typeof params.filter === "string" ? params.filter : void 0,
        limit: typeof params.limit === "number" ? params.limit : void 0,
        // Defect fix: this param used to be dropped here, so `bodies: true`
        // from the gateway never reached background.ts/network.ts and the
        // "discover the site's own XHR then replay it" workflow could never
        // surface requestHeaders/postData no matter what the caller asked
        // for. Default stays metadata-only when omitted.
        bodies: typeof params.bodies === "boolean" ? params.bodies : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "network.log failed");
    }
    case "auth.status": {
      const origin = typeof params.origin === "string" ? params.origin : "";
      const result = await delegate({
        target: "background",
        type: "auth.status",
        origin
      });
      return requireOk(result, ERROR_CODES.GRANT_DENIED, "auth.status failed");
    }
    case "page.upload": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "page.upload",
        tabId,
        selector: typeof params.selector === "string" ? params.selector : "",
        filename: typeof params.filename === "string" ? params.filename : "",
        mimeType: typeof params.mimeType === "string" ? params.mimeType : "",
        contentBase64: typeof params.contentBase64 === "string" ? params.contentBase64 : ""
      });
      return requireOk(result, ERROR_CODES.INVALID_PARAMS, "page.upload failed");
    }
    case "downloads.search": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "downloads.search",
        tabId,
        filter: typeof params.filter === "string" ? params.filter : void 0,
        limit: typeof params.limit === "number" ? params.limit : void 0,
        waitMs: typeof params.waitMs === "number" ? params.waitMs : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "downloads.search failed");
    }
    case "page.dialog": {
      const tabId = Number(params.tabId);
      const dialogId = typeof params.dialogId === "string" ? params.dialogId : "";
      if (!dialogId) {
        throw bridgeError(ERROR_CODES.INVALID_PARAMS, "page.dialog missing dialogId");
      }
      const result = await delegate({
        target: "background",
        type: "page.dialog",
        tabId,
        dialogId,
        accept: Boolean(params.accept),
        promptText: typeof params.promptText === "string" ? params.promptText : void 0,
        ackMessage: typeof params.ackMessage === "string" ? params.ackMessage : void 0
      });
      return requireOk(result, ERROR_CODES.DIALOG_NOT_FOUND, "page.dialog failed");
    }
    case "console.entries": {
      const tabId = Number(params.tabId);
      const result = await delegate({
        target: "background",
        type: "console.entries",
        tabId,
        levelFilter: typeof params.levelFilter === "string" ? params.levelFilter : void 0,
        textFilter: typeof params.textFilter === "string" ? params.textFilter : void 0,
        limit: typeof params.limit === "number" ? params.limit : void 0,
        since: typeof params.since === "number" ? params.since : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "console.entries failed");
    }
    case "page.evaluate": {
      const tabId = Number(params.tabId);
      const expression = typeof params.expression === "string" ? params.expression : "";
      if (!expression) {
        throw bridgeError(ERROR_CODES.INVALID_PARAMS, "page.evaluate requires a non-empty expression");
      }
      const result = await delegate({
        target: "background",
        type: "page.evaluate",
        tabId,
        expression,
        world: params.world === "isolated" ? "isolated" : "main",
        awaitPromise: typeof params.awaitPromise === "boolean" ? params.awaitPromise : void 0,
        returnByValue: typeof params.returnByValue === "boolean" ? params.returnByValue : void 0,
        timeoutMs: typeof params.timeoutMs === "number" ? params.timeoutMs : void 0
      });
      return requireOk(result, ERROR_CODES.TARGET_NOT_ATTACHED, "page.evaluate failed");
    }
    case "approval.request": {
      const approvalId = typeof params.approval_id === "string" ? params.approval_id : "";
      if (!approvalId) {
        throw bridgeError(ERROR_CODES.INVALID_PARAMS, "approval.request missing approval_id");
      }
      addPendingApproval({
        approvalId,
        origin: typeof params.origin === "string" ? params.origin : "",
        capability: typeof params.capability === "string" ? params.capability : "",
        summary: typeof params.summary === "string" ? params.summary : "",
        detail: typeof params.detail === "string" ? params.detail : void 0,
        expiresAt: typeof params.expires_at === "number" ? params.expires_at : Date.now()
      });
      return { queued: true };
    }
    default: {
      {
        const { code, message } = formatRefusal("unsupported_method", { method });
        throw bridgeError(code, message);
      }
    }
  }
}
function delegate(message) {
  return chrome.runtime.sendMessage(message).then((response) => response ?? { ok: false, error: "no response" }).catch((error) => ({ ok: false, error: String(error) }));
}
function syncAttachedFromTab(tab) {
  const has = client.attachedTabIds.includes(tab.tabId);
  if (tab.attached && !has) client.attachedTabIds = [...client.attachedTabIds, tab.tabId];
  else if (!tab.attached && has) client.attachedTabIds = client.attachedTabIds.filter((id) => id !== tab.tabId);
}
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen") return false;
  switch (message.type) {
    case "status.get":
      void client.status().then((status) => sendResponse({ ok: true, data: status })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "connect":
      void client.connect(message.pairCode).then(() => client.status()).then((status) => sendResponse({ ok: status.state !== "error", data: status, error: status.lastError })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "disconnect":
      client.disconnect(message.reason ?? "user requested");
      sendResponse({ ok: true });
      return false;
    case "cancelConnect":
      client.cancelConnect();
      sendResponse({ ok: true });
      return false;
    case "unpair":
      void client.unpair().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "killSwitch":
      void delegate({ target: "background", type: "tabs.releaseAll" }).finally(() => {
        client.attachedTabIds = [];
        client.notify("kill.switch", { reason: "popup kill switch" });
        client.disconnect("kill switch");
        sendResponse({ ok: true });
      });
      return true;
    case "tab.changed":
      syncAttachedFromTab(message.tab);
      if (!isPaused()) client.notify("tab.changed", { tab: message.tab, change: message.change });
      sendResponse({ ok: true });
      return false;
    case "page.loadFired":
      if (!isPaused()) client.notify("page.loadFired", { tabId: message.tabId, url: message.url });
      sendResponse({ ok: true });
      return false;
    case "auth.replayed":
      client.notify("auth.replayed", { origin: message.origin });
      sendResponse({ ok: true });
      return false;
    case "attach.retried":
      client.notify("attach.retried", { tabId: message.tabId, attempt: message.attempt, maxAttempts: message.maxAttempts });
      sendResponse({ ok: true });
      return false;
    case "attach.stripped":
      client.notify("attach.stripped", {
        tabId: message.tabId,
        strippedCount: message.strippedCount,
        extensionIds: message.extensionIds,
        restored: message.restored,
        sessionSurvived: message.sessionSurvived,
        reloaded: message.reloaded,
        attached: message.attached
      });
      sendResponse({ ok: true });
      return false;
    case "attach.sessionDropped":
      client.notify("attach.sessionDropped", {
        tabId: message.tabId,
        reason: message.reason,
        extensionIds: message.extensionIds,
        autoReattachAttempted: message.autoReattachAttempted,
        autoReattachSucceeded: message.autoReattachSucceeded
      });
      sendResponse({ ok: true });
      return false;
    case "attach.modeChanged":
      client.notify("attach.modeChanged", {
        tabId: message.tabId,
        mode: message.mode,
        ...message.reason !== void 0 ? { reason: message.reason } : {}
      });
      sendResponse({ ok: true });
      return false;
    case "focus.stolen":
      client.notify("focus.stolen", {
        tabId: message.tabId,
        actionKind: message.actionKind,
        msSinceAction: message.msSinceAction,
        via: message.via
      });
      sendResponse({ ok: true });
      return false;
    case "grant.set":
      void client.request("grant.set", { origin: message.origin, mode: message.mode }).then(() => {
        client.recordGrant(message.origin, message.mode);
        sendResponse({ ok: true });
      }).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "settings.changed":
      void refreshPausedFromSettings();
      if (latestStatus?.state === "connected") {
        client.disconnect("settings changed");
        void client.connect().catch(() => {
        });
      }
      sendResponse({ ok: true });
      return false;
    case "stoppedFromPage":
      client.attachedTabIds = [];
      void refreshPausedFromSettings().then(() => {
        client.notify("state.report", { paused: isPaused(), tabs: [] });
        sendResponse({ ok: true });
      });
      return true;
    case "pause.changed":
      void refreshPausedFromSettings().then(() => {
        client.notify("state.report", { paused: isPaused() });
        sendResponse({ ok: true });
      });
      return true;
    case "redaction.changed":
      void client.reportRedactionPolicy().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "powers.changed":
      void client.reportPowerPolicy().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "defaultMode.changed":
      void client.reportDefaultMode().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "lease.changed":
      void client.reportLeaseSeconds().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "commitMode.changed":
      void client.reportCommitMode().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    case "approvals.list":
      sendResponse({ ok: true, data: { approvals: listPendingApprovals() } });
      return false;
    case "approvals.respond":
      sendResponse(respondToApproval(message.approvalId, message.choice));
      return false;
    default:
      return false;
  }
});
void refreshPausedFromSettings();
void (async () => {
  try {
    const [settings, credentials] = await Promise.all([getSettings(), getCredentials()]);
    if (settings.autoConnect && credentials) {
      await client.connect();
    }
  } catch (error) {
    client.reportError(
      `auto-connect failed to read extension settings: ${error instanceof Error ? error.message : String(error)}`
    );
  }
})();
