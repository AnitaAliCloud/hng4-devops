import os
import json
import time
import random
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

MODE        = os.environ.get("MODE", "stable")
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
APP_PORT    = int(os.environ.get("APP_PORT", 3000))
START_TIME  = time.time()

chaos_state = {"mode": None, "duration": 0, "rate": 0.0}
chaos_lock  = threading.Lock()

metrics_lock      = threading.Lock()
request_counts    = {}
request_durations = {}

HISTOGRAM_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


def record_request(method, path, status_code, duration):
    with metrics_lock:
        key = (method, path, str(status_code))
        request_counts[key] = request_counts.get(key, 0) + 1
        dur_key = (method, path)
        if dur_key not in request_durations:
            request_durations[dur_key] = []
        request_durations[dur_key].append(duration)


def format_prometheus_metrics():
    lines = []
    with metrics_lock:
        lines.append("# HELP http_requests_total Total HTTP requests")
        lines.append("# TYPE http_requests_total counter")
        for (method, path, status_code), count in request_counts.items():
            lines.append(
                f'http_requests_total{{method="{method}",path="{path}",status_code="{status_code}"}} {count}'
            )
        lines.append("# HELP http_request_duration_seconds Request latency")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for (method, path), durations in request_durations.items():
            total_count = len(durations)
            total_sum   = sum(durations)
            for bucket in HISTOGRAM_BUCKETS:
                bucket_count = sum(1 for d in durations if d <= bucket)
                lines.append(
                    f'http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="{bucket}"}} {bucket_count}'
                )
            lines.append(
                f'http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="+Inf"}} {total_count}'
            )
            lines.append(
                f'http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {total_sum}'
            )
            lines.append(
                f'http_request_duration_seconds_count{{method="{method}",path="{path}"}} {total_count}'
            )

    uptime = time.time() - START_TIME
    lines.append("# HELP app_uptime_seconds Seconds since app started")
    lines.append("# TYPE app_uptime_seconds gauge")
    lines.append(f"app_uptime_seconds {uptime:.2f}")

    mode_value = 1 if MODE == "canary" else 0
    lines.append("# HELP app_mode Current deployment mode (0=stable, 1=canary)")
    lines.append("# TYPE app_mode gauge")
    lines.append(f"app_mode {mode_value}")

    with chaos_lock:
        chaos_mode = chaos_state["mode"]
    if chaos_mode == "slow":
        chaos_value = 1
    elif chaos_mode == "error":
        chaos_value = 2
    else:
        chaos_value = 0
    lines.append("# HELP chaos_active Current chaos state (0=none, 1=slow, 2=error)")
    lines.append("# TYPE chaos_active gauge")
    lines.append(f"chaos_active {chaos_value}")

    return "\n".join(lines) + "\n"


def json_response(handler, status, body):
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    if MODE == "canary":
        handler.send_header("X-Mode", "canary")
    handler.end_headers()
    handler.wfile.write(payload)


class AppHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def handle_root(self):
        json_response(self, 200, {
            "message": f"Welcome to SwiftDeploy API — running in {MODE} mode",
            "mode":    MODE,
            "version": APP_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def handle_healthz(self):
        uptime = round(time.time() - START_TIME, 2)
        json_response(self, 200, {
            "status": "ok",
            "mode":   MODE,
            "uptime_seconds": uptime,
        })

    def handle_metrics(self):
        payload = format_prometheus_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_chaos(self):
        if MODE != "canary":
            json_response(self, 403, {"error": "Chaos endpoint only available in canary mode"})
            return
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

    def do_GET(self):
        start = time.time()
        if not self._apply_chaos():
            record_request("GET", self.path, 500, time.time() - start)
            return
        if self.path == "/":
            self.handle_root()
            status = 200
        elif self.path == "/healthz":
            self.handle_healthz()
            status = 200
        elif self.path == "/metrics":
            self.handle_metrics()
            return
        else:
            json_response(self, 404, {"error": "Not found"})
            status = 404
        record_request("GET", self.path, status, time.time() - start)

    def do_POST(self):
        start = time.time()
        if not self._apply_chaos():
            record_request("POST", self.path, 500, time.time() - start)
            return
        if self.path == "/chaos":
            self.handle_chaos()
            status = 200
        else:
            json_response(self, 404, {"error": "Not found"})
            status = 404
        record_request("POST", self.path, status, time.time() - start)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if MODE == "canary":
            self.send_header("X-Mode", "canary")
        self.end_headers()

    def _apply_chaos(self):
        with chaos_lock:
            mode = chaos_state["mode"]
            if mode == "slow":
                time.sleep(chaos_state["duration"])
            elif mode == "error":
                if random.random() < chaos_state["rate"]:
                    json_response(self, 500, {"error": "Chaos: simulated server error"})
                    return False
        return True


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    print(f"[swiftdeploy] API running on port {APP_PORT} | mode={MODE} | version={APP_VERSION}")
    server.serve_forever()