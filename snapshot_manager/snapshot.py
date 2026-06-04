import os
import shutil
import time
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SnapshotManager")

# Path to the live SQLite database used by the Target Application
DB_SOURCE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))
BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backups"))
MAX_SNAPSHOTS = 10
SNAPSHOT_INTERVAL_SEC = 30

def take_snapshot():
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        
    if not os.path.exists(DB_SOURCE_PATH):
        logger.warning(f"Source database not found at {DB_SOURCE_PATH}. Is the target app running?")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"app_{timestamp}.db.bak")
    
    try:
        shutil.copy2(DB_SOURCE_PATH, backup_file)
        logger.info(f"Snapshot created: {backup_file}")
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}")

def cleanup_old_snapshots():
    if not os.path.exists(BACKUP_DIR):
        return
        
    snapshots = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".db.bak")]
    # Sort by creation time, oldest first
    snapshots.sort(key=os.path.getctime)
    
    while len(snapshots) > MAX_SNAPSHOTS:
        oldest = snapshots.pop(0)
        try:
            os.remove(oldest)
            logger.info(f"Deleted old snapshot: {oldest}")
        except Exception as e:
            logger.error(f"Failed to delete old snapshot {oldest}: {e}")

if __name__ == "__main__":
    logger.info(f"Starting Snapshot Manager. Backing up every {SNAPSHOT_INTERVAL_SEC} seconds.")
    logger.info(f"Source DB: {DB_SOURCE_PATH}")
    logger.info(f"Backup Dir: {BACKUP_DIR}")
    
    while True:
        take_snapshot()
        cleanup_old_snapshots()
        time.sleep(SNAPSHOT_INTERVAL_SEC)
