"""python -E -s -m independent_checker  <  {"path": ..., "kind": "pdf"|"image", "expect": {...}}"""
import json
import sys

from independent_checker.checker import run


def main() -> int:
    req = json.load(sys.stdin)
    try:
        result = run(req["path"], req.get("kind", "pdf"), req.get("expect") or {})
    except Exception as e:  # noqa: BLE001
        result = {"checker": "sparkprep-independent-checker/1", "checks": [], "ok": False,
                  "error": f"{type(e).__name__}: {e}"}
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
