import subprocess
import sys
import time
import os

def main():
    print("==========================================================")
    print("[SYSTEM] DeepBell AIOps SRE Cockpit Launcher")
    print("==========================================================")
    print("Starting unified web service on http://127.0.0.1:8000")
    print("This launcher auto-recovers and restarts the app if killed.")
    print("Press Ctrl+C to terminate.")
    print("----------------------------------------------------------")

    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "target_app"))
    
    while True:
        try:
            # We launch uvicorn pointing to target_app/main.py. 
            # We use uvicorn command directly with target_app directory in Python path
            p = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
                cwd=app_dir
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
