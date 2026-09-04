"""
Vercel entrypoint. Vercel's Python runtime (@vercel/python) looks for a
WSGI-compatible `app` object in this file and routes every request into it
(see ../vercel.json). The actual Flask app lives in ../app.py so the exact
same code runs locally (`python app.py`) and on Vercel - this file just
exposes it to Vercel's builder.
"""
import os
import sys

# Make the project root (one level up, where app.py / modl.py / templates /
# static / data live) importable, since this file sits inside api/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app  # noqa: E402  (Flask app, used as the WSGI callable)

# Some Vercel Python runtime versions look for `handler` instead of `app`;
# exposing both covers either case.
handler = app
