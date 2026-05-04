"""
app/main.py — The API Service

This is the HTTP server that swiftdeploy manages. It reads its own
behaviour from environment variables injected by docker-compose.

How it works:
- Reads MODE, APP_VERSION, APP_PORT from environment at startup
- Runs in "stable" or "canary" mode (same image, different behaviour)
- Canary mode: adds X-Mode: canary header to every response
- Chaos endpoint only works in canary mode
- Uses Python's built-in http.server (no heavy frameworks = small image)
"""

import os
import json
import time
import random
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# ── Read config from environment (injected by Docker / docker-compose) ──────
MODE        = os.environ.get("MODE", "stable")          # "stable" or "canary"
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")    # version string
APP_PORT    = int(os.environ.get("APP_PORT", 3000))     # port to listen on

START_TIME  = time.time()   # captured at startup, used for uptime calculation

# ── Global chaos state (only active in canary mode) ─────────────────────────
# This dict is shared across requests (protected by a lock for thread safety)
chaos_state = {"mode": None, "duration": 0, "rate": 0.0}
chaos_lock  = threading.Lock()


def json_response(handler, status: int, body: dict):
    """
    Helper: sends a JSON HTTP response.
    - Encodes body dict → UTF-8 bytes
    - Sets Content-Type header
    - In canary mode, also sets X-Mode: canary header
    """
    payload = json.dumps(body).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))

    # Canary mode signals itself via this header on EVERY response
    if MODE == "canary":
        handler.send_header("X-Mode", "canary")

    handler.end_headers()
    handler.wfile.write(payload)


class AppHandler(BaseHTTPRequestHandler):
    """
    The request handler. Each incoming HTTP request creates one instance.
    do_GET / do_POST are called automatically by HTTPServer based on method.
    """
    def do_HEAD(self):
        # HEAD is like GET but returns headers only, no body.
        # curl -I uses HEAD. We just respond 200 with no body.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if MODE == "canary":
           self.send_header("X-Mode", "canary")
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress default stderr logging (Nginx handles access logs)
        pass

    # ── GET / ────────────────────────────────────────────────────────────────
    def handle_root(self):
        """
        Welcome endpoint. Returns:
        - current mode (stable/canary)
        - app version
        - server timestamp in ISO format
        """
        json_response(self, 200, {
            "message": f"Welcome to SwiftDeploy API — running in {MODE} mode",
            "mode":    MODE,
            "version": APP_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ── GET /healthz ─────────────────────────────────────────────────────────
    def handle_healthz(self):
        """
        Liveness check. Used by:
        - Docker's HEALTHCHECK directive (every 30s)
        - swiftdeploy deploy (polls this until UP before unblocking)
        - swiftdeploy promote (confirms new mode after restart)

        Returns uptime in seconds (float) so operators can see how long
        the process has been running without restarting.
        """
        uptime = round(time.time() - START_TIME, 2)
        json_response(self, 200, {
            "status": "ok",
            "mode":   MODE,
            "uptime_seconds": uptime,
        })

    # ── POST /chaos ───────────────────────────────────────────────────────────
    def handle_chaos(self):
        """
        Chaos injection — ONLY available in canary mode.
        Accepts a JSON body and updates the global chaos_state.

        Three modes:
          {"mode": "slow",    "duration": N}  → sleeps N seconds before replying
          {"mode": "error",   "rate": 0.5}    → 50% of future requests get HTTP 500
          {"mode": "recover"}                 → clears all chaos, back to normal

        Why canary only? Canary is the "test" deployment. You inject chaos
        there to see how your system handles failures, without breaking stable.
        """
        if MODE != "canary":
            json_response(self, 403, {"error": "Chaos endpoint only available in canary mode"})
            return

        # Read and parse request body
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            json_response(self, 400, {"error": "Invalid JSON body"})
            return

        chaos_mode = body.get("mode")

        with chaos_lock:
            if chaos_mode == "slow":
                duration = int(body.get("duration", 1))
                chaos_state.update({"mode": "slow", "duration": duration, "rate": 0.0})
                json_response(self, 200, {"status": "chaos activated", "mode": "slow", "duration": duration})

            elif chaos_mode == "error":
                rate = float(body.get("rate", 0.5))
                chaos_state.update({"mode": "error", "duration": 0, "rate": rate})
                json_response(self, 200, {"status": "chaos activated", "mode": "error", "rate": rate})

            elif chaos_mode == "recover":
                chaos_state.update({"mode": None, "duration": 0, "rate": 0.0})
                json_response(self, 200, {"status": "chaos cleared"})

            else:
                json_response(self, 400, {"error": f"Unknown chaos mode: {chaos_mode}"})

    # ── Router: dispatch GET requests by path ─────────────────────────────────
    def do_GET(self):
        # Apply chaos BEFORE processing (slow/error affects all endpoints)
        if not self._apply_chaos():
            return  # chaos returned an error response already

        if self.path == "/":
            self.handle_root()
        elif self.path == "/healthz":
            self.handle_healthz()
        else:
            json_response(self, 404, {"error": "Not found"})

    # ── Router: dispatch POST requests by path ────────────────────────────────
    def do_POST(self):
        if not self._apply_chaos():
            return

        if self.path == "/chaos":
            self.handle_chaos()
        else:
            json_response(self, 404, {"error": "Not found"})

    # ── Chaos middleware ───────────────────────────────────────────────────────
    def _apply_chaos(self) -> bool:
        """
        Called before every request handler.
        Returns True  → continue processing normally
        Returns False → chaos already sent an error response, stop here

        This is the "middleware" pattern: intercept the request, decide
        whether to let it through or short-circuit with an error.
        """
        with chaos_lock:
            mode = chaos_state["mode"]

            if mode == "slow":
                # Sleep BEFORE responding — simulates a slow upstream
                time.sleep(chaos_state["duration"])

            elif mode == "error":
                # Randomly fail based on configured rate
                if random.random() < chaos_state["rate"]:
                    json_response(self, 500, {"error": "Chaos: simulated server error"})
                    return False  # stop here, don't call the real handler

        return True  # no chaos, proceed normally


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    print(f"[swiftdeploy] API running on port {APP_PORT} | mode={MODE} | version={APP_VERSION}")
    server.serve_forever()