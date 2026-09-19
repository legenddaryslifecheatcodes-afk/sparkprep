"""Entrypoint for the RQ worker Render service.

There's no dedicated "background worker" service type available for this
deploy, so this runs as a Web Service like the main app -- which means
Render's health check needs something listening on $PORT, even though this
process's actual job is running `rq worker`, not serving HTTP. A trivial
health server runs on a background thread while the RQ worker blocks the
main thread, same process either way.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from redis import Redis
from rq import Worker

from job_queue import REDIS_URL


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # don't spam the worker's logs with health-check hits


def _serve_health():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_serve_health, daemon=True).start()
    conn = Redis.from_url(REDIS_URL)
    Worker(["sparkprep"], connection=conn).work()
