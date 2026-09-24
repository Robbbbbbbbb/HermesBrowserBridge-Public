(function() {
  "use strict";
  const PRESENCE_HOST_ID = "__hermes-bridge-presence-host";
  const ANNOTATE_HOST_ID = "__hermes-bridge-annotate-host";
  const OWN_UI_HOST_IDS = /* @__PURE__ */ new Set([PRESENCE_HOST_ID, ANNOTATE_HOST_ID]);
  function isOwnUiHostId(id) {
    return !!id && OWN_UI_HOST_IDS.has(id);
  }
  const SHADOW_CAPABLE_TAGS = /* @__PURE__ */ new Set([
    "ARTICLE",
    "ASIDE",
    "BLOCKQUOTE",
    "BODY",
    "DIV",
    "FOOTER",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "HEADER",
    "MAIN",
    "NAV",
    "P",
    "SECTION",
    "SPAN"
  ]);
  function canHostShadowRoot(tagName) {
    return tagName.includes("-") || SHADOW_CAPABLE_TAGS.has(tagName.toUpperCase());
  }
  function makeShadowRootAccessor(dom) {
    return (el) => {
      if (isOwnUiHostId(el.getAttribute("id"))) return null;
      const open = el.shadowRoot;
      if (open) return open;
      if (!dom || !canHostShadowRoot(el.tagName)) return null;
      try {
        return dom.openOrClosedShadowRoot(el) ?? null;
      } catch {
        return null;
      }
    };
  }
  function availableChromeDom() {
    if (typeof chrome === "undefined") return void 0;
    const dom = chrome.dom;
    return dom && typeof dom.openOrClosedShadowRoot === "function" ? dom : void 0;
  }
  const shadowRootAccessor = makeShadowRootAccessor(
    availableChromeDom()
  );
  let walkEpoch = 0;
  const knownRoots = /* @__PURE__ */ new WeakMap();
  const noRootInEpoch = /* @__PURE__ */ new WeakMap();
  function beginWalk() {
    walkEpoch += 1;
  }
  function shadowRootOfElement(el) {
    const known = knownRoots.get(el);
    if (known) return known;
    if (noRootInEpoch.get(el) === walkEpoch) return null;
    const root = shadowRootAccessor(el);
    if (root) knownRoots.set(el, root);
    else noRootInEpoch.set(el, walkEpoch);
    return root;
  }
  const adapted = /* @__PURE__ */ new WeakMap();
  function adaptElement(el) {
    const cached = adapted.get(el);
    if (cached) return cached;
    const wrapper = {
      __real: el,
      nodeType: 1,
      tagName: el.tagName,
      get childNodes() {
        return adaptChildNodes(el);
      },
      get previousElementSibling() {
        return el.previousElementSibling ? adaptElement(el.previousElementSibling) : null;
      },
      get nextElementSibling() {
        return el.nextElementSibling ? adaptElement(el.nextElementSibling) : null;
      },
      get parentElement() {
        return el.parentElement ? adaptElement(el.parentElement) : null;
      },
      get textContent() {
        return el.textContent;
      },
      getAttribute: (name) => el.getAttribute(name),
      hasAttribute: (name) => el.hasAttribute(name),
      get value() {
        return readValue(el);
      },
      get validationMessage() {
        return readValidationMessage(el);
      },
      attributeNames: () => el.getAttributeNames(),
      get labelTexts() {
        return readLabelTexts(el);
      },
      get resolvedHref() {
        return resolveHref(el);
      },
      get frame() {
        return resolveFrame(el);
      },
      get shadowRoot() {
        const root = shadowRootOfElement(el);
        return root ? adaptShadowRoot(root) : null;
      },
      get rootHost() {
        const host2 = el.getRootNode().host;
        return host2 ? adaptElement(host2) : null;
      },
      get frameHost() {
        return resolveFrameHost(el);
      },
      ...el.tagName === "SLOT" ? { assignedNodes: () => adaptNodes(el.assignedNodes({ flatten: true })) } : {}
    };
    adapted.set(el, wrapper);
    return wrapper;
  }
  const adaptedRoots = /* @__PURE__ */ new WeakMap();
  function adaptShadowRoot(root) {
    const cached = adaptedRoots.get(root);
    if (cached) return cached;
    const wrapper = {
      mode: root.mode,
      get childNodes() {
        return adaptNodes(root.childNodes);
      }
    };
    adaptedRoots.set(root, wrapper);
    return wrapper;
  }
  function adaptChildNodes(el) {
    return adaptNodes(el.childNodes);
  }
  function adaptNodes(nodes) {
    const out = [];
    for (const node of Array.from(nodes)) {
      if (node.nodeType === Node.ELEMENT_NODE) out.push(adaptElement(node));
      else if (node.nodeType === Node.TEXT_NODE) out.push({ nodeType: 3, textContent: node.textContent });
    }
    return out;
  }
  function readValue(el) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return el.value;
    if (el instanceof HTMLSelectElement) return el.options[el.selectedIndex]?.text ?? el.value;
    return void 0;
  }
  function readValidationMessage(el) {
    const withValidity = el;
    return typeof withValidity.validationMessage === "string" ? withValidity.validationMessage : void 0;
  }
  function readLabelTexts(el) {
    const withLabels = el;
    if (withLabels.labels && withLabels.labels.length > 0) {
      const texts = Array.from(withLabels.labels).map((label) => (label.textContent ?? "").trim()).filter(Boolean);
      if (texts.length > 0) return texts;
    }
    const id = el.getAttribute("id");
    if (id && el.ownerDocument) {
      try {
        const scope = el.getRootNode();
        const label = (typeof scope.querySelector === "function" ? scope : el.ownerDocument).querySelector(
          `label[for="${cssEscape(id)}"]`
        );
        const text = label?.textContent?.trim();
        if (text) return [text];
      } catch {
      }
    }
    return void 0;
  }
  function cssEscape(id) {
    if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(id);
    return id.replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }
  function resolveHref(el) {
    if (el.tagName !== "A") return void 0;
    const href = el.getAttribute("href");
    if (!href) return void 0;
    try {
      const url = new URL(href, window.location.href);
      return url.origin === window.location.origin ? `${url.pathname}${url.search}${url.hash}` : url.href;
    } catch {
      return href;
    }
  }
  function resolveFrameHost(el) {
    try {
      const view = el.ownerDocument?.defaultView;
      const frameElement = view?.frameElement;
      return frameElement ? adaptElement(frameElement) : null;
    } catch {
      return null;
    }
  }
  function resolveFrameOrigin(doc) {
    try {
      return doc.defaultView?.location.origin ?? doc.location?.origin;
    } catch {
      return void 0;
    }
  }
  function resolveFrame(el) {
    if (el.tagName !== "IFRAME") return void 0;
    try {
      const doc = el.contentDocument;
      if (!doc || !doc.body) return { crossOrigin: true };
      return { crossOrigin: false, body: adaptElement(doc.body), origin: resolveFrameOrigin(doc) };
    } catch {
      return { crossOrigin: true };
    }
  }
  function makeDomContext() {
    beginWalk();
    const active = document.activeElement;
    const focalTop = active && active !== document.body && active !== document.documentElement ? midpoint(active.getBoundingClientRect()) : window.innerHeight / 2;
    return {
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      focalTop,
      rectOf: (domEl) => toRect(domEl.__real.getBoundingClientRect()),
      styleOf: (domEl) => toStyle(window.getComputedStyle(domEl.__real)),
      scrollOf: (domEl) => toScrollMetrics(domEl.__real),
      // G3 (`browser_bridge_inspect`'s `style` question): a fresh
      // getComputedStyle() read per call, same cost profile as `styleOf`
      // above -- content/inspect.ts's own allowlist is what keeps this from
      // ever being asked for an arbitrary CSS property (see dom-types.ts's
      // doc comment on this method).
      computedStyleValue: (domEl, prop) => window.getComputedStyle(domEl.__real).getPropertyValue(prop),
      resolveId: (id, near) => {
        const scope = near ? near.__real.getRootNode() : document;
        const found = typeof scope.getElementById === "function" ? scope.getElementById(id) : document.getElementById(id);
        return found ? adaptElement(found) : null;
      }
    };
  }
  function midpoint(rect) {
    return (rect.top + rect.bottom) / 2;
  }
  function toRect(rect) {
    return { top: rect.top, bottom: rect.bottom, left: rect.left, width: rect.width, height: rect.height };
  }
  function toStyle(style) {
    return {
      display: style.display,
      visibility: style.visibility,
      overflowY: style.overflowY,
      overflowX: style.overflowX,
      zIndex: style.zIndex,
      position: style.position
    };
  }
  function toScrollMetrics(el) {
    return {
      scrollTop: el.scrollTop,
      scrollLeft: el.scrollLeft,
      scrollHeight: el.scrollHeight,
      scrollWidth: el.scrollWidth,
      clientHeight: el.clientHeight,
      clientWidth: el.clientWidth
    };
  }
  function getViewportSize() {
    return {
      width: window.innerWidth,
      height: window.innerHeight,
      dpr: window.devicePixelRatio,
      scrollX: window.scrollX,
      scrollY: window.scrollY
    };
  }
  function browserPierceEnv() {
    beginWalk();
    return {
      document,
      queryAll: (scope, selector) => Array.from(scope.querySelectorAll(selector)),
      shadowRootOf: (el) => shadowRootOfElement(el),
      isVisible: (el) => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden" && style.display !== "none";
      },
      intersectsViewport: (el) => {
        const r = el.getBoundingClientRect();
        return r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
      }
    };
  }
  function resolveAnnotateCandidates(boxes, indexMap, candidateIdxs) {
    const byIdx = new Map(boxes.map((b) => [b.idx, b]));
    const out = [];
    for (const idx of candidateIdxs) {
      const box = byIdx.get(idx);
      const selector = indexMap[idx];
      if (!box || !selector) continue;
      out.push({ idx, role: box.role, name: box.name, selector });
    }
    return out;
  }
  function createAnnotateSession(overlay) {
    let settled = false;
    let resolveResult;
    const result = new Promise((resolve2) => {
      resolveResult = resolve2;
    });
    const settle = (r) => {
      if (settled) return;
      settled = true;
      try {
        resolveResult(r);
      } finally {
        overlay.destroy();
      }
    };
    return {
      choose: (idx) => settle({ choice: idx, cancelled: false }),
      cancel: () => settle({ choice: null, cancelled: true }),
      result
    };
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
  function resolvePierceSelector(selector, env, scope = env.document) {
    const split = splitPierceSelector(selector);
    if (!split.ok) return { found: false, invalid: split.error };
    const parts = split.parts;
    if (parts.some((part) => part === "")) return { found: false, invalid: `empty selector part in "${selector}"` };
    try {
      return parts.length === 1 ? resolvePlain(parts[0], env, scope) : resolveChain(parts, env, scope);
    } catch (error) {
      return { found: false, invalid: error instanceof Error ? error.message : String(error) };
    }
  }
  function resolveChain(parts, env, scope) {
    let current = scope;
    for (let i = 0; i < parts.length - 1; i++) {
      const host2 = env.queryAll(current, parts[i])[0];
      if (!host2) return { found: false };
      const root = env.shadowRootOf(host2);
      if (!root) return { found: false };
      current = root;
    }
    const matches = env.queryAll(current, parts[parts.length - 1]);
    if (matches.length === 0) return { found: false };
    return {
      found: true,
      element: matches[0],
      matchCount: matches.length,
      chosenReason: matches.length === 1 ? "only-match" : "first-match"
    };
  }
  function resolvePlain(selector, env, scope) {
    const matches = queryAllComposed(selector, env, scope);
    if (matches.length === 0) return { found: false };
    if (matches.length === 1) return { found: true, element: matches[0], matchCount: 1, chosenReason: "only-match" };
    const inView = matches.find((el) => env.isVisible(el) && env.intersectsViewport(el));
    if (inView) return { found: true, element: inView, matchCount: matches.length, chosenReason: "visible-in-viewport" };
    const visible = matches.find((el) => env.isVisible(el));
    if (visible) return { found: true, element: visible, matchCount: matches.length, chosenReason: "first-visible" };
    return { found: true, element: matches[0], matchCount: matches.length, chosenReason: "first-match" };
  }
  function queryAllComposed(selector, env, scope = env.document) {
    const out = [...env.queryAll(scope, selector)];
    for (const root of shadowRootsWithin(env, scope)) out.push(...env.queryAll(root, selector));
    return out;
  }
  function shadowRootsWithin(env, scope) {
    const roots = [];
    const collect = (within) => {
      for (const el of env.queryAll(within, "*")) {
        const root = env.shadowRootOf(el);
        if (!root) continue;
        roots.push(root);
        collect(root);
      }
    };
    collect(scope);
    return roots;
  }
  function clean(text) {
    return (text ?? "").trim().replace(/\s+/g, " ");
  }
  function computeAccessibleName(el, resolver) {
    const ariaLabel = clean(el.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = clean(
        labelledBy.split(/\s+/).filter(Boolean).map((id) => resolver.byId(id)?.textContent ?? "").join(" ")
      );
      if (text) return text;
    }
    const labelText = clean(
      resolver.labelsFor(el).map((label) => label.textContent ?? "").join(" ")
    );
    if (labelText) return labelText;
    const placeholder = clean(el.getAttribute("placeholder"));
    if (placeholder) return placeholder;
    const title = clean(el.getAttribute("title"));
    if (title) return title;
    return clean(el.textContent);
  }
  const MAIN_TAGS = /* @__PURE__ */ new Set(["MAIN", "ARTICLE"]);
  const MAIN_ROLES = /* @__PURE__ */ new Set(["main", "article"]);
  const CHROME_TAGS$1 = /* @__PURE__ */ new Set(["NAV", "ASIDE", "FOOTER", "HEADER"]);
  const CHROME_ROLES = /* @__PURE__ */ new Set(["navigation", "complementary", "contentinfo", "banner"]);
  function regionOf(tagName, role, parent) {
    if (parent === "dialog") return "dialog";
    if (parent === "main") return "main";
    const tag = tagName.toUpperCase();
    const r = (role ?? "").trim().toLowerCase();
    if (MAIN_TAGS.has(tag) || MAIN_ROLES.has(r)) return "main";
    if (parent === "chrome") return "chrome";
    if (CHROME_TAGS$1.has(tag) || CHROME_ROLES.has(r)) return "chrome";
    return "neutral";
  }
  function dropTier(line) {
    const inView = line.distance === 0;
    switch (line.region ?? "neutral") {
      case "dialog":
        return line.interactive ? -3 : -1.5;
      case "chrome":
        return line.interactive ? inView ? 0 : 2 : 3;
      case "main":
        return line.interactive ? -2 : inView ? -1 : 0.5;
      default:
        return line.interactive ? 0 : 1;
    }
  }
  const TRUNCATION_MARKER = "\n[... truncated ...]";
  function byteLength(text) {
    return new TextEncoder().encode(text).length;
  }
  function fitToBudget(lines, budgetBytes) {
    if (lines.length === 0) return { tree: "", truncated: false, droppedCount: 0 };
    const lineBytes = lines.map((l) => byteLength(l.text) + 1);
    let total = lineBytes.reduce((a, b) => a + b, 0);
    if (total <= budgetBytes) {
      return { tree: lines.map((l) => l.text).join("\n"), truncated: false, droppedCount: 0 };
    }
    const markerBytes = byteLength(TRUNCATION_MARKER);
    const targetBytes = Math.max(0, budgetBytes - markerBytes);
    const dropFirst = (a, b) => dropTier(b) - dropTier(a) || b.distance - a.distance || b.order - a.order;
    const keep = new Set(lines.map((_, i) => i));
    const dropOrder = lines.map((l, i) => ({ i, l })).filter(({ l }) => !l.pinned).sort((a, b) => dropFirst(a.l, b.l));
    for (const { i } of dropOrder) {
      if (total <= targetBytes) break;
      keep.delete(i);
      total -= lineBytes[i];
    }
    const kept = lines.filter((_, i) => keep.has(i)).sort((a, b) => a.order - b.order);
    const tree = kept.length > 0 ? kept.map((l) => l.text).join("\n") + TRUNCATION_MARKER : TRUNCATION_MARKER.trimStart();
    return { tree, truncated: true, droppedCount: lines.length - keep.size };
  }
  function shadowRootOf(el) {
    const root = el.shadowRoot;
    if (!root) return null;
    if (isOwnUiHostId(el.getAttribute("id"))) return null;
    return root;
  }
  function composedChildNodes(el) {
    const root = shadowRootOf(el);
    if (root) return root.childNodes;
    if (el.assignedNodes) {
      const assigned = el.assignedNodes();
      if (assigned.length > 0) return assigned;
    }
    return el.childNodes;
  }
  function childElements(el) {
    const out = [];
    const nodes = composedChildNodes(el);
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (node.nodeType === 1) out.push(node);
    }
    return out;
  }
  function directText(el) {
    let out = "";
    const nodes = composedChildNodes(el);
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (node.nodeType === 3) out += node.textContent ?? "";
    }
    return collapseWhitespace(out);
  }
  function slottedText(el) {
    let out = "";
    const nodes = composedChildNodes(el);
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (node.nodeType === 1 && node.tagName.toUpperCase() === "SLOT") out += composedText(node);
    }
    return collapseWhitespace(out);
  }
  const NON_TEXT_TAGS = /* @__PURE__ */ new Set(["STYLE", "SCRIPT", "TEMPLATE", "NOSCRIPT"]);
  function composedText(el) {
    if (!shadowRootOf(el) && !el.assignedNodes && !hasElementChild(el)) return el.textContent ?? "";
    const parts = [];
    appendComposedText(el, false, parts);
    return parts.join("");
  }
  function appendComposedText(el, inShadow, parts) {
    const childInShadow = inShadow || shadowRootOf(el) !== null;
    const nodes = composedChildNodes(el);
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (node.nodeType === 3) {
        parts.push(node.textContent ?? "");
      } else if (!(childInShadow && NON_TEXT_TAGS.has(node.tagName.toUpperCase()))) {
        appendComposedText(node, childInShadow, parts);
      }
    }
  }
  function hasElementChild(el) {
    for (let i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 1) return true;
    }
    return false;
  }
  function collapseWhitespace(text) {
    return (text ?? "").trim().replace(/\s+/g, " ");
  }
  function redactionMarker(kind) {
    return `[redacted:${kind}]`;
  }
  const ALL_ENABLED = {
    password: true,
    card: true,
    ssn: true,
    email: true,
    phone: true
  };
  function passwordNameSegments(value) {
    return value.replace(/([a-z0-9])([A-Z])/g, "$1_$2").split(/[-_\s]+/).map((segment) => segment.toLowerCase()).filter(Boolean);
  }
  const PASSWORD_NAME_SEGMENTS = /* @__PURE__ */ new Set(["pass", "password", "passcode", "pwd", "secret", "pin", "pincode"]);
  function hasPasswordNameSegment(segments) {
    return segments.some((segment) => PASSWORD_NAME_SEGMENTS.has(segment));
  }
  function autocompleteTokens(autocomplete) {
    return autocomplete.toLowerCase().split(/\s+/).filter(Boolean);
  }
  function hasAutocompleteToken(autocomplete, tokens) {
    return autocompleteTokens(autocomplete).some((token) => tokens.has(token));
  }
  const PASSWORD_AUTOCOMPLETE_TOKENS = /* @__PURE__ */ new Set(["current-password", "new-password"]);
  const CARD_AUTOCOMPLETE_TOKENS = /* @__PURE__ */ new Set(["cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year"]);
  const OTP_AUTOCOMPLETE_TOKENS = /* @__PURE__ */ new Set(["one-time-code"]);
  function nameSegments(value) {
    return value.replace(/([a-z0-9])([A-Z])/g, "$1_$2").split(/[-_\s]+/).map((segment) => segment.toLowerCase()).map((segment) => segment.replace(/\d+$/, "")).filter(Boolean);
  }
  const CARD_NAME_SEGMENTS = /* @__PURE__ */ new Set(["cvv", "cvc", "csc"]);
  const OTP_NAME_SEGMENTS = /* @__PURE__ */ new Set(["otp", "totp", "hotp"]);
  const CARD_NAME_BIGRAMS = [
    ["security", "code"],
    ["card", "verification"]
  ];
  function hasCardNameSegment(segments) {
    if (segments.some((segment) => CARD_NAME_SEGMENTS.has(segment))) return true;
    for (let i = 0; i < segments.length - 1; i++) {
      for (const [first, second] of CARD_NAME_BIGRAMS) {
        if (segments[i] === first && segments[i + 1] === second) return true;
      }
    }
    return false;
  }
  function hasOtpNameSegment(segments) {
    return segments.some((segment) => OTP_NAME_SEGMENTS.has(segment));
  }
  function classifySensitiveField(field) {
    const type = (field.type ?? "").toLowerCase();
    const autocomplete = field.autocomplete ?? "";
    const name = field.name ?? "";
    const id = field.id ?? "";
    const nameSegs = nameSegments(name);
    const idSegs = nameSegments(id);
    if (type === "password") return "password";
    if (hasAutocompleteToken(autocomplete, PASSWORD_AUTOCOMPLETE_TOKENS)) return "password";
    if (hasPasswordNameSegment(passwordNameSegments(name)) || hasPasswordNameSegment(passwordNameSegments(id)))
      return "password";
    if (hasAutocompleteToken(autocomplete, CARD_AUTOCOMPLETE_TOKENS)) return "card";
    if (hasCardNameSegment(nameSegs) || hasCardNameSegment(idSegs)) return "card";
    if (hasAutocompleteToken(autocomplete, OTP_AUTOCOMPLETE_TOKENS)) return "otp";
    if (hasOtpNameSegment(nameSegs) || hasOtpNameSegment(idSegs)) return "otp";
    return null;
  }
  function isSensitiveField(field) {
    return classifySensitiveField(field) !== null;
  }
  function luhnCheck(digits) {
    if (!/^\d+$/.test(digits) || digits.length === 0) return false;
    let sum = 0;
    let double = false;
    for (let i = digits.length - 1; i >= 0; i--) {
      let n = digits.charCodeAt(i) - 48;
      if (double) {
        n *= 2;
        if (n > 9) n -= 9;
      }
      sum += n;
      double = !double;
    }
    return sum % 10 === 0;
  }
  const CARD_CANDIDATE = /\b\d(?:[\d][ -]?){11,22}\d\b/g;
  const SSN_PATTERN = /\b\d{3}[- ]\d{2}[- ]\d{4}\b/g;
  const EMAIL_PATTERN = /\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}\b/g;
  const PHONE_PATTERN = /\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b/g;
  function redactText(input, policy = ALL_ENABLED) {
    let count = 0;
    let out = input;
    if (policy.card) {
      out = out.replace(CARD_CANDIDATE, (match) => {
        const digits = match.replace(/[ -]/g, "");
        if (digits.length < 13 || digits.length > 19) return match;
        if (!luhnCheck(digits)) return match;
        count++;
        return redactionMarker("card");
      });
    }
    if (policy.ssn) {
      out = out.replace(SSN_PATTERN, () => {
        count++;
        return redactionMarker("ssn");
      });
    }
    if (policy.email) {
      out = out.replace(EMAIL_PATTERN, () => {
        count++;
        return redactionMarker("email");
      });
    }
    if (policy.phone) {
      out = out.replace(PHONE_PATTERN, () => {
        count++;
        return redactionMarker("phone");
      });
    }
    return { text: out, count };
  }
  function redactFieldValue(value, field, policy = ALL_ENABLED) {
    const kind = classifySensitiveField(field);
    if (kind === "password") {
      if (policy.password) return { text: redactionMarker("password"), count: 1 };
    } else if (kind === "card" || kind === "otp") {
      return { text: redactionMarker(kind), count: 1 };
    }
    return redactText(value, policy);
  }
  const INPUT_TYPE_ROLES = {
    text: "textbox",
    search: "textbox",
    email: "textbox",
    url: "textbox",
    tel: "textbox",
    number: "textbox",
    password: "textbox",
    date: "textbox",
    "datetime-local": "textbox",
    month: "textbox",
    week: "textbox",
    time: "textbox",
    color: "textbox",
    checkbox: "checkbox",
    radio: "radio",
    range: "slider",
    file: "file",
    submit: "button",
    button: "button",
    reset: "button",
    image: "button"
  };
  function roleForElement(d) {
    const explicit = (d.explicitRole ?? "").trim().toLowerCase();
    if (explicit) return explicit;
    switch (d.tagName) {
      case "BUTTON":
        return "button";
      case "A":
        return "link";
      case "SELECT":
        return "combobox";
      case "TEXTAREA":
        return "textbox";
      // <summary> is the always-focusable disclosure control for its parent
      // <details> (native "button"-ish semantics with no ARIA role of its
      // own) — walker.ts's emitInteractive reads the parent's `open`
      // attribute to print `expanded`/`collapsed` for it, same as an
      // aria-expanded element.
      case "SUMMARY":
        return "button";
      case "INPUT":
        return INPUT_TYPE_ROLES[(d.type ?? "text").toLowerCase()] ?? "textbox";
      default:
        return "generic";
    }
  }
  const NATIVE_INTERACTIVE_TAGS = /* @__PURE__ */ new Set(["BUTTON", "INPUT", "SELECT", "TEXTAREA"]);
  const INTERACTIVE_ROLES = /* @__PURE__ */ new Set([
    "button",
    "link",
    "checkbox",
    "radio",
    "combobox",
    "textbox",
    "switch",
    "slider",
    "menuitem",
    "tab",
    "option"
  ]);
  function isInteractiveDescriptor(d) {
    const tag = d.tagName;
    const role = (d.role ?? "").trim().toLowerCase();
    const tabindexUsable = d.tabindex != null && d.tabindex !== "-1";
    if (tag === "A") return !!d.hasHref || INTERACTIVE_ROLES.has(role) || tabindexUsable;
    if (NATIVE_INTERACTIVE_TAGS.has(tag)) return true;
    if (tag === "SUMMARY") return true;
    if (d.hasAriaExpanded) return true;
    if (d.hasClickHandlerAttr) return true;
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (tabindexUsable) return true;
    if (d.hasOnclick) return true;
    return false;
  }
  const CLICK_HANDLER_ATTRS = [
    "ng-click",
    "data-ng-click",
    "v-on:click",
    "@click",
    "x-on:click",
    "jsaction"
  ];
  const FRAME_HOP = "|>>";
  function wouldRedact(value, policy) {
    return redactText(value, policy).count > 0;
  }
  function cssEscapeIdent(id) {
    let out = "";
    for (let i = 0; i < id.length; i++) {
      const code = id.charCodeAt(i);
      const ch = id[i];
      if (code === 0) out += "�";
      else if (code >= 1 && code <= 31 || code === 127) out += `\\${code.toString(16)} `;
      else if (i === 0 && code >= 48 && code <= 57) out += `\\${code.toString(16)} `;
      else if (i === 1 && code >= 48 && code <= 57 && id.charCodeAt(0) === 45) out += `\\${code.toString(16)} `;
      else if (i === 0 && ch === "-" && id.length === 1) out += "\\-";
      else if (code >= 128 || ch === "-" || ch === "_" || /[0-9A-Za-z]/.test(ch)) out += ch;
      else out += `\\${ch}`;
    }
    return out;
  }
  function cssEscapeAttrValue(value) {
    let out = "";
    for (let i = 0; i < value.length; i++) {
      const ch = value[i];
      const code = value.charCodeAt(i);
      if (code === 0) out += "�";
      else if (ch === '"' || ch === "\\") out += `\\${ch}`;
      else if (ch === ">" || ch === "|") out += `\\${ch}`;
      else if (code <= 31 || code === 127) out += `\\${code.toString(16)} `;
      else out += ch;
    }
    return out;
  }
  const FORM_CONTROL_TAGS = /* @__PURE__ */ new Set(["INPUT", "SELECT", "TEXTAREA", "BUTTON"]);
  const TEST_ID_ATTRS = ["data-testid", "data-test", "data-cy"];
  const SEMANTIC_INDEX_NODE_CAP = 5e4;
  function keyFor(kind, value) {
    return `${kind}\0${value}`;
  }
  function buildSemanticIndex(root, getChildren) {
    const counts = /* @__PURE__ */ new Map();
    let visited = 0;
    let overflowed = false;
    const bump = (kind, value) => {
      const key = keyFor(kind, value);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    };
    const visit = (node) => {
      if (overflowed) return;
      visited++;
      if (visited > SEMANTIC_INDEX_NODE_CAP) {
        overflowed = true;
        return;
      }
      const tag = node.tagName.toUpperCase();
      const id = node.getAttribute("id");
      if (id) bump("id", id);
      for (const attr of TEST_ID_ATTRS) {
        const value = node.getAttribute(attr);
        if (value) bump(attr, value);
      }
      if (FORM_CONTROL_TAGS.has(tag)) {
        const name = node.getAttribute("name");
        if (name) bump(`name:${tag}`, name);
      }
      const ariaLabel = node.getAttribute("aria-label");
      if (ariaLabel) bump("aria-label", ariaLabel);
      const role = node.getAttribute("role");
      if (role) bump("role", role);
      for (const child of getChildren(node)) visit(child);
    };
    visit(root);
    return {
      overflowed,
      isUnique: (kind, value) => !overflowed && counts.get(keyFor(kind, value)) === 1
    };
  }
  function selfAttributeCandidate(el, index, policy) {
    let redactedCandidate = false;
    for (const attr of TEST_ID_ATTRS) {
      const value = el.getAttribute(attr);
      if (value && index.isUnique(attr, value)) {
        if (wouldRedact(value, policy)) {
          redactedCandidate = true;
          continue;
        }
        return { candidate: { selector: `[${attr}="${cssEscapeAttrValue(value)}"]`, strategy: attr }, redactedCandidate };
      }
    }
    const tag = el.tagName.toUpperCase();
    if (FORM_CONTROL_TAGS.has(tag)) {
      const name = el.getAttribute("name");
      if (name && index.isUnique(`name:${tag}`, name)) {
        if (wouldRedact(name, policy)) {
          redactedCandidate = true;
        } else {
          return { candidate: { selector: `${tag.toLowerCase()}[name="${cssEscapeAttrValue(name)}"]`, strategy: "name" }, redactedCandidate };
        }
      }
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && index.isUnique("aria-label", ariaLabel)) {
      if (wouldRedact(ariaLabel, policy)) {
        redactedCandidate = true;
      } else {
        return { candidate: { selector: `[aria-label="${cssEscapeAttrValue(ariaLabel)}"]`, strategy: "aria-label" }, redactedCandidate };
      }
    }
    const role = el.getAttribute("role");
    if (role && index.isUnique("role", role)) {
      if (wouldRedact(role, policy)) {
        redactedCandidate = true;
      } else {
        return { candidate: { selector: `[role="${cssEscapeAttrValue(role)}"]`, strategy: "role" }, redactedCandidate };
      }
    }
    return { candidate: null, redactedCandidate };
  }
  function renderSelector(segments, anchorTop = false) {
    if (segments.length === 0) return "";
    for (let i = segments.length - 1; i >= 0; i--) {
      const seg = segments[i];
      if (seg.id) {
        const tail = segments.slice(i + 1).map(segmentToCss).join(">");
        const head = `#${cssEscapeIdent(seg.id)}`;
        return tail ? `${head}>${tail}` : head;
      }
    }
    const css = segments.map(segmentToCss);
    if (anchorTop) css[0] = `${segments[0].tag.toLowerCase()}:not(* *):nth-of-type(${segments[0].nth})`;
    return css.join(">");
  }
  function segmentToCss(seg) {
    return seg.id ? `#${cssEscapeIdent(seg.id)}` : `${seg.tag.toLowerCase()}:nth-of-type(${seg.nth})`;
  }
  function buildPathSegments(el, root, index, policy, flags) {
    const segments = [];
    let cur = el;
    while (cur) {
      const id = cur.getAttribute("id") ?? void 0;
      const tag = cur.tagName.toUpperCase();
      if (id && (!index || index.isUnique("id", id))) {
        if (wouldRedact(id, policy)) {
          if (flags) flags.redactedCandidate = true;
        } else {
          segments.unshift({ tag, nth: 1, id });
          break;
        }
      }
      let nth = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName.toUpperCase() === tag) nth++;
        sib = sib.previousElementSibling;
      }
      segments.unshift({ tag, nth });
      if (cur === root) break;
      cur = cur.parentElement;
    }
    return segments;
  }
  function selectorForTreeLevel(cur, root, anchorTop, isTarget, index, policy) {
    const flags = { redactedCandidate: false };
    const idSegments = buildPathSegments(cur, root, index, policy, flags);
    if (idSegments[0]?.id) return { selector: renderSelector(idSegments, anchorTop), strategy: "id" };
    let redactedCandidate = flags.redactedCandidate;
    if (isTarget) {
      const outcome = selfAttributeCandidate(cur, index, policy);
      if (outcome.candidate) return outcome.candidate;
      redactedCandidate = redactedCandidate || outcome.redactedCandidate;
    }
    return {
      selector: renderSelector(idSegments, anchorTop),
      strategy: redactedCandidate ? "positional:redacted-candidate" : "positional"
    };
  }
  function buildPierceSelector(el, root, index, policy) {
    const parts = [];
    const hopKinds = [];
    let cur = el;
    let strategy = "positional";
    let isTarget = true;
    for (; ; ) {
      const shadowHost = cur.rootHost ?? null;
      if (shadowHost) {
        const result2 = selectorForTreeLevel(cur, null, true, isTarget, index, policy);
        if (isTarget) strategy = result2.strategy;
        parts.unshift(result2.selector);
        hopKinds.unshift("shadow");
        isTarget = false;
        cur = shadowHost;
        continue;
      }
      const frameHost = cur.frameHost ?? null;
      const result = selectorForTreeLevel(cur, frameHost ? null : root, false, isTarget, index, policy);
      if (isTarget) strategy = result.strategy;
      parts.unshift(result.selector);
      isTarget = false;
      if (!frameHost) break;
      hopKinds.unshift("frame");
      cur = frameHost;
    }
    let selector = parts[0] ?? "";
    for (let i = 1; i < parts.length; i++) {
      const hop = hopKinds[i - 1] === "frame" ? FRAME_HOP : PIERCE_SEPARATOR;
      selector += hop + parts[i];
    }
    return { selector, strategy };
  }
  const CHROME_TAGS = /* @__PURE__ */ new Set(["NAV", "HEADER", "FOOTER", "ASIDE", "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG"]);
  function isChromeTag(tagName) {
    return CHROME_TAGS.has(tagName.toUpperCase());
  }
  function headingLevel(tagName) {
    const match = /^H([1-6])$/.exec(tagName.toUpperCase());
    return match ? Number(match[1]) : null;
  }
  function formatMarkdown(blocks) {
    return blocks.map(formatMarkdownBlock).join("\n\n");
  }
  function formatMarkdownBlock(block) {
    switch (block.kind) {
      case "heading":
        return `${"#".repeat(clampLevel(block.level))} ${block.text}`;
      case "listitem":
        return `${block.ordered ? "1." : "-"} ${block.text}`;
      case "link":
        return `[${block.text}](${block.href ?? ""})`;
      case "paragraph":
      default:
        return block.text;
    }
  }
  function clampLevel(level) {
    return Math.min(Math.max(level ?? 1, 1), 6);
  }
  function formatText(blocks) {
    return blocks.map((b) => b.text).join("\n\n");
  }
  function capToBytes(text, maxBytes) {
    const encoder = new TextEncoder();
    if (encoder.encode(text).length <= maxBytes) return { text, truncated: false };
    let lo = 0;
    let hi = text.length;
    while (lo < hi) {
      const mid = lo + hi + 1 >> 1;
      if (encoder.encode(text.slice(0, mid)).length <= maxBytes) lo = mid;
      else hi = mid - 1;
    }
    return { text: text.slice(0, lo), truncated: true };
  }
  const NEVER_VISIBLE_TAGS = /* @__PURE__ */ new Set([
    "SCRIPT",
    "STYLE",
    "NOSCRIPT",
    "TEMPLATE",
    "META",
    "LINK",
    "HEAD"
  ]);
  function hasHiddenMarkup(el) {
    const tag = el.tagName.toUpperCase();
    if (NEVER_VISIBLE_TAGS.has(tag)) return true;
    if (el.hasAttribute("hidden")) return true;
    if ((el.getAttribute("aria-hidden") ?? "").toLowerCase() === "true") return true;
    const style = el.getAttribute("style") ?? "";
    if (/display\s*:\s*none/i.test(style)) return true;
    if (/visibility\s*:\s*hidden/i.test(style)) return true;
    return false;
  }
  function isEffectivelyHidden(el, computed, rect) {
    if (hasHiddenMarkup(el)) return true;
    if (computed.display === "none" || computed.visibility === "hidden") return true;
    if (rect.width <= 0 && rect.height <= 0 && !mayRenderWithoutOwnBox(el, computed)) return true;
    return false;
  }
  function mayRenderWithoutOwnBox(el, computed) {
    return computed.display === "contents" || el.tagName.toUpperCase() === "SLOT" || !!el.shadowRoot;
  }
  const DENY_ALL_ORIGIN_POLICY = { granted: [], denied: [], defaultFull: false };
  function canonicalizeOrigin(origin) {
    if (origin === "null") return origin;
    try {
      const u = new URL(origin);
      const host2 = u.hostname.replace(/\.+$/, "");
      const port = u.port ? `:${u.port}` : "";
      return `${u.protocol}//${host2}${port}`;
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
  const MAX_SNAPSHOT_BUDGET_BYTES = 65536;
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
  const SYNONYM_GROUPS = [
    ["sign in", "log in", "login"],
    ["sign out", "log out", "logout"],
    ["delete", "remove", "trash"],
    ["settings", "preferences", "options", "configuration"],
    ["search", "find"],
    ["next", "continue", "proceed"],
    ["back", "previous"],
    ["close", "dismiss", "cancel"],
    ["add", "create", "new"],
    ["save", "apply", "submit"]
  ];
  const SYNONYM_TIER = 3;
  function nameResolverFor(el, ctx) {
    return {
      byId: (id) => {
        const target = ctx.resolveId(id, el);
        return target ? nameable(target) : null;
      },
      labelsFor: () => (el.labelTexts ?? []).map((text) => ({
        tagName: "LABEL",
        getAttribute: () => null,
        textContent: text
      }))
    };
  }
  function describeElement(el, ctx, policy) {
    const tag = el.tagName.toUpperCase();
    const type = (el.getAttribute("type") ?? "").toLowerCase();
    const explicitRole = el.getAttribute("role");
    const role = roleForElement({ tagName: tag, type, explicitRole });
    const redacted = redactText(computeAccessibleName(nameable(el), nameResolverFor(el, ctx)), policy);
    return { role, name: clip(redacted.text), redactionCount: redacted.count };
  }
  function scrollContainerInfo(el, ctx) {
    const style = ctx.styleOf(el);
    const scroll = ctx.scrollOf(el);
    const maxTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    const maxLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
    const vertical = (style.overflowY === "auto" || style.overflowY === "scroll") && scroll.scrollHeight > scroll.clientHeight + 2;
    const horizontal = (style.overflowX === "auto" || style.overflowX === "scroll") && scroll.scrollWidth > scroll.clientWidth + 2;
    if (!vertical && !horizontal) return null;
    return { vertical, horizontal, scrollTop: scroll.scrollTop, scrollLeft: scroll.scrollLeft, maxTop, maxLeft };
  }
  function nameable(el) {
    return {
      tagName: el.tagName,
      getAttribute: (name) => el.getAttribute(name),
      get textContent() {
        return composedText(el);
      }
    };
  }
  const MAX_LINE_CHARS = 200;
  const MAX_LISTED_OPTIONS = 8;
  function buildSnapshotFromDom(root, ctx, budgetBytes, policy, originPolicy = DENY_ALL_ORIGIN_POLICY, opts = {}) {
    const lines = [];
    const indexMap = {};
    const indexMeta = {};
    const selectorStrategy = {};
    const boxes = [];
    const shadow2 = { open: 0, closed: 0 };
    const frameBoundaries = [];
    const findMatches = [];
    let redactions = 0;
    const assigner = new IndexAssigner(opts.existingIndexMap, opts.startIndex && opts.startIndex > 0 ? opts.startIndex : void 0);
    let order = 0;
    let findOrder = 0;
    let modalRoot = null;
    let dialogTitleHeadingEl = null;
    const semanticChildren = (el) => {
      const frame = el.frame;
      if (frame && !frame.crossOrigin && frame.body && (frame.origin === void 0 || isOriginAllowed(frame.origin, originPolicy))) {
        return childElements(frame.body);
      }
      return childElements(el);
    };
    const semanticIndex = buildSemanticIndex(root, semanticChildren);
    const snapshotRoot = root;
    const distanceOf = (el) => {
      const rect = ctx.rectOf(el);
      if (rect.bottom >= 0 && rect.top <= ctx.viewportHeight) return 0;
      return Math.abs((rect.top + rect.bottom) / 2 - ctx.focalTop);
    };
    const pushLine = (text, interactive, el, region, pinned = false, dialogMarker = false) => {
      lines.push({ text: sanitizeControlChars(text), interactive, order: order++, distance: distanceOf(el), region, pinned, dialogMarker });
    };
    const accessibleName = (el) => computeAccessibleName(nameable(el), nameResolverFor(el, ctx));
    const explicitName = (el) => {
      const ariaLabel = collapseWhitespace(el.getAttribute("aria-label"));
      if (ariaLabel) return ariaLabel;
      const labelledBy = el.getAttribute("aria-labelledby");
      if (!labelledBy) return "";
      return collapseWhitespace(
        labelledBy.split(/\s+/).filter(Boolean).map((id) => {
          const target = ctx.resolveId(id, el);
          return target ? composedText(target) : "";
        }).join(" ")
      );
    };
    const nameForScrollContainer = (el, dialogTitle2) => {
      const own = explicitName(el);
      if (own) return own;
      let sib = el.previousElementSibling;
      while (sib) {
        if (headingLevel(sib.tagName.toUpperCase())) {
          const text = directText(sib) || accessibleName(sib);
          if (text) return text;
        }
        sib = sib.previousElementSibling;
      }
      if (dialogTitle2) return dialogTitle2;
      return "region";
    };
    const emitScrollContainer = (el, region, info, dialogTitle2, fold = void 0, isHidden = false) => {
      const name = sanitizeControlChars(record(nameForScrollContainer(el, dialogTitle2)).text);
      const built = buildPierceSelector(el, root, semanticIndex, policy);
      const { idx } = assigner.assign(built.selector, { role: "scrollable region", name });
      indexMap[idx] = built.selector;
      selectorStrategy[idx] = built.strategy;
      indexMeta[idx] = { role: "scrollable region", name };
      const rect = ctx.rectOf(el);
      boxes.push({ idx, role: "scrollable region", name, x: rect.left, y: rect.top, width: rect.width, height: rect.height });
      const parts = [];
      if (info.vertical) {
        const pct = info.maxTop > 0 ? Math.round(info.scrollTop / info.maxTop * 100) : 100;
        const moreAbove = info.scrollTop > 2;
        const moreBelow = info.scrollTop < info.maxTop - 2;
        const note = moreAbove && moreBelow ? " (more above and below)" : moreBelow ? " (more below)" : moreAbove ? " (more above)" : "";
        parts.push(`↕ ${pct}%${note}`);
      }
      if (info.horizontal) {
        const pct = info.maxLeft > 0 ? Math.round(info.scrollLeft / info.maxLeft * 100) : 100;
        const moreLeft = info.scrollLeft > 2;
        const moreRight = info.scrollLeft < info.maxLeft - 2;
        const note = moreLeft && moreRight ? " (more left and right)" : moreRight ? " (more right)" : moreLeft ? " (more left)" : "";
        parts.push(`↔ ${pct}%${note}`);
      }
      pushLine(`[${idx}] scrollable region "${escapeQuotes(name)}" ${parts.join(" ")}`.trimEnd(), true, el, region, dialogTitle2 !== void 0);
      tryRecordExistingMatch(idx, "scrollable region", name, parts.join(" ").trim(), el, isHidden, fold, [{ text: name, source: "name" }]);
      return idx;
    };
    const BACKDROP_MIN_VIEWPORT_COVERAGE = 0.6;
    const isBackdropCandidate = (el) => {
      const style = ctx.styleOf(el);
      if (style.position !== "fixed" && style.position !== "absolute") return false;
      const z = Number.parseInt(style.zIndex, 10);
      if (Number.isNaN(z)) return false;
      const rect = ctx.rectOf(el);
      const viewportArea = ctx.viewportWidth * ctx.viewportHeight;
      if (viewportArea <= 0) return false;
      const area = Math.max(0, rect.width) * Math.max(0, rect.height);
      return area >= viewportArea * BACKDROP_MIN_VIEWPORT_COVERAGE;
    };
    const findTopmostOpenDialog = (start) => {
      let best = null;
      let bestZ = Number.NEGATIVE_INFINITY;
      const scan = (el) => {
        if (el.frame) return;
        if (isEffectivelyHidden(el, ctx.styleOf(el), ctx.rectOf(el))) return;
        if (isOpenDialogCandidate(el) || isBackdropCandidate(el)) {
          const raw = Number.parseInt(ctx.styleOf(el).zIndex, 10);
          const z = Number.isNaN(raw) ? 0 : raw;
          if (best === null || z >= bestZ) {
            best = el;
            bestZ = z;
          }
        }
        for (const child of childElements(el)) scan(child);
      };
      scan(start);
      return best;
    };
    const dialogTitleOf = (el) => {
      const own = explicitName(el);
      if (own) return { title: own };
      const heading = firstHeadingDescendant(el);
      if (heading) return { title: heading.text, titleElement: heading.element };
      const titled = firstTitleClassDescendant(el);
      if (titled) return { title: titled };
      return { title: "dialog" };
    };
    const firstHeadingDescendant = (el) => {
      for (const child of childElements(el)) {
        if (headingLevel(child.tagName.toUpperCase())) {
          const text = directText(child) || accessibleName(child);
          if (text) return { text, element: child };
        }
        const nested = firstHeadingDescendant(child);
        if (nested) return nested;
      }
      return null;
    };
    const firstTitleClassDescendant = (el) => {
      for (const child of childElements(el)) {
        const classes = (child.getAttribute("class") ?? "").toLowerCase();
        if (classes.includes("title")) {
          const text = directText(child) || accessibleName(child);
          if (text) return text;
        }
        const nested = firstTitleClassDescendant(child);
        if (nested) return nested;
      }
      return "";
    };
    const record = (text) => {
      const result = redactText(text, policy);
      redactions += result.count;
      return result;
    };
    const visit = (el, parentRegion, dialogTitle2, fold) => {
      if (modalRoot && el === modalRoot) return;
      const hidden = isEffectivelyHidden(el, ctx.styleOf(el), ctx.rectOf(el));
      if (hidden && !opts.find) return;
      const tag = el.tagName.toUpperCase();
      const region = regionOf(tag, el.getAttribute("role"), parentRegion);
      if (el.frame) {
        const accessible = !el.frame.crossOrigin && !!el.frame.body;
        const blocked = accessible && el.frame.origin !== void 0 && !isOriginAllowed(el.frame.origin, originPolicy);
        if (!accessible || blocked) {
          const name = accessibleName(el) || "iframe";
          const line = blocked ? `iframe "${escapeQuotes(clip(record(name).text))}" [frame ${el.frame.origin} — not granted]` : `iframe "${escapeQuotes(clip(record(name).text))}" [cross-origin]`;
          pushLine(line, false, el, region);
          const rect = ctx.rectOf(el);
          frameBoundaries.push({
            selector: buildPierceSelector(el, snapshotRoot, semanticIndex, policy).selector,
            name: sanitizeControlChars(record(name).text),
            src: el.getAttribute("src"),
            area: Math.max(0, rect.width) * Math.max(0, rect.height),
            origin: accessible ? el.frame.origin : void 0,
            blocked: blocked || void 0
          });
        } else if (el.frame.body) {
          for (const child of childElements(el.frame.body)) visit(child, region, dialogTitle2, fold);
        }
        return;
      }
      if (tag === "SVG") {
        const name = collapseWhitespace(el.getAttribute("aria-label") ?? el.getAttribute("title"));
        if (name) pushLine(`"[icon: ${escapeQuotes(clip(record(name).text))}]"`, false, el, region);
        return;
      }
      if (isInteractive(el)) {
        emitInteractive(el, region, fold);
        return;
      }
      const scrollInfo = scrollContainerInfo(el, ctx);
      if (scrollInfo) {
        const idx = emitScrollContainer(el, region, scrollInfo, dialogTitle2, fold, hidden);
        const rect = ctx.rectOf(el);
        const scroll = ctx.scrollOf(el);
        const childFold = { idx, top: rect.top, bottom: rect.top + scroll.clientHeight };
        for (const child of childElements(el)) visit(child, region, dialogTitle2, childFold);
        return;
      }
      const level = headingLevel(tag);
      if (level) {
        if (el !== dialogTitleHeadingEl) {
          const text = directText(el) || accessibleName(el);
          const safe = clip(record(text).text);
          if (safe) {
            pushLine(`heading "${escapeQuotes(safe)}"`, false, el, region);
            tryRecordTextMatch(el, "heading", safe, hidden, fold);
          }
        }
        if (hasInteractiveDescendant(el, ctx)) for (const child of childElements(el)) visit(child, region, dialogTitle2, fold);
        return;
      }
      const root2 = shadowRootOf(el);
      if (root2) shadow2[root2.mode] += 1;
      const children = childElements(el);
      if (children.length === 0) {
        const text = collapseWhitespace(composedText(el));
        if (text) {
          const safe = clip(record(text).text);
          pushLine(`"${escapeQuotes(safe)}"`, false, el, region);
          tryRecordTextMatch(el, "text", safe, hidden, fold);
        }
        return;
      }
      const own = directText(el);
      if (own) {
        const safe = clip(record(own).text);
        pushLine(`"${escapeQuotes(safe)}"`, false, el, region);
        tryRecordTextMatch(el, "text", safe, hidden, fold);
      }
      for (const child of children) visit(child, region, dialogTitle2, fold);
    };
    const tierOf = (candidate, query) => {
      if (!candidate || !query) return null;
      const c = candidate.toLowerCase();
      const q = query.toLowerCase();
      if (c === q) return 0;
      if (c.startsWith(q)) return 1;
      if (c.includes(q)) return 2;
      return null;
    };
    const bestCandidateMatch = (candidates, query) => {
      let best = null;
      for (const c of candidates) {
        const t = tierOf(c.text, query);
        if (t !== null && (best === null || t < best.tier)) best = { tier: t, source: c.source };
      }
      return best;
    };
    const matchCandidates = (candidates, query) => {
      const literal = bestCandidateMatch(candidates, query);
      if (literal) return literal;
      const q = query.toLowerCase().trim();
      const group = SYNONYM_GROUPS.find((g) => g.includes(q));
      if (!group) return null;
      for (const word of group) {
        if (word === q) continue;
        for (const c of candidates) {
          if (tierOf(c.text, word) !== null) return { tier: SYNONYM_TIER, source: `synonym:${word}` };
        }
      }
      return null;
    };
    const locationOf = (el, isHidden, fold) => {
      const rect = ctx.rectOf(el);
      if (fold) {
        if (rect.top >= fold.bottom - 2) return `below the fold of scrollable region ${fold.idx}`;
        if (rect.bottom <= fold.top + 2) return `above the fold of scrollable region ${fold.idx}`;
      }
      if (!isHidden && rect.bottom >= 0 && rect.top <= ctx.viewportHeight && rect.left + rect.width >= 0 && rect.left <= ctx.viewportWidth) {
        return "in viewport";
      }
      return "off-screen";
    };
    const visTierOf = (el, isHidden, fold) => {
      if (isHidden) return 2;
      if (fold) {
        const rect = ctx.rectOf(el);
        if (rect.top >= fold.bottom - 2 || rect.bottom <= fold.top + 2) return 1;
      }
      return 0;
    };
    function tryRecordExistingMatch(idx, role, name, extra, el, isHidden, fold, candidates) {
      if (!opts.find) return;
      if (opts.find.role && role.toLowerCase() !== opts.find.role.toLowerCase()) return;
      const match = matchCandidates(candidates, opts.find.query);
      if (!match) return;
      findMatches.push({
        idx,
        role,
        name,
        extra,
        location: locationOf(el, isHidden, fold),
        matchTier: match.tier,
        visTier: visTierOf(el, isHidden, fold),
        order: findOrder++,
        matchedVia: match.source
      });
    }
    function tryRecordTextMatch(el, role, text, isHidden, fold) {
      if (!opts.find || !text) return;
      if (opts.find.role && role.toLowerCase() !== opts.find.role.toLowerCase()) return;
      const match = matchCandidates([{ text, source: "text" }], opts.find.query);
      if (!match) return;
      const built = buildPierceSelector(el, snapshotRoot, semanticIndex, policy);
      const { idx } = assigner.assign(built.selector, { role, name: text });
      indexMap[idx] = built.selector;
      selectorStrategy[idx] = built.strategy;
      indexMeta[idx] = { role, name: text };
      findMatches.push({
        idx,
        role,
        name: text,
        extra: "",
        location: locationOf(el, isHidden, fold),
        matchTier: match.tier,
        visTier: visTierOf(el, isHidden, fold),
        order: findOrder++,
        matchedVia: match.source
      });
    }
    function emitInteractive(el, region, fold) {
      const described = describeElement(el, ctx, policy);
      const { role, redactionCount } = described;
      const name = sanitizeControlChars(described.name);
      redactions += redactionCount;
      const built = buildPierceSelector(el, root, semanticIndex, policy);
      const { idx } = assigner.assign(built.selector, { role, name });
      indexMap[idx] = built.selector;
      selectorStrategy[idx] = built.strategy;
      indexMeta[idx] = { role, name };
      const rect = ctx.rectOf(el);
      boxes.push({ idx, role, name, x: rect.left, y: rect.top, width: rect.width, height: rect.height });
      let extra = "";
      if (role === "textbox" && typeof el.value === "string") {
        const redacted = redactFieldValue(
          el.value,
          {
            type: el.getAttribute("type"),
            name: el.getAttribute("name"),
            id: el.getAttribute("id"),
            autocomplete: el.getAttribute("autocomplete")
          },
          policy
        );
        redactions += redacted.count;
        extra = ` value="${escapeQuotes(clip(redacted.text))}"`;
      } else if (role === "combobox") {
        const current = typeof el.value === "string" ? el.value : "";
        const currentSafe = record(current).text;
        const optionTexts = childElements(el).filter((child) => child.tagName.toUpperCase() === "OPTION").map((opt) => collapseWhitespace(directText(opt) || accessibleName(opt))).filter((text) => text.length > 0);
        let optionsPart = "";
        if (optionTexts.length > 0) {
          const shown = optionTexts.slice(0, MAX_LISTED_OPTIONS).map((text) => `"${escapeQuotes(clip(record(text).text))}"`);
          const overflow = optionTexts.length - shown.length;
          optionsPart = ` options=[${shown.join(", ")}${overflow > 0 ? `, …(+${overflow} more)` : ""}]`;
        }
        extra = ` value="${escapeQuotes(clip(currentSafe))}"${optionsPart}`;
      } else if (role === "link") {
        extra = ` url=${el.resolvedHref ?? ""}`;
      } else if ((role === "checkbox" || role === "radio") && el.hasAttribute("checked")) {
        extra = " checked";
      }
      const expandedInfo = expandedStateOf(el, ctx);
      if (expandedInfo.state === "expanded") extra += " expanded";
      else if (expandedInfo.state === "collapsed") extra += " collapsed";
      if (el.hasAttribute("disabled")) extra += " disabled";
      let foldNote = "";
      if (fold) {
        if (rect.top >= fold.bottom - 2) foldNote = ` (below the fold of scrollable region ${fold.idx})`;
        else if (rect.bottom <= fold.top + 2) foldNote = ` (above the fold of scrollable region ${fold.idx})`;
      }
      pushLine(`[${idx}] ${role} "${escapeQuotes(name)}"${extra}${foldNote}`, true, el, region);
      if (opts.find) {
        const candidates = [{ text: name, source: "name" }];
        const placeholder = collapseWhitespace(el.getAttribute("placeholder"));
        if (placeholder) candidates.push({ text: placeholder, source: "placeholder" });
        if (el.labelTexts) for (const labelText of el.labelTexts) candidates.push({ text: labelText, source: "label" });
        if ((role === "textbox" || role === "combobox") && typeof el.value === "string") {
          const sensitive = isSensitiveField({
            type: el.getAttribute("type"),
            name: el.getAttribute("name"),
            id: el.getAttribute("id"),
            autocomplete: el.getAttribute("autocomplete")
          });
          if (!sensitive) candidates.push({ text: el.value, source: "value" });
        }
        const titleAttr = collapseWhitespace(el.getAttribute("title"));
        if (titleAttr) candidates.push({ text: titleAttr, source: "title" });
        const altAttr = collapseWhitespace(el.getAttribute("alt"));
        if (altAttr) candidates.push({ text: altAttr, source: "alt" });
        const describedBy = el.getAttribute("aria-describedby");
        if (describedBy) {
          const tooltipText = collapseWhitespace(
            describedBy.split(/\s+/).filter(Boolean).map((id) => {
              const target = ctx.resolveId(id, el);
              return target ? composedText(target) : "";
            }).join(" ")
          );
          if (tooltipText) candidates.push({ text: tooltipText, source: "tooltip" });
        }
        const isHiddenEl = isEffectivelyHidden(el, ctx.styleOf(el), ctx.rectOf(el));
        tryRecordExistingMatch(idx, role, name, extra.trim(), el, isHiddenEl, fold, candidates);
      }
    }
    modalRoot = findTopmostOpenDialog(root);
    const dialogResult = modalRoot ? dialogTitleOf(modalRoot) : void 0;
    const dialogTitle = dialogResult?.title;
    dialogTitleHeadingEl = dialogResult?.titleElement ?? null;
    if (modalRoot && dialogTitle !== void 0) {
      pushLine(`dialog "${escapeQuotes(clip(record(dialogTitle).text))}"`, false, modalRoot, "dialog", false, true);
      const ownScroll = scrollContainerInfo(modalRoot, ctx);
      let dialogFold;
      if (ownScroll) {
        const idx = emitScrollContainer(modalRoot, "dialog", ownScroll, dialogTitle);
        const rect = ctx.rectOf(modalRoot);
        const scroll = ctx.scrollOf(modalRoot);
        dialogFold = { idx, top: rect.top, bottom: rect.top + scroll.clientHeight };
      }
      for (const child of childElements(modalRoot)) visit(child, "dialog", dialogTitle, dialogFold);
    }
    const dialogMissing = opts.dialogOnly === true && !modalRoot;
    visit(root, "neutral", void 0, void 0);
    let linesToUse = lines;
    if (opts.dialogOnly) linesToUse = linesToUse.filter((l) => l.region === "dialog");
    if (opts.viewportOnly) linesToUse = linesToUse.filter((l) => l.distance === 0);
    let interactiveOnlyBytesSaved;
    if (opts.interactiveOnly) {
      const before = linesToUse.reduce((sum, l) => sum + byteLength(l.text) + 1, 0);
      linesToUse = linesToUse.filter((l) => l.interactive || l.dialogMarker);
      const after = linesToUse.reduce((sum, l) => sum + byteLength(l.text) + 1, 0);
      interactiveOnlyBytesSaved = before - after;
    }
    const { tree, truncated, droppedCount } = fitToBudget(linesToUse, budgetBytes);
    const hint = truncated ? `truncated: ~${droppedCount} more control${droppedCount === 1 ? "" : "s"}; raise budget_bytes (max ${MAX_SNAPSHOT_BUDGET_BYTES}) or scope with root/dialog_only` : void 0;
    return {
      tree,
      truncated,
      redactions,
      indexMap,
      indexMeta,
      selectorStrategy,
      boxes,
      shadow: shadow2,
      frameBoundaries,
      findMatches,
      dialogMissing,
      ...hint ? { hint } : {},
      ...interactiveOnlyBytesSaved !== void 0 ? { interactiveOnlyBytesSaved } : {}
    };
  }
  function isOpenDialogCandidate(el) {
    const tag = el.tagName.toUpperCase();
    if (tag === "DIALOG") return el.hasAttribute("open");
    const role = (el.getAttribute("role") ?? "").trim().toLowerCase();
    if (role === "dialog" || role === "alertdialog") return true;
    if ((el.getAttribute("aria-modal") ?? "").trim().toLowerCase() === "true") return true;
    const classes = (el.getAttribute("class") ?? "").split(/\s+/).filter(Boolean);
    if (classes.includes("clr-modal")) return true;
    if (classes.includes("cdk-overlay-pane")) return true;
    if (classes.includes("modal-backdrop")) return true;
    if (classes.includes("vui-wizard")) return true;
    if (classes.includes("modal") && classes.includes("show")) return true;
    return false;
  }
  function hasExpanderClickAttr(el) {
    for (const attr of CLICK_HANDLER_ATTRS) if (el.hasAttribute(attr)) return true;
    return false;
  }
  function expandedStateOf(el, ctx) {
    const tag = el.tagName.toUpperCase();
    const expandedAttr = el.getAttribute("aria-expanded");
    if (expandedAttr !== null) {
      return { state: expandedAttr.trim().toLowerCase() === "true" ? "expanded" : "collapsed", via: "aria-expanded" };
    }
    if (tag === "SUMMARY") {
      const parent = el.parentElement;
      const open = !!parent && parent.tagName.toUpperCase() === "DETAILS" && parent.hasAttribute("open");
      return { state: open ? "expanded" : "collapsed", via: "details" };
    }
    if (tag === "DETAILS") {
      return { state: el.hasAttribute("open") ? "expanded" : "collapsed", via: "details" };
    }
    if (hasExpanderClickAttr(el)) {
      const sibling = el.nextElementSibling;
      if (sibling) {
        const hidden = isEffectivelyHidden(sibling, ctx.styleOf(sibling), ctx.rectOf(sibling));
        return { state: hidden ? "collapsed" : "expanded", via: "sibling-visibility" };
      }
    }
    return { state: "not-expandable" };
  }
  function isInteractive(el) {
    const tag = el.tagName.toUpperCase();
    return isInteractiveDescriptor({
      tagName: tag,
      hasHref: tag === "A" && !!el.resolvedHref,
      role: el.getAttribute("role"),
      tabindex: el.getAttribute("tabindex"),
      hasOnclick: el.hasAttribute("onclick"),
      hasAriaExpanded: el.hasAttribute("aria-expanded"),
      hasClickHandlerAttr: hasExpanderClickAttr(el)
    });
  }
  function hasInteractiveDescendant(el, ctx) {
    for (const child of childElements(el)) {
      if (isEffectivelyHidden(child, ctx.styleOf(child), ctx.rectOf(child))) continue;
      if (isInteractive(child) || hasInteractiveDescendant(child, ctx)) return true;
    }
    return false;
  }
  function clip(text) {
    return text.length > MAX_LINE_CHARS ? `${text.slice(0, MAX_LINE_CHARS - 1)}…` : text;
  }
  function escapeQuotes(text) {
    return text.replace(/"/g, '\\"');
  }
  function sanitizeControlChars(text) {
    return text.indexOf("\0") === -1 ? text : text.replace(/\u0000/g, "�");
  }
  const FULL_TRAVERSAL_BUDGET_BYTES = Number.MAX_SAFE_INTEGER;
  const HOST_ID$1 = ANNOTATE_HOST_ID;
  function cancelActiveAnnotation() {
    window.__hermesBridgeAnnotateCancel?.();
  }
  async function runAnnotate(question, candidateIdxs) {
    window.__hermesBridgeAnnotateCancel?.();
    const domRoot = adaptElement(document.body);
    const ctx = makeDomContext();
    const { boxes, indexMap } = buildSnapshotFromDom(domRoot, ctx, FULL_TRAVERSAL_BUDGET_BYTES);
    const resolved = resolveAnnotateCandidates(boxes, indexMap, candidateIdxs).map((c) => ({ candidate: c, el: safeQuerySelector(c.selector) })).filter((r) => r.el !== null);
    let destroyOverlay = () => {
    };
    const session = createAnnotateSession({ destroy: () => destroyOverlay() });
    window.__hermesBridgeAnnotateCancel = session.cancel;
    void session.result.finally(() => {
      if (window.__hermesBridgeAnnotateCancel === session.cancel) {
        window.__hermesBridgeAnnotateCancel = void 0;
      }
    });
    destroyOverlay = mountOverlay(
      question,
      resolved,
      (idx) => session.choose(idx),
      () => session.cancel()
    );
    return session.result;
  }
  function safeQuerySelector(selector) {
    const found = resolvePierceSelector(selector, browserPierceEnv());
    return found.found ? found.element : null;
  }
  const OVERLAY_CSS = `
  :host { all: initial; }
  .hb-banner {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 10px;
    max-width: min(90vw, 520px);
    padding: 8px 12px;
    border-radius: 8px;
    background: #1f2430;
    color: #f4f6fb;
    border: 1px solid #3a4155;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
    font: 500 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    pointer-events: auto;
  }
  @media (prefers-color-scheme: light) {
    .hb-banner { background: #ffffff; color: #1a1d24; border-color: #d7dbe3; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15); }
    .hb-cancel { background: #eef0f4; color: #1a1d24; }
  }
  .hb-question { flex: 1; }
  .hb-cancel {
    flex: none;
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    background: #3a4155;
    color: #f4f6fb;
    font: inherit;
    cursor: pointer;
  }
  .hb-cancel:hover { filter: brightness(1.15); }
  .hb-badge {
    position: fixed;
    min-width: 22px;
    height: 22px;
    padding: 0 5px;
    border-radius: 11px;
    background: #ffcc00;
    color: #1a1d24;
    border: 2px solid #1a1d24;
    font: 700 12px/18px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    text-align: center;
    cursor: pointer;
    transform: translate(-50%, -50%);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.45);
    pointer-events: auto;
  }
  @media (prefers-reduced-motion: no-preference) {
    .hb-badge { transition: left 60ms linear, top 60ms linear; }
  }
  .hb-badge:hover, .hb-badge:focus-visible { filter: brightness(1.1); outline: 2px solid #fff; outline-offset: 1px; }
`;
  function mountOverlay(question, candidates, onPick, onCancel) {
    const host2 = document.createElement("div");
    host2.id = HOST_ID$1;
    host2.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    const shadow2 = host2.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = OVERLAY_CSS;
    shadow2.appendChild(style);
    const banner = document.createElement("div");
    banner.className = "hb-banner";
    const questionEl = document.createElement("span");
    questionEl.className = "hb-question";
    questionEl.textContent = question;
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "hb-cancel";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", onCancel);
    banner.append(questionEl, cancelBtn);
    shadow2.appendChild(banner);
    const badgeEls = /* @__PURE__ */ new Map();
    for (const { candidate } of candidates) {
      const badge = document.createElement("button");
      badge.type = "button";
      badge.className = "hb-badge";
      badge.textContent = String(candidate.idx);
      const label = `${candidate.role} ${candidate.name}`.trim();
      badge.setAttribute("aria-label", label || `element ${candidate.idx}`);
      badge.addEventListener("click", (event) => {
        event.stopPropagation();
        onPick(candidate.idx);
      });
      shadow2.appendChild(badge);
      badgeEls.set(candidate.idx, badge);
    }
    const reposition = () => {
      for (const { candidate, el } of candidates) {
        const badge = badgeEls.get(candidate.idx);
        if (!badge) continue;
        if (!el.isConnected) {
          badge.style.display = "none";
          continue;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 && rect.height <= 0) {
          badge.style.display = "none";
          continue;
        }
        badge.style.display = "";
        badge.style.left = `${rect.left}px`;
        badge.style.top = `${rect.top}px`;
      }
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") onCancel();
    };
    const listenerOpts = { capture: true };
    window.addEventListener("scroll", reposition, { capture: true, passive: true });
    window.addEventListener("resize", reposition, { passive: true });
    window.addEventListener("keydown", onKeydown, listenerOpts);
    window.addEventListener("pagehide", onCancel, { once: true });
    document.documentElement.appendChild(host2);
    reposition();
    return () => {
      window.removeEventListener("scroll", reposition, { capture: true });
      window.removeEventListener("resize", reposition);
      window.removeEventListener("keydown", onKeydown, listenerOpts);
      window.removeEventListener("pagehide", onCancel);
      host2.remove();
    };
  }
  function foreignExtensionIds(frameUrls, ownId) {
    const ids = [];
    for (const url of frameUrls) {
      const match = /^chrome-extension:\/\/([a-p]{32})\//.exec(url);
      if (!match) continue;
      const id = match[1];
      if (id === ownId || ids.includes(id)) continue;
      ids.push(id);
    }
    return ids;
  }
  const FOREIGN_FRAME_SELECTOR = "iframe[src], embed[src], object[data]";
  const ALL_FRAME_ELEMENT_SELECTOR = "iframe, frame, embed, object";
  function srcOf(el) {
    return el.tagName === "OBJECT" ? el.getAttribute("data") : el.getAttribute("src");
  }
  const FOREIGN_FRAME_SRC = /^chrome-extension:\/\/([a-p]{32})\/|^moz-extension:\/\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\//i;
  function isForeignFrameElement(el, ownId) {
    const src = srcOf(el);
    if (!src) return false;
    const match = FOREIGN_FRAME_SRC.exec(src);
    if (!match) return false;
    const id = match[1] ?? match[2];
    return id !== ownId;
  }
  function scanForeignExtensionFrames(env, ownId) {
    const scopes = [env.document, ...shadowRootsWithin(env, env.document)];
    const srcs = [];
    for (const scope of scopes) {
      for (const el of env.queryAll(scope, FOREIGN_FRAME_SELECTOR)) {
        const src = srcOf(el);
        if (src) srcs.push(src);
      }
    }
    const foreignSrcs = srcs.filter((src) => {
      const match = FOREIGN_FRAME_SRC.exec(src);
      if (!match) return false;
      const id = match[1] ?? match[2];
      return id !== ownId;
    });
    return { count: foreignSrcs.length, extensionIds: foreignExtensionIds(foreignSrcs, ownId) };
  }
  function countAllFrameElements(env) {
    const scopes = [env.document, ...shadowRootsWithin(env, env.document)];
    let count = 0;
    for (const scope of scopes) count += env.queryAll(scope, ALL_FRAME_ELEMENT_SELECTOR).length;
    return count;
  }
  function scanForeignExtensionFramesInPage() {
    const ownId = typeof chrome !== "undefined" && chrome.runtime?.id ? chrome.runtime.id : "";
    const env = browserPierceEnv();
    return { ...scanForeignExtensionFrames(env, ownId), ...frameDiagnosticsInPage(env) };
  }
  function frameDiagnosticsInPage(env) {
    return { windowLength: window.length, elementCount: countAllFrameElements(env) };
  }
  function ownExtensionId() {
    return typeof chrome !== "undefined" && chrome.runtime?.id ? chrome.runtime.id : "";
  }
  function findForeignExtensionFrameElements(env, ownId) {
    const scopes = [env.document, ...shadowRootsWithin(env, env.document)];
    const found = [];
    for (const scope of scopes) {
      for (const el of env.queryAll(scope, FOREIGN_FRAME_SELECTOR)) {
        if (isForeignFrameElement(el, ownId)) found.push(el);
      }
    }
    return found;
  }
  function findUnaccountedFrameElements(env, probe, reportedChildUrls) {
    const scopes = [env.document, ...shadowRootsWithin(env, env.document)];
    const remaining = [...reportedChildUrls];
    const suspects = [];
    for (const scope of scopes) {
      for (const el of env.queryAll(scope, "iframe")) {
        if (!probe.isCrossOriginFrame(el)) continue;
        const src = probe.resolvedSrcOf(el);
        const matchIndex = src ? remaining.indexOf(src) : -1;
        if (matchIndex !== -1) {
          remaining.splice(matchIndex, 1);
          continue;
        }
        suspects.push(el);
      }
    }
    return suspects;
  }
  function findFramesToStrip(env, ownId, probe, reportedChildUrls) {
    const named = findForeignExtensionFrameElements(env, ownId);
    if (!probe) return named;
    const suspects = findUnaccountedFrameElements(env, probe, reportedChildUrls);
    const combined = [...named];
    for (const suspect of suspects) if (!combined.includes(suspect)) combined.push(suspect);
    return combined;
  }
  function findBlankIframeCandidates(env) {
    const scopes = [env.document, ...shadowRootsWithin(env, env.document)];
    const out = [];
    for (const scope of scopes) {
      for (const el of env.queryAll(scope, "iframe")) {
        const src = (el.getAttribute("src") ?? "").trim().toLowerCase();
        if (src === "" || src === "about:blank") out.push(el);
      }
    }
    return out;
  }
  function browserUnaccountedFrameProbe() {
    return {
      isCrossOriginFrame: (el) => {
        const iframe = el;
        try {
          const win = iframe.contentWindow;
          if (!win) return false;
          void win.location.href;
          return false;
        } catch {
          return true;
        }
      },
      resolvedSrcOf: (el) => el.src || null
    };
  }
  function stripForeignExtensionFrames(env, ownId, opts) {
    const elements = findFramesToStrip(env, ownId, opts?.probe, opts?.reportedChildUrls ?? []);
    const removed = [];
    for (const element of elements) {
      const container = env.containerOf(element);
      if (!container) continue;
      const nextSibling = env.nextSiblingOf(element);
      env.remove(element);
      removed.push({ element, container, nextSibling });
    }
    const srcs = removed.map(({ element }) => srcOf(element)).filter((src) => src !== null);
    return { removed, scan: { count: removed.length, extensionIds: foreignExtensionIds(srcs, ownId) } };
  }
  function restoreForeignExtensionFrames(env, removed) {
    for (let i = removed.length - 1; i >= 0; i--) {
      const { element, container, nextSibling } = removed[i];
      env.insertBefore(container, element, nextSibling);
    }
  }
  function browserStripEnv() {
    const base = browserPierceEnv();
    return {
      ...base,
      containerOf: (el) => el.parentNode ?? null,
      nextSiblingOf: (el) => el.nextElementSibling,
      remove: (el) => el.remove(),
      insertBefore: (container, el, before) => {
        const parentNode = container;
        if (before) parentNode.insertBefore(el, before);
        else parentNode.appendChild(el);
      }
    };
  }
  let activeStripped = [];
  function stripForeignExtensionFramesInPage(reportedChildUrls = []) {
    const env = browserStripEnv();
    const result = stripForeignExtensionFrames(env, ownExtensionId(), {
      probe: browserUnaccountedFrameProbe(),
      reportedChildUrls
    });
    activeStripped = result.removed;
    return { ...result.scan, ...frameDiagnosticsInPage(env) };
  }
  function restoreForeignExtensionFramesInPage() {
    const removed = activeStripped;
    activeStripped = [];
    restoreForeignExtensionFrames(browserStripEnv(), removed);
    return { restored: removed.length };
  }
  function guardSweep(env, ownId, observedRoots2, heldFrames2, probe, reportedChildUrls = []) {
    const { removed } = stripForeignExtensionFrames(env, ownId, { probe, reportedChildUrls });
    heldFrames2.push(...removed);
    const roots = shadowRootsWithin(env, env.document);
    return roots.filter((root) => !observedRoots2.has(root));
  }
  let armed = false;
  let observers = [];
  let observedRoots = /* @__PURE__ */ new Set();
  let heldFrames = [];
  let watchedBlankIframes = /* @__PURE__ */ new Set();
  let currentReportedChildUrls = [];
  function watchBlankIframes(env) {
    for (const el of findBlankIframeCandidates(env)) {
      if (watchedBlankIframes.has(el)) continue;
      watchedBlankIframes.add(el);
      el.addEventListener("load", () => sweepAndObserveNewRoots(), { once: true });
    }
  }
  function sweepAndObserveNewRoots() {
    const env = browserStripEnv();
    const newRoots = guardSweep(
      env,
      ownExtensionId(),
      observedRoots,
      heldFrames,
      browserUnaccountedFrameProbe(),
      currentReportedChildUrls
    );
    for (const root of newRoots) {
      observedRoots.add(root);
      const observer = new MutationObserver(sweepAndObserveNewRoots);
      observer.observe(root, { childList: true, subtree: true });
      observers.push(observer);
    }
    watchBlankIframes(env);
  }
  function armForeignFrameGuardInPage(reportedChildUrls = []) {
    const wasArmed = armed;
    armed = true;
    currentReportedChildUrls = [...reportedChildUrls];
    const beforeCount = heldFrames.length;
    if (!wasArmed) {
      const rootObserver = new MutationObserver(sweepAndObserveNewRoots);
      rootObserver.observe(document, { childList: true, subtree: true });
      observers.push(rootObserver);
    }
    sweepAndObserveNewRoots();
    const newlyHeld = heldFrames.slice(beforeCount);
    const srcs = newlyHeld.map(({ element }) => srcOf(element)).filter((src) => src !== null);
    return {
      count: newlyHeld.length,
      extensionIds: foreignExtensionIds(srcs, ownExtensionId()),
      ...frameDiagnosticsInPage(browserStripEnv())
    };
  }
  function disarmForeignFrameGuardInPage() {
    armed = false;
    for (const observer of observers) observer.disconnect();
    observers = [];
    observedRoots = /* @__PURE__ */ new Set();
    watchedBlankIframes = /* @__PURE__ */ new Set();
    currentReportedChildUrls = [];
    const removed = heldFrames;
    heldFrames = [];
    restoreForeignExtensionFrames(browserStripEnv(), removed);
    return { restored: removed.length };
  }
  function foreignFrameGuardStatusInPage() {
    const srcs = heldFrames.map(({ element }) => srcOf(element)).filter((src) => src !== null);
    return { armed, heldCount: heldFrames.length, extensionIds: foreignExtensionIds(srcs, ownExtensionId()) };
  }
  const MIN_GLIDE_MS = 450;
  const MAX_GLIDE_MS = 900;
  const GLIDE_MS_PER_PX = 0.9;
  const FAST_GLIDE_MS = 150;
  const CURSOR_FADE_IN_MS = 200;
  const CURSOR_IDLE_DIM_MS = 8e3;
  const GLOW_LINGER_MS = 4e3;
  const GLOW_FADE_MS = 1500;
  const OP_SELF_HEAL_MS = 2e4;
  const RIPPLE_MS = 600;
  const ARRIVE_EARLY_TOLERANCE_MS = 40;
  const CAPTURE_HIDE_SELF_HEAL_MS = 5e3;
  const MARKS_SELF_HEAL_MS = 5e3;
  function glideDurationMs(from, to) {
    const distance = Math.hypot(to.x - from.x, to.y - from.y);
    if (distance < 1) return 0;
    return Math.round(Math.min(MAX_GLIDE_MS, Math.max(MIN_GLIDE_MS, distance * GLIDE_MS_PER_PX)));
  }
  function effectiveGlideDurationMs(mode, from, to) {
    if (mode === "off") return 0;
    const distance = Math.hypot(to.x - from.x, to.y - from.y);
    if (distance < 1) return 0;
    if (mode === "fast") return FAST_GLIDE_MS;
    return glideDurationMs(from, to);
  }
  function createGlowTracker(view) {
    const ops = /* @__PURE__ */ new Map();
    let linger;
    let fade;
    let lit = false;
    const end = (opId) => {
      const heal = ops.get(opId);
      if (heal === void 0) return;
      clearTimeout(heal);
      ops.delete(opId);
      if (ops.size > 0) return;
      clearTimeout(linger);
      linger = setTimeout(() => {
        lit = false;
        view.setLit(false);
        fade = setTimeout(() => view.fadeFinished(), GLOW_FADE_MS);
      }, GLOW_LINGER_MS);
    };
    return {
      begin(opId, label, ttlMs = OP_SELF_HEAL_MS) {
        clearTimeout(linger);
        clearTimeout(fade);
        clearTimeout(ops.get(opId));
        ops.set(opId, setTimeout(() => end(opId), Math.max(0, ttlMs)));
        view.setLabel(label);
        if (!lit) {
          lit = true;
          view.setLit(true);
        }
      },
      end,
      inFlight() {
        return ops.size;
      },
      isLit() {
        return lit;
      },
      reset() {
        for (const heal of ops.values()) clearTimeout(heal);
        ops.clear();
        clearTimeout(linger);
        clearTimeout(fade);
        lit = false;
      }
    };
  }
  function createCursorController(view, reducedMotion) {
    let last = null;
    let idleTimer;
    return {
      move(p, label, click, mode = "normal") {
        view.setLabel(label);
        view.setIdle(false);
        clearTimeout(idleTimer);
        let durationMs;
        if (!last) {
          view.appear(p);
          durationMs = reducedMotion() ? 0 : CURSOR_FADE_IN_MS;
        } else {
          durationMs = reducedMotion() ? 0 : effectiveGlideDurationMs(mode, last, p);
          view.glide(p, durationMs);
        }
        last = p;
        const startedAt = Date.now();
        return new Promise((resolve2) => {
          let settled = false;
          let dispose;
          let timer;
          const arrive = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            dispose?.();
            if (click) view.ripple(p);
            idleTimer = setTimeout(() => view.setIdle(true), CURSOR_IDLE_DIM_MS);
            resolve2({ durationMs });
          };
          timer = setTimeout(arrive, durationMs);
          const arriveFromView = () => {
            if (Date.now() - startedAt < durationMs - ARRIVE_EARLY_TOLERANCE_MS) return;
            arrive();
          };
          if (durationMs > 0 && view.onArrive) dispose = view.onArrive(arriveFromView);
        });
      },
      reset() {
        clearTimeout(idleTimer);
        last = null;
      }
    };
  }
  function createCaptureHider(view) {
    let hidden = false;
    let heal;
    const restore = () => {
      clearTimeout(heal);
      if (!hidden) return;
      hidden = false;
      view.setHidden(false);
    };
    return {
      async hide() {
        hidden = true;
        view.setHidden(true);
        clearTimeout(heal);
        heal = setTimeout(restore, CAPTURE_HIDE_SELF_HEAL_MS);
        await view.afterPaint();
        return { hidden: true };
      },
      restore,
      isHidden() {
        return hidden;
      }
    };
  }
  function createMarksController(view) {
    let shown = false;
    let heal;
    const clear = () => {
      clearTimeout(heal);
      if (!shown) return;
      shown = false;
      view.clear();
    };
    return {
      show(marks2) {
        shown = true;
        view.render(marks2);
        clearTimeout(heal);
        heal = setTimeout(clear, MARKS_SELF_HEAL_MS);
      },
      hide: clear,
      isShown() {
        return shown;
      }
    };
  }
  function createStopHandler(onStop) {
    let pressed = false;
    return {
      handle(event) {
        if (!event.isTrusted) return false;
        event.preventDefault?.();
        event.stopPropagation?.();
        if (pressed) return false;
        pressed = true;
        onStop();
        return true;
      },
      reset() {
        pressed = false;
      }
    };
  }
  const HOST_ID = PRESENCE_HOST_ID;
  const PRESENCE_TYPES = /* @__PURE__ */ new Set([
    "presence.begin",
    "presence.end",
    "presence.cursor",
    "presence.hideForCapture",
    "presence.restoreAfterCapture",
    "presence.teardown",
    "presence.attached",
    "presence.marks.show",
    "presence.marks.hide"
  ]);
  function isPresenceRequest(message) {
    if (typeof message !== "object" || message === null) return false;
    const candidate = message;
    return candidate.target === "content" && typeof candidate.type === "string" && PRESENCE_TYPES.has(candidate.type);
  }
  function handlePresenceRequest(message) {
    switch (message.type) {
      case "presence.begin":
        ensureMounted();
        glow.begin(String(message.opId), message.label, message.ttlMs);
        return Promise.resolve({});
      case "presence.end":
        glow.end(String(message.opId));
        return Promise.resolve({});
      case "presence.cursor":
        ensureMounted();
        return cursor.move({ x: message.x, y: message.y }, message.label, message.click ?? false, message.mode ?? "normal");
      case "presence.hideForCapture":
        return capture.hide();
      case "presence.restoreAfterCapture":
        capture.restore();
        return Promise.resolve({});
      case "presence.teardown":
        teardown();
        return Promise.resolve({});
      case "presence.attached":
        ensureMounted();
        tabAttached = true;
        stopShortcut = typeof message.stopShortcut === "string" ? message.stopShortcut : "";
        renderStopTitle();
        renderPill();
        return Promise.resolve({});
      case "presence.marks.show":
        ensureMounted();
        marks.show(Array.isArray(message.marks) ? message.marks : []);
        return Promise.resolve({});
      case "presence.marks.hide":
        marks.hide();
        return Promise.resolve({});
      default:
        return Promise.resolve({});
    }
  }
  let host = null;
  let shadow = null;
  let glowEl = null;
  let labelEl = null;
  let cursorAnchorEl = null;
  let labelTextEl = null;
  let marksEl = null;
  let glowLit = false;
  let cursorShown = false;
  let currentVerb = "";
  const ACCENT = "#8b5cf6";
  const STOP_BUTTON_ATTRS = { type: "button", tabindex: "-1" };
  let stopButtonEl = null;
  let stopShortcut = "";
  let tabAttached = false;
  function renderStopTitle() {
    if (!stopButtonEl) return;
    stopButtonEl.title = stopShortcut ? `Stop Hermes: release every shared tab and pause sharing (${stopShortcut})` : "Stop Hermes: release every shared tab and pause sharing";
  }
  const PRESENCE_CSS = `
  :host { all: initial; }
  .hb-glow {
    position: fixed;
    inset: 0;
    opacity: 0;
    transition: opacity 1500ms ease-in-out;
  }
  .hb-glow--lit { opacity: 1; transition: opacity 400ms ease-out; }
  .hb-glow-pulse {
    position: absolute;
    inset: 0;
    /* No border line: two inset shadows that fade toward the centre. */
    box-shadow: inset 0 0 36px 4px rgba(139, 92, 246, 0.5), inset 0 0 120px 16px rgba(139, 92, 246, 0.16);
    opacity: 0.8;
  }
  .hb-glow--lit .hb-glow-pulse, .hb-glow--fading .hb-glow-pulse {
    animation: hb-breathe 3s ease-in-out infinite;
  }
  @keyframes hb-breathe {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }
  .hb-label {
    position: fixed;
    right: 14px;
    bottom: 14px;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 11px 5px 9px;
    border-radius: 999px;
    background: #1e1633;
    color: #f5f3ff;
    border: 1px solid rgba(139, 92, 246, 0.65);
    font: 600 12px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    box-shadow: 0 2px 12px rgba(139, 92, 246, 0.35);
    opacity: 0;
    transition: opacity 400ms ease;
  }
  .hb-label::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: ${ACCENT};
  }
  .hb-label--lit { opacity: 1; }
  .hb-stop {
    margin-left: 6px;
    padding: 2px 9px;
    border: 1px solid rgba(255, 255, 255, 0.35);
    border-radius: 999px;
    background: ${ACCENT};
    color: #ffffff;
    font: 700 11px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    cursor: pointer;
    /* The host and everything else are click-through; only a visible Stop
     * button takes clicks. Unlit it is not rendered at all, so it can
     * neither eat the corner nor be activated unseen. display, not
     * visibility: a descendant set back to visible would override the
     * host being hidden during a capture and put the button in
     * the model's screenshot. */
    pointer-events: none;
    display: none;
  }
  .hb-label--lit .hb-stop { pointer-events: auto; display: inline-block; }
  .hb-stop:hover { filter: brightness(1.12); }
  .hb-stop:focus-visible { outline: 2px solid #ffffff; outline-offset: 1px; }
  .hb-label--stopped .hb-stop { display: none; }
  @media (prefers-color-scheme: light) {
    .hb-label { background: #ffffff; color: #2e1065; box-shadow: 0 2px 12px rgba(139, 92, 246, 0.25); }
  }
  .hb-cursor {
    position: fixed;
    top: 0;
    left: 0;
    width: 0;
    height: 0;
    opacity: 0;
    transform: translate(-9999px, -9999px);
  }
  .hb-cursor--visible { opacity: 1; }
  .hb-cursor--visible.hb-cursor--idle { opacity: 0.45; }
  .hb-halo {
    position: absolute;
    left: -18px;
    top: -14px;
    width: 52px;
    height: 56px;
    border-radius: 50%;
    background: radial-gradient(closest-side, rgba(139, 92, 246, 0.45), rgba(139, 92, 246, 0));
    transition: opacity 600ms ease;
  }
  .hb-cursor--idle .hb-halo { opacity: 0.4; }
  .hb-pointer {
    position: absolute;
    /* The path's tip is at (1, 1): offset so it sits exactly on the point. */
    left: -1px;
    top: -1px;
    width: 20px;
    height: 28px;
    overflow: visible;
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.35)) drop-shadow(0 0 5px rgba(139, 92, 246, 0.7));
  }
  .hb-ripple {
    position: absolute;
    left: -20px;
    top: -20px;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border: 2px solid rgba(139, 92, 246, 0.9);
    opacity: 0;
    animation: hb-ripple ${RIPPLE_MS}ms cubic-bezier(0.2, 0, 0.2, 1) forwards;
  }
  .hb-ripple--echo { border-color: rgba(139, 92, 246, 0.55); animation-delay: 140ms; }
  @keyframes hb-ripple {
    from { transform: scale(0.3); opacity: 0.5; }
    to { transform: scale(1.5); opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .hb-cursor { transition: opacity 150ms ease !important; }
    .hb-glow-pulse, .hb-glow--lit .hb-glow-pulse, .hb-glow--fading .hb-glow-pulse { animation: none; opacity: 0.8; }
    .hb-ripple { animation: none; opacity: 0.35; transform: scale(1); }
    .hb-ripple--echo { display: none; }
  }
  .hb-marks {
    position: fixed;
    inset: 0;
    /* D1: badges must render even while the pointer/glow's shared host is
     * hidden for a plain (non-marks) capture -- this asserts its own
     * visibility, overriding the ancestor host.style.visibility = 'hidden'
     * that presenceHideForCapture sets (a descendant CAN reassert visible
     * over a hidden ancestor per the CSS visibility spec; this is that
     * override, not a bug). Empty (no badge children) when marks aren't
     * shown, so this costs nothing when a capture doesn't ask for marks. */
    visibility: visible;
    display: none;
  }
  .hb-marks--shown { display: block; }
  .hb-mark {
    position: absolute;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 5px;
    background: ${ACCENT};
    color: #ffffff;
    font: 700 11px/18px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.45);
    /* Anchored at the box's top-left corner (background.ts already clamps
     * marks to the viewport) so the badge sits just outside the element
     * rather than obscuring its centre. */
    transform: translate(-2px, -2px);
  }
`;
  const EASE = "cubic-bezier(0.25, 0.1, 0.25, 1)";
  const SVG_NS = "http://www.w3.org/2000/svg";
  function prefersReducedMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  }
  function buildPointer() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "hb-pointer");
    svg.setAttribute("viewBox", "0 0 20 28");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "28");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M1 1 L1 23 L6.6 17.6 L10.4 26.4 L14 24.8 L10.3 16.2 L17.8 16.2 Z");
    path.setAttribute("fill", ACCENT);
    path.setAttribute("stroke", "#ffffff");
    path.setAttribute("stroke-width", "1.6");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }
  function ensureMounted() {
    if (host && host.isConnected) return;
    host = document.createElement("div");
    host.id = HOST_ID;
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    if (capture.isHidden()) host.style.visibility = "hidden";
    shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = PRESENCE_CSS;
    shadow.appendChild(style);
    glowEl = document.createElement("div");
    glowEl.className = "hb-glow";
    const pulse = document.createElement("div");
    pulse.className = "hb-glow-pulse";
    glowEl.appendChild(pulse);
    labelEl = document.createElement("div");
    labelEl.className = "hb-label";
    labelTextEl = document.createElement("span");
    const stopButton = document.createElement("button");
    for (const [name, value] of Object.entries(STOP_BUTTON_ATTRS)) stopButton.setAttribute(name, value);
    stopButton.className = "hb-stop";
    stopButton.textContent = "Stop";
    stopButtonEl = stopButton;
    renderStopTitle();
    stopButton.addEventListener("click", (event) => stop$1.handle(event));
    labelEl.append(labelTextEl, stopButton);
    stop$1.reset();
    cursorAnchorEl = document.createElement("div");
    cursorAnchorEl.className = "hb-cursor";
    const halo = document.createElement("div");
    halo.className = "hb-halo";
    cursorAnchorEl.append(halo, buildPointer());
    marksEl = document.createElement("div");
    marksEl.className = "hb-marks";
    shadow.append(glowEl, labelEl, cursorAnchorEl, marksEl);
    cursor.reset();
    document.documentElement.appendChild(host);
  }
  function renderPill() {
    if (!labelEl || !labelTextEl || labelEl.classList.contains("hb-label--stopped")) return;
    labelEl.classList.toggle("hb-label--lit", glowLit || cursorShown || tabAttached);
    labelTextEl.textContent = glowLit && currentVerb ? `Hermes is ${currentVerb}` : "Hermes has this tab";
  }
  const glow = createGlowTracker({
    setLit(lit) {
      glowLit = lit;
      glowEl?.classList.toggle("hb-glow--lit", lit);
      glowEl?.classList.toggle("hb-glow--fading", !lit);
      renderPill();
    },
    fadeFinished() {
      glowEl?.classList.remove("hb-glow--fading");
    },
    setLabel(label) {
      currentVerb = label;
      renderPill();
    }
  });
  const stop$1 = createStopHandler(() => {
    if (labelEl && labelTextEl) {
      labelEl.classList.add("hb-label--stopped", "hb-label--lit");
      labelTextEl.textContent = "Stopped";
    }
    glowEl?.classList.remove("hb-glow--lit");
    cursorAnchorEl?.classList.remove("hb-cursor--visible");
    try {
      void chrome.runtime.sendMessage({ target: "background", type: "presence.stop" }).catch(() => {
      });
    } catch {
    }
  });
  function place(p) {
    if (cursorAnchorEl) cursorAnchorEl.style.transform = `translate(${p.x}px, ${p.y}px)`;
  }
  const cursor = createCursorController(
    {
      appear(p) {
        if (!cursorAnchorEl) return;
        cursorAnchorEl.style.transition = "opacity 200ms ease";
        place(p);
        cursorAnchorEl.classList.add("hb-cursor--visible");
        cursorShown = true;
        renderPill();
      },
      glide(p, durationMs) {
        if (!cursorAnchorEl) return;
        cursorAnchorEl.style.transition = `opacity 300ms ease, transform ${durationMs}ms ${EASE}`;
        place(p);
        cursorAnchorEl.classList.add("hb-cursor--visible");
      },
      onArrive(done) {
        const el = cursorAnchorEl;
        if (!el) return () => {
        };
        const listener = (event) => {
          if (event.target === el && event.propertyName === "transform") done();
        };
        el.addEventListener("transitionend", listener);
        return () => el.removeEventListener("transitionend", listener);
      },
      ripple(p) {
        spawnRipple(p);
      },
      setIdle(idle) {
        cursorAnchorEl?.classList.toggle("hb-cursor--idle", idle);
      },
      setLabel(label) {
        currentVerb = label;
        renderPill();
      }
    },
    prefersReducedMotion
  );
  const capture = createCaptureHider({
    setHidden(hidden) {
      if (host) host.style.visibility = hidden ? "hidden" : "";
    },
    afterPaint() {
      return new Promise((resolve2) => {
        const fallback = setTimeout(resolve2, 100);
        requestAnimationFrame(
          () => requestAnimationFrame(() => {
            clearTimeout(fallback);
            resolve2();
          })
        );
      });
    }
  });
  const marks = createMarksController({
    render(list) {
      if (!marksEl) return;
      marksEl.replaceChildren();
      for (const m of list) {
        const badge = document.createElement("div");
        badge.className = "hb-mark";
        badge.style.left = `${m.x}px`;
        badge.style.top = `${m.y}px`;
        badge.textContent = String(m.idx);
        marksEl.appendChild(badge);
      }
      marksEl.classList.toggle("hb-marks--shown", list.length > 0);
    },
    clear() {
      marksEl?.replaceChildren();
      marksEl?.classList.remove("hb-marks--shown");
    }
  });
  function spawnRipple(p) {
    if (!shadow) return;
    const anchor = document.createElement("div");
    anchor.style.cssText = "position:fixed;top:0;left:0;";
    anchor.style.transform = `translate(${p.x}px, ${p.y}px)`;
    const ring = document.createElement("div");
    ring.className = "hb-ripple";
    const echo = document.createElement("div");
    echo.className = "hb-ripple hb-ripple--echo";
    anchor.append(ring, echo);
    shadow.appendChild(anchor);
    setTimeout(() => anchor.remove(), RIPPLE_MS + 300);
  }
  function teardown() {
    glow.reset();
    cursor.reset();
    capture.restore();
    marks.hide();
    host?.remove();
    host = null;
    shadow = null;
    glowEl = null;
    labelEl = null;
    cursorAnchorEl = null;
    labelTextEl = null;
    marksEl = null;
    stopButtonEl = null;
    tabAttached = false;
    glowLit = false;
    cursorShown = false;
    currentVerb = "";
  }
  function selectorOf(el, root, semanticIndex, policy) {
    return buildPierceSelector(el, root, semanticIndex, policy);
  }
  function nameForScrollableRegion(el, ctx) {
    const ariaLabel = collapseWhitespace(el.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = collapseWhitespace(
        labelledBy.split(/\s+/).filter(Boolean).map((id) => {
          const target = ctx.resolveId(id, el);
          return target ? composedText(target) : "";
        }).join(" ")
      );
      if (text) return text;
    }
    let sib = el.previousElementSibling;
    while (sib) {
      if (headingLevel(sib.tagName.toUpperCase())) {
        const text = directText(sib) || collapseWhitespace(composedText(sib));
        if (text) return text;
      }
      sib = sib.previousElementSibling;
    }
    return "region";
  }
  function walkComposed(el, ctx, originPolicy, visit) {
    if (isEffectivelyHidden(el, ctx.styleOf(el), ctx.rectOf(el))) return;
    if (el.frame) {
      const accessible = !el.frame.crossOrigin && !!el.frame.body;
      const blocked = accessible && el.frame.origin !== void 0 && !isOriginAllowed(el.frame.origin, originPolicy);
      if (accessible && !blocked && el.frame.body) {
        for (const child of childElements(el.frame.body)) walkComposed(child, ctx, originPolicy, visit);
      }
      return;
    }
    visit(el);
    for (const child of childElements(el)) walkComposed(child, ctx, originPolicy, visit);
  }
  const MAX_SCROLLABLES = 200;
  function findScrollables(root, ctx, semanticIndex, assigner, policy, originPolicy) {
    const scrollables = [];
    let truncated = false;
    walkComposed(root, ctx, originPolicy, (el) => {
      const info = scrollContainerInfo(el, ctx);
      if (!info) return;
      if (scrollables.length >= MAX_SCROLLABLES) {
        truncated = true;
        return;
      }
      const name = redactText(nameForScrollableRegion(el, ctx), policy).text;
      const built = selectorOf(el, root, semanticIndex, policy);
      const { idx } = assigner.assign(built.selector, { role: "scrollable region", name });
      const rect = ctx.rectOf(el);
      const entry = {
        idx,
        name,
        role: "scrollable region",
        rect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height }
      };
      if (info.vertical) {
        const percent = info.maxTop > 0 ? Math.round(info.scrollTop / info.maxTop * 100) : 100;
        entry.vertical = { percent, moreAbove: info.scrollTop > 2, moreBelow: info.scrollTop < info.maxTop - 2 };
      }
      if (info.horizontal) {
        const percent = info.maxLeft > 0 ? Math.round(info.scrollLeft / info.maxLeft * 100) : 100;
        entry.horizontal = { percent, moreLeft: info.scrollLeft > 2, moreRight: info.scrollLeft < info.maxLeft - 2 };
      }
      scrollables.push(entry);
    });
    return { scrollables, truncated };
  }
  function rectContainsPoint(rect, x, y) {
    return x >= rect.left && x <= rect.left + rect.width && y >= rect.top && y <= rect.top + rect.height;
  }
  function extensionOriginOf(src) {
    const match = /^([a-z][a-z0-9+.-]*:\/\/[^/]+)/i.exec(src);
    return match ? match[1] : src;
  }
  function isForeignExtensionSrc(src, ownExtensionId2) {
    const match = FOREIGN_FRAME_SRC.exec(src);
    if (!match) return false;
    const id = match[1] ?? match[2];
    return id !== ownExtensionId2;
  }
  function hitTest(root, ctx, x, y, originPolicy, ownExtensionId2) {
    let best = null;
    let bestArea = Number.POSITIVE_INFINITY;
    const visit = (el) => {
      if (isEffectivelyHidden(el, ctx.styleOf(el), ctx.rectOf(el))) return;
      const rect = ctx.rectOf(el);
      if (rectContainsPoint(rect, x, y)) {
        const area = Math.max(0, rect.width) * Math.max(0, rect.height);
        if (area <= bestArea) {
          best = el;
          bestArea = area;
        }
      }
      if (el.frame) return;
      for (const child of childElements(el)) visit(child);
    };
    visit(root);
    if (!best) return { kind: "none" };
    const found = best;
    const src = srcOf(found);
    if (src && isForeignExtensionSrc(src, ownExtensionId2)) {
      return { kind: "foreign_extension_overlay", origin: extensionOriginOf(src) };
    }
    if (found.frame) {
      const accessible = !found.frame.crossOrigin && !!found.frame.body;
      const blocked = accessible && found.frame.origin !== void 0 && !isOriginAllowed(found.frame.origin, originPolicy);
      if (!accessible || blocked) {
        return { kind: "ungranted_frame", origin: accessible ? found.frame.origin : void 0 };
      }
      const rect = ctx.rectOf(found);
      return hitTest(found.frame.body, ctx, x - rect.left, y - rect.top, originPolicy, ownExtensionId2);
    }
    return { kind: "element", el: found };
  }
  function nearestScrollableAncestor(el, ctx) {
    let cur = el.parentElement ?? el.rootHost ?? null;
    while (cur) {
      if (!isEffectivelyHidden(cur, ctx.styleOf(cur), ctx.rectOf(cur))) {
        const info = scrollContainerInfo(cur, ctx);
        if (info) return { el: cur, info };
      }
      cur = cur.parentElement ?? cur.rootHost ?? null;
    }
    return null;
  }
  function isSelfOrDescendant(candidate, el) {
    let cur = candidate;
    while (cur) {
      if (cur === el) return true;
      cur = cur.parentElement ?? cur.rootHost ?? null;
    }
    return false;
  }
  function visibilityOf(el, root, ctx, semanticIndex, assigner, policy, originPolicy, ownExtensionId2) {
    const style = ctx.styleOf(el);
    const rect = ctx.rectOf(el);
    if (hasHiddenMarkup(el) || style.display === "none" || style.visibility === "hidden") {
      return { state: "display:none" };
    }
    const zeroRect = rect.width <= 0 && rect.height <= 0;
    const mayRenderWithoutOwnBox2 = style.display === "contents" || el.tagName.toUpperCase() === "SLOT" || !!el.shadowRoot;
    if (zeroRect && !mayRenderWithoutOwnBox2) {
      return { state: "zero-size" };
    }
    if (rect.bottom <= 0 || rect.top >= ctx.viewportHeight || rect.left + rect.width <= 0 || rect.left >= ctx.viewportWidth) {
      return { state: "off-screen" };
    }
    const ancestor = nearestScrollableAncestor(el, ctx);
    if (ancestor) {
      const ancestorRect = ctx.rectOf(ancestor.el);
      const ancestorScroll = ctx.scrollOf(ancestor.el);
      const fold = { top: ancestorRect.top, bottom: ancestorRect.top + ancestorScroll.clientHeight };
      if (rect.top >= fold.bottom - 2 || rect.bottom <= fold.top + 2) {
        const name = redactText(nameForScrollableRegion(ancestor.el, ctx), policy).text;
        const built = selectorOf(ancestor.el, root, semanticIndex, policy);
        const { idx } = assigner.assign(built.selector, { role: "scrollable region", name });
        return { state: "clipped", by: { idx, name } };
      }
    }
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    const hit = hitTest(root, ctx, centerX, centerY, originPolicy, ownExtensionId2);
    if (hit.kind === "element" && !isSelfOrDescendant(hit.el, el)) {
      const described = describeElement(hit.el, ctx, policy);
      return { state: "covered", by: { role: described.role, name: described.name } };
    }
    if (hit.kind === "foreign_extension_overlay") {
      return { state: "covered", by: { role: "iframe", name: `[another extension: ${hit.origin}]` } };
    }
    if (hit.kind === "ungranted_frame") {
      return { state: "covered", by: { role: "iframe", name: `[ungranted frame${hit.origin ? `: ${hit.origin}` : ""}]` } };
    }
    return { state: "visible" };
  }
  function expandedOf(el, ctx) {
    return expandedStateOf(el, ctx);
  }
  const MAX_OPTIONS = 100;
  function optionsOf(el, policy) {
    const tag = el.tagName.toUpperCase();
    const out = [];
    let truncated = false;
    const push = (label, selected, disabled) => {
      if (out.length >= MAX_OPTIONS) {
        truncated = true;
        return;
      }
      out.push({ label: redactText(label, policy).text, selected, disabled });
    };
    if (tag === "SELECT") {
      const currentValue = typeof el.value === "string" ? el.value : "";
      const collect = (container) => {
        for (const child of childElements(container)) {
          const childTag = child.tagName.toUpperCase();
          if (childTag === "OPTGROUP") {
            collect(child);
            continue;
          }
          if (childTag !== "OPTION") continue;
          const label = collapseWhitespace(directText(child)) || collapseWhitespace(composedText(child));
          const value = child.getAttribute("value") ?? label;
          push(label, value === currentValue, child.hasAttribute("disabled"));
        }
      };
      collect(el);
      return { options: out, truncated };
    }
    const role = (el.getAttribute("role") ?? "").trim().toLowerCase();
    const OPTION_ROLES = /* @__PURE__ */ new Set(["option", "menuitem", "menuitemradio", "menuitemcheckbox"]);
    if (role === "listbox" || role === "combobox" || role === "menu" || role === "tablist") {
      const collectAria = (container) => {
        for (const child of childElements(container)) {
          const childRole = (child.getAttribute("role") ?? "").trim().toLowerCase();
          if (OPTION_ROLES.has(childRole) || childRole === "tab") {
            const label = collapseWhitespace(directText(child)) || collapseWhitespace(composedText(child));
            const selected = (child.getAttribute("aria-selected") ?? "").toLowerCase() === "true" || (child.getAttribute("aria-checked") ?? "").toLowerCase() === "true";
            const disabled = (child.getAttribute("aria-disabled") ?? "").toLowerCase() === "true" || child.hasAttribute("disabled");
            push(label, selected, disabled);
          }
          collectAria(child);
        }
      };
      collectAria(el);
      return { options: out, truncated };
    }
    return { invalid: `element is neither a <select> nor role=listbox/combobox/menu/tablist (found role=${role || "(none)"}, tag=${tag})` };
  }
  const MAX_FORM_FIELDS = 60;
  const FORM_FIELD_TAGS = /* @__PURE__ */ new Set(["INPUT", "SELECT", "TEXTAREA"]);
  const NON_FIELD_INPUT_TYPES = /* @__PURE__ */ new Set(["hidden", "submit", "button", "reset", "image"]);
  const FORM_FIELD_ROLES = /* @__PURE__ */ new Set(["checkbox", "radio", "switch", "combobox", "textbox", "slider", "spinbutton", "searchbox"]);
  function isFormFieldElement(el) {
    const tag = el.tagName.toUpperCase();
    if (tag === "INPUT") {
      const type = (el.getAttribute("type") ?? "text").toLowerCase();
      return !NON_FIELD_INPUT_TYPES.has(type);
    }
    if (FORM_FIELD_TAGS.has(tag)) return true;
    const role = (el.getAttribute("role") ?? "").trim().toLowerCase();
    return FORM_FIELD_ROLES.has(role);
  }
  function ariaFlag(el, name) {
    return (el.getAttribute(name) ?? "").trim().toLowerCase() === "true";
  }
  function formFieldValue(el, policy) {
    const value = typeof el.value === "string" ? el.value : "";
    const identity = {
      type: el.getAttribute("type"),
      name: el.getAttribute("name"),
      id: el.getAttribute("id"),
      autocomplete: el.getAttribute("autocomplete")
    };
    const alwaysPasswordPolicy = {
      password: true,
      card: policy?.card ?? true,
      ssn: policy?.ssn ?? true,
      email: policy?.email ?? true,
      phone: policy?.phone ?? true
    };
    return redactFieldValue(value, identity, alwaysPasswordPolicy).text;
  }
  function formFieldType(el) {
    const tag = el.tagName.toUpperCase();
    if (tag === "INPUT") return (el.getAttribute("type") ?? "text").toLowerCase();
    if (tag === "SELECT") return "select";
    if (tag === "TEXTAREA") return "textarea";
    const role = (el.getAttribute("role") ?? "").trim().toLowerCase();
    return role || "generic";
  }
  function formFieldEntry(el, ctx, policy) {
    const described = describeElement(el, ctx, policy);
    const role = described.role;
    const entry = {
      label: described.name,
      type: formFieldType(el),
      value: formFieldValue(el, policy),
      required: el.hasAttribute("required") || ariaFlag(el, "aria-required"),
      disabled: el.hasAttribute("disabled") || ariaFlag(el, "aria-disabled"),
      readonly: el.hasAttribute("readonly") || ariaFlag(el, "aria-readonly"),
      validationMessage: el.validationMessage ?? "",
      ariaInvalid: ariaFlag(el, "aria-invalid")
    };
    if (role === "checkbox" || role === "radio" || role === "switch") {
      entry.checked = el.hasAttribute("checked") || (el.getAttribute("aria-checked") ?? "").toLowerCase() === "true";
    }
    if ((el.getAttribute("aria-selected") ?? "") !== "") {
      entry.selected = ariaFlag(el, "aria-selected");
    }
    return entry;
  }
  function formStateOf(container, ctx, policy, originPolicy) {
    const fields = [];
    let truncated = false;
    const visit = (el) => {
      if (isFormFieldElement(el)) {
        if (fields.length >= MAX_FORM_FIELDS) {
          truncated = true;
          return;
        }
        fields.push(formFieldEntry(el, ctx, policy));
      }
    };
    walkComposed(container, ctx, originPolicy, visit);
    return { fields, truncated };
  }
  const ELEMENT_ATTR_ALLOWLIST = ["id", "class", "href", "src", "title", "alt", "type", "name", "placeholder"];
  const MAX_DATA_ATTRS = 10;
  const MAX_ATTR_VALUE_CHARS = 200;
  function clipAttrValue(value, policy) {
    const redacted = redactText(value, policy).text;
    return redacted.length > MAX_ATTR_VALUE_CHARS ? `${redacted.slice(0, MAX_ATTR_VALUE_CHARS - 1)}…` : redacted;
  }
  function elementInfoOf(el, ctx, policy) {
    const described = describeElement(el, ctx, policy);
    const rect = ctx.rectOf(el);
    const attributes = {};
    for (const name of ELEMENT_ATTR_ALLOWLIST) {
      if (el.hasAttribute(name)) attributes[name] = clipAttrValue(el.getAttribute(name) ?? "", policy);
    }
    const names = el.attributeNames ? el.attributeNames() : [];
    for (const name of names) {
      if (name.toLowerCase().startsWith("aria-")) {
        attributes[name] = clipAttrValue(el.getAttribute(name) ?? "", policy);
      }
    }
    let dataCount = 0;
    for (const name of names) {
      if (!name.toLowerCase().startsWith("data-")) continue;
      if (dataCount >= MAX_DATA_ATTRS) break;
      dataCount++;
      attributes[name] = clipAttrValue(el.getAttribute(name) ?? "", policy);
    }
    return {
      tag: el.tagName.toUpperCase(),
      role: described.role,
      name: described.name,
      rect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
      attributes
    };
  }
  const STYLE_PROP_ALLOWLIST = [
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "pointer-events",
    "cursor",
    "position",
    "z-index",
    "width",
    "height"
  ];
  const STYLE_PROP_SET = new Set(STYLE_PROP_ALLOWLIST);
  function styleOf(el, ctx, props) {
    if (props.length === 0) {
      return { invalid: `style requires at least one prop from the allowlist: ${STYLE_PROP_ALLOWLIST.join(", ")}` };
    }
    const disallowed = props.filter((p) => !STYLE_PROP_SET.has(p));
    if (disallowed.length > 0) {
      return {
        invalid: `unsupported style propert${disallowed.length === 1 ? "y" : "ies"} ${disallowed.join(", ")} -- allowed: ${STYLE_PROP_ALLOWLIST.join(", ")}`
      };
    }
    const values = {};
    for (const prop of props) values[prop] = ctx.computedStyleValue(el, prop);
    return { values };
  }
  const MAX_LISTENER_ANCESTOR_LEVELS = 5;
  function ancestorChain(el, root, ctx, semanticIndex, assigner, policy, maxLevels = MAX_LISTENER_ANCESTOR_LEVELS) {
    const out = [];
    let cur = el.parentElement ?? el.rootHost ?? null;
    let levels = 0;
    while (cur && levels < maxLevels) {
      const described = describeElement(cur, ctx, policy);
      const built = selectorOf(cur, root, semanticIndex, policy);
      const { idx } = assigner.assign(built.selector, described);
      out.push({ idx, role: described.role, name: described.name, selector: built.selector });
      cur = cur.parentElement ?? cur.rootHost ?? null;
      levels++;
    }
    return out;
  }
  function runInspect(question, params, inspectCtx, resolve2) {
    const { root, ctx, policy, originPolicy, ownExtensionId: ownExtensionId2, existingIndexMap, nextIdx } = inspectCtx;
    const assigner = new IndexAssigner(existingIndexMap, nextIdx);
    const semanticIndex = buildSemanticIndex(root, (node) => childElements(node));
    let data;
    switch (question) {
      case "scrollables": {
        const { scrollables, truncated } = findScrollables(root, ctx, semanticIndex, assigner, policy, originPolicy);
        data = { question, scrollables, truncated };
        break;
      }
      case "at_point": {
        if (typeof params.x !== "number" || typeof params.y !== "number") {
          throw new Error("inspect(at_point) requires numeric x and y");
        }
        const hit = hitTest(root, ctx, params.x, params.y, originPolicy, ownExtensionId2);
        if (hit.kind === "element") {
          const described = describeElement(hit.el, ctx, policy);
          const built = selectorOf(hit.el, root, semanticIndex, policy);
          const { idx } = assigner.assign(built.selector, described);
          const ancestor = nearestScrollableAncestor(hit.el, ctx);
          let nearestScrollable;
          if (ancestor) {
            const name = redactText(nameForScrollableRegion(ancestor.el, ctx), policy).text;
            const ancestorBuilt = selectorOf(ancestor.el, root, semanticIndex, policy);
            const assignment = assigner.assign(ancestorBuilt.selector, { role: "scrollable region", name });
            nearestScrollable = { idx: assignment.idx, name };
          }
          data = { question, hit: { kind: "element", role: described.role, name: described.name, idx }, nearestScrollable };
        } else {
          data = { question, hit };
        }
        break;
      }
      case "visibility": {
        if (!params.selector) throw new Error("inspect(visibility) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(visibility): no element resolved for '${params.selector}'`);
        const state = visibilityOf(el, root, ctx, semanticIndex, assigner, policy, originPolicy, ownExtensionId2);
        data = { question, ...state };
        break;
      }
      case "expanded": {
        if (!params.selector) throw new Error("inspect(expanded) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(expanded): no element resolved for '${params.selector}'`);
        const info = expandedOf(el, ctx);
        data = { question, ...info };
        break;
      }
      case "options": {
        if (!params.selector) throw new Error("inspect(options) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(options): no element resolved for '${params.selector}'`);
        data = { question, ...optionsOf(el, policy) };
        break;
      }
      case "form_state": {
        if (!params.selector) throw new Error("inspect(form_state) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(form_state): no element resolved for '${params.selector}'`);
        data = { question, ...formStateOf(el, ctx, policy, originPolicy) };
        break;
      }
      case "element": {
        if (!params.selector) throw new Error("inspect(element) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(element): no element resolved for '${params.selector}'`);
        data = { question, ...elementInfoOf(el, ctx, policy) };
        break;
      }
      case "style": {
        if (!params.selector) throw new Error("inspect(style) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(style): no element resolved for '${params.selector}'`);
        data = { question, ...styleOf(el, ctx, params.props ?? []) };
        break;
      }
      case "listeners": {
        if (!params.selector) throw new Error("inspect(listeners) requires idx or selector");
        const el = resolve2(params.selector);
        if (!el) throw new Error(`inspect(listeners): no element resolved for '${params.selector}'`);
        const described = describeElement(el, ctx, policy);
        const ancestors = ancestorChain(el, root, ctx, semanticIndex, assigner, policy);
        data = { question, self: { role: described.role, name: described.name }, ancestors };
        break;
      }
      default: {
        const exhaustive = question;
        throw new Error(`inspect: unknown question '${String(exhaustive)}'`);
      }
    }
    return { data, newIndexMap: assigner.newIndexMap, newIndexMeta: assigner.newIndexMeta };
  }
  const INTERACTIVE_RE = /^\[(\d+)]\s+(\S+)\s+"((?:[^"\\]|\\.)*)"(.*)$/;
  const HEADING_RE = /^heading\s+"((?:[^"\\]|\\.)*)"$/;
  const DIALOG_RE = /^dialog\s+"((?:[^"\\]|\\.)*)"$/;
  const IFRAME_RE = /^iframe\s+"((?:[^"\\]|\\.)*)"\s+\[cross-origin]$/;
  const TEXT_RE = /^"((?:[^"\\]|\\.)*)"$/;
  const TRUNCATION_LINE = TRUNCATION_MARKER.replace(/^\n/, "");
  function parseSnapshotLines(tree) {
    const out = [];
    if (!tree) return out;
    for (const raw of tree.split("\n")) {
      if (raw === "" || raw === TRUNCATION_LINE) continue;
      const interactive = INTERACTIVE_RE.exec(raw);
      if (interactive) {
        out.push({
          raw,
          kind: "interactive",
          pos: out.length,
          idx: Number(interactive[1]),
          role: interactive[2],
          name: unescapeQuotes(interactive[3]),
          extra: interactive[4]
        });
        continue;
      }
      const heading = HEADING_RE.exec(raw);
      if (heading) {
        out.push({ raw, kind: "heading", pos: out.length, name: unescapeQuotes(heading[1]), extra: "" });
        continue;
      }
      const dialog = DIALOG_RE.exec(raw);
      if (dialog) {
        out.push({ raw, kind: "dialog", pos: out.length, name: unescapeQuotes(dialog[1]), extra: "" });
        continue;
      }
      const iframe = IFRAME_RE.exec(raw);
      if (iframe) {
        out.push({ raw, kind: "iframe", pos: out.length, name: unescapeQuotes(iframe[1]), extra: "" });
        continue;
      }
      const text = TEXT_RE.exec(raw);
      if (text) {
        out.push({ raw, kind: "text", pos: out.length, name: unescapeQuotes(text[1]), extra: "" });
        continue;
      }
      out.push({ raw, kind: "text", pos: out.length, name: raw, extra: "" });
    }
    return out;
  }
  function unescapeQuotes(text) {
    return text.replace(/\\"/g, '"');
  }
  function keyOf(line) {
    switch (line.kind) {
      case "interactive":
        return `i:${line.role}:${line.name}`;
      case "heading":
        return `h:${line.name}`;
      case "dialog":
        return `d:${line.name}`;
      case "iframe":
        return `f:${line.name}`;
      case "text":
        return `t:${line.name}`;
    }
  }
  function matchLines(prev, curr) {
    const queues = /* @__PURE__ */ new Map();
    for (const p of prev) {
      const k = keyOf(p);
      const q = queues.get(k);
      if (q) q.push(p);
      else queues.set(k, [p]);
    }
    const matched = [];
    const added = [];
    for (const c of curr) {
      const q = queues.get(keyOf(c));
      const p = q?.shift();
      if (p) matched.push({ prev: p, curr: c });
      else added.push(c);
    }
    const removed = [];
    for (const q of queues.values()) removed.push(...q);
    removed.sort((a, b) => a.pos - b.pos);
    return { matched, added, removed };
  }
  function extractDisplay(role, extra) {
    const valueMatch = /value="((?:[^"\\]|\\.)*)"/.exec(extra);
    if (valueMatch) return `"${unescapeQuotes(valueMatch[1])}"`;
    const urlMatch = /url=(\S*)/.exec(extra);
    if (urlMatch) return `url=${urlMatch[1]}`;
    const flags = [];
    if (/\bchecked\b/.test(extra)) flags.push("checked");
    else if (role === "checkbox" || role === "radio") flags.push("unchecked");
    if (/\bdisabled\b/.test(extra)) flags.push("disabled");
    return flags.length > 0 ? flags.join(" ") : "enabled";
  }
  function renderValueChange(prev, curr) {
    if (prev.extra === curr.extra) return void 0;
    const before = extractDisplay(curr.role ?? "", prev.extra);
    const after = extractDisplay(curr.role ?? "", curr.extra);
    if (before === after) return void 0;
    return `[${curr.idx}] ${curr.role} "${curr.name}" ${before} → ${after}`;
  }
  function renderShiftNotice(shifts) {
    if (shifts.length <= 3) {
      return shifts.map((s) => `index shifted: [${s.prevIdx}]→[${s.currIdx}] ${s.role} "${s.name}"`).join("\n");
    }
    return `indices shifted for ${shifts.length} elements — re-snapshot before reusing remembered [idx] values`;
  }
  const GROUP_MIN_RUN = 4;
  function findPrecedingHeading(all, pos) {
    for (let p = pos - 1; p >= 0; p--) {
      if (all[p]?.kind === "heading") return all[p].name;
    }
    return void 0;
  }
  function groupConsecutive(lines, allLines) {
    const sorted = [...lines].sort((a, b) => a.pos - b.pos);
    const groups = [];
    let i = 0;
    while (i < sorted.length) {
      let j = i + 1;
      while (j < sorted.length && sorted[j].kind === sorted[i].kind && sorted[j].role === sorted[i].role && sorted[j].pos === sorted[j - 1].pos + 1) {
        j++;
      }
      const run = sorted.slice(i, j);
      groups.push({
        kind: run[0].kind,
        role: run[0].role,
        count: run.length,
        lines: run,
        contextHeading: findPrecedingHeading(allLines, run[0].pos)
      });
      i = j;
    }
    return groups;
  }
  function kindLabel(kind, role, count) {
    const plural = count > 1;
    if (kind === "interactive") return `${role} control${plural ? "s" : ""}`;
    if (kind === "heading") return `heading${plural ? "s" : ""}`;
    if (kind === "dialog") return `dialog${plural ? "s" : ""}`;
    if (kind === "iframe") return `iframe${plural ? "s" : ""}`;
    return `line${plural ? "s" : ""}`;
  }
  function renderGroup(g, sign) {
    if (g.count < GROUP_MIN_RUN) {
      return g.lines.map((l) => `${sign} ${l.raw}`).join("\n");
    }
    const where = g.contextHeading ? ` in "${g.contextHeading}"` : "";
    return `${sign}${g.count} ${kindLabel(g.kind, g.role, g.count)}${where} (e.g. ${g.lines[0].raw})`;
  }
  const DEFAULT_DIFF_BUDGET_BYTES = 800;
  const NAVIGATION_CHURN_THRESHOLD = 0.7;
  const NAVIGATION_MIN_PREV_LINES = 4;
  function diffSnapshot(prevTree, currTree, budgetBytes = DEFAULT_DIFF_BUDGET_BYTES) {
    const prev = parseSnapshotLines(prevTree);
    const curr = parseSnapshotLines(currTree);
    const { matched, added, removed } = matchLines(prev, curr);
    const entries = [];
    if (prev.length >= NAVIGATION_MIN_PREV_LINES && 1 - matched.length / prev.length >= NAVIGATION_CHURN_THRESHOLD) {
      entries.push("page content mostly replaced (likely navigation)");
    }
    const shifts = [];
    for (const { prev: p, curr: c } of matched) {
      if (p.kind === "interactive" && c.kind === "interactive" && p.idx !== c.idx) {
        shifts.push({ prevIdx: p.idx, currIdx: c.idx, role: c.role, name: c.name });
      }
    }
    if (shifts.length > 0) entries.push(renderShiftNotice(shifts));
    for (const { prev: p, curr: c } of matched) {
      if (p.kind === "interactive" && c.kind === "interactive") {
        const change = renderValueChange(p, c);
        if (change) entries.push(change);
      }
    }
    const addedHeadings = added.filter((l) => l.kind === "heading" || l.kind === "dialog");
    const removedHeadings = removed.filter((l) => l.kind === "heading" || l.kind === "dialog");
    for (const h of addedHeadings) entries.push(`appeared: "${h.name}"`);
    for (const h of removedHeadings) entries.push(`disappeared: "${h.name}"`);
    const addedRest = added.filter((l) => l.kind !== "heading" && l.kind !== "dialog");
    const removedRest = removed.filter((l) => l.kind !== "heading" && l.kind !== "dialog");
    for (const g of groupConsecutive(addedRest, curr)) entries.push(renderGroup(g, "+"));
    for (const g of groupConsecutive(removedRest, prev)) entries.push(renderGroup(g, "-"));
    if (entries.length === 0) return "no visible change";
    return fitEntriesToBudget(entries, budgetBytes);
  }
  const MAX_DIFF_LINES = 30;
  function fitEntriesToBudget(entries, budgetBytes) {
    const full = entries.join("\n");
    const fullLineCount = full.split("\n").length;
    if (byteLength(full) <= budgetBytes && fullLineCount <= MAX_DIFF_LINES) return full;
    const kept = [];
    let total = 0;
    let lines = 0;
    let i = 0;
    for (; i < entries.length; i++) {
      const entryLines = entries[i].split("\n").length;
      const bytes = byteLength(entries[i]) + (kept.length > 0 ? 1 : 0);
      if (total + bytes > budgetBytes || lines + entryLines > MAX_DIFF_LINES) break;
      kept.push(entries[i]);
      total += bytes;
      lines += entryLines;
    }
    const remaining = entries.length - kept.length;
    if (remaining <= 0) return kept.join("\n");
    const suffix = `
…(+${remaining} more; snapshot for the full list)`;
    while (kept.length > 0 && byteLength(kept.join("\n") + suffix) > budgetBytes) {
      kept.pop();
    }
    return kept.length > 0 ? kept.join("\n") + suffix : suffix.trimStart();
  }
  const DEFAULT_SNAPSHOT_BUDGET_BYTES = 4096;
  function buildSnapshot(root, budgetBytes = DEFAULT_SNAPSHOT_BUDGET_BYTES, diffAgainst, policy, originPolicy = DENY_ALL_ORIGIN_POLICY, opts = {}) {
    const domRoot = adaptElement(root);
    const ctx = makeDomContext();
    const result = buildSnapshotFromDom(domRoot, ctx, budgetBytes, policy, originPolicy, opts);
    const data = {
      tree: result.tree,
      truncated: result.truncated,
      redactions: result.redactions,
      url: window.location.href,
      title: document.title,
      viewport: getViewportSize(),
      indexMap: result.indexMap,
      indexMeta: result.indexMeta,
      shadow: result.shadow,
      frameBoundaries: result.frameBoundaries
    };
    if (diffAgainst !== void 0) data.diff = diffSnapshot(diffAgainst, result.tree);
    if (opts.find) data.findMatches = result.findMatches;
    if (opts.dialogOnly) data.dialogMissing = result.dialogMissing;
    if (result.hint) data.hint = result.hint;
    if (opts.interactiveOnly && result.interactiveOnlyBytesSaved !== void 0) data.interactiveOnlyBytesSaved = result.interactiveOnlyBytesSaved;
    return data;
  }
  function resolveElementDescription(selector, policy) {
    const found = resolvePierceSelector(selector, browserPierceEnv());
    if (!found.found) return found.invalid ? { invalid: found.invalid } : null;
    const { role, name } = describeElement(adaptElement(found.element), makeDomContext(), policy);
    return { role, name };
  }
  const BOXES_TRAVERSAL_BUDGET_BYTES = Number.MAX_SAFE_INTEGER;
  function buildBoxes(root = document.body, policy, originPolicy) {
    const domRoot = adaptElement(root);
    const ctx = makeDomContext();
    const result = buildSnapshotFromDom(domRoot, ctx, BOXES_TRAVERSAL_BUDGET_BYTES, policy, originPolicy);
    return {
      viewport: getViewportSize(),
      boxes: result.boxes
    };
  }
  function collectBlocks(root) {
    const blocks = [];
    const visit = (el) => {
      const tag = el.tagName.toUpperCase();
      if (isChromeTag(tag)) return;
      if (el.frame) {
        if (!el.frame.crossOrigin && el.frame.body) {
          for (const child of childElements(el.frame.body)) visit(child);
        }
        return;
      }
      const level = headingLevel(tag);
      if (level) {
        const text = directText(el) || slottedText(el);
        if (text) blocks.push({ kind: "heading", level, text });
        return;
      }
      if (tag === "A" && el.resolvedHref) {
        const text = collapseWhitespace(composedText(el));
        if (text) blocks.push({ kind: "link", text, href: el.resolvedHref });
        return;
      }
      if (tag === "LI") {
        const text = collapseWhitespace(composedText(el));
        const ordered = (el.parentElement?.tagName ?? "").toUpperCase() === "OL";
        if (text) blocks.push({ kind: "listitem", text, ordered });
        return;
      }
      const children = childElements(el);
      if (children.length === 0) {
        const text = collapseWhitespace(composedText(el));
        if (text) blocks.push({ kind: "paragraph", text });
        return;
      }
      for (const child of children) visit(child);
    };
    visit(root);
    return blocks;
  }
  const DEFAULT_READ_BYTE_CAP = 2e4;
  function readPage(root, format = "markdown", byteCap = DEFAULT_READ_BYTE_CAP, policy) {
    beginWalk();
    const domRoot = adaptElement(root);
    const blocks = collectBlocks(domRoot);
    const rendered = format === "markdown" ? formatMarkdown(blocks) : formatText(blocks);
    const { text: redacted } = redactText(rendered, policy);
    const capped = capToBytes(redacted, byteCap);
    return { content: capped.text, truncated: capped.truncated };
  }
  const MIN_QUIET_MS = 100;
  const MAX_QUIET_MS = 150;
  const ADAPTIVE_QUIET_FULL_BUDGET_MS = MAX_QUIET_MS * 10;
  function computeAdaptiveQuietMs(budgetMs) {
    if (budgetMs <= 0) return MIN_QUIET_MS;
    const scaled = budgetMs / ADAPTIVE_QUIET_FULL_BUDGET_MS * MAX_QUIET_MS;
    return Math.max(MIN_QUIET_MS, Math.min(MAX_QUIET_MS, scaled));
  }
  const MAX_OBSERVED_ROOTS = 500;
  function runQuietDetector(env, options) {
    const quietMs = options.quietMs ?? computeAdaptiveQuietMs(options.budgetMs);
    const maxRoots = options.maxRoots ?? MAX_OBSERVED_ROOTS;
    const scheduleTimeout = options.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms));
    const cancelTimeout = options.clearTimeoutFn ?? ((handle) => clearTimeout(handle));
    return new Promise((resolve2) => {
      const observers2 = [];
      const observedScopes = /* @__PURE__ */ new Set();
      let quietTimer;
      let budgetTimer;
      let finished = false;
      function finish(settled) {
        if (finished) return;
        finished = true;
        if (quietTimer !== void 0) cancelTimeout(quietTimer);
        if (budgetTimer !== void 0) cancelTimeout(budgetTimer);
        for (const observer of observers2) observer.disconnect();
        resolve2({ settled, observedRoots: observedScopes.size });
      }
      function armQuietTimer() {
        if (quietTimer !== void 0) cancelTimeout(quietTimer);
        quietTimer = scheduleTimeout(() => finish(true), quietMs);
      }
      function observeScope(scope) {
        if (observedScopes.has(scope) || observedScopes.size >= maxRoots) return;
        observedScopes.add(scope);
        observers2.push(
          env.observe(scope, () => {
            armQuietTimer();
            discoverFrom(scope);
          })
        );
      }
      function discoverFrom(scope) {
        let elements;
        try {
          elements = env.queryAll(scope, "*");
        } catch {
          return;
        }
        for (const el of elements) {
          if (observedScopes.size >= maxRoots) return;
          const root = env.shadowRootOf(el);
          if (root && !observedScopes.has(root)) {
            observeScope(root);
            discoverFrom(root);
          }
        }
      }
      observeScope(env.document);
      discoverFrom(env.document);
      armQuietTimer();
      budgetTimer = scheduleTimeout(() => finish(false), Math.max(0, options.budgetMs));
      options.registerCancel?.(() => finish(false));
    });
  }
  function browserQuietEnv() {
    const accessor = makeShadowRootAccessor(availableChromeDom());
    return {
      document,
      queryAll: (scope, selector) => Array.from(scope.querySelectorAll(selector)),
      shadowRootOf: (el) => accessor(el),
      observe: (scope, onMutate) => {
        const observer = new MutationObserver(onMutate);
        observer.observe(scope, { subtree: true, childList: true, characterData: true, attributes: true });
        return { disconnect: () => observer.disconnect() };
      }
    };
  }
  function resolve(selector, scope) {
    const env = browserPierceEnv();
    return resolvePierceSelector(selector, env, scope ?? env.document);
  }
  function scrollToCenter(el) {
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  }
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  }
  function resolveTarget(selector, scroll, policy) {
    const found = resolve(selector);
    if (!found.found) return found;
    if (scroll) scrollToCenter(found.element);
    const { role, name } = describeElement(adaptElement(found.element), makeDomContext(), policy);
    return {
      found: true,
      rect: rectOf(found.element),
      matchCount: found.matchCount,
      chosenReason: found.chosenReason,
      description: { role, name },
      draggable: found.element.draggable === true
    };
  }
  const NON_TEXT_INPUT_TYPES = /* @__PURE__ */ new Set([
    "checkbox",
    "radio",
    "button",
    "submit",
    "reset",
    "file",
    "image",
    "range",
    "color"
  ]);
  function isEditableTextHost(el) {
    if (el instanceof HTMLTextAreaElement) return true;
    if (el instanceof HTMLInputElement) {
      return !NON_TEXT_INPUT_TYPES.has((el.getAttribute("type") || "text").toLowerCase());
    }
    return el.isContentEditable === true;
  }
  function nearestEditableAncestor(el) {
    let node = el;
    while (node) {
      if (isEditableTextHost(node)) return node;
      node = node.parentElement;
    }
    return null;
  }
  function editableState(el) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
      return { disabled: el.disabled, readOnly: el.readOnly };
    }
    return {
      disabled: (el.getAttribute("aria-disabled") || "").toLowerCase() === "true",
      readOnly: (el.getAttribute("aria-readonly") || "").toLowerCase() === "true"
    };
  }
  function resolveEditableTarget(selector, scroll, policy) {
    const found = resolve(selector);
    if (!found.found) return found;
    const matched = found.element;
    const finalEl = isEditableTextHost(matched) ? matched : nearestEditableAncestor(matched);
    const editable = finalEl !== null;
    const target = finalEl ?? matched;
    if (scroll) scrollToCenter(target);
    const { role, name } = describeElement(adaptElement(target), makeDomContext(), policy);
    const state = editable ? editableState(target) : { disabled: false, readOnly: false };
    return {
      found: true,
      rect: rectOf(target),
      matchCount: found.matchCount,
      chosenReason: found.chosenReason,
      description: { role, name },
      editable,
      disabled: state.disabled,
      readOnly: state.readOnly,
      retargeted: editable && target !== matched
    };
  }
  const FIELD_VALUE_CLIP = 500;
  function clipValue(text) {
    return text.length > FIELD_VALUE_CLIP ? `${text.slice(0, FIELD_VALUE_CLIP)}…(truncated)` : text;
  }
  function readFieldValue(selector, policy) {
    const found = resolve(selector);
    if (!found.found) return { found: false, ...found.invalid ? { invalid: found.invalid } : {} };
    const matched = found.element;
    const finalEl = isEditableTextHost(matched) ? matched : nearestEditableAncestor(matched);
    if (!finalEl) return { found: false };
    const isInputLike = finalEl instanceof HTMLInputElement || finalEl instanceof HTMLTextAreaElement;
    const raw = isInputLike ? finalEl.value : finalEl.textContent ?? "";
    const redacted = isInputLike ? redactFieldValue(
      raw,
      {
        type: finalEl.getAttribute("type"),
        name: finalEl.getAttribute("name"),
        id: finalEl.getAttribute("id"),
        autocomplete: finalEl.getAttribute("autocomplete")
      },
      policy
    ) : redactText(raw, policy);
    return { found: true, value: clipValue(redacted.text), isContentEditable: !isInputLike };
  }
  function readFileInputValue(selector) {
    const found = resolve(selector);
    if (!found.found) return { found: false, ...found.invalid ? { invalid: found.invalid } : {} };
    const el = found.element;
    if (!(el instanceof HTMLInputElement) || el.type !== "file") {
      return { found: true, notFileInput: true };
    }
    const file = el.files?.[0];
    if (!file) return { found: true };
    return { found: true, name: file.name, size: file.size, type: file.type };
  }
  function selectorPresent(selector) {
    const found = resolve(selector);
    if (!found.found) return { present: false, ...found.invalid ? { invalid: found.invalid } : {} };
    const r = found.element.getBoundingClientRect();
    return { present: r.width > 0 && r.height > 0 };
  }
  function textPresent(text, caseInsensitive = false) {
    const needle = caseInsensitive ? text.toLowerCase() : text;
    const matches = (haystack) => caseInsensitive ? haystack.toLowerCase().includes(needle) : haystack.includes(needle);
    if (document.body && matches(document.body.innerText)) return true;
    const env = browserPierceEnv();
    for (const root of shadowRootsWithin(env, env.document)) {
      for (const node of Array.from(root.childNodes)) {
        const rendered = node instanceof HTMLElement ? node.innerText : node.nodeType === Node.TEXT_NODE ? node.textContent : "";
        if (rendered && matches(rendered)) return true;
      }
    }
    return false;
  }
  function selectOption(selector, text, optionText) {
    const found = resolve(selector);
    if (!found.found) {
      return found.invalid ? { matched: false, reason: "invalid_selector", detail: found.invalid } : { matched: false, reason: "not_found" };
    }
    const el = found.element;
    if (!(el instanceof HTMLSelectElement)) return { matched: false, reason: "not_a_select" };
    let option;
    if (optionText !== void 0) {
      const byText = matchSelectOptionByVisibleText(el, optionText, 20);
      if ("failure" in byText) return { matched: false, reason: byText.failure, options: byText.options };
      option = byText.option;
    } else {
      option = Array.from(el.options).find((opt) => opt.value === text || (opt.textContent || "").trim() === text);
      if (!option) return { matched: false, reason: "no_matching_option" };
    }
    el.value = option.value;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { matched: true, matchCount: found.matchCount, chosenReason: found.chosenReason };
  }
  function normalizeOptionText(text) {
    return text.trim().toLowerCase();
  }
  function matchSelectOptionByVisibleText(el, text, optionsLimit) {
    const options = Array.from(el.options);
    const optionTexts = options.map((opt) => (opt.textContent || "").trim());
    const target = normalizeOptionText(text);
    const exact = options.find((opt) => normalizeOptionText(opt.textContent || "") === target);
    if (exact) return { option: exact };
    const prefixMatches = options.filter((opt) => normalizeOptionText(opt.textContent || "").startsWith(target));
    if (prefixMatches.length === 1) return { option: prefixMatches[0] };
    const failure = prefixMatches.length > 1 ? "ambiguous_option" : "no_matching_option";
    return { failure, options: optionTexts.slice(0, optionsLimit) };
  }
  function fillOneField(selector, value) {
    const found = resolve(selector);
    if (!found.found) {
      return found.invalid ? { ok: false, reason: "invalid_selector", detail: found.invalid } : { ok: false, reason: "not_found" };
    }
    const el = found.element;
    if (el instanceof HTMLSelectElement) {
      if (typeof value !== "string") return { ok: false, reason: "wrong_value_type", detail: "a select field needs a string (the option's visible text)" };
      const matched = matchSelectOptionByVisibleText(el, value, 10);
      if ("failure" in matched) return { ok: false, reason: matched.failure, options: matched.options };
      el.value = matched.option.value;
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true };
    }
    if (el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio")) {
      if (typeof value !== "boolean") return { ok: false, reason: "wrong_value_type", detail: `a ${el.type} field needs a boolean` };
      if (el.disabled) return { ok: false, reason: "not_editable" };
      if (el.checked !== value) {
        scrollToCenter(el);
        dispatchRealClick(el);
      }
      return { ok: true };
    }
    const finalEl = isEditableTextHost(el) ? el : nearestEditableAncestor(el);
    if (!finalEl) return { ok: false, reason: "not_editable" };
    if (typeof value !== "string") return { ok: false, reason: "wrong_value_type", detail: "a text field needs a string" };
    const state = editableState(finalEl);
    if (state.disabled || state.readOnly) return { ok: false, reason: "not_editable" };
    scrollToCenter(finalEl);
    try {
      finalEl.focus?.();
    } catch {
    }
    if (finalEl instanceof HTMLInputElement || finalEl instanceof HTMLTextAreaElement) {
      const proto = finalEl instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(finalEl, value);
      else finalEl.value = value;
      finalEl.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      finalEl.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      document.execCommand("selectAll", false);
      if (!document.execCommand("insertText", false, value)) {
        finalEl.textContent = value;
        finalEl.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      }
    }
    return { ok: true };
  }
  function fillFields(fields) {
    for (let i = 0; i < fields.length; i++) {
      const result = fillOneField(fields[i].selector, fields[i].value);
      if (!result.ok) {
        return { completed: i, failedIndex: i, reason: result.reason, detail: result.detail, options: result.options };
      }
    }
    return { completed: fields.length };
  }
  const SUBMIT_CONTROL_SELECTOR = 'button[type="submit"], input[type="submit"], button:not([type])';
  function submitTarget(selector) {
    let scope = document;
    let match = {};
    if (selector) {
      const found = resolve(selector);
      if (!found.found) return { kind: "none", ...found.invalid ? { invalid: found.invalid } : {} };
      scope = found.element;
      match = { matchCount: found.matchCount, chosenReason: found.chosenReason };
    }
    const form = scope instanceof HTMLFormElement ? scope : scope instanceof Element ? scope.closest("form") : null;
    const root = form ?? scope;
    const control = resolve(SUBMIT_CONTROL_SELECTOR, root);
    if (control.found) {
      scrollToCenter(control.element);
      const r = control.element.getBoundingClientRect();
      return { kind: "control", x: r.x + r.width / 2, y: r.y + r.height / 2, ...match };
    }
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
      return { kind: "submitted", ...match };
    }
    return { kind: "none" };
  }
  function resolveFrameAnchor(selector, scroll) {
    const found = resolve(selector);
    if (!found.found) return { found: false, ...found.invalid ? { invalid: found.invalid } : {} };
    const el = found.element;
    if (!(el instanceof HTMLIFrameElement) && !(el instanceof HTMLFrameElement)) {
      return { found: true, notAFrame: true, matchCount: found.matchCount, chosenReason: found.chosenReason };
    }
    if (scroll) scrollToCenter(el);
    const style = getComputedStyle(el);
    const borderLeft = parseFloat(style.borderLeftWidth) || 0;
    const paddingLeft = parseFloat(style.paddingLeft) || 0;
    const borderTop = parseFloat(style.borderTopWidth) || 0;
    const paddingTop = parseFloat(style.paddingTop) || 0;
    const rect = rectOf(el);
    return {
      found: true,
      rect,
      contentBoxOffset: { x: rect.x + borderLeft + paddingLeft, y: rect.y + borderTop + paddingTop },
      src: el.src || null,
      matchCount: found.matchCount,
      chosenReason: found.chosenReason
    };
  }
  function resolveRootElement(selector) {
    const found = resolve(selector);
    if (!found.found) {
      throw new Error(found.invalid ? `invalid selector ${selector}: ${found.invalid}` : `selector not found: ${selector}`);
    }
    return found.element;
  }
  function eventPointOf(el) {
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  function dispatchRealClick(el) {
    const { x, y } = eventPointOf(el);
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...base, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousedown", base));
    el.dispatchEvent(new PointerEvent("pointerup", { ...base, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mouseup", base));
    el.click();
  }
  function dispatchHoverEnter(el) {
    const { x, y } = eventPointOf(el);
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y };
    el.dispatchEvent(new PointerEvent("pointerover", { ...base, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mouseover", base));
    el.dispatchEvent(new MouseEvent("mouseenter", { ...base, bubbles: false }));
    el.dispatchEvent(new PointerEvent("pointermove", { ...base, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousemove", base));
  }
  function describeFound(el, policy) {
    const { role, name } = describeElement(adaptElement(el), makeDomContext(), policy);
    return { role, name };
  }
  function limitedClick(selector, policy) {
    const found = resolve(selector);
    if (!found.found) return found;
    const el = found.element;
    scrollToCenter(el);
    try {
      el.focus?.();
    } catch {
    }
    dispatchRealClick(el);
    return {
      found: true,
      rect: rectOf(el),
      matchCount: found.matchCount,
      chosenReason: found.chosenReason,
      description: describeFound(el, policy)
    };
  }
  function limitedHover(selector, policy) {
    const found = resolve(selector);
    if (!found.found) return found;
    const el = found.element;
    scrollToCenter(el);
    dispatchHoverEnter(el);
    return {
      found: true,
      rect: rectOf(el),
      matchCount: found.matchCount,
      chosenReason: found.chosenReason,
      description: describeFound(el, policy)
    };
  }
  function limitedType(selector, text, mode, policy) {
    const found = resolve(selector);
    if (!found.found) return found;
    const matched = found.element;
    const finalEl = isEditableTextHost(matched) ? matched : nearestEditableAncestor(matched);
    const editable = finalEl !== null;
    const target = finalEl ?? matched;
    scrollToCenter(target);
    const state = editable ? editableState(target) : { disabled: false, readOnly: false };
    if (editable && !state.disabled && !state.readOnly) {
      try {
        target.focus?.();
      } catch {
      }
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
        const proto = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        const current = target.value;
        const next = mode === "append" ? current + text : mode === "prepend" ? text + current : text;
        if (setter) setter.call(target, next);
        else target.value = next;
        target.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        const selection = window.getSelection();
        if (mode === "replace") {
          document.execCommand("selectAll", false);
        } else if (selection) {
          const range = document.createRange();
          range.selectNodeContents(target);
          range.collapse(mode === "prepend");
          selection.removeAllRanges();
          selection.addRange(range);
        }
        if (!document.execCommand("insertText", false, text)) {
          target.textContent = mode === "prepend" ? text + (target.textContent ?? "") : (target.textContent ?? "") + text;
          target.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        }
      }
    }
    return {
      found: true,
      rect: rectOf(target),
      matchCount: found.matchCount,
      chosenReason: found.chosenReason,
      description: describeFound(target, policy),
      editable,
      disabled: state.disabled,
      readOnly: state.readOnly,
      retargeted: editable && target !== matched
    };
  }
  function limitedPress(selector, key, policy) {
    let el = null;
    let matchInfo = {};
    if (selector) {
      const found = resolve(selector);
      if (!found.found) return found;
      el = found.element;
      matchInfo = { matchCount: found.matchCount, chosenReason: found.chosenReason };
      try {
        el.focus?.();
      } catch {
      }
    }
    const target = el ?? (document.activeElement instanceof Element ? document.activeElement : null);
    const base = { key, bubbles: true, cancelable: true, composed: true };
    const dispatchOn = target ?? document.body;
    dispatchOn.dispatchEvent(new KeyboardEvent("keydown", base));
    dispatchOn.dispatchEvent(new KeyboardEvent("keypress", base));
    dispatchOn.dispatchEvent(new KeyboardEvent("keyup", base));
    if (key.toLowerCase() === "enter") {
      const form = dispatchOn instanceof Element ? dispatchOn.closest("form") : null;
      if (form) {
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.submit();
      }
    }
    if (!el) return { found: true, ...matchInfo };
    return { found: true, rect: rectOf(el), description: describeFound(el, policy), ...matchInfo };
  }
  function limitedSubmit(selector) {
    let scope = document;
    let matchInfo = {};
    if (selector) {
      const found = resolve(selector);
      if (!found.found) return found;
      scope = found.element;
      matchInfo = { matchCount: found.matchCount, chosenReason: found.chosenReason };
    }
    const form = scope instanceof HTMLFormElement ? scope : scope instanceof Element ? scope.closest("form") : null;
    const root = form ?? scope;
    const control = resolve(SUBMIT_CONTROL_SELECTOR, root);
    if (control.found) {
      scrollToCenter(control.element);
      dispatchRealClick(control.element);
      return { found: true, rect: rectOf(control.element), ...matchInfo };
    }
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
      return { found: true, ...matchInfo };
    }
    return { found: false };
  }
  const NEXT_PAGE_MODES = /* @__PURE__ */ new Set(["top", "bottom", "next_page"]);
  function findScrollableAncestor(start) {
    let el = start;
    while (el && el.nodeType === 1 && el !== document.documentElement && el !== document.body) {
      const cs = window.getComputedStyle(el);
      const scrollableY = (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight > el.clientHeight;
      const scrollableX = (cs.overflowX === "auto" || cs.overflowX === "scroll") && el.scrollWidth > el.clientWidth;
      if (scrollableY || scrollableX) return el;
      el = el.parentElement;
    }
    return null;
  }
  function measureScrollable(el) {
    const round = Math.round;
    return {
      top: round(el.scrollTop),
      left: round(el.scrollLeft),
      maxTop: round(Math.max(0, el.scrollHeight - el.clientHeight)),
      maxLeft: round(Math.max(0, el.scrollWidth - el.clientWidth)),
      height: el.scrollHeight,
      count: el.querySelectorAll("*").length
    };
  }
  function sleep(ms) {
    return new Promise((resolve2) => setTimeout(resolve2, Math.max(0, ms)));
  }
  async function waitForGrowth(target, heightBefore, countBefore, budgetMs) {
    const start = Date.now();
    if (budgetMs <= 0) return { heightAfter: heightBefore, contentGrew: false, waitedMs: 0 };
    const deadline = start + budgetMs;
    let lastHeight = heightBefore;
    let grew = false;
    while (Date.now() < deadline) {
      const m = measureScrollable(target);
      lastHeight = m.height;
      if (m.height > heightBefore || m.count > countBefore) {
        grew = true;
        break;
      }
      await sleep(Math.min(150, Math.max(0, deadline - Date.now())));
    }
    if (grew) {
      const remaining = Math.max(0, deadline - Date.now());
      if (remaining > 0) {
        try {
          await runQuietDetector(browserQuietEnv(), { budgetMs: remaining });
        } catch {
        }
      }
      lastHeight = measureScrollable(target).height;
    }
    return { heightAfter: lastHeight, contentGrew: grew, waitedMs: Date.now() - start };
  }
  async function limitedScroll(selector, dx, dy, to, waitForGrowthMs) {
    let matchInfo = {};
    let startEl = null;
    if (selector) {
      const found = resolve(selector);
      if (!found.found) return found;
      matchInfo = { matchCount: found.matchCount, chosenReason: found.chosenReason };
      startEl = found.element;
    } else {
      startEl = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
    }
    const hasScrollRequest = Boolean(selector) && (to !== void 0 || dx !== void 0 || dy !== void 0);
    const container = findScrollableAncestor(startEl);
    const target = container ?? document.scrollingElement ?? document.documentElement;
    const before = measureScrollable(target);
    if (selector && startEl && !hasScrollRequest) {
      scrollToCenter(startEl);
    } else if (to && NEXT_PAGE_MODES.has(to)) {
      if (to === "top") target.scrollTo({ top: 0, left: target.scrollLeft });
      else if (to === "bottom") target.scrollTo({ top: before.maxTop, left: target.scrollLeft });
      else target.scrollBy(0, target.clientHeight || 0);
    } else {
      target.scrollBy(dx ?? 0, dy ?? 0);
    }
    const afterMeasure = measureScrollable(target);
    const growth = await waitForGrowth(target, before.height, before.count, Math.max(0, Math.min(1e4, waitForGrowthMs)));
    const scroll = {
      target: container ? "container" : "page",
      before: { top: before.top, left: before.left },
      after: { top: afterMeasure.top, left: afterMeasure.left },
      max: { top: before.maxTop, left: before.maxLeft },
      moved: afterMeasure.top !== before.top || afterMeasure.left !== before.left,
      at_top: afterMeasure.top <= 2,
      at_bottom: growth.contentGrew ? false : afterMeasure.top >= before.maxTop - 2,
      content_grew: growth.contentGrew,
      height_before: before.height,
      height_after: growth.heightAfter,
      waited_ms: growth.waitedMs
    };
    if (selector && startEl) {
      return { found: true, rect: rectOf(startEl), scroll, ...matchInfo };
    }
    return { found: true, scroll };
  }
  const DEFAULT_SETTINGS = {
    gatewayUrl: "ws://localhost:8765/bridge",
    deviceName: "",
    autoConnect: true,
    redactPasswords: true,
    redactCreditCards: true,
    redactSsn: true,
    redactEmails: false,
    redactPhones: false,
    showPresenceCursor: true,
    showActivityGlow: true,
    highlightSharedTabs: true,
    pointerAnimation: "normal",
    gatewayUrlConfirmed: false,
    paused: false,
    pauseReason: null,
    pausedAt: null,
    allowFileUpload: false,
    allowFileUploadFromAgent: false,
    allowDialogDismiss: true,
    allowDialogAccept: false,
    allowEvaluate: false,
    allowConsoleRead: false,
    allowCookieWrite: false,
    allowHttpAuth: false,
    allowDownloadsRead: true,
    uploadRoots: "",
    maxUploadBytes: 10485760,
    defaultAccessMode: "full",
    holdForeignFramesWhileAttached: true,
    leaseSeconds: 60,
    unlimitedLease: false,
    commitMode: "auto",
    recordReplay: false,
    replayRetention: 200
  };
  const SETTINGS_KEY = "settings";
  async function getSettings() {
    const stored = await chrome.storage.local.get(SETTINGS_KEY);
    return { ...DEFAULT_SETTINGS, ...stored[SETTINGS_KEY] ?? {} };
  }
  function redactionPolicyOf(settings) {
    return {
      password: settings.redactPasswords,
      card: settings.redactCreditCards,
      ssn: settings.redactSsn,
      email: settings.redactEmails,
      phone: settings.redactPhones
    };
  }
  function registerContentListener() {
    if (window.__hermesBridgeContentRegistered) return;
    window.__hermesBridgeContentRegistered = true;
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (isPresenceRequest(message)) {
        handlePresenceRequest(message).then(
          (data) => sendResponse({ ok: true, data }),
          (error) => sendResponse({ ok: false, error: describeError(error) })
        );
        return true;
      }
      if (!isContentRequest(message)) return false;
      switch (message.type) {
        case "snapshot":
          void handleSnapshot(message, sendResponse);
          return true;
        // response is sent asynchronously
        case "read":
          void handleRead(message, sendResponse);
          return true;
        case "resolveElement":
          void handleResolveElement(message, sendResponse);
          return true;
        case "resolveTarget":
          void handleResolveTarget(message, sendResponse);
          return true;
        case "resolveEditableTarget":
          void handleResolveEditableTarget(message, sendResponse);
          return true;
        case "resolveFrameAnchor":
          respondSync(sendResponse, () => resolveFrameAnchor(message.selector, message.scroll === true));
          return false;
        case "readField":
          void handleReadField(message, sendResponse);
          return true;
        case "readFileInput":
          respondSync(sendResponse, () => readFileInputValue(message.selector));
          return false;
        case "settleQuiet":
          void handleSettleQuiet(message, sendResponse);
          return true;
        case "settleQuiet.cancel":
          activeSettleCancel?.();
          sendResponse({ ok: true, data: { cancelled: true } });
          return false;
        case "selectOption":
          respondSync(sendResponse, () => selectOption(message.selector, message.text, message.optionText));
          return false;
        case "fillFields":
          respondSync(sendResponse, () => fillFields(message.fields));
          return false;
        case "submitTarget":
          respondSync(sendResponse, () => submitTarget(message.selector));
          return false;
        case "limitedAct":
          void handleLimitedAct(message, sendResponse);
          return true;
        case "selectorPresent":
          respondSync(sendResponse, () => selectorPresent(message.selector));
          return false;
        case "textPresent":
          respondSync(sendResponse, () => ({ present: textPresent(message.text, message.caseInsensitive) }));
          return false;
        case "boxes":
          void handleBoxes(message, sendResponse);
          return true;
        case "inspect":
          void handleInspect(message, sendResponse);
          return true;
        case "annotate":
          handleAnnotate(message, sendResponse);
          return true;
        // response arrives whenever the user picks/cancels, or the page unloads
        case "annotate.cancel":
          cancelActiveAnnotation();
          sendResponse({ ok: true, data: { cancelled: true } });
          return false;
        case "scanForeignFrames":
          respondSync(sendResponse, () => scanForeignExtensionFramesInPage());
          return false;
        case "stripForeignFrames":
          respondSync(sendResponse, () => stripForeignExtensionFramesInPage(message.childFrameUrls ?? []));
          return false;
        case "restoreForeignFrames":
          respondSync(sendResponse, () => restoreForeignExtensionFramesInPage());
          return false;
        case "armForeignFrameGuard":
          respondSync(sendResponse, () => armForeignFrameGuardInPage(message.childFrameUrls ?? []));
          return false;
        case "disarmForeignFrameGuard":
          respondSync(sendResponse, () => disarmForeignFrameGuardInPage());
          return false;
        case "foreignFrameGuardStatus":
          respondSync(sendResponse, () => foreignFrameGuardStatusInPage());
          return false;
        default:
          return false;
      }
    });
  }
  function isContentRequest(message) {
    return typeof message === "object" && message !== null && message.target === "content";
  }
  async function currentRedactionPolicy() {
    try {
      return redactionPolicyOf(await getSettings());
    } catch {
      return void 0;
    }
  }
  function originPolicyOf(message) {
    const base = message.grantedOrigins && message.grantedOrigins.includes(window.location.origin) ? [] : [window.location.origin];
    if (!message.grantedOrigins && !message.deniedOrigins && message.defaultFull === void 0) {
      return { granted: base, denied: [], defaultFull: false };
    }
    return {
      granted: [...message.grantedOrigins ?? [], ...base],
      denied: message.deniedOrigins ?? [],
      defaultFull: message.defaultFull === true
    };
  }
  async function handleSnapshot(message, sendResponse) {
    try {
      const root = resolveRoot(message.selector);
      const policy = await currentRedactionPolicy();
      sendResponse({
        ok: true,
        data: buildSnapshot(root, message.budgetBytes, message.diffAgainst, policy, originPolicyOf(message), {
          find: message.find,
          startIndex: message.startIndex,
          dialogOnly: message.dialogOnly,
          viewportOnly: message.viewportOnly,
          interactiveOnly: message.interactiveOnly,
          existingIndexMap: message.existingIndexMap
        })
      });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleRead(message, sendResponse) {
    try {
      const root = resolveRoot(message.selector);
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: readPage(root, message.format, void 0, policy) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleResolveElement(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: resolveElementDescription(message.selector, policy) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleResolveTarget(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: resolveTarget(message.selector, message.scroll === true, policy) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleResolveEditableTarget(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: resolveEditableTarget(message.selector, message.scroll === true, policy) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleLimitedAct(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      let data;
      switch (message.kind) {
        case "click":
          if (!message.selector) throw new Error("limitedAct(click) requires a selector");
          data = limitedClick(message.selector, policy);
          break;
        case "hover":
          if (!message.selector) throw new Error("limitedAct(hover) requires a selector");
          data = limitedHover(message.selector, policy);
          break;
        case "type":
          if (!message.selector) throw new Error("limitedAct(type) requires a selector");
          data = limitedType(message.selector, message.text ?? "", message.typeMode ?? "replace", policy);
          break;
        case "key":
          data = limitedPress(message.selector, message.keyName ?? "", policy);
          break;
        case "submit":
          data = limitedSubmit(message.selector);
          break;
        case "scroll":
          data = await limitedScroll(message.selector, message.dx, message.dy, message.to, message.waitForGrowthMs ?? 1500);
          break;
        default:
          throw new Error(`limitedAct: unknown kind ${String(message.kind)}`);
      }
      sendResponse({ ok: true, data });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleReadField(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: readFieldValue(message.selector, policy) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  let activeSettleCancel;
  async function handleSettleQuiet(message, sendResponse) {
    try {
      const result = await runQuietDetector(browserQuietEnv(), {
        budgetMs: message.timeoutMs,
        registerCancel: (cancel) => {
          activeSettleCancel = cancel;
        }
      });
      activeSettleCancel = void 0;
      sendResponse({ ok: true, data: { settled: result.settled } });
    } catch (error) {
      activeSettleCancel = void 0;
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  function respondSync(sendResponse, run) {
    try {
      sendResponse({ ok: true, data: run() });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleBoxes(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      sendResponse({ ok: true, data: buildBoxes(document.body, policy, originPolicyOf(message)) });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  async function handleInspect(message, sendResponse) {
    try {
      const policy = await currentRedactionPolicy();
      const result = runInspect(
        message.question,
        { selector: message.selector, x: message.x, y: message.y, props: message.props },
        {
          root: adaptElement(document.body),
          ctx: makeDomContext(),
          policy,
          originPolicy: originPolicyOf(message),
          ownExtensionId: ownExtensionId(),
          existingIndexMap: message.existingIndexMap,
          nextIdx: message.nextIdx
        },
        (selector) => {
          const found = resolvePierceSelector(selector, browserPierceEnv());
          return found.found ? adaptElement(found.element) : void 0;
        }
      );
      sendResponse({ ok: true, data: { ...result.data, newIndexMap: result.newIndexMap, newIndexMeta: result.newIndexMeta } });
    } catch (error) {
      sendResponse({ ok: false, error: describeError(error) });
    }
  }
  function handleAnnotate(message, sendResponse) {
    runAnnotate(message.question, message.candidates ?? []).then((data) => sendResponse({ ok: true, data })).catch((error) => sendResponse({ ok: false, error: describeError(error) }));
  }
  function resolveRoot(selector) {
    if (!selector) return document.body;
    return resolveRootElement(selector);
  }
  function describeError(error) {
    if (error instanceof Error) return error.message;
    return String(error);
  }
  function debounce(fn, waitMs) {
    let timer;
    const debounced = ((...args) => {
      if (timer !== void 0) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = void 0;
        fn(...args);
      }, waitMs);
    });
    debounced.cancel = () => {
      if (timer !== void 0) clearTimeout(timer);
      timer = void 0;
    };
    return debounced;
  }
  const RESIZE_DEBOUNCE_MS = 300;
  function notifyResize() {
    void chrome.runtime.sendMessage({ target: "background", type: "viewport.resized" }).catch(() => {
    });
  }
  let stop;
  function startViewportWatch() {
    if (stop) return;
    const debounced = debounce(notifyResize, RESIZE_DEBOUNCE_MS);
    const observer = new ResizeObserver(() => debounced());
    observer.observe(document.documentElement);
    stop = () => {
      observer.disconnect();
      debounced.cancel();
    };
  }
  registerContentListener();
  startViewportWatch();
})();
