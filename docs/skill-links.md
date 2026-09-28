# Skill links — connecting the bridge to the skill library

Hermes keeps two kinds of knowledge about the same product: a **product
skill** (API/CLI knowledge — endpoints, object model, workflows) and, when
someone has driven that product through a browser tab, a **bridge
reference** (browser-specific knowledge — selectors, timing, wizard quirks)
saved under the local `browser-bridge` skill's `references/` directory.
Left alone, these never meet: an agent loads skills by subject ("ESXi"),
not by medium ("browser"), so it can sit on a tab for a whole session
without ever thinking to check whether a reference already covers it.

Skill links closes that gap by having the bridge notice the overlap and put
a pointer in front of the model, in both directions. It never edits a skill
itself — writes still go through Hermes's own skill management, with its
ledger and backups — it only detects, matches, and suggests. The full design
is `ProjectRules/skilllinks.md`; the agent-facing rules the bundled skill
teaches are `hermes_plugin/skill/SKILL.md` §0b ("Skills and products"). This page is the
operator's view: what the two result blocks mean, the header formats that
drive matching, how an origin earns a real header entry over time, how to
audit the whole graph, what to do when a skill can't be patched
automatically, and how to avoid (and detect) the one deploy mistake that
destroys a learned skill outright.

## The two blocks

### `skills` — on `attach`, `open_tab`, `snapshot`, and `navigate` results

Emitted per (session, origin) when a tab's origin has anything to report: a
matching product skill, a matching bridge reference, or neither. Two
examples, both real states the matcher actually produces:

A fully-linked origin, matched by its declared header — nothing left to do,
so `learn` is absent entirely:

```json
{
  "origin": "https://esxi01.example.lan",
  "matched_by": "origin",
  "load": [
    "skill_view('vmware-esxi')",
    "skill_view('browser-bridge', file_path='references/esxi-ui.md')"
  ],
  "product_skills": [{"name": "vmware-esxi", "description": "..."}],
  "bridge_references": [{"file": "references/esxi-ui.md", "heading": "ESXi / vCenter UI through the bridge"}]
}
```

A brand-new product, matched only by the page's own `<title>` (nothing
trusted matched) — `note` appears, `learn` nudges toward saving a lesson:

```json
{
  "origin": "https://new-console.example.lan",
  "matched_by": "page_title",
  "learn": "No bridge reference or product skill covers this yet. Save what works as references/<product>.md under browser-bridge with a products/origins header; link any other skill for it via related_skills: [browser-bridge].",
  "note": "matched on the page's own title, which the page controls — confirm it is the product before relying on it"
}
```

- **`matched_by`** says how confident the match is, most trusted first:
  `origin` (a reference/skill's own header named this exact origin), `used`
  (this exact origin has been recorded loading that reference or skill
  before, §"Observed bindings" below — also exact, just learned rather than
  authored), `hostname` (the origin's own host name shares a word with a
  known product term, e.g. `esxi01.example.lan` for a product named `esxi`
  — not page-controlled, so still trusted, but weaker than an exact origin).
  `page_title` means none of those matched and the page's own `<title>` was
  used instead — a hint, not a fact, since a page's title is content the
  site controls.
- **`load`** is copy-pasteable: the exact `skill_view(...)` calls to run.
- **`learn`** is a one-line nudge for whatever's missing (no product skill,
  no reference, or a reference and skill that exist but aren't linked yet).
  **Rule: `learn` is absent exactly when the origin is already fully
  linked** — nothing to do, so nothing to say.
- **`note`** carries the untrusted-title warning. **Rule: `note` appears
  only when `matched_by` is `page_title`** — every other `matched_by` rests
  on a non-page-controlled signal, so there's nothing to confirm.
- **`promote`** (added once an origin has been used enough times, see
  "Promotion to the origin tier" below) is a one-line nudge to turn that use
  into a real header entry.
- The first block of a session also carries `"manual":
  "skill_view('browser-bridge:browser-bridge')"`, because the bundled
  manual registers as a plugin skill and never appears in Hermes's own
  `<available_skills>` listing — but only when the plugin actually shipped
  one (see "Installing" below); a plugin dir with no `skill/SKILL.md` never
  points the model at a skill that was never registered.
- **Re-emission:** a block is normally sent once per (session, origin) and
  then stays quiet — but if what it would say actually *changes* (the match
  upgrades from `hostname` to `origin` because someone added a header
  mid-session, a new product skill starts overlapping, or a `promote`
  suggestion newly appears), the bridge sends a fresh block for that same
  (session, origin) rather than staying silent for the rest of the process.
  An unchanged repeat is still suppressed exactly as before.

### `browser_bridge` — on a `skill_view` of a product skill

```json
{
  "bridge_references": [{"file": "references/esxi-ui.md", "load": "skill_view('browser-bridge', file_path='references/esxi-ui.md')"}],
  "observed_origins": ["https://esxi01.example.lan"],
  "bridge_connected": true,
  "link": "missing",
  "suggested": ["When this task is done: Add browser-bridge to this skill's metadata.hermes.related_skills and one line under '## Via Browser Bridge' pointing to browser-bridge references/esxi-ui.md."]
}
```

- **`link`** is `"linked"` when both halves exist (the skill's
  `related_skills` names `browser-bridge`, and the reference's `skills:`
  header names the skill), otherwise `"missing"`.
- **`suggested`** is a list with one action per missing half, empty when
  linked, plus (see below) a `promote` action when this skill has one. Skill
  side: add `browser-bridge` to `related_skills` plus a `## Via Browser
  Bridge` line — or, when the skill can't be safely auto-patched, the
  reason-specific hint below instead of an edit. Reference side: add the
  skill to the reference's `skills:` header. Each entry starts with "Do
  now:" inside Hermes's background skill review and "When this task is
  done:" in a normal turn, so it never derails the user's task.
- A trailing `suggested` entry names a `promote` action (same one-liner as
  the `skills` block's `promote` field, see below) when this skill itself
  has a promotable origin.
- This key only appears on a SKILL.md view (not on a `file_path` view of a
  reference or an unrelated skill), and only when the skill has any overlap
  with a known product at all — no key means skill-links found nothing to
  say, not that it didn't run.

## Header formats

### A bridge reference (`references/<name>.md` under `browser-bridge`)

```yaml
---
products: [vmware-esxi, vmware-vcenter]      # canonical names
aliases: [esxi, vcenter, vsphere, vmware]    # extra match terms
origins: ["https://esxi01.example.lan"]      # exact origins this reference covers
skills: [vmware-esxi]                        # product skills covering this outside the browser
---
```

All four keys are optional, and a header at all is optional — both
references live today (`esxi-ui.md`, `marketplace-search.md`) predate this
scheme and are matched by inferring terms from the filename and the first
`#` heading instead. Add a header when you want a reference matched by
`origins` (exact, most reliable) rather than by inferred terms alone, or
when you want it to name specific product skills via `skills:`.

### A product skill, linked to the bridge

```yaml
metadata:
  hermes:
    related_skills: [browser-bridge]
    browser_bridge:
      products: [securityconsole]                          # optional: override/extend inferred terms
      origins: ["https://tenant.console.example.com"]       # optional: exact origins
```

and, in the body, a short pointer so a human (or the model, reading the
skill directly) sees the same thing:

```markdown
## Via Browser Bridge
See browser-bridge's references/security-console.md for driving the console UI directly.
```

`related_skills` is what actually makes the skill "linked" — the
`metadata.browser_bridge` block only sharpens matching. A skill can be
matched by shared terms (name/tags) without either, but stays `"missing"`
until `related_skills` names `browser-bridge`.

### Observed bindings

A `used` match doesn't need either header: once a session actually loads a
bridge reference while a tab on some origin is attached, the bridge records
that (origin, reference) pairing (capped at 500 rows, oldest evicted first)
and treats it as a trusted match from then on — the same way `origins:` in a
header would. This is how matching improves over time without anyone
hand-writing headers for every product.

## Promotion to the origin tier

An observed binding is already trusted (see above), but it's still an
implicit one — nobody can see it just by reading the reference's or skill's
own header. Once the SAME (origin, target) pair has been used **3 or more
times**, and the target doesn't already declare that origin, skill-links
suggests writing it down for real: a `promote` field/action naming the
origin, the use count, and where to add it —

```
This site has used references/esxi-ui.md 3 times. Add origins: ["https://esxi01.example.lan"] to that reference's header so it matches directly.
```

— or, for a product skill, the equivalent pointing at
`metadata.browser_bridge.origins`. A suggestion corroborated only by the
page's own title gets an extra sentence: *"It was matched by the page's
title, so confirm the site first."* Never the title text itself — only the
origin, the target, and the count, none of which the page authored.

`promote` shows up in three places once the threshold is crossed: the
`skills` block (one sentence, for the current origin), a product skill's
`browser_bridge.suggested` (one sentence, for that skill's own origins), and
`hermes browser-bridge skills`' `promote:` line per product group (every
candidate attached to that group, not just one). Counting is per
independent use — the same session re-loading the same reference or skill
over and over cannot cross the threshold by itself; it takes three genuinely
different sessions/tasks touching the same origin.

## Auditing the graph: `hermes browser-bridge skills`

Read-only, no side effects, no network, and no need for a paired device or a
running relay — a report of everything skill-links currently knows, one
section per product group (`products_index()`; rows with a missing link
sort first):

```
$ hermes browser-bridge skills
securityconsole
  references:  (none)
  skills:      console-querying (not linked, protected: unknown)
  origins:     https://tenant.console.example.com
  missing:     no bridge reference exists yet for this product (covered by: console-querying)
  promote:     (none)

vmware-esxi, vmware-vcenter
  references:  references/esxi-ui.md — ESXi / vCenter UI through the bridge
  skills:      vmware-esxi (linked, reference lists it, protected: unknown)
  origins:     https://esxi01.example.lan
  missing:     (none)
  promote:     This site has used references/esxi-ui.md 3 times. Add origins: ["https://esxi02.example.lan"] to that reference's header so it matches directly.

2 products, 1 missing link
Protected or unknown-writability skills can't be auto-linked — run `hermes curator adopt <name>` first (affects: console-querying).
```

- Each product skill entry shows **linked**/**not linked**, whether the
  reference's own `skills:` header names it back (**reference lists
  it**/**reference doesn't list it**, shown only when the group has a
  reference), and **protected: yes**/**no**/**unknown**
  (see below). `protected: unknown` above is the normal state whenever
  Hermes's own provenance helpers aren't reachable from wherever `hermes` is
  being run; on the real gateway most skills resolve to a definite yes/no.
- **`promote:`** lists every candidate attached to that group (§"Promotion
  to the origin tier" above), `(none)` when there isn't one — a second
  origin observed 3+ times for the same product (`esxi02` above) shows up
  here even though `esxi01` is already declared in the header.
- The final line is a one-line summary (`N products, M missing links`),
  followed by up to three reason-grouped hints (see below) for whatever
  unlinked skills the report found — never more than one line per reason
  group, and never a `hermes curator adopt`/`unpin` command for a skill that
  command can't actually help.
- A group whose reference has no header and no matching skill (so there's
  nothing to name it by) prints as `(unnamed — see <file>)` instead of a
  blank heading.
- An empty library (no local `browser-bridge` skill, no product skills with
  any bridge signal) prints one line naming the resolved skills root instead
  of an empty report, so it's obvious the command looked in the right place.
- If the local `browser-bridge/SKILL.md` looks like the bundled manual
  rather than Hermes's own learned skill (see "Installing" below), a
  `WARNING:` line prints first, before anything else — including for an
  otherwise-empty report.

Add `--json` for `products_index()`'s own return value, unchanged — useful
for scripting a check into a deploy or a periodic review rather than reading
the table by eye; the misplacement warning is a human-report line only and
never appears in `--json` output. Reach for this command when: a product
skill you expected to see linked isn't, a reference you just wrote doesn't
seem to be matching, or you just want to know what the bridge and the skill
library currently agree on without opening files by hand.

## When a product skill can't be auto-linked

Not every skill is safe to patch automatically. `skill-links` marks a skill
`protected` when it's bundled with a plugin, installed from the hub,
pinned, or otherwise outside Hermes's own curator management — patching
those directly would be overwritten on the next update or bypass the
provenance the curator relies on. Alongside `protected`, a `protected_reason`
names exactly *which* of those it is, so the hint points at the fix that
actually applies instead of always guessing "adopt it":

| `protected_reason` | What it means | The hint |
|---|---|---|
| `unmanaged` | Curation-eligible, but never adopted (no `created_by: agent` marker) | `hermes curator adopt <name>` |
| `pinned` | Adopted, but pinned against auto-transitions | `hermes curator unpin <name>` |
| `bundled` | Ships with a plugin | installed from outside the local library; an edit would be overwritten on update. Leave it unlinked — the reference's `skills:` header still links this side |
| `hub` | Installed from the Hub | same as `bundled` |
| `external` | Lives in `skills.external_dirs` | same as `bundled` |
| `None` (unknown) | Hermes's provenance helpers weren't reachable | the same `hermes curator adopt <name>` wording used before `protected_reason` existed |

The three externally-owned reasons (`bundled`/`hub`/`external`) get **no
command at all** — there is nothing to run, because the fix would be
overwritten on the next update regardless. For the two reversible ones:

- **`hermes curator adopt <name>`** (`unmanaged`) — brings the skill under
  curator management (ledger, backups, the normal `skill_manage`
  guardrails) so it can be patched like any other skill from then on.
  Prefer this when you expect to keep this product skill long-term.
- **`hermes curator unpin <name>`** (`pinned`) — drops the pin so the
  curator (and this auto-patch) can touch it again.
- **Edit by hand** — for any reason, add `related_skills: [browser-bridge]`
  and the `## Via Browser Bridge` line yourself, the same shape the
  auto-patch would have written (see the header formats above). Fine for a
  one-off, or for a skill you don't want under curator management at all —
  though a `bundled`/`hub`/`external` skill's hand edit is still at risk of
  being overwritten on that skill's own next update.

Either way, the reference's own `skills:` header can still name the skill
even though the skill can't name the reference back — the link is one-sided
until someone does one of the things above, and both the `skills` block and
`hermes browser-bridge skills` will keep showing it as `missing` until then.
`hermes browser-bridge skills`' own summary groups its hints the same way:
one line for `unmanaged`+unknown, one for `pinned`, one for the three
externally-owned reasons together.

## Installing

The bundled manual ships **inside the plugin directory**
(`hermes_plugin/skill/SKILL.md`), registered as the plugin skill
`browser-bridge:browser-bridge` — it is not, and must never become, the
local skill Hermes itself learns into at
`~/.hermes/skills/browser-bridge/SKILL.md`. Those are two different files
with two different jobs: the plugin's manual teaches the bridge's own
mechanics and never changes on its own; the local skill is where the
background reviewer saves product lessons (references, links) and changes
constantly.

**Never copy the manual into a skills directory.** A deploy that does —
even by mistake, even just once — silently overwrites whatever the
background reviewer had learned there: every reference, every link, gone,
with no error anywhere. `skill-links` detects this (never fixes it): on
plugin load it checks whether the local `browser-bridge/SKILL.md` is
byte-identical to the bundled manual, or its body starts with the manual's
own heading, and if so logs a warning and records a `bundled_manual_misplaced`
audit event (the path and both files' sizes only — never their content).
`hermes browser-bridge skills` prints the same warning first, before its own
report. Either way, the fix is the same: restore the learned skill from a
backup — the bridge only ever detects the mistake, it never attempts to
undo it.
