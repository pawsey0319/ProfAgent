from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profagent.app import app


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="on")
    )
    thread = threading.Thread(
        target=lambda: server.run(sockets=[sock]),
        daemon=True,
    )
    thread.start()

    try:
        health = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    health = json.load(response)
                break
            except Exception:
                time.sleep(0.1)
        if health is None:
            raise RuntimeError("ASGI server did not become ready")

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=2
        ) as response:
            home_status = response.status
            home = response.read().decode("utf-8")

        result = {
            "ready": health["ready"],
            "users": health["data"]["counts"]["users"],
            "garments": health["data"]["counts"]["garments"],
            "outfits": health["data"]["counts"]["outfits"],
            "catalog": health["data"]["counts"]["catalog"],
            "static_2d": health["capabilities"]["static_2d"],
            "video": health["capabilities"]["video"],
            "three_d": health["capabilities"]["three_d"],
            "home_status": home_status,
            "home_marker": "ProfAgent" in home,
        }
        expected = {
            "ready": True,
            "users": 3,
            "garments": 50,
            "outfits": 20,
            "catalog": 50,
        "static_2d": True,
            "video": False,
            "three_d": False,
            "home_status": 200,
            "home_marker": True,
        }
        if result != expected:
            raise AssertionError({"actual": result, "expected": expected})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("ASGI server did not stop cleanly")


if __name__ == "__main__":
    main()
