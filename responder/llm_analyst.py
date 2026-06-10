import os
import sqlite3
import pandas as pd
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import PromptTemplate
from datetime import datetime

# Load environment variables (API Key)
load_dotenv()

DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "metrics.db"))
REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "reports"))

# Prompt Template for the RCA Report
RCA_PROMPT_TEMPLATE = """
You are an expert Site Reliability Engineer (SRE) and AI Agent.
The system watchdog has detected a fatal anomaly or crash.

Here is the telemetry data from the last 5 minutes before the crash:
{telemetry_data}

Please generate a highly professional Root Cause Analysis (RCA) report in Markdown format.
Include:
1. Executive Summary
2. Timeline of Anomaly
3. Suspected Root Cause (e.g. Memory Leak, CPU lockup)
4. Recommended Preventative Actions
"""

def setup_llm():
    if not os.getenv("GOOGLE_API_KEY"):
        raise ValueError("GOOGLE_API_KEY environment variable is missing! Please set it in a .env file.")
    
    return ChatGoogleGenerativeAI(
        model="gemini-pro",
        temperature=0.2
    )

def get_recent_metrics(limit=10):
    if not os.path.exists(DB_FILE):
        return "No telemetry data found."
        
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(f"SELECT * FROM metrics ORDER BY timestamp DESC LIMIT {limit}", conn)
    conn.close()
    return df.to_markdown()

def generate_rca_report():
    print("Initiating LLM Analyst...")
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    try:
        llm = setup_llm()
    except ValueError as e:
        print(f"Error: {e}")
        return

    telemetry = get_recent_metrics()
    
    prompt = PromptTemplate(
        input_variables=["telemetry_data"],
        template=RCA_PROMPT_TEMPLATE
    )
    
    chain = prompt | llm
    
    print("Generating RCA Report using Gemini...")
    response = chain.invoke({"telemetry_data": telemetry})
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORTS_DIR, f"RCA_Report_{timestamp}.md")
    
    with open(report_path, "w") as f:
        f.write(response.content)
        
    print(f"RCA Report successfully written to {report_path}")

if __name__ == "__main__":
    generate_rca_report()
