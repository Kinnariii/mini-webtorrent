"""
tracker.py  —  mini-webtorrent tracker
Improvements over v1:
  • TTL-based peer eviction  (peers inactive > 90 s are removed)
  • Peer never sees itself in the returned list
  • /stats endpoint for dashboard / demo
  • Background cleanup thread
"""

from flask import Flask, request, jsonify
import time
import threading

app = Flask(__name__)

# torrents[file_name][peer_addr] = last_seen_timestamp
torrents: dict[str, dict[str, float]] = {}
torrents_lock = threading.Lock()

PEER_TTL = 90  # seconds — peers not seen in this window are evicted


# ─────────────────────────────────────────────
#  Background cleanup
# ─────────────────────────────────────────────
def evict_stale_peers():
    """Remove peers that haven't announced within PEER_TTL seconds."""
    while True:
        time.sleep(30)
        now = time.time()
        with torrents_lock:
            for file_name in list(torrents):
                stale = [p for p, ts in torrents[file_name].items()
                         if now - ts > PEER_TTL]
                for p in stale:
                    del torrents[file_name][p]
                    print(f"[Tracker] Evicted stale peer {p} for '{file_name}'")
                # clean up empty torrent entries
                if not torrents[file_name]:
                    del torrents[file_name]


cleanup_thread = threading.Thread(target=evict_stale_peers, daemon=True)
cleanup_thread.start()


# ─────────────────────────────────────────────
#  Announce endpoint
# ─────────────────────────────────────────────
@app.route("/tracker", methods=["GET"])
def tracker_announce():
    file_name = request.args.get("file_name")
    peer_port = request.args.get("port")
    peer_ip   = request.remote_addr
    peer_addr = f"{peer_ip}:{peer_port}"

    if not file_name or not peer_port:
        return jsonify({"error": "Missing required parameters"}), 400

    now = time.time()

    with torrents_lock:
        if file_name not in torrents:
            torrents[file_name] = {}

        # Return all *other* active peers before updating timestamp
        # ✅ FIX: peer never receives itself
        peer_list = [
            p for p, ts in torrents[file_name].items()
            if p != peer_addr and now - ts <= PEER_TTL
        ]

        # Register / refresh this peer
        torrents[file_name][peer_addr] = now

    print(f"[Tracker] Announce: {peer_addr} for '{file_name}' | "
          f"returning {len(peer_list)} peer(s)")

    return jsonify({"peers": peer_list})


# ─────────────────────────────────────────────
#  Stats endpoint  (great for demo!)
# ─────────────────────────────────────────────
@app.route("/stats", methods=["GET"])
def stats():
    now = time.time()
    with torrents_lock:
        result = {}
        for file_name, peers in torrents.items():
            active = {p: round(now - ts, 1)
                      for p, ts in peers.items()
                      if now - ts <= PEER_TTL}
            result[file_name] = {
                "active_peers": len(active),
                "peers": active,   # peer → seconds since last announce
            }
    return jsonify(result)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("[Tracker] Starting on 0.0.0.0:5000")
    print(f"[Tracker] Peer TTL: {PEER_TTL}s | Cleanup interval: 30s")
    app.run(host="0.0.0.0", port=5000, debug=False)
