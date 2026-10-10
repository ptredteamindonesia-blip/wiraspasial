"""Jalankan server WIRA-SPASIAL.

    python run.py                 # jalankan di terminal (foreground)
    python run.py start           # jalankan di latar belakang (tetap berjalan setelah terminal ditutup)
    python run.py status          # cek apakah server latar belakang berjalan
    python run.py stop            # hentikan server latar belakang
    python run.py restart         # stop + start
    python run.py logs [-n 50]    # tampilkan log terakhir

Opsi: --host 127.0.0.1 --port 8000
Data, log (data/logs/geoint.log) dan PID (data/geoint.pid) disimpan di GEOINT_DATA_DIR (default ./data).
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GEOINT_DATA_DIR", ROOT / "data"))
PID_FILE = DATA_DIR / "geoint.pid"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "geoint.log"
MAX_LOG_BYTES = 10 * 1024 * 1024


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> dict | None:
    try:
        info = json.loads(PID_FILE.read_text())
        return info if pid_alive(info["pid"]) else None
    except (OSError, ValueError, KeyError):
        return None


def health(host: str, port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def rotate_log() -> None:
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
        old = LOG_FILE.with_suffix(".log.1")
        old.unlink(missing_ok=True)
        LOG_FILE.rename(old)


def check_port_or_exit(host: str, port: int) -> None:
    if not port_free(host, port):
        alt = next((p for p in range(port + 1, port + 50) if port_free(host, p)), None)
        print(f"Port {port} sudah dipakai proses lain (mungkin WIRA-SPASIAL yang masih berjalan — cek: python run.py status).",
              file=sys.stderr)
        if alt:
            print(f"Jalankan dengan port lain, mis.:  python run.py --port {alt}", file=sys.stderr)
        sys.exit(1)


def cmd_foreground(args) -> None:
    import uvicorn

    check_port_or_exit(args.host, args.port)
    print(f"WIRA-SPASIAL berjalan di http://{args.host}:{args.port}")
    uvicorn.run("geoint.api:app", host=args.host, port=args.port)


def cmd_start(args) -> None:
    info = read_pid()
    if info:
        print(f"WIRA-SPASIAL sudah berjalan di latar belakang (PID {info['pid']}, http://{info['host']}:{info['port']}).")
        return
    check_port_or_exit(args.host, args.port)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_log()
    log = open(LOG_FILE, "a", encoding="utf-8")
    cmd = [sys.executable, str(ROOT / "run.py"), "--host", args.host, "--port", str(args.port)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(ROOT), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # lepas dari terminal: tidak ikut mati saat terminal ditutup
    proc = subprocess.Popen(cmd, **kwargs)
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "host": args.host, "port": args.port, "started": time.time()}))
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            print(f"Server gagal start (kode {proc.returncode}). Lihat log: {LOG_FILE}", file=sys.stderr)
            PID_FILE.unlink(missing_ok=True)
            sys.exit(1)
        if health(args.host, args.port):
            print(f"WIRA-SPASIAL berjalan di latar belakang: http://{args.host}:{args.port}  (PID {proc.pid})")
            print(f"Log: {LOG_FILE}  |  Hentikan: python run.py stop")
            return
    print(f"Server dijalankan (PID {proc.pid}) tetapi belum merespons; cek log: {LOG_FILE}")


def cmd_stop(args) -> None:
    info = read_pid()
    if not info:
        PID_FILE.unlink(missing_ok=True)
        print("WIRA-SPASIAL tidak sedang berjalan di latar belakang.")
        return
    pid = info["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)  # uvicorn berhenti rapi; analisa yang terputus dilanjutkan saat start berikutnya
    for _ in range(30):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        if os.name != "nt":
            os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    print(f"WIRA-SPASIAL dihentikan (PID {pid}). Analisa yang terputus akan dilanjutkan saat server dijalankan lagi.")


def cmd_status(args) -> None:
    info = read_pid()
    if not info:
        print("WIRA-SPASIAL: TIDAK berjalan di latar belakang.")
        sys.exit(3)
    h = health(info["host"], info["port"])
    up = time.time() - info.get("started", time.time())
    print(f"WIRA-SPASIAL: berjalan (PID {info['pid']}), http://{info['host']}:{info['port']}, uptime {up / 3600:.1f} jam")
    if h:
        try:
            with urllib.request.urlopen(f"http://{info['host']}:{info['port']}/api/monitor", timeout=5) as r:
                m = json.loads(r.read())
            print(f"Analisa otomatis: {'AKTIF' if m['enabled'] else 'BERHENTI'} | {m['active_aois']} area aktif | "
                  f"{len(m['running'])} analisa antri/berjalan | cek berikutnya {m['next_tick'] or '-'}")
        except Exception:
            pass
    else:
        print("Peringatan: proses ada tetapi API tidak merespons.")


def cmd_logs(args) -> None:
    if not LOG_FILE.exists():
        print("Belum ada log.")
        return
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.n:]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="WIRA-SPASIAL — Wawasan Intelijen & Ruang Analisis Spasial")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "start", "stop", "status", "restart", "logs"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-n", type=int, default=50, help="jumlah baris untuk 'logs'")
    args = ap.parse_args()

    if args.command == "run":
        cmd_foreground(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "restart":
        cmd_stop(args)
        time.sleep(1)
        cmd_start(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "logs":
        cmd_logs(args)
