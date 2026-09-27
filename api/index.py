"""
Vercel entrypoint. Vercel's Python runtime looks for a WSGI-compatible
`app` object in this file and routes every request into it (see
../vercel.json). The actual Flask app lives in ../app.py so the exact
same code runs locally (`python app.py`) and on Vercel - this file just
exposes it to Vercel's builder.

If importing the real app fails for any reason (a missing dependency, a
misconfigured path, a bad environment variable), this falls back to a
tiny diagnostic Flask app that reports the actual Python exception and
traceback as JSON instead of Vercel's generic, unhelpful "FUNCTION_
INVOCATION_FAILED" - so the real problem is visible immediately instead
of needing guesswork.
"""
import os
import sys
import traceback

# Make the project root (one level up, where app.py / modl.py / templates /
# static / data live) importable, since this file sits inside api/.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from app import app  # noqa: E402  (the real Flask app)
except Exception:
    _startup_traceback = traceback.format_exc()
    print("[api/index.py] FAILED TO IMPORT app.py:\n" + _startup_traceback)

    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/", defaults={"_path": ""})
    @app.route("/<path:_path>")
    def _startup_error(_path):
        return jsonify({
            "ok": False,
            "error": "The app failed to start on Vercel. This is almost always "
                     "a missing dependency, a missing environment variable, or "
                     "a file that wasn't included in the deployment.",
            "traceback": _startup_traceback.splitlines(),
        }), 500

# Some Vercel Python runtime versions look for `handler` instead of `app`;
# exposing both covers either case.
handler = app
