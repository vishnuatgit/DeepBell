import os
import shutil
import logging
import psutil
import glob

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Rollback")

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backups"))
DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

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

if __name__ == "__main__":
    restore_database()
