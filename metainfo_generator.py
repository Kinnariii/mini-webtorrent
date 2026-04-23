"""
metainfo_generator.py  —  mini-webtorrent
Generates a .json metainfo file (analogous to a .torrent file).

Usage:
    python metainfo_generator.py <file> [--tracker URL] [--piece-size KB]

Example:
    python metainfo_generator.py video.mp4 --tracker http://127.0.0.1:5000/tracker --piece-size 512
"""

import hashlib
import json
import os
import argparse


def create_metainfo(file_path: str, tracker_url: str, piece_size_kb: int):
    piece_size = piece_size_kb * 1024
    piece_hashes = []

    if not os.path.exists(file_path):
        print(f"[Error] File '{file_path}' not found.")
        return

    file_size = os.path.getsize(file_path)
    print(f"File      : {file_path}")
    print(f"Size      : {file_size / 1024:.1f} KB  ({file_size} bytes)")
    print(f"Piece size: {piece_size_kb} KB")

    with open(file_path, "rb") as f:
        while True:
            piece = f.read(piece_size)
            if not piece:
                break
            piece_hashes.append(hashlib.sha1(piece).hexdigest())

    metainfo = {
        "tracker_url":   tracker_url,
        "file_name":     os.path.basename(file_path),
        "file_size":     file_size,
        "piece_size":    piece_size,
        "piece_hashes":  piece_hashes,
    }

    out_path = f"{file_path}.json"
    with open(out_path, "w") as f:
        json.dump(metainfo, f, indent=4)

    print(f"\nMetainfo  : {out_path}")
    print(f"Pieces    : {len(piece_hashes)}")
    print("Done! Share the .json file with all peers.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate mini-webtorrent metainfo file")
    parser.add_argument("file", help="Path to the file to share")
    parser.add_argument(
        "--tracker",
        default="http://127.0.0.1:5000/tracker",
        help="Tracker URL (default: http://127.0.0.1:5000/tracker)",
    )
    parser.add_argument(
        "--piece-size",
        type=int,
        default=256,
        help="Piece size in KB (default: 256)",
    )
    args = parser.parse_args()
    create_metainfo(args.file, args.tracker, args.piece_size)
