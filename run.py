"""Development entry point.

    python run.py

Serves on http://127.0.0.1:5000. Use localhost rather than the LAN IP while
developing: browsers only grant camera access on a secure origin, and localhost
counts as one whereas a plain-http LAN address does not.

For production use wsgi.py with Waitress.
"""

from __future__ import annotations

from app import create_app

app = create_app("development")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
