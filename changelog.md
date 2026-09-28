# Changelog

This doc contains changes, fixes, and features for public releases of the Browser Bridge plugin.

## v0.2.4 - 09/28/2026

### New
* Added Silent Fetch
  * New tool `browser_bridge_silent_fetch`
  * Uses your synced browser to capture its network fingerprint, so bot-managed APIs answer.
  * Works for searching, too!
* Heartbeat added to sense if synced browsers are alive and ready to respond to requests
  * Added ability to set priority of synced browsers and fallback
* Added [Documentation](https://github.com/Robbbbbbbbb/HermesBrowserBridge-Public/tree/main/docs) for plugin features

### Fixes
* Fixed a bug that disregarded origins
* Fixed a bug that allowed bypassing approval on sites set to always require

### Known Bugs
* None! Open a new Issue if you find one.
