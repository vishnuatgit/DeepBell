import sqlite3
import pandas as pd
from sklearn.ensemble import IsolationForest
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AnomalyDetector")

DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "metrics.db"))

def fetch_data():
    if not os.path.exists(DB_FILE):
        logger.error("Database not found! Run telemetry.py first.")
        return None
    
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM metrics", conn)
    conn.close()
    return df

def train_and_detect():
    df = fetch_data()
    if df is None or len(df) < 10:
        logger.warning("Not enough data to train Isolation Forest. Need at least 10 data points.")
        return

    # Select features for anomaly detection
    features = ['cpu_percent', 'memory_mb']
    X = df[features]

    # Train Isolation Forest
    logger.info("Training Isolation Forest on baseline data...")
    clf = IsolationForest(contamination=0.05, random_state=42)
    clf.fit(X)

    # Predict anomalies (-1 is anomaly, 1 is normal)
    df['anomaly'] = clf.predict(X)
    
    anomalies = df[df['anomaly'] == -1]
    
    if not anomalies.empty:
        logger.warning(f"DETECTED {len(anomalies)} ANOMALOUS DATA POINTS!")
        # In the future, this will trigger the Agentic Responder
    else:
        logger.info("System health is normal. No anomalies detected.")

if __name__ == "__main__":
    train_and_detect()
