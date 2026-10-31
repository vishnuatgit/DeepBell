"""The Gemini client (through LangChain): the API key, lazy loading and AI-written reports."""

import os

from . import config, eventlog, reports, runtime


def get_api_key() -> str:
    if runtime.api_key and not config.PUBLIC_DEMO:
        return runtime.api_key
    return os.getenv("GOOGLE_API_KEY", "").strip()


def load_llm(relearn: bool) -> None:
    """Import the LangChain/Gemini client before the first AI report.

    The import grows the process by about 30 MB, which the watchdog would otherwise report as a
    memory leak, so after a late import the detector re-learns its baseline.
    """
    if runtime.llm_loaded:
        return
    try:
        import langchain_core.prompts  # noqa: F401
        import langchain_google_genai  # noqa: F401
    except Exception as e:
        config.logger.error(f"Could not load the Gemini client: {e}")
        return
    runtime.llm_loaded = True
    if relearn:
        with runtime.lock:
            runtime.detector.reset()
        eventlog.log_event(
            "info", "Gemini client loaded; the watchdog is re-learning its memory baseline."
        )


def generate_ai_report(api_key: str, rows: list[dict], incident: str = "") -> str:
    """Ask Gemini (through LangChain) for an RCA report. Imported lazily."""
    from langchain_core.prompts import PromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        temperature=0.2,
        google_api_key=api_key,
    )
    prompt_template = """
You are an expert Site Reliability Engineer (SRE).
The system watchdog has detected an anomaly or database incident.
Incident type reported by the watchdog: {incident}

Here is the telemetry data (timestamps in UTC) from the last few minutes before the incident:
{telemetry_data}

Write a Root Cause Analysis (RCA) report in Markdown with:
1. Executive Summary
2. Timeline of Anomaly (when values started deviating)
3. Suspected Root Cause (CPU lockup, memory leak or database corruption, based on the data)
4. Recommended Preventative Actions
"""
    prompt = PromptTemplate(
        input_variables=["telemetry_data", "incident"], template=prompt_template
    )
    response = (prompt | llm).invoke(
        {"telemetry_data": reports.telemetry_table(rows), "incident": incident or "manual request"}
    )
    content = response.content
    if isinstance(content, list):
        content = "".join(c if isinstance(c, str) else c.get("text", "") for c in content)
    return content
