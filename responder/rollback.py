import os
import shutil
import logging
import psutil
import glob
import subprocess
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Rollback")

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backups"))
DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "target_app"))

def find_latest_backup():
    backups = glob.glob(os.path.join(BACKUP_DIR, "app_*.db.bak"))
    if not backups:
        return None
    latest_backup = max(backups, key=os.path.getmtime)
    return latest_backup

def restore_database():
    latest = find_latest_backup()
    if not latest:
        logger.error("No backups found to restore!")
        return False
    
    logger.info(f"Restoring database from: {latest}")
    shutil.copy2(latest, DB_FILE)
    logger.info("Database successfully restored.")
    return True

def terminate_target_app():
    logger.info("Terminating crashed application process...")
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['cmdline'] and 'main:app' in ' '.join(proc.info['cmdline']):
                proc.kill()
                logger.info(f"Killed process PID {proc.pid}")
                time.sleep(2)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

def restart_target_app():
    logger.info("Restarting application...")
    subprocess.Popen(
        ["python", "-m", "uvicorn", "main:app", "--reload"], 
        cwd=APP_DIR, 
        shell=True
    )
    logger.info("Application successfully restarted!")

def execute_rollback():
    logger.info("=== INITIATING AUTO-RECOVERY ===")
    terminate_target_app()
    if restore_database():
        restart_target_app()
        logger.info("=== AUTO-RECOVERY COMPLETE ===")

if __name__ == "__main__":
    execute_rollback()
