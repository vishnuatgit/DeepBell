import time
import os
import sqlite3
import logging
import uuid
import sys
from fastapi import FastAPI, HTTPException
from typing import List, Dict

# Set up logging to stdout so it can be captured by the Watchdog process
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("TargetApp")

app = FastAPI(title="DeepBell Target App")
DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

# A global list to simulate a memory leak
_memory_leak_array = []

def get_db():
    conn = sqlite3.connect(DB_FILE)
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            amount REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup_event():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    init_db()
    logger.info("Application started. Database initialized.")

@app.get("/")
def read_root():
    return {"status": "healthy", "service": "DeepBell Target App"}

@app.post("/transaction")
def create_transaction(amount: float):
    tx_id = str(uuid.uuid4())
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO transactions (id, amount) VALUES (?, ?)", (tx_id, amount))
        conn.commit()
        logger.info(f"Transaction created: {tx_id} with amount {amount}")
        return {"id": tx_id, "amount": amount}
    except Exception as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="Database Error")
    finally:
        conn.close()

@app.get("/transactions")
def list_transactions():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, amount, timestamp FROM transactions ORDER BY timestamp DESC LIMIT 10")
        rows = cursor.fetchall()
        return [{"id": r[0], "amount": r[1], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch transactions: {e}")
        raise HTTPException(status_code=500, detail="Database fetch error")
    finally:
        conn.close()

# --- FAULT INJECTION ENDPOINTS ---

@app.get("/fault/memory_leak")
def inject_memory_leak(size_mb: int = 50):
    """Simulates a memory leak by appending a large string to a global array."""
    logger.warning(f"FAULT INJECTED: Allocating {size_mb} MB of leaked memory.")
    # 1 MB is approx 1,000,000 chars
    _memory_leak_array.append("x" * (size_mb * 1024 * 1024))
    return {"status": "fault_injected", "type": "memory_leak", "allocated_mb": size_mb, "total_leaked_blocks": len(_memory_leak_array)}

@app.get("/fault/cpu_spike")
def inject_cpu_spike(duration_sec: int = 5):
    """Simulates a CPU spike with a blocking busy loop."""
    logger.warning(f"FAULT INJECTED: CPU spike for {duration_sec} seconds.")
    end_time = time.time() + duration_sec
    while time.time() < end_time:
        _ = 1000000 * 1000000 # Do some math
    logger.info("CPU spike ended.")
    return {"status": "fault_injected", "type": "cpu_spike", "duration": duration_sec}

@app.get("/fault/fatal_crash")
def inject_fatal_crash():
    """Simulates a fatal application crash by exiting the process."""
    logger.fatal("FAULT INJECTED: Fatal crash triggered. Shutting down process immediately.")
    sys.exit(1)

@app.get("/fault/corrupt_db")
def inject_db_corruption():
    """Simulates database corruption by writing garbage to the SQLite file."""
    logger.error("FAULT INJECTED: Corrupting database file.")
    try:
        with open(DB_FILE, "a") as f:
            f.write("GARBAGE DATA CORRUPTION" * 100)
        logger.error("Database corrupted successfully.")
        return {"status": "fault_injected", "type": "corrupt_db"}
    except Exception as e:
        logger.error(f"Failed to corrupt DB: {e}")
        return {"status": "failed_to_corrupt"}
