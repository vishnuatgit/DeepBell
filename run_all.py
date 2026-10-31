import os
import subprocess
import sys
import time


def main():
    # Bind address and port come from the environment (HOST, PORT); the defaults keep the
    # server private to this machine. The app itself decides whether to read .env:
    # it never does when PUBLIC_DEMO is set.
    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")
    print("==========================================================")
    print("[SYSTEM] DeepBell AIOps SRE Cockpit Launcher")
    print("==========================================================")
    print(f"Starting unified web service on http://{host}:{port}")
    print("This launcher auto-recovers and restarts the app if killed.")
    print("Press Ctrl+C to terminate.")
    print("----------------------------------------------------------")

    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "target_app"))

    while True:
        try:
            # We launch uvicorn pointing to target_app/main.py.
            # We use uvicorn command directly with target_app directory in Python path
            p = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    host,
                    "--port",
                    port,
                ],
                cwd=app_dir,
            )
            p.wait()

            if p.returncode == 0:
                print("\n[SYSTEM] DeepBell SRE Cockpit exited cleanly.")
                break
            else:
                print(f"\n[CRASH] Crash detected! Process exited with code {p.returncode}.")
                print("[RECOVERY] Healing database & restarting service in 2 seconds...")
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Stopping DeepBell SRE Cockpit Launcher.")
            try:
                p.terminate()
            except Exception:
                pass
            break


if __name__ == "__main__":
    main()
