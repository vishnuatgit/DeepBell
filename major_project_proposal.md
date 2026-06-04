# Major Project Proposal: DeepBell

**Project Title:** DeepBell - AI-Driven Point-in-Time Recovery and Analytical Reporting System
**Domain:** Site Reliability Engineering (SRE), Artificial Intelligence, Machine Learning (AIOps)
**Project Type:** Major Project

## 1. Problem Statement
In modern distributed software systems, silent failures, resource leaks (like memory leaks), and database corruption are inevitable. Traditional monitoring systems (like Prometheus or Grafana) rely on static thresholds and only *alert* human engineers when a failure occurs. This leads to prolonged downtime (high Mean Time To Recovery - MTTR) and requires hours of manual log parsing to discover the root cause. 

## 2. Proposed Solution (DeepBell)
DeepBell is an autonomous, agentic AIOps system designed to eliminate manual intervention during system crashes. The main theme of the project is to build a system that:
1. Takes continuous Point-in-Time backups of an application's state.
2. Uses Machine Learning (ML) to monitor telemetry and logs to predict or detect failures.
3. Automatically triggers a Point-in-Time Recovery (PITR) to restore the system to its last known healthy state.
4. Leverages Generative AI (LLMs) to synthesize pre-crash data into an exact Analytical Report detailing the root cause.

## 3. Core Architecture & Modules
The project is divided into four distinct technical modules:

### A. The Target Infrastructure
A pure Python API (FastAPI) backed by a SQLite database. This serves as the testing ground and includes "fault injection" capabilities to simulate real-world crashes (CPU spikes, memory leaks, fatal exceptions).

### B. Point-in-Time Recovery (PITR) Engine
A high-performance daemon that maintains a rolling window of database and system state snapshots. It ensures data durability and provides the safe restoration points required for recovery.

### C. ML Watchdog (Failure Detection)
This module streams live process telemetry (CPU, Memory, I/O) using psutil. It utilizes an **Isolation Forest** (an unsupervised Machine Learning algorithm) trained on baseline "healthy" data to detect multidimensional anomalies in real-time. Simultaneously, it parses application logs for fatal error signatures.

### D. Agentic Responder & LLM Analyst
An orchestration agent built using LangChain. Upon receiving an alert from the ML Watchdog, it executes the rollback sequence. It then aggregates the anomalous metrics and crash logs and passes them to a Large Language Model (LLM) to generate a human-readable **Root Cause Analysis (RCA)** report.

## 4. Technologies & Tools Used
*   **Languages:** Python 3.10+
*   **Machine Learning:** `scikit-learn` (Isolation Forest), `pandas`, `numpy`
*   **Generative AI:** LangChain, OpenAI / Gemini API
*   **Backend & DB:** FastAPI, Uvicorn, SQLite
*   **Infrastructure:** Git, Local OS Processes

## 5. Expected Outcomes & Deliverables
By the conclusion of this project, the following deliverables will be completed:
1. A fully functional AIOps monitoring agent built purely in Python.
2. A demonstration of automated system recovery (zero-touch MTTR reduction).
3. Auto-generated, highly accurate Analytical Reports pinpointing exact lines of code or resource constraints that caused the injected failures.
4. A comprehensive GitHub repository demonstrating advanced DevOps and AI engineering skills.
