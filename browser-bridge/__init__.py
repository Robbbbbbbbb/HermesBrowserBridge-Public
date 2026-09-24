"""browser_bridge — Hermes plugin entrypoint.

Registers the tool surface, the ``hermes browser`` CLI, the bundled skill, and
starts the WebSocket relay that the Chrome extension dials into.

Design notes that differ from plan.md §3.2 (which was written against docs, not
code — see Documentation/plugin-api-findings.md):

* ``ctx.register_approval_transport`` DOES exist on the gateway's real install
  (v0.21.4, ``/usr/local/lib/hermes-agent``) -- an earlier audit checked the
  wrong source tree (v0.19.1 at ``~/.hermes/hermes-agent``) and reported it
  missing. This plugin registers "browser-bridge" as a transport below; it
  stays inactive until the operator sets ``security.approval.transport:
  browser-bridge`` in config.yaml (see docs/security.md). This plugin's OWN
  `request`-mode capability approvals (act/fetch/cookies/network/ask) do NOT
  depend on that selection -- see ``hermes_plugin/approvals.py``'s module
  docstring for why.
* ``ctx.inject_message`` only works in an interactive CLI process; in the
  gateway it logs and returns False. Async bridge events therefore surface
  through ``browser_bridge_status`` and tool results rather than being pushed.
* There is no gateway-startup hook, so the relay starts here, on its own daemon
  thread with a private event loop.

Every registration is individually guarded: a browser-bridge failure must never
take down the gateway that hosts it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import approvals, audit, cli, config, relay, skill_hooks, state, tools

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


def register(ctx) -> None:
    """Called by the Hermes plugin loader inside the gateway (and CLI) process."""
    try:
        state.connect()
    except Exception:
        # Degrade rather than take the whole registration down: every
        # DB-backed call below already guards itself (see state.py callers),
        # so the plugin still loads with pairing/status simply reporting
        # "no devices" until the DB comes back.
        logger.exception("browser_bridge: state.connect failed; DB-backed registration degraded")

    registered: list[str] = []
    try:
        registered = tools.register_tools(ctx)
    except Exception:
        logger.exception("browser_bridge: tool registration failed")

    try:
        # Name is "browser-bridge", NOT "browser". Hermes core (v0.21.x) ships
        # its own `hermes browser` subcommand (real-Chrome-profile helpers /
        # close-profile), and colliding with it makes this plugin's whole CLI
        # unreachable with no warning anywhere.
        #
        # The mechanism is worth stating exactly, because the obvious guess is
        # wrong: it is NOT argparse overwriting our subparser. Core registers
        # `browser` at main.py:3369, BEFORE plugin CLI registration at :3434,
        # so a plain overwrite would have let us win. The real cause is
        # `_plugin_cli_discovery_needed()` (main.py:2835): it compares the first
        # positional argv against `_BUILTIN_SUBCOMMANDS`, and because "browser"
        # is in that set, `hermes browser ...` short-circuits and never calls
        # discover_plugins() at all — our register_cli_command() never runs for
        # that invocation. So any name already in _BUILTIN_SUBCOMMANDS is
        # unusable by a plugin, no matter what order things register in.
        # Verified live against the gateway's v0.21.4 install. Before renaming
        # this, check the new name against `hermes --help`'s full command list.
        ctx.register_cli_command(
            name="browser-bridge",
            help="Browser bridge: pair Chrome devices, manage grants, read the audit log",
            description="Pair and manage the Chrome devices Hermes can see and act in.",
            setup_fn=cli.setup_cli,
            handler_fn=cli.handle_cli,
        )
    except Exception:
        logger.exception("browser_bridge: CLI registration failed")

    try:
        # Inactive until the operator opts in via config.yaml's
        # security.approval.transport: browser-bridge -- registering it here
        # never changes that config itself (see approvals.py's module
        # docstring for what "active" means and why this plugin's own
        # request-mode gate doesn't need it anyway).
        ctx.register_approval_transport(approvals.TRANSPORT_NAME, approvals.present)
    except Exception:
        logger.exception("browser_bridge: approval transport registration failed")

    skill_path = Path(__file__).resolve().parent / "skill" / "SKILL.md"
    if skill_path.exists():
        try:
            ctx.register_skill(
                name="browser-bridge",
                path=skill_path,
                description="How to drive the user's real Chrome through the Hermes Browser Bridge.",
            )
        except Exception:
            logger.exception("browser_bridge: skill registration failed")

    try:
        # Skill Links (ProjectRules/skilllinks.md SL3): annotates skill_view
        # results with what the bridge knows about a product skill's
        # counterpart reference, and invalidates that index on skill writes.
        skill_hooks.register(ctx)
    except Exception:
        logger.exception("browser_bridge: skill-link hooks registration failed")

    started = False
    relay_error = ""
    try:
        if relay.is_gateway_process():
            relay_obj = relay.start_relay()
            if relay_obj is not None:
                relay_status = relay_obj.status()
                # A bind failure still sets Relay._ready, so "the relay object
                # exists" is not the same as "it is listening" — check the
                # real outcome rather than reporting success unconditionally.
                started = bool(relay_status["listening"])
                relay_error = relay_status.get("error", "")
                if started:
                    _register_relay_shutdown(ctx, relay_obj)
        else:
            logger.debug("browser_bridge: not the gateway process — relay not started here")
    except Exception:
        logger.exception("browser_bridge: relay failed to start")

    cfg = config.load()
    try:
        device_count = len(state.list_devices())
    except Exception:
        device_count = 0
    audit.record(
        "plugin_loaded",
        version=__version__,
        tools=registered,
        relay_started=started,
        relay_error=relay_error,
        port=cfg["port"],
        devices=device_count,
    )
    logger.info(
        "browser_bridge %s loaded: %d tool(s), relay=%s%s",
        __version__, len(registered), "up" if started else "down",
        f" ({relay_error})" if relay_error else "",
    )


def _register_relay_shutdown(ctx, relay_obj) -> None:
    """Release the relay thread and its bound port when the plugin unloads.

    Without this, a plugin reload (``hermes plugins reload browser-bridge``, a
    profile switch, a force-reload) leaves the old daemon thread alive holding
    port 8765. The next ``register()`` then either fails to bind — and, because
    a bind failure is non-fatal by design, degrades silently to "no relay" —
    or ends up with two listeners racing for the same connections. Neither
    surfaces as an error anyone would notice.

    ``ctx.on_unload`` runs cleanup callbacks in reverse acquisition order. It
    is guarded because it is newer than the rest of the surface this plugin
    uses: an older host without it should still load the plugin, just without
    clean reload semantics.
    """
    on_unload = getattr(ctx, "on_unload", None)
    if on_unload is None:
        logger.debug("browser_bridge: host has no ctx.on_unload; relay will leak on reload")
        return

    def _shutdown() -> None:
        try:
            relay_obj.stop()
            audit.record("relay_stopped", reason="plugin unload")
        except Exception:
            logger.exception("browser_bridge: relay shutdown failed")

    try:
        on_unload(_shutdown)
    except Exception:
        logger.exception("browser_bridge: could not register relay shutdown")
