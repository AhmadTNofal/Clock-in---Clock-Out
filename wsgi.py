"""Production entry point, served by Waitress.

    python wsgi.py

Waitress is a pure-Python WSGI server that runs happily as a Windows service via
NSSM or a Scheduled Task, which suits a small office better than setting up
IIS or nginx.

Threads are kept low deliberately: face inference is CPU-bound and serialised
behind a lock inside the engine, so extra threads add queueing, not throughput.

Camera access needs a secure origin. Options, cheapest first:

1. Run the kiosk browser on the same machine as the server and point it at
   http://localhost:8000 - localhost is treated as secure.
2. Put a reverse proxy with a self-signed or internal certificate in front, and
   trust that certificate on the kiosk machine.
"""

from __future__ import annotations

import os

from waitress import serve

from app import create_app

app = create_app("production")

if __name__ == "__main__":
    host = os.getenv("BIND_HOST", "0.0.0.0")
    port = int(os.getenv("BIND_PORT", "8000"))
    print(f"Serving the clocking system on http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)
