"""Vercel / WSGI entrypoint.  Local dev:  python app.py   (http://127.0.0.1:5000)"""
from participation import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
