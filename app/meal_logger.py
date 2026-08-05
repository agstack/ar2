import logging
import json
from typing import Optional

logger = logging.getLogger("meal_audit")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(handler)

def log_traceforward(user_sub: str, credential_jti: str, scope: Optional[str], match_count: int):
    """
    Logs a traceforward.invoked MEAL packet.
    """
    packet = {
        "event": "traceforward.invoked",
        "who": user_sub,
        "credential_id": credential_jti,
        "scope": scope or "global",
        "match_count": match_count
    }
    logger.info(f"MEAL_AUDIT: {json.dumps(packet)}")
    
def log_traceback(user_sub: str, credential_jti: str, artifact_id: str):
    """
    Logs a traceback.invoked MEAL packet.
    """
    packet = {
        "event": "traceback.invoked",
        "who": user_sub,
        "credential_id": credential_jti,
        "artifact": artifact_id
    }
    logger.info(f"MEAL_AUDIT: {json.dumps(packet)}")
