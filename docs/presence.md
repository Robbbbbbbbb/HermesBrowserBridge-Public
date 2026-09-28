# Presence — seeing Hermes work in your browser

What you see in the browser itself while Hermes is attached to a tab and
driving it, separate from the pairing/connection status the popup shows.
Three independent pieces, each with its own Options checkbox
(`showPresenceCursor`, `showActivityGlow`, `highlightSharedTabs` — all on by
default). Implementation: `extension/src/content/presence.ts` (the overlay's
DOM and CSS), `extension/src/content/presence-model.ts` (its behaviour and
timing), `extension/src/background/presence.ts` (the bridge from the capture
and action handlers), `extension/src/background/highlight.ts` (the tab group and
shared-tab count), `extension/src/background/background.ts`'s `paintBadge()`
(the sole toolbar-badge writer — see the badge precedence under
[Known limits](#honest-limits)).

## What you see

**Pointer.** A purple arrow (about 28 px tall, white outline, soft purple
halo) that shows where Hermes is in the page. Before every click, keypress
on a target, typing focus or form submit it glides to the exact point the
real event will land on — the arrow's tip is that point — at a pace that
scales with distance (0.45–0.9 s, eased, never faster), and the real event
is sent only when it arrives, so the ripple (a faint purple ring, twice)
and the click coincide. Its first appearance fades in at the target instead
of flying in. A scroll with no target moves it to the middle of the page
without a ripple; `navigate` shows no click. It stays at its last point
while the tab is shared, dims after about 8 s idle, and wakes on the next
action. It is removed when the tab is released.

**"Being viewed" glow.** Whenever Hermes is reading the page —
`dom.snapshot`, `page.read`, `page.screenshot`, `annotate`, and every
`page.act` (which snapshots before and after) — a soft purple glow sits on
the inside edges of the viewport and slowly breathes (about a 3 s cycle).
There is no hard border line; it fades toward the centre. It stays lit
while any of those calls is in flight and for about 4 s after the last one
ends, then fades out over about 1.5 s, so back-to-back calls read as one
continuous "being looked at". A purple-accented corner label names the
current verb: `viewing` (snapshot), `reading` (read), `looking`
(screenshot), `asking` (annotate), and for actions `clicking`, `typing`,
`selecting`, `submitting`, `scrolling`, `pressing key`, `navigating`,
`waiting`.

**Stop button.** The corner label is a pill — `● Hermes is viewing  [Stop]`,
or `● Hermes has this tab  [Stop]` when nothing is in flight — shown on
every shared tab (unless both presence settings are off), re-shown after
each navigation. The same Stop is on the keyboard: **Alt+Shift+S** (the
`stop-hermes` extension command; change it at
`chrome://extensions/shortcuts`), shown in the button's tooltip and the
popup. Pressing **Stop**
releases every tab shared from this browser (which also aborts whatever
Hermes was in the middle of) and pauses sharing, exactly like the popup's
"Pause sharing": the same pause, recorded as "stopped from the page" with
the time. The pill says "Stopped", the glow and pointer fade, and the
overlay goes away with the release. The popup's paused note then reads
"Stopped from the page at HH:MM"; "Resume sharing" there undoes the pause
(tabs have to be shared again). The gateway is told immediately, not at the
next heartbeat. While stopped (or paused from the popup), every request
Hermes makes is refused — including re-attaching a tab — with a message
saying you pressed Stop and when, and telling it to stop and ask you.
Sharing a tab yourself (the popup's Share buttons or the tab's right-click
menu) counts as consent: it resumes sharing and clears the pause. Only the
Stop button itself takes clicks — the rest of the pill, and the whole
overlay, is click-through — and only a real click counts: a click
synthesised by the page's own script is ignored. The button is mouse-only
(not in the Tab order, not rendered while the pill is hidden); the keyboard
path is the shortcut. Stop pauses first and releases second, so nothing can
re-attach in between, and a call already running when you press it returns
a "paused" refusal rather than anything it captured afterwards. The popup's
own Pause reaches the gateway immediately too.

**Never in Hermes' own screenshots.** Before `page.screenshot` captures, the
overlay is hidden (visibility, not removed), and the capture waits until the
content script confirms a frame without it has been painted; it is shown
again right after. If that confirmation doesn't arrive within 250 ms the
capture goes ahead anyway and its result carries a note saying the overlay
may be in the image.

**Shared tab group.** Every tab Hermes is attached to is collected into one
Chrome tab group titled **"Shared with Hermes"**, in purple (matching the pointer and glow), in
the tab strip of whichever window that tab lives in. A tab that was already
in one of your own groups is put back into it the moment it's released — and
if that original group no longer exists (Chrome deletes a group the instant
its last tab leaves it, which sharing the sole member of your own group can
itself trigger), the tab is plainly ungrouped instead of being left stuck in
"Shared with Hermes" — see [Known limits](#honest-limits) for the one restore
case that still doesn't fully hold.

**Badge.** The toolbar icon's badge shows one of four things, in a fixed
order of precedence — see [Known limits](#honest-limits) for the full
precedence table. The shared-tab count is one tier of it, visible whenever
it's nonzero and nothing more urgent (a pending approval, or paused sharing)
is currently showing.

## What each setting controls

| Setting | Default | Turns off |
|---|---|---|
| `showPresenceCursor` | on | The pointer and its click ripple — and with them the wait for the pointer before a click. Nothing else. |
| `showActivityGlow` | on | The "being viewed" edge glow and the corner verb label. Nothing else. |
| `highlightSharedTabs` | on | **Tab movement.** With this off, Hermes never calls `chrome.tabs.group`/`chrome.tabGroups.*` at all — no tab is ever regrouped, moved, or touched in the tab strip. The popup's own attached-tabs list stays the only way to see what's shared. |

The toolbar **badge's shared-tab tier is not gated by any of these** — the
count itself always reflects how many tabs are attached, independent of
whether you can see a cursor, glow, or tab group for them. Whether that
count is currently the *visible* thing on the badge depends on the
precedence below.

## Honest limits

**Clicks are slower on purpose.** Waiting for the pointer adds up to about
0.9 s per targeted action (the glide), bounded: the worker never waits more
than 1.05 s for the pointer, and not at all when the pointer setting is off
or the page has no content script (chrome://, Web Store, PDF viewer). A
presence failure can delay an action by at most that bound; it can never
fail or hang it.

**Reduced motion.** With the OS "reduce motion" preference on, the pointer
jumps instead of gliding (and the click is not delayed), the glow is a
steady soft glow with no breathing, and the ripple is a brief static ring.

**Costs.** Motion is CSS only (transform/opacity transitions and one CSS
animation for the breathing); there is no animation-frame loop and no
repeating timer. The glow's breathing animation runs only while it is lit
or fading.

**The on-page Stop button is a convenience; the shortcut and the popup are
authoritative.** The overlay lives in the page's own DOM. The page's script
cannot press Stop (untrusted clicks are ignored, and the button is in a
closed shadow root), but it can see that the overlay exists, hide the
host, or swallow your real click with its own capturing listener before it
reaches the button. A hostile page can therefore keep the on-page Stop
from working. The `stop-hermes` shortcut and the popup's Pause run in the
extension, outside the page, and no page can intercept them. On a Mac the
shortcut is Option+Shift+S, which normally types "Í"; while the command is
bound, Chrome takes the key instead — rebind it at
`chrome://extensions/shortcuts` if that gets in the way.

**"Around the window" is really "around the page."** A Chrome extension
cannot draw on the browser's own window chrome (title bar, tab strip
itself) — only inside a tab's own viewport, via a normal (if very
high-`z-index`) DOM overlay. "Glow around the window" is, literally, a
border drawn just inside the visible page content of that tab.

**A page that refuses content-script injection gets no overlay at all.**
`chrome://` pages, the Chrome Web Store, and the built-in PDF viewer all
refuse `chrome.scripting.executeScript` — the same restriction that already
applies to `browser_bridge_snapshot`/`read`/`act` on those pages. The cursor
and glow simply never appear there; `page.act` itself is unaffected (CDP
input dispatch doesn't go through the content script), so actions still
work, you just don't see them happen.

**The cursor/glow don't survive the instant of navigation.** A `navigate`
action (or any click that triggers one) destroys the content script's whole
JS world; the overlay is destroyed with it. It is recreated automatically —
lazily, on the very next presence event sent to that tab, the same way
`dom.snapshot`/`page.read` re-inject their own content script after every
navigation — but there is a brief window right at navigation where nothing
is drawn.

**Group restore has one gap: a service-worker restart between attach and
release.** MV3 kills the extension's background worker after ~30 seconds
idle. `highlight.ts` records a tab's pre-attach group only in memory (not
`chrome.storage.session`), because that fact — "what group was this tab in
right before Hermes touched it" — genuinely only exists at the moment attach
happens; there's no way to recover it after the fact. If the worker restarts
while a tab is attached, `cdp.ts`'s own resync logic re-adopts the tab as
attached (and, separately, re-syncs the toolbar badge's shared-tab count so
it doesn't undercount — see the badge precedence below) but does **not**
re-run the group-highlight hook (deliberately — see `cdp.ts`'s
`resyncAttachments()` and `highlight.ts`'s own header comment), so releasing
that tab later does not attempt to restore a group it no longer remembers.
The tab stays in "Shared with Hermes" until you move it yourself. This is a
real, occasional gap, not a hidden one — and distinct from the auto-deleted-
group case above, which IS handled (falls back to plain ungroup).

**The badge has one writer and a fixed precedence.** `background.ts`'s
`paintBadge()` is the only code in the extension that calls
`chrome.action.setBadgeText`/`setBadgeBackgroundColor` — `highlight.ts` only
maintains the shared-tab count and tells `background.ts` when it changes
(`onSharedCountChanged`). Precedence, highest first:

1. **Pending approval count** — a decision only you can make; always wins.
2. **Paused** — sharing is explicitly paused; "connected" alone would look
   identical whether or not it were.
3. **Shared-tab count**, while nonzero — more specific than plain connection
   state: "Hermes can see/act in N of your tabs right now."
4. **Connection state** (connected/connecting/pairing/disconnected/error) —
   the default, shown whenever nothing above applies (in particular,
   whenever no tab is currently shared).

A tier lower than the one currently showing never overwrites it; the badge
always reflects the highest tier that currently applies, repainted the
moment any tier's underlying state changes (a heartbeat, an approval push,
or an attach/release/resync).

**The glow shows only for calls that really run.** A `page.act` refused
before anything happens (an `ELEMENT_MISMATCH` pre-flight refusal) shows no
glow or label, on purpose. A worker restart mid-call can lose an "end"
message; each call's glow then self-heals after 20 s (annotate: its own
timeout plus 5 s).

**The overlay host is invisible to Hermes' own reads, not to the page's own
JavaScript.** The host lives outside `<body>` (a light-DOM child of `<html>`
itself) specifically so this extension's own snapshot/read/boxes walkers
never descend into it — see `presence.ts`'s header comment and
`tests/content/presence-invisibility.test.ts`. That guarantee is about
Hermes' own tools, not about the page. A script running on the page can
still notice Hermes is active: `document.getElementById` finds the host by
its id, and a `MutationObserver` watching `document.documentElement`'s
`childList` fires when it's added (mount) and removed (teardown). A page
that wanted to detect "Hermes is watching me right now" could.

**`page.read`'s safety here is structural, not a filter.**
`reader-walker.ts` (the code behind `browser_bridge_read`) has no
`aria-hidden`/hidden-attribute check at all, unlike the snapshot walker — it
only skips a fixed list of chrome-ish tags (`NAV`, `HEADER`, `FOOTER`,
`ASIDE`, `SCRIPT`, `STYLE`, `NOSCRIPT`, `TEMPLATE`, `SVG`). The overlay host
is safe from `page.read` only because it is given zero light-DOM children —
all of its actual content lives inside a closed shadow root, which
`textContent`/`childNodes` never reach, so an empty host produces nothing to
read. **This is load-bearing, not incidental**: if `presence.ts` is ever
changed to append a real light-DOM child to the host (an image fallback, a
`<noscript>`, anything not inside `shadow`), that content starts silently
appearing in every `page.read` call on every page. `presence.ts`'s own
`ensureMounted()` carries this warning at the point it would be violated,
and `tests/content/presence-invisibility.test.ts`'s canary test proves the
leak would happen (a host modeled with a light-DOM text child appears
directly in `collectBlocks`'s output) so this can't be reintroduced
silently.
