"""Mint a field-access grant for one GeoID with the dev issuer key.

Shared by every suite that has to pass the grant gate on a trace route. Lives
here rather than in one test module so that a second copy cannot drift from
the first; the credential shape is the one Pancake's testkit mints
(vct agstack.org/credentials/field-access-grant/v1, one selective disclosure
naming the field, a status-list reference the tests resolve locally).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path

import jwt

DEV_KEYS = Path(__file__).parent / "dev_keys"
GRANT_VCT = "agstack.org/credentials/field-access-grant/v1"
STATUS_URI = "http://localhost:8100/grants/status-list"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def valid_grant_for(geoid: str, *, ttl_seconds: int = 3600) -> str:
    """An SD-JWT grant whose single disclosure names ``geoid``."""
    private_pem = (DEV_KEYS / "dev_issuer_private.pem").read_bytes()
    salt = _b64url(secrets.token_bytes(16))
    disclosure = _b64url(json.dumps([salt, "fields.0", geoid]).encode("utf-8"))
    digest = _b64url(hashlib.sha256(disclosure.encode("ascii")).digest())
    now = int(time.time())
    claims = {
        "iss": "did:web:pancake.test",
        "sub": "owner@demo.com",
        "iat": now,
        "exp": now + ttl_seconds,
        "vct": GRANT_VCT,
        "status": {"status_list": {"uri": STATUS_URI, "idx": 1}},
        "_sd": [digest],
        "_sd_alg": "sha-256",
    }
    token = jwt.encode(claims, private_pem, algorithm="EdDSA",
                       headers={"typ": "vc+sd-jwt", "kid": "pancake-test-1"})
    return f"{token}~{disclosure}~"


def grant_header_for(geoid: str) -> dict[str, str]:
    return {"X-Grant-Token": valid_grant_for(geoid)}
