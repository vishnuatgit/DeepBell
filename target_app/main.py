import time
import os
import sqlite3
import logging
import uuid
import sys
import glob
import shutil
import threading
from datetime import datetime
from typing import List, Dict
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("DeepBellCockpit")

app = FastAPI(title="DeepBell SRE Cockpit")

# Directory Definitions
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
DB_FILE = os.path.join(PROJECT_ROOT, "data", "app.db")
METRICS_DB = os.path.join(PROJECT_ROOT, "data", "metrics.db")
BACKUP_DIR = os.path.join(PROJECT_ROOT, "backups")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")

# Ensure directories exist
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# Dashboard UI Setup
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Global states
_memory_leak_array = []
system_health = "Healthy"
latest_anomaly_score = 0.0
last_cpu = 0.0
last_ram = 0.0
is_generating_rca = False

# Database helper functions
def get_db():
    return sqlite3.connect(DB_FILE)

def get_metrics_db():
    return sqlite3.connect(METRICS_DB)

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

def init_metrics_db():
    conn = get_metrics_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            pid INTEGER,
            cpu_percent REAL,
            memory_mb REAL
        )
    ''')
    # Check if anomaly_status column exists and add it if not
    cursor.execute("PRAGMA table_info(metrics)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'anomaly_status' not in columns:
        cursor.execute("ALTER TABLE metrics ADD COLUMN anomaly_status TEXT DEFAULT 'Normal'")
        logger.info("Database migration: Added anomaly_status column to metrics table.")
    conn.commit()
    conn.close()

# Self-Healing Database Integrity Check
def verify_and_heal_database():
    logger.info("Verifying database integrity...")
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        cursor.fetchall()
        conn.close()
        logger.info("Database integrity verified successfully.")
    except Exception as e:
        logger.error(f"Database corruption detected: {e}. Executing auto-recovery...")
        restore_latest_backup()

def restore_latest_backup() -> bool:
    backups = glob.glob(os.path.join(BACKUP_DIR, "app_*.db.bak"))
    if not backups:
        logger.error("No backups available for restoration.")
        return False
    latest_backup = max(backups, key=os.path.getmtime)
    logger.info(f"Restoring database from snapshot: {latest_backup}")
    try:
        # Close any active db handles by copying over
        shutil.copy2(latest_backup, DB_FILE)
        logger.info("Database restored successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to restore database: {e}")
        return False

# Background Snapshot Worker
def snapshot_manager_worker():
    logger.info("Snapshot Manager started.")
    while True:
        try:
            if os.path.exists(DB_FILE):
                # Verify that it is not currently corrupted before taking a backup
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM transactions")
                cursor.fetchone()
                conn.close()

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_file = os.path.join(BACKUP_DIR, f"app_{timestamp}.db.bak")
                shutil.copy2(DB_FILE, backup_file)
                logger.info(f"Auto-snapshot created: {os.path.basename(backup_file)}")

                # Cleanup old snapshots (> 10)
                snapshots = sorted(glob.glob(os.path.join(BACKUP_DIR, "app_*.db.bak")), key=os.path.getctime)
                while len(snapshots) > 10:
                    oldest = snapshots.pop(0)
                    os.remove(oldest)
                    logger.info(f"Deleted old snapshot: {os.path.basename(oldest)}")
        except Exception as e:
            logger.error(f"Snapshot Manager Error: {e}")
        time.sleep(30)

# Background Watchdog & Anomaly Detection Worker
def watchdog_worker():
    global system_health, latest_anomaly_score, last_cpu, last_ram
    logger.info("Watchdog Telemetry & Anomaly Detector started.")
    
    # Initialize psutil Process object for our own PID
    import psutil
    process = psutil.Process(os.getpid())
    # First CPU read to establish baseline
    process.cpu_percent(interval=None)
    time.sleep(0.5)

    while True:
        try:
            # 1. Telemetry Collection
            cpu = process.cpu_percent(interval=None)
            mem = process.memory_info().rss / (1024 * 1024) # RSS in MB
            last_cpu = round(cpu, 1)
            last_ram = round(mem, 1)

            # 2. Anomaly Detection using Isolation Forest
            anomaly_status = "Normal"
            conn = get_metrics_db()
            
            # Fetch past metrics to train model
            import pandas as pd
            df = pd.read_sql_query("SELECT cpu_percent, memory_mb FROM metrics ORDER BY id DESC LIMIT 200", conn)
            
            # Save current metrics to SQLite first
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO metrics (pid, cpu_percent, memory_mb, anomaly_status) VALUES (?, ?, ?, ?)",
                (os.getpid(), last_cpu, last_ram, "Normal")
            )
            conn.commit()
            current_row_id = cursor.lastrowid
            conn.close()

            if len(df) >= 15:
                # Dynamically train Isolation Forest
                from sklearn.ensemble import IsolationForest
                X = df[['cpu_percent', 'memory_mb']].copy()
                
                # Append current data point to X to run predict
                current_point = pd.DataFrame([[last_cpu, last_ram]], columns=['cpu_percent', 'memory_mb'])
                X = pd.concat([X, current_point], ignore_index=True)
                
                clf = IsolationForest(contamination=0.08, random_state=42)
                clf.fit(X)
                
                # Predict latest row status (-1: anomaly, 1: normal)
                pred = clf.predict(current_point)[0]
                
                if pred == -1:
                    anomaly_status = "Anomalous"
                    latest_anomaly_score = 1.0
                    
                    # Characterize anomaly type
                    mean_cpu = df['cpu_percent'].mean()
                    mean_ram = df['memory_mb'].mean()
                    
                    if last_cpu > mean_cpu * 2.5:
                        system_health = "Anomalous (CPU Spike)"
                    elif last_ram > mean_ram * 1.5:
                        system_health = "Anomalous (Memory Leak)"
                    else:
                        system_health = "Anomalous (System Degradation)"
                    
                    logger.warning(f"Watchdog Anomaly Detected! CPU: {last_cpu}%, RAM: {last_ram}MB. Status: {system_health}")
                    
                    # Update status in db
                    conn = get_metrics_db()
                    conn.execute("UPDATE metrics SET anomaly_status = ? WHERE id = ?", (system_health, current_row_id))
                    conn.commit()
                    conn.close()

                    # Trigger automatic LLM RCA Report Generation
                    trigger_auto_rca()
                else:
                    system_health = "Healthy"
                    latest_anomaly_score = 0.0
            else:
                system_health = "Healthy (Training Watchdog)"
                latest_anomaly_score = 0.0

        except Exception as e:
            logger.error(f"Watchdog Worker Error: {e}")
        time.sleep(2)

# Dynamic LLM RCA Report Generator
def trigger_auto_rca():
    global is_generating_rca
    if is_generating_rca:
        return
    
    # Run RCA generation in a separate thread to prevent blocking
    thread = threading.Thread(target=generate_rca_report_task, daemon=True)
    thread.start()

def generate_rca_report_task():
    global is_generating_rca
    is_generating_rca = True
    logger.info("AI SRE Agent: Starting Root Cause Analysis...")
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.error("RCA Generation aborted: GOOGLE_API_KEY environment variable is missing!")
            is_generating_rca = False
            return

        # Fetch recent metrics
        conn = get_metrics_db()
        import pandas as pd
        df = pd.read_sql_query("SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics ORDER BY id DESC LIMIT 15", conn)
        conn.close()
        
        telemetry_markdown = df.to_markdown(index=False)

        # Dynamic langchain imports to avoid startup failure if libraries are missing
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.prompts import PromptTemplate

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.2,
            google_api_key=api_key
        )

        prompt_template = """
You are an expert Site Reliability Engineer (SRE) and AI Agent.
The system watchdog has detected an anomaly or database incident.

Here is the telemetry data from the last few minutes before the incident:
{telemetry_data}

Please generate a highly professional Root Cause Analysis (RCA) report in Markdown format.
Include:
1. Executive Summary
2. Timeline of Anomaly (indicate when values started deviating)
3. Suspected Root Cause (diagnose if it is CPU lockup, memory leak, or database corruption based on the data)
4. Recommended Preventative Actions (SRE best practices)

Make the report detailed, engaging, and professional.
"""
        prompt = PromptTemplate(input_variables=["telemetry_data"], template=prompt_template)
        chain = prompt | llm
        
        response = chain.invoke({"telemetry_data": telemetry_markdown})
        
        # Save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(REPORTS_DIR, f"RCA_Report_{timestamp}.md")
        
        with open(report_path, "w") as f:
            f.write(response.content)
            
        logger.info(f"AI SRE Agent: RCA report generated successfully: {report_path}")
    except Exception as e:
        logger.error(f"AI SRE Agent Error: {e}")
    finally:
        is_generating_rca = False

# Lifespan / Startup Handler
@app.on_event("startup")
def startup_event():
    init_db()
    init_metrics_db()
    verify_and_heal_database()

    # Start managed background workers
    threading.Thread(target=snapshot_manager_worker, daemon=True).start()
    threading.Thread(target=watchdog_worker, daemon=True).start()
    logger.info("All SRE Cockpit background threads initialized.")

# --- COCKPIT UI ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request):
    """Serve the SRE Cockpit Dashboard."""
    latest_report = "No RCA reports generated yet. System is healthy!"
    if os.path.exists(REPORTS_DIR):
        reports = glob.glob(os.path.join(REPORTS_DIR, "RCA_Report_*.md"))
        if reports:
            latest_file = max(reports, key=os.path.getmtime)
            with open(latest_file, "r") as f:
                latest_report = f.read()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "report_content": latest_report,
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY"))
    })

# --- TRANSACTION ENDPOINTS ---

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
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, amount, timestamp FROM transactions ORDER BY timestamp DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        return [{"id": r[0], "amount": r[1], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch transactions: {e}")
        return []

# --- COCKPIT REST APIs ---

@app.get("/api/status")
def get_system_status():
    """Returns the live status of the cockpit."""
    return {
        "status": system_health,
        "anomaly_score": latest_anomaly_score,
        "cpu": last_cpu,
        "ram": last_ram,
        "is_generating_rca": is_generating_rca,
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY"))
    }

@app.get("/api/metrics")
def get_metrics():
    """Fetch the latest 15 telemetry entries."""
    conn = get_metrics_db()
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    conn.close()
    return [{"timestamp": r[0], "cpu": r[1], "ram": r[2], "status": r[3]} for r in rows]

@app.get("/api/backups")
def get_backups():
    """List all available database snapshots."""
    backups = glob.glob(os.path.join(BACKUP_DIR, "app_*.db.bak"))
    backups.sort(key=os.path.getmtime, reverse=True)
    
    backup_list = []
    for b in backups:
        stat = os.stat(b)
        backup_list.append({
            "filename": os.path.basename(b),
            "size_kb": round(stat.st_size / 1024, 2),
            "timestamp": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })
    return backup_list

@app.post("/api/rollback")
def post_rollback(filename: str = Form(...)):
    """Manually restore the database to a selected snapshot."""
    target_backup = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(target_backup):
        raise HTTPException(status_code=404, detail="Backup snapshot file not found.")
    
    logger.info(f"Triggering manual database rollback to: {filename}")
    try:
        shutil.copy2(target_backup, DB_FILE)
        return {"status": "success", "message": f"Database successfully rolled back to snapshot {filename}."}
    except Exception as e:
        logger.error(f"Manual rollback failed: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")

@app.post("/api/rca/generate")
def post_generate_rca():
    """Manually trigger the AI agent to generate a Root Cause Analysis report."""
    if is_generating_rca:
        return {"status": "busy", "message": "AI Agent is already generating a report."}
    
    trigger_auto_rca()
    return {"status": "started", "message": "Root Cause Analysis report generation triggered."}

@app.post("/api/settings/apikey")
def post_save_apikey(api_key: str = Form(...)):
    """Save the Gemini API Key to the environment variables and .env file."""
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key cannot be empty.")
    
    # Update current runtime environment
    os.environ["GOOGLE_API_KEY"] = api_key
    
    # Save to root .env file
    try:
        with open(ENV_FILE, "w") as f:
            f.write(f"GOOGLE_API_KEY={api_key}\n")

        logger.info("Gemini API key successfully saved to configuration.")
        return {"status": "success", "message": "API key saved successfully."}
    except Exception as e:
        logger.error(f"Failed to write API Key to file: {e}")
        raise HTTPException(status_code=500, detail="Failed to save API key to configuration.")

# --- CHAOS INJECTION ENDPOINTS ---

@app.get("/fault/memory_leak")
def inject_memory_leak(size_mb: int = 50):
    """Simulates a memory leak by appending a large string to a global array."""
    logger.warning(f"FAULT INJECTED: Allocating {size_mb} MB of leaked memory.")
    _memory_leak_array.append("x" * (size_mb * 1024 * 1024))
    return {"status": "fault_injected", "type": "memory_leak", "allocated_mb": size_mb, "total_leaked_blocks": len(_memory_leak_array)}

@app.get("/fault/cpu_spike")
def inject_cpu_spike(duration_sec: int = 5):
    """Simulates a CPU spike with a blocking busy loop."""
    logger.warning(f"FAULT INJECTED: CPU spike for {duration_sec} seconds.")
    
    def busy_loop():
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            _ = 1000000 * 1000000

    # Run in background to prevent blocking main web app requests completely
    threading.Thread(target=busy_loop, daemon=True).start()
    return {"status": "fault_injected", "type": "cpu_spike", "duration": duration_sec}

@app.get("/fault/fatal_crash")
def inject_fatal_crash():
    """Simulates a fatal application crash by exiting the process."""
    logger.fatal("FAULT INJECTED: Fatal crash triggered. Shutting down process immediately.")
    
    def kill_soon():
        time.sleep(1)
        os._exit(1) # Immediate hard exit

    threading.Thread(target=kill_soon, daemon=True).start()
    return {"status": "fault_injected", "type": "fatal_crash", "message": "Process will terminate in 1 second."}

@app.get("/fault/corrupt_db")
def inject_db_corruption():
    """Simulates database corruption by writing garbage to the SQLite file."""
    logger.error("FAULT INJECTED: Corrupting database file.")
    try:
        # Write garbage
        with open(DB_FILE, "a") as f:
            f.write("GARBAGE DATA CORRUPTION" * 100)
        logger.error("Database corrupted successfully.")
        return {"status": "fault_injected", "type": "corrupt_db"}
    except Exception as e:
        logger.error(f"Failed to corrupt DB: {e}")
        return {"status": "failed_to_corrupt"}
