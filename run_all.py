import subprocess, os, sys, signal, time

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def start_process(command, cwd):
    """Launch a subprocess in its own process group (Windows compatible)."""
    return subprocess.Popen(
        command,
        cwd=cwd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )

def main():
    processes = []
    # 1. Snapshot manager – continuously creates DB backups
    snapshot_path = os.path.join(BASE_DIR, "snapshot_manager")
    processes.append(start_process([sys.executable, "snapshot.py"], cwd=snapshot_path))

    # 2. Watchdog – collects telemetry & runs anomaly detection
    watchdog_path = os.path.join(BASE_DIR, "watchdog")
    processes.append(start_process([sys.executable, "telemetry.py"], cwd=watchdog_path))

    # 3. FastAPI target app – serves the dashboard and fault endpoints
    target_path = os.path.join(BASE_DIR, "target_app")
    processes.append(start_process([
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ], cwd=target_path))

    print("✅ All services started. Press Ctrl+C to stop them.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down services…")
        for p in processes:
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT)
                p.wait(timeout=5)
            except Exception:
                p.terminate()
        print("✅ All services stopped.")

if __name__ == "__main__":
    main()
