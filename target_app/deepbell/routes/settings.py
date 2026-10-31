"""Settings for local runs: the Gemini API key, kept in memory only."""

import threading

from fastapi import APIRouter, Form, HTTPException

from .. import config, llm, runtime

router = APIRouter()


@router.post("/api/settings/apikey", tags=["settings"])
def post_save_apikey(api_key: str = Form("")):
    """Set the Gemini API key for this server process (local runs only, never saved to disk)."""
    if config.PUBLIC_DEMO:
        raise HTTPException(
            status_code=403,
            detail="The API key cannot be changed on the public demo.",
        )
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key cannot be empty.")
    runtime.api_key = api_key
    threading.Thread(target=llm.load_llm, kwargs={"relearn": True}, daemon=True).start()
    config.logger.info("Gemini API key set for this process.")
    return {
        "status": "success",
        "message": (
            "API key set until the server restarts. It is not saved to disk: "
            "put GOOGLE_API_KEY in your .env file to keep it."
        ),
    }
