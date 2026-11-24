#!/usr/bin/env python3
"""NFC reader bridge: sends card taps from a USB PC/SC reader (e.g. ACR122U) to the Participation web app.

    pip install pyscard
    python nfc_bridge.py --server https://your-app.vercel.app --token nfcp_xxx

Create the token in the web app under  Account -> Reader bridge.
Taps go into YOUR currently open session (start one in the browser first).
No reader at hand?  Use  --simulate  and type UIDs on the keyboard.
Only the Python standard library is needed apart from pyscard (not needed for --simulate).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]  # PC/SC pseudo-APDU: read card UID


def beep(ok):
    try:
        import winsound  # Windows only
        winsound.Beep(1000 if ok else 300, 120 if ok else 300)
    except Exception:
        sys.stdout.write("\a")
        sys.stdout.flush()


def http(server, token, path, payload=None):
    req = urllib.request.Request(
        server.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "nfc-bridge/1.0"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "message": f"HTTP {e.code}"}


def read_uid(reader):
    """Return the UID of the card on the reader as upper-case hex, or None when no card is present."""
    try:
        conn = reader.createConnection()
        conn.connect()
        data, sw1, sw2 = conn.transmit(GET_UID)
        conn.disconnect()
        return "".join(f"{b:02X}" for b in data) if data and (sw1, sw2) == (0x90, 0x00) else None
    except Exception:
        return None


def send(args, uid):
    try:
        res = http(args.server, args.token, "/api/bridge/scan", {"uid": uid})
    except Exception as e:  # network down etc. – keep running
        print(f"  ! could not reach server: {e}")
        beep(False)
        return
    ok = bool(res.get("ok"))
    print(f"  [{'OK' if ok else 'NO'}] {res.get('message', res)}")
    beep(ok)


def run_simulated(args):
    print("Simulation mode – type a card UID and press Enter (Ctrl+C to quit).")
    for line in sys.stdin:
        uid = line.strip()
        if uid:
            send(args, uid)


def run_reader(args):
    try:
        from smartcard.System import readers
    except ImportError:
        sys.exit("pyscard is not installed. Run:  pip install pyscard   (or use --simulate)")
    last, gone_at = None, None  # `last`: card currently on the reader; `gone_at`: when it was first missed
    print("Waiting for cards… (Ctrl+C to quit)")
    while True:
        rs = readers()
        if not rs:
            print("No NFC reader found – plug it in. Retrying…")
            time.sleep(3)
            continue
        uid = read_uid(rs[min(args.reader, len(rs) - 1)])
        if uid is None:
            if last is not None:
                if gone_at is None:
                    gone_at = time.time()
                elif time.time() - gone_at > args.release:
                    last, gone_at = None, None  # card removed -> the next tap counts again
        else:
            gone_at = None
            if uid != last:
                last = uid
                print(f"[{time.strftime('%H:%M:%S')}] card {uid}")
                send(args, uid)
        time.sleep(args.interval)


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and would crash on non-ASCII
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True, help="Base URL, e.g. https://your-app.vercel.app or http://127.0.0.1:5000")
    p.add_argument("--token", required=True, help="API token from Account -> Reader bridge")
    p.add_argument("--reader", type=int, default=0, help="Reader index if several are attached (default 0)")
    p.add_argument("--interval", type=float, default=0.25, help="Polling interval in seconds")
    p.add_argument("--release", type=float, default=1.0, help="Seconds without a card before the same card may tap again")
    p.add_argument("--simulate", action="store_true", help="Read UIDs from the keyboard instead of a reader")
    args = p.parse_args()

    info = http(args.server, args.token, "/api/bridge/ping")
    if not info.get("ok"):
        sys.exit(f"Server rejected the token: {info.get('message')}")
    sess = info.get("session")
    print(f"Connected as {info['user']}. " + (f"Open session: {sess['name']} ({sess['subject']})" if sess else "No session is open yet – start one in the browser."))
    try:
        (run_simulated if args.simulate else run_reader)(args)
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
