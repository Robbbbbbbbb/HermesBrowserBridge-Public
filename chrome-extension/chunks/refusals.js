const PROTOCOL_VERSION = "1.3";
const HEARTBEAT_INTERVAL_MS = 2e4;
const ERROR_CODES = {
  PARSE_ERROR: -32700,
  // malformed frame
  INVALID_REQUEST: -32600,
  // not a valid JSON-RPC 2.0 request
  METHOD_NOT_FOUND: -32601,
  // unknown method
  INVALID_PARAMS: -32602,
  // invalid params
  INTERNAL_ERROR: -32603,
  // internal error
  PROTOCOL_MISMATCH: 4e3,
  // protocol version not supported; hard reset required
  PAIR_CODE_INVALID: 4001,
  // pairing code unknown
  PAIR_CODE_EXPIRED: 4002,
  // pairing code expired or already used
  TOKEN_INVALID: 4003,
  // device token not recognised
  TOKEN_REVOKED: 4004,
  // device token revoked
  NOT_AUTHENTICATED: 4005,
  // device.hello must succeed before any other method
  RATE_LIMITED: 4006,
  // too many attempts; back off
  SEQ_GAP: 4007,
  // sequence gap detected; resync required
  GRANT_DENIED: 4100,
  // origin mode forbids this operation
  SHARING_PAUSED: 4103,
  // the user has paused sharing in the extension popup; nothing is being read or driven until they resume
  APPROVAL_REQUIRED: 4101,
  // operation parked pending user approval
  APPROVAL_DENIED: 4102,
  // user denied the operation
  TARGET_NOT_ATTACHED: 4200,
  // tab is not attached
  TARGET_BUSY: 4201,
  // tab is driven by another session
  CDP_ERROR: 4202,
  // chrome.debugger command failed
  CONTENT_SCRIPT_ERROR: 4203,
  // content script could not be injected or did not respond; the page may forbid injection (chrome://, the Web Store, the PDF viewer)
  ELEMENT_MISMATCH: 4204,
  // the element at that index is not the one the snapshot described; re-snapshot and retry
  TIMEOUT: 4300,
  // operation timed out
  URL_SCHEME_BLOCKED: 4205,
  // only http and https URLs may be opened in the browser
  VIEWPORT_MISMATCH: 4206,
  // the tab's viewport no longer matches the one recorded when these coordinates were captured; re-snapshot and retry
  UNSUPPORTED_METHOD: 4207,
  // this method is not supported by the connected extension build; reload the extension in chrome://extensions and reconnect
  FIELD_NOT_EDITABLE: 4210,
  // the resolved element is disabled, read-only, or not a text field/contenteditable region; nothing was typed
  COOKIE_WRITE_ORIGIN_UNATTACHED: 4213,
  // G3.6's origin bound: a cookie write is refused unless the url's origin matches a tab this device currently has attached
  COOKIE_SAME_SITE_INVALID: 4214,
  // G3.6.1's SameSite traps: "none" is not a valid chrome.cookies.SameSiteStatus member, and no_restriction requires secure:true and an https:// url
  DIALOG_NOT_FOUND: 4208,
  // no open JS dialog with that id on this tab; it may already have been resolved
  DIALOG_ACK_MISMATCH: 4209,
  // ack_message does not match the dialog's recorded message; re-read the dialog and try again
  NO_HIT_TESTABLE_TARGET: 4217,
  // every content quad for this element is zero-area or lies entirely outside the viewport; there is nothing to click
  DRAG_HTML5_UNSUPPORTED: 4219,
  // mode:'html5' is not implemented; the pointer path already drives native HTML5 drag-and-drop on Chromium 117+ (see G0.1.6)
  FRAME_ACTION_UNSUPPORTED: 4226,
  // G2.2.6: this frame cannot be acted on -- its frameId is not mapped by chrome.webNavigation.getAllFrames, or it is an out-of-process frame this build cannot reach -- re-snapshot and check frameGaps for the reason
  FRAME_TARGET_UNRESOLVED: 4227,
  // G2.2.5: an ancestor <iframe> named by this frame-qualified selector could not be resolved to a live Chrome frame (the anchor element is gone, ambiguous, or its src no longer matches any child frame) -- re-snapshot and retry with a fresh selector
  FRAME_ORIGIN_DENIED: 4228,
  // G2.2.13: this frame's own origin is not granted 'full' for this device -- a grant on the top frame's origin does not authorize acting inside an embedded frame from a different origin; grant the frame's own origin in the extension popup
  HIT_TEST_FAILED: 4229,
  // G2.2.14: could not determine what lives at this dispatch point (DOM.getNodeForLocation failed, returned nothing, or named a frame this build could not map to a known origin) -- refused rather than dispatching input blindly; re-snapshot or retry
  SESSION_CLOSED: 4230,
  // G3.1.6: this agent session was closed with browser_bridge_session close and refuses every further tool call -- create a new session, or resume a different open one
  SESSION_ACCESS_DENIED: 4231,
  // G3.1.4: this session belongs to a different device -- it can only be resumed, renamed, closed or described from the device that owns it
  SESSION_NOT_FOUND: 4232,
  // G3.1.2: no agent session matches that id or label for this device -- call browser_bridge_session list to see open sessions
  TAB_KEY_AMBIGUOUS: 4233,
  // G3.3.2: that tab key/label matches more than one of your session's tabs -- call browser_bridge_tabs and use the exact key, or a longer prefix
  TAB_NOT_FOUND: 4234,
  // G3.3.2: no tab in your session's workspace matches that key/label -- call browser_bridge_tabs to see what you hold
  TAB_KEY_CONFLICT: 4235,
  // G3.3.1: that key is already used by a different tab in your session -- pick another, or omit tab_key to auto-assign one
  UPLOAD_PATH_DENIED: 4222,
  // G1.3.3: this path is not under an allowlisted uploadRoots entry, contains a '..' traversal segment, or names a hidden/secret-looking directory (.ssh, .aws, .gnupg, a browser profile dir, etc) even inside an allowed root
  UPLOAD_FILE_URL_ACCESS_DISABLED: 4223,
  // G1.3.2: DOM.setFileInputFiles needs this extension's 'Allow access to file URLs' toggle on chrome://extensions -- open chrome://extensions, find this extension, enable 'Allow access to file URLs', and retry
  EVAL_WORLD_UNSUPPORTED: 4224,
  // world must be 'main'; 'isolated' is not shipped yet (see G1.5.2 — it can forge messages to the background worker until that hazard is closed)
  EVAL_EXPRESSION_TOO_LARGE: 4225,
  // expression exceeds the size cap; shorten it (an approval prompt and an audit log entry both need to stay readable)
  FOREIGN_EXTENSION_FRAME_DETECTED: 4236,
  // attach was refused because another extension's frame is present in this tab, and the content-script scan (which sees inside closed shadow roots, unlike chrome.webNavigation.getAllFrames) confirmed which one -- distinct from the plain CDP_ERROR case where no such frame could be confirmed
  FRAME_SHADOWED_BY_EXTENSION: 4237,
  // G2.2.14 follow-up: this dispatch point is covered by another extension's overlay (a chrome-extension:// frame), not a frame of the page itself -- there is no page origin to grant here. Re-target the element by idx or selector (a freshly resolved, element-routed point) instead of a bare coordinate, or ask the user to pause/adjust that extension on this page.
  FOREIGN_FRAME_SESSION_DROPPED: 4238,
  // attach succeeded after temporarily removing another extension's frame so chrome.debugger could attach, but Chrome dropped the debugger session the instant that frame was put back -- the tab is not attached. Ask the user to set that extension's Site access to 'On click' on this page, then retry browser_bridge_attach
  LIMITED_MODE_CAPABILITY_UNAVAILABLE: 4239,
  // this capability needs chrome.debugger and this tab is currently shared in limited (no-debugger) mode
  FILL_OPTION_AMBIGUOUS: 4242,
  // speedimprovements.md A2: a fill field targeting a <select> matched more than one option's visible text, or none at all; see the result's `options` list
  FILL_FIELD_INVALID: 4243,
  // speedimprovements.md A2: a fill field could not be resolved, is disabled/read-only, or its value's type does not match the control it targets (a string for text/select, a boolean for checkbox/radio)
  INVALID_INSPECT_QUESTION: 4247,
  // browser_bridge_inspect: unsupported question, or a param combination that question doesn't accept (e.g. x/y for a selector-based question, or idx/selector for at_point)
  SNAPSHOT_ROOT_NOT_FOUND: 4245,
  // speedimprovements.md B3: browser_bridge_snapshot's `root` (an idx from a prior snapshot, or a CSS selector) did not resolve to any live element on this tab
  NO_OPEN_DIALOG: 4246,
  // speedimprovements.md B3: browser_bridge_snapshot's dialog_only was requested but no open dialog is present on this tab
  INVALID_STEP_KIND: 4252,
  // speedimprovements.md G1: browser_bridge_act's steps: each step needs exactly one of `action` (a mutating step) or `tool` (a read-only step: snapshot/screenshot/find/read/inspect), `tool` must be one of those five, and at most 2 tool:"screenshot" steps are allowed per batch
  TAB_NOT_AGENT_OPENED: 4255,
  // speedimprovements.md H4: browser_bridge_tabs action=close_opened (or browser_bridge_session close's close_opened_tabs) only closes tabs THIS session opened with browser_bridge_open_tab -- a tab the user opened by hand, or one a different session opened, is refused outright
  VIEWPORT_OUT_OF_RANGE: 4253,
  // speedimprovements.md G2: dom.snapshot/page.screenshot's `viewport` must have width in 320-3840 and height in 240-8000
  COMMIT_CONFIRM_REQUIRED: 4250,
  // speedimprovements.md H1: this is a committing action (Submit/Delete/etc) and this device is in Pause-for-confirmation mode; pass confirm: true and obtain user approval first
  COMMIT_APPROVAL_DENIED: 4251
  // speedimprovements.md H1: the user declined the approval prompt for this committing action
};
const REFUSALS = {
  "power_capability_disabled": { code: "GRANT_DENIED", params: ["capability", "settingNames"], message: "{capability} needs {settingNames} switched on for this device — open the extension's Options page, tick it under Powers, and press Save. Off is the shipped default: both the extension and the gateway refuse this independently before anything is dispatched, and a device that has never reported its settings is treated as having them all off." },
  "origin_off": { code: "GRANT_DENIED", params: ["origin", "capability"], message: "origin '{origin}' is set to 'off' for this device — the user must switch it to 'request' or 'full' in the extension popup before {capability} is allowed." },
  "origin_request_readonly": { code: "GRANT_DENIED", params: ["origin", "capability"], message: "origin '{origin}' is in 'request' mode. Read-only capabilities (snapshot, read, screenshot, attach) deliberately do not raise an approval prompt — one prompt per page read would train the user to click through them, which is how an approval habit stops being consent. Set this origin to 'full' in the extension popup to allow {capability}." },
  "dangerous_ceiling_readonly_gate": { code: "GRANT_DENIED", params: ["capability", "origin"], message: "{capability} is one of this bridge's per-capability-ceiling capabilities: a 'full' grant for '{origin}' does not cover it, and this read-only gate has no approval-transport integration to present a prompt with — refused rather than silently allowed. This capability must go through the approval-aware gate instead." },
  "approval_transport_missing": { code: "GRANT_DENIED", params: ["capability", "origin"], message: "the approval queue is not loaded on this gateway, so {capability} is refused at '{origin}' rather than silently allowed — this applies even when the origin is set to 'full', since {capability} is one of the capabilities a 'full' grant does not cover on its own. An operator must deploy hermes_plugin/approvals.py, or the origin must wait for it." },
  "operator_capability_disabled": { code: "GRANT_DENIED", params: ["capability"], message: "an operator has disabled {capability} fleet-wide (browser_bridge.powers.{capability}: false in config.yaml) — refused regardless of any per-device grant, per-capability grant, or approval decision. An operator must re-enable it in config.yaml; no popup choice can override this." },
  "upload_path_control_char": { code: "UPLOAD_PATH_DENIED", params: ["basename"], message: "upload path '{basename}' contains a control character (U+0000-U+001F or U+007F) — refused outright. Use a plain absolute path with no embedded control characters." },
  "upload_path_not_absolute": { code: "UPLOAD_PATH_DENIED", params: [], message: 'upload requires an absolute path (POSIX: a leading slash; Windows: a drive letter, colon and separator, e.g. "C:\\Downloads" or "C:/Downloads") — a drive-relative, root-relative, or otherwise relative path is refused outright.' },
  "upload_roots_empty": { code: "UPLOAD_PATH_DENIED", params: [], message: "uploadRoots is empty for this device — open the extension's Options page and add at least one root under Powers; enabling 'Allow file upload' alone grants nothing." },
  "upload_path_traversal": { code: "UPLOAD_PATH_DENIED", params: ["basename"], message: "upload path '{basename}' contains a '..' segment — refused outright, never resolved." },
  "upload_path_hidden_segment": { code: "UPLOAD_PATH_DENIED", params: ["basename", "segment"], message: "upload path '{basename}' passes through a hidden or known-secret directory ('{segment}') — refused even though it may be inside an allowed root." },
  "upload_path_outside_roots": { code: "UPLOAD_PATH_DENIED", params: ["basename"], message: "upload path '{basename}' is not under any of this device's uploadRoots — open the extension's Options page and add the containing directory under Powers, or move the file under an already-allowed root." },
  "upload_file_url_access_disabled": { code: "UPLOAD_FILE_URL_ACCESS_DISABLED", params: [], message: "this extension's 'Allow access to file URLs' toggle is off — open chrome://extensions, find this extension's card, click Details, and enable 'Allow access to file URLs', then retry." },
  "dialog_ack_mismatch": { code: "DIALOG_ACK_MISMATCH", params: [], message: "ack_message does not match the dialog's recorded message — re-read the dialog and retry with the exact text." },
  "dialog_not_found": { code: "DIALOG_NOT_FOUND", params: [], message: "no open dialog with that id on this tab — it may already have been resolved; re-check the current dialogs field and retry." },
  "viewport_mismatch": { code: "VIEWPORT_MISMATCH", params: ["expectedViewport", "actualViewport"], message: "viewport was {expectedViewport} when these coordinates were captured, but is now {actualViewport} — the page resized or scrolled since the last snapshot/screenshot; call browser_bridge_snapshot again and retry with fresh coordinates, or target this element by idx/selector." },
  "element_mismatch": { code: "ELEMENT_MISMATCH", params: [], message: "the element at that index no longer matches what the snapshot described — the page changed since the snapshot that produced it (a same-page re-render, not necessarily a navigation); call browser_bridge_snapshot again and retry with the fresh idx it returns. The expected/actual role and name ride alongside this in the structured `data` field, not in this message." },
  "no_hit_testable_target": { code: "NO_HIT_TESTABLE_TARGET", params: [], message: "every content quad for this element is zero-area or lies entirely outside the viewport; there is nothing to click — scroll it into view or call browser_bridge_snapshot again." },
  "field_not_editable": { code: "FIELD_NOT_EDITABLE", params: ["selector"], message: "selector '{selector}' did not resolve to a text field, textarea, or contenteditable region (or is disabled or read-only) — nothing was typed; this is the page's own state, not a bridge setting. Pick a different target." },
  "drag_html5_unsupported": { code: "DRAG_HTML5_UNSUPPORTED", params: [], message: 'mode:"html5" is not implemented in this build — a plain pointer-driven mouse sequence already drives native HTML5 drag-and-drop on Chromium 117+. Retry with mode:"pointer" or omit mode to use the default "auto".' },
  "frame_action_unsupported": { code: "FRAME_ACTION_UNSUPPORTED", params: ["selector"], message: "this frame cannot be acted on: '{selector}' — its frameId is not mapped by chrome.webNavigation.getAllFrames (an ambiguous or missing child frame), or it is an out-of-process frame this build cannot reach at all. Re-snapshot and check frameGaps for the reason." },
  "frame_target_unresolved": { code: "FRAME_TARGET_UNRESOLVED", params: ["selector"], message: "an ancestor <iframe> named by this frame-qualified selector could not be resolved: '{selector}' — the anchor element is gone, ambiguous, or its src no longer matches any child frame. Re-snapshot and retry with a fresh selector." },
  "frame_no_explicit_grant": { code: "GRANT_DENIED", params: ["origin", "capability"], message: "origin '{origin}' (an embedded frame) has no explicit grant for this device — a frame's own origin is never authorized by the tab-level default, even when that default is 'full'. Grant '{origin}' explicitly (Options popup, full or request) before {capability} is allowed inside it." },
  "frame_origin_unrecorded": { code: "GRANT_DENIED", params: ["selector"], message: "this selector reaches into a frame this gateway has no recorded origin for: '{selector}' — call browser_bridge_snapshot again so the extension can report the frame's origin, then retry." },
  "frame_origin_changed": { code: "FRAME_ORIGIN_DENIED", params: ["selector"], message: "the frame at '{selector}' now has a different origin than the one this act was authorized against — it likely navigated since the last snapshot. Re-snapshot and retry so the new origin can be granted (or refused) explicitly." },
  "frame_origin_not_authorized": { code: "FRAME_ORIGIN_DENIED", params: ["selector"], message: "'{selector}' crosses a frame boundary, but this call carries no authorized origin for it (an older gateway build) — refused rather than trusting the extension's own read of which origin is live. Update the gateway, or re-snapshot and retry." },
  "pixel_route_frame_denied": { code: "FRAME_ORIGIN_DENIED", params: ["origin"], message: "G2.2.14: this dispatch point lands inside an embedded frame at origin '{origin}', which is not authorized for this device — a grant on the top page does not cover a frame Chrome routes the click/hover/drag input to. Re-target the element by idx or selector (element-routed — a freshly resolved point is less likely to cross into this frame) if the real target is on the top page, or grant '{origin}' explicitly in the extension popup if you genuinely need to act inside it." },
  "pixel_route_shadowed_by_extension": { code: "FRAME_SHADOWED_BY_EXTENSION", params: ["origin"], message: "G2.2.14 follow-up: this dispatch point is covered by another extension's overlay ({origin}), not a frame belonging to the page — granting an origin cannot fix this, since that frame is never the page's own content. Re-target the element by idx or selector instead of a bare coordinate (a freshly resolved element centre is far less likely to land on the overlay), or ask the user to pause/adjust that extension on this page." },
  "scroll_frame_denied": { code: "FRAME_ORIGIN_DENIED", params: ["origin"], message: "G2.2.14 follow-up: the scroll container at this point lives inside an embedded frame at origin '{origin}', which is not authorized for this device — scrolling its content would mean acting inside that frame. Grant '{origin}' explicitly in the extension popup, or scroll the top page itself instead." },
  "pixel_route_hit_test_failed": { code: "HIT_TEST_FAILED", params: [], message: "G2.2.14: could not confirm which frame this dispatch point lands in before sending real input -- refused rather than risk acting inside an unauthorized frame. Re-snapshot and retry, or target the element by selector/idx instead of a bare coordinate." },
  "focused_frame_denied": { code: "FRAME_ORIGIN_DENIED", params: ["origin"], message: "G2.2.14 follow-up: keyboard input (type/key) would be delivered to a currently-focused frame at origin '{origin}', which is not authorized for this device -- Input.insertText/Input.dispatchKeyEvent go wherever focus already is, not to any coordinate this call controls. Grant '{origin}' explicitly in the extension popup, or click into an authorized field first." },
  "focused_frame_unresolved": { code: "HIT_TEST_FAILED", params: [], message: "G2.2.14 follow-up: could not confirm which frame currently holds keyboard focus before sending real input -- refused rather than risk typing/pressing keys inside an unauthorized frame. Re-snapshot and retry, or target the element explicitly with a guarded click first." },
  "unsupported_method": { code: "UNSUPPORTED_METHOD", params: ["method"], message: '"{method}" is not supported by the connected extension build — reload the extension in chrome://extensions and reconnect.' },
  "cookie_write_origin_unattached": { code: "COOKIE_WRITE_ORIGIN_UNATTACHED", params: ["origin"], message: "'{origin}' is not a currently-attached tab's origin — a cookie write is bound to attached origins only; call browser_bridge_attach on that tab first." },
  "once_only_capability": { code: null, params: ["capability"], message: "{capability} cannot be granted standing access — approve once, every time." },
  "session_closed": { code: "SESSION_CLOSED", params: ["session"], message: "session '{session}' is closed and refuses further tool calls — call browser_bridge_session create for a new one, or resume a different open session." },
  "session_access_denied": { code: "SESSION_ACCESS_DENIED", params: ["session"], message: "session '{session}' belongs to a different device — a session can only be resumed, renamed, closed or described from the device that owns it." },
  "session_not_found": { code: "SESSION_NOT_FOUND", params: ["session"], message: "no session matches '{session}' for this device — call browser_bridge_session list to see open sessions." },
  "tab_ambiguous": { code: "TAB_KEY_AMBIGUOUS", params: ["tab"], message: "'{tab}' matches more than one tab in your workspace — call browser_bridge_tabs and use the exact key, or a longer prefix." },
  "tab_not_found": { code: "TAB_NOT_FOUND", params: ["tab"], message: "no tab in your workspace matches '{tab}' — call browser_bridge_tabs to see what you hold." },
  "tab_key_conflict": { code: "TAB_KEY_CONFLICT", params: ["tab"], message: "'{tab}' is already used by a different tab in your session — pick another key, or omit it to auto-assign one." },
  "limited_mode_capability_unavailable": { code: "LIMITED_MODE_CAPABILITY_UNAVAILABLE", params: ["capability"], message: "this tab is shared in limited mode right now (another extension has a frame in this tab's WebContents, so Chrome refuses or drops chrome.debugger) — {capability} needs full mode and is unavailable until the bridge upgrades automatically (it quietly retries the real attach every few seconds) or you resolve the blocking extension yourself, e.g. set its Site access to 'On click' on this page, then reattach." },
  "fill_option_ambiguous": { code: "FILL_OPTION_AMBIGUOUS", params: ["field_index"], message: "fill: fields[{field_index}] targets a <select> whose visible option text matched ambiguously or not at all — see the result's `options` list (up to 10) and retry with an exact, or unambiguous-prefix, option text." },
  "fill_field_invalid": { code: "FILL_FIELD_INVALID", params: ["field_index"], message: "fill: fields[{field_index}] could not be filled — its selector/idx did not resolve, the control is disabled/read-only, or the value's type does not match the control (a string for text/select, a boolean for checkbox/radio)." },
  "invalid_inspect_question": { code: "INVALID_INSPECT_QUESTION", params: ["question"], message: "'{question}' is not a supported browser_bridge_inspect question, or the params given don't match what it accepts — pass one of scrollables/at_point/visibility/expanded/options/form_state/element/listeners/style, with idx or selector for visibility/expanded/options/form_state/element/listeners/style (style also needs props, from its own allowlist), x and y for at_point, and neither for scrollables." },
  "tab_not_agent_opened": { code: "TAB_NOT_AGENT_OPENED", params: ["tab"], message: "'{tab}' was not opened by THIS session via browser_bridge_open_tab — close_opened only closes tabs the agent itself opened, never one the user opened by hand or one a different session opened. Use browser_bridge_release to stop driving it without closing it." },
  "classify_target_unresolved": { code: "HIT_TEST_FAILED", params: [], message: "speedimprovements.md H1: classify_only could not determine what is at this point (nothing hit-testable there, or no accessibility information for it) -- nothing was dispatched. In Pause-for-confirmation mode the gateway treats this target as committing." }
};
const ALLOWED_PARAMS = /* @__PURE__ */ new Set([
  "capability",
  "settingNames",
  "origin",
  "selector",
  "basename",
  "segment",
  "expectedViewport",
  "actualViewport",
  "method",
  "session",
  "tab",
  "field_index",
  // B2's browser_bridge_inspect (invalid_inspect_question): the `question`
  // the caller itself typed (scrollables/at_point/visibility/expanded/
  // options, or a typo of one) — same "generated into this shared file,
  // caller-supplied and never secret/page content" reasoning as `tab`/
  // `method` above.
  "question"
]);
function interpolate(template, params) {
  return template.replace(/\{(\w+)\}/g, (_whole, key) => {
    if (!(key in params)) {
      throw new Error(`refusal template referenced undeclared param '${key}'`);
    }
    return params[key];
  });
}
function formatRefusal(reasonId, params = {}) {
  const entry = REFUSALS[reasonId];
  if (!entry) throw new Error(`unknown refusal reason id '${reasonId}'`);
  const declared = new Set(entry.params);
  for (const key of declared) {
    if (!ALLOWED_PARAMS.has(key)) {
      throw new Error(`refusal '${reasonId}' declares disallowed param '${key}'`);
    }
    if (!(key in params)) {
      throw new Error(`refusal '${reasonId}' requires param '${key}', which was not supplied`);
    }
  }
  for (const key of Object.keys(params)) {
    if (!declared.has(key)) {
      throw new Error(`refusal '${reasonId}' was called with undeclared param '${key}'`);
    }
  }
  const code = entry.code === null ? null : ERROR_CODES[entry.code];
  return { code, message: interpolate(entry.message, params) };
}
export {
  ERROR_CODES as E,
  HEARTBEAT_INTERVAL_MS as H,
  PROTOCOL_VERSION as P,
  formatRefusal as f
};
