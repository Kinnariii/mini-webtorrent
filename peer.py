import socket
import json
import requests
import hashlib
import threading
import time
import os
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich import box
import datetime

console = Console()

# ─────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────
pieces_lock = threading.Lock()
stats = {
    "downloaded": 0,   # pieces downloaded this session
    "uploaded":   0,   # pieces served to others
    "peers_seen": set(),
    "start_time": time.time(),
}

# ─────────────────────────────────────────────
#  Uploader  (serves pieces to other peers)
# ─────────────────────────────────────────────
def handle_peer_request(client_socket, addr, file_path, piece_size, file_size):
    """Handle a single peer's piece request in its own thread."""
    try:
        message = client_socket.recv(1024).decode().strip()
        if message.startswith("GET_PIECE:"):
            piece_index = int(message.split(":")[1])

            # ✅ FIX: compute the real size of this piece (last piece may be smaller)
            offset = piece_index * piece_size
            actual_piece_size = min(piece_size, file_size - offset)

            with open(file_path, "rb") as f:
                f.seek(offset)
                piece_data = f.read(actual_piece_size)

            client_socket.sendall(piece_data)

            with pieces_lock:
                stats["uploaded"] += 1
                stats["peers_seen"].add(addr[0])

            console.log(f"[green][Uploader][/green] Served piece {piece_index} ({actual_piece_size} B) to {addr[0]}")
    except Exception as e:
        console.log(f"[red][Uploader] Error handling {addr}: {e}[/red]")
    finally:
        client_socket.close()


def run_uploader(port, file_path, piece_size, file_size):
    """TCP server that listens for piece requests from peers."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("", port))
    server_socket.listen(10)
    console.log(f"[cyan][Uploader][/cyan] Listening on port {port}")

    with ThreadPoolExecutor(max_workers=10) as executor:
        while True:
            try:
                client_socket, addr = server_socket.accept()
                executor.submit(handle_peer_request, client_socket, addr, file_path, piece_size, file_size)
            except Exception as e:
                console.log(f"[red][Uploader] Accept error: {e}[/red]")


# ─────────────────────────────────────────────
#  Download a single piece from a single peer
# ─────────────────────────────────────────────
def download_piece(peer_addr, piece_index, piece_size, file_size, expected_hash):
    """
    Connects to peer_addr, requests piece_index, verifies SHA1.
    Returns (piece_index, piece_data) on success, raises on failure.
    """
    peer_ip, peer_port = peer_addr.split(":")

    # ✅ FIX: compute actual piece size for last piece
    offset = piece_index * piece_size
    actual_piece_size = min(piece_size, file_size - offset)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)  # ✅ FIX: don't hang forever on dead peers
    try:
        s.connect((peer_ip, int(peer_port)))
        s.sendall(f"GET_PIECE:{piece_index}".encode())

        piece_data = b""
        while len(piece_data) < actual_piece_size:
            chunk = s.recv(actual_piece_size - len(piece_data))
            if not chunk:
                break
            piece_data += chunk
    finally:
        s.close()

    if hashlib.sha1(piece_data).hexdigest() != expected_hash:
        raise ValueError(f"Hash mismatch for piece {piece_index} from {peer_addr}")

    return piece_index, piece_data


# ─────────────────────────────────────────────
#  Downloader  (fetches missing pieces in parallel)
# ─────────────────────────────────────────────
def run_downloader(metainfo, my_port, is_seeder):
    file_name   = metainfo["file_name"]
    file_size   = metainfo["file_size"]
    piece_size  = metainfo["piece_size"]
    piece_hashes = metainfo["piece_hashes"]
    num_pieces  = len(piece_hashes)
    tracker_url = metainfo["tracker_url"]

    # Initialise file on disk
    if not is_seeder and not os.path.exists(file_name):
        with open(file_name, "wb") as f:
            f.truncate(file_size)

    # ✅ FIX: thread-safe piece tracking with a lock
    my_pieces = [is_seeder] * num_pieces

    # ── Rich live dashboard ──────────────────
    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=3),
        Layout(name="progress",size=num_pieces + 4),
        Layout(name="stats",   size=8),
    )

    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
        TextColumn("({task.completed}/{task.total} pieces)"),
        console=console,
    ) as progress:

        dl_task = progress.add_task("Downloading", total=num_pieces,
                                    completed=sum(my_pieces))

        def print_stats(peers):
            elapsed = int(time.time() - stats["start_time"])
            table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
            table.add_row("[bold]File[/bold]",        file_name)
            table.add_row("[bold]Size[/bold]",        f"{file_size / 1024:.1f} KB")
            table.add_row("[bold]Pieces[/bold]",      f"{num_pieces} × {piece_size // 1024} KB")
            table.add_row("[bold]Active peers[/bold]",str(len(peers)))
            table.add_row("[bold]Downloaded[/bold]",  str(stats["downloaded"]))
            table.add_row("[bold]Uploaded[/bold]",    str(stats["uploaded"]))
            table.add_row("[bold]Elapsed[/bold]",     f"{elapsed}s")
            console.print(Panel(table, title="[bold magenta]mini-webtorrent[/bold magenta]",
                                border_style="magenta"))

        while True:
            with pieces_lock:
                done = sum(my_pieces)

            progress.update(dl_task, completed=done)

            if done == num_pieces:
                console.print("\n[bold green]✔ All pieces downloaded! Now seeding.[/bold green]")
                time.sleep(60)
                continue

            # ── Announce to tracker ──────────────
            try:
                resp = requests.get(
                    tracker_url,
                    params={"file_name": file_name, "port": my_port},
                    timeout=5,
                )
                peers = resp.json().get("peers", [])
                console.log(f"[yellow][Tracker][/yellow] {len(peers)} peer(s) known")
            except Exception as e:
                console.log(f"[red][Tracker] Error: {e}[/red]")
                time.sleep(10)
                continue

            print_stats(peers)

            if not peers:
                console.log("[yellow]No peers yet. Waiting...[/yellow]")
                time.sleep(10)
                continue

            # ── Parallel piece download ───────────
            with pieces_lock:
                missing = [i for i in range(num_pieces) if not my_pieces[i]]

            # Build (piece_index, peer_addr) work items — round-robin peers
            work_items = []
            for idx, piece_index in enumerate(missing):
                peer_addr = peers[idx % len(peers)]
                work_items.append((piece_index, peer_addr))

            # ✅ KEY UPGRADE: parallel downloads via thread pool
            with ThreadPoolExecutor(max_workers=min(8, len(work_items))) as executor:
                futures = {
                    executor.submit(
                        download_piece,
                        peer_addr,
                        piece_index,
                        piece_size,
                        file_size,
                        piece_hashes[piece_index],
                    ): piece_index
                    for piece_index, peer_addr in work_items
                }

                for future in as_completed(futures):
                    piece_index = futures[future]
                    try:
                        pi, piece_data = future.result()
                        # Write piece to disk
                        with open(file_name, "r+b") as f:
                            f.seek(pi * piece_size)
                            f.write(piece_data)
                        with pieces_lock:
                            my_pieces[pi] = True
                            stats["downloaded"] += 1
                        progress.update(dl_task, advance=1)
                        console.log(f"[green]✔ Piece {pi} verified & saved[/green]")
                    except Exception as e:
                        console.log(f"[red]✘ Piece {piece_index} failed: {e}[/red]")

            time.sleep(15)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="mini-webtorrent peer")
    parser.add_argument("metainfo_file", help="Path to .json metainfo file")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--seeder", action="store_true", help="Start as initial seeder")
    args = parser.parse_args()

    with open(args.metainfo_file) as f:
        metainfo = json.load(f)

    file_size = metainfo["file_size"]
    piece_size = metainfo["piece_size"]

    console.print(Panel.fit(
        f"[bold cyan]mini-webtorrent[/bold cyan]  |  port [yellow]{args.port}[/yellow]  |  "
        f"mode [magenta]{'SEEDER' if args.seeder else 'LEECHER'}[/magenta]",
        border_style="cyan",
    ))

    uploader_thread = threading.Thread(
        target=run_uploader,
        args=(args.port, metainfo["file_name"], piece_size, file_size),
        daemon=True,
    )
    uploader_thread.start()

    run_downloader(metainfo, args.port, args.seeder)


if __name__ == "__main__":
    main()
