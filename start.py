"""Start the local chatbot, or reuse an already-running instance."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8000"


def server_state():
    try:
        with urlopen(URL + "/health/live", timeout=3) as response:
            health = json.loads(response.read(100_000))
        with urlopen(URL + "/openapi.json", timeout=3) as response:
            schema = json.loads(response.read(1_000_000))
        if (health.get("status") == "ok"
                and schema.get("info", {}).get("title") == "Allianz evidence-only RAG"
                and "/chat" in schema.get("paths", {})):
            return "running"
    except (URLError, OSError, ValueError):
        pass
    with socket.socket() as connection:
        connection.settimeout(2)
        return "occupied" if connection.connect_ex(("127.0.0.1", 8000)) == 0 else "available"


def main():
    parser = argparse.ArgumentParser(description="Start the Allianz local chatbot safely")
    parser.add_argument("--reload", action="store_true", help="Reload app code during development")
    args = parser.parse_args()
    state = server_state()
    if state == "running":
        print(f"Chatbot is already running. Open {URL}/ in your browser.")
        return 0
    if state == "occupied":
        print(f"Port 8000 is occupied, but the chatbot is not ready. Wait for startup or stop the previous server and retry.", file=sys.stderr)
        return 1
    python = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        print("Project virtual environment is missing. See README.md for setup.", file=sys.stderr)
        return 1
    command = [str(python), "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", "8000"]
    if args.reload:
        command += ["--reload", "--reload-dir", str(ROOT / "app")]
    print(f"Starting chatbot at {URL}/. Keep this terminal open; Ctrl+C stops it.", flush=True)
    try:
        return subprocess.run(command, cwd=ROOT).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
