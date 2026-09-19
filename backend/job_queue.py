"""RQ connection + queue, shared by server.py (enqueue side) and the worker
process (dequeue side, started as `rq worker` against this same REDIS_URL).
"""
import os

import redis
from rq import Queue

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

redis_conn = redis.from_url(REDIS_URL)
job_queue = Queue("sparkprep", connection=redis_conn, default_timeout=600)
