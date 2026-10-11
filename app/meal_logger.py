import os

import httpx


def _append_to_meal_chain(packet: dict):
    pancake_url = os.getenv("PANCAKE_URL", "http://localhost:8100")
    secret = os.getenv("AR2_INTERNAL_SHARED_SECRET")
    headers = {"X-Pancake-Internal": secret} if secret else {}
    
    try:
        resp = httpx.post(f"{pancake_url}/audit/events", json=packet, headers=headers, timeout=5)
        resp.raise_for_status()
    except Exception as e:
        import logging
        logging.getLogger("meal_audit").error(f"Failed to append to MEAL chain: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Audit logging failed") from e

def log_traceforward(user_sub: str, credential_jti: str, seed_geoid: str, scope: str | None, match_count: int, list_ids: list[str]):
    """
    Logs a traceforward.invoked MEAL packet.
    """
    packet = {
        "event": "traceforward.invoked",
        "who": user_sub,
        "credential_id": credential_jti,
        "seed_geoid": seed_geoid,
        "scope": scope or "global",
        "match_count": match_count,
        "list_ids": list_ids
    }
    _append_to_meal_chain(packet)
    
def log_internal_read(user_sub: str | None, artifact_id: str):
    """
    Logs a list.read.internal MEAL packet.

    A trusted service (Pancake) read a lot's members on the shared secret
    rather than on a credential: it does so to mint a grant for the lot's
    owner, before any grant exists. That read used to leave no trace. It is
    not a disclosure to a person, but it is a read of a gated artifact with
    no credential behind it, and the ledger is where such reads belong.
    """
    # Pancake's AuditEventRequest requires credential_id as a string; there is
    # no credential here, and the string says so rather than inventing one.
    packet = {
        "event": "list.read.internal",
        "who": user_sub or "service",
        "credential_id": "none:internal-shared-secret",
        "seed_geoid": artifact_id,
        "artifact": artifact_id,
    }
    _append_to_meal_chain(packet)

def log_traceback(user_sub: str, credential_jti: str, artifact_id: str):
    """
    Logs a traceback.invoked MEAL packet.
    """
    packet = {
        "event": "traceback.invoked",
        "who": user_sub,
        "credential_id": credential_jti,
        "seed_geoid": artifact_id,
        "artifact": artifact_id
    }
    _append_to_meal_chain(packet)
