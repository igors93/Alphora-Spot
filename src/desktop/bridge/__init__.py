"""JSON-lines bridge between the native (Rust) desktop interface and the Python core.

The interface starts ``python -m desktop.bridge`` and exchanges one JSON object
per line over stdin/stdout:

* request:  ``{"id": 7, "method": "symbols.rows", "params": {...}}``
* response: ``{"id": 7, "ok": true, "result": ...}`` or
  ``{"id": 7, "ok": false, "error": {"title": "...", "message": "..."}}``
* event:    ``{"event": "ready", "data": {...}}`` (sent once at startup)

The bridge never starts long-running processes itself. It validates settings, builds
launch plans (command + environment) and parses results; the interface owns the
lifecycle of the engine, backtest and training processes.
"""
