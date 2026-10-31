"""Sample transactions in app.db, the data the snapshots protect."""

import math
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException

from .. import config, db, incidents, timeutil

router = APIRouter()


def database_unavailable(error: str) -> HTTPException:
    incidents.set_db_error(error)
    return HTTPException(
        status_code=503,
        detail=f"Database unreadable ({error}). Roll back to a snapshot in the Time Machine.",
    )


@router.post("/transaction", tags=["database"])
def create_transaction(amount: float):
    if not math.isfinite(amount) or not 0 < amount <= config.MAX_AMOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Amount must be greater than 0 and at most {config.MAX_AMOUNT:,}.",
        )
    amount = round(amount, 2)
    tx_id = str(uuid.uuid4())
    try:
        with db.app_db() as conn:
            conn.execute(
                "INSERT INTO transactions (id, amount, timestamp) VALUES (?, ?, ?)",
                (tx_id, amount, timeutil.iso(timeutil.utcnow())),
            )
            # Keep the demo table small: only the newest TRANSACTIONS_KEEP rows.
            conn.execute(
                "DELETE FROM transactions WHERE rowid NOT IN "
                "(SELECT rowid FROM transactions ORDER BY rowid DESC LIMIT ?)",
                (config.TRANSACTIONS_KEEP,),
            )
            conn.commit()
    except sqlite3.Error as e:
        config.logger.error(f"Database error: {e}")
        raise database_unavailable(str(e))
    config.logger.info(f"Transaction created: {tx_id} with amount {amount}")
    return {"id": tx_id, "amount": amount}


@router.get("/transactions", tags=["database"])
def list_transactions():
    try:
        with db.app_db() as conn:
            rows = conn.execute(
                "SELECT id, amount, timestamp FROM transactions ORDER BY rowid DESC LIMIT 10"
            ).fetchall()
    except sqlite3.Error as e:
        config.logger.error(f"Failed to fetch transactions: {e}")
        raise database_unavailable(str(e))
    return [{"id": r[0], "amount": r[1], "timestamp": timeutil.to_iso_utc(r[2])} for r in rows]
