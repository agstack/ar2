# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.grant_verifier import verify_sdjwt_grant


@lru_cache
def get_issuer_pubkey():
    key_path = os.getenv("AR_TRUSTED_ISSUER_PUBKEY")
    if not key_path or not os.path.exists(key_path):
        return None
    with open(key_path, "rb") as f:
        return f.read()

@lru_cache
def get_authority_pubkey():
    """Separate trust anchor: authority credentials are accredited by the Hub,
    NOT by the field-grant issuer. Must be independently rotatable/revocable."""
    path = os.getenv("AR_TRUSTED_AUTHORITY_PUBKEY")
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()

from app.grant_verifier import verify_authority_credential


def get_jti(token: str) -> str:
    try:
        t = token.split("~")[0]
        unverified = jwt.decode(t, options={"verify_signature": False})
        return unverified.get("jti", "unknown")
    except jwt.PyJWTError:
        return "unknown"

def verify_field_grant(grant_token: str, requested_geoid: str) -> bool:
    try:
        res = authorize_artifact(grant_token=grant_token, geoid=requested_geoid, raise_404_on_fail=True)
        return res.get("authorized", False)
    except Exception:  # noqa: BLE001
        return False

def authorize_artifact(
    grant_token: str | None = None,
    authority_token: str | None = None,
    list_id: str | None = None,
    geoid: str | None = None,
    scope: str | None = None,
    raise_404_on_fail: bool = True
) -> dict:
    pubkey = get_issuer_pubkey()
    if not pubkey:
        raise HTTPException(status_code=401, detail="Issuer public key not configured")
        
    test_dir = os.environ.get("TEST_STATUS_LIST_DIR")
    # Path (ii): Authority Credential
    if authority_token:
        apub = get_authority_pubkey()
        if not apub:
            raise HTTPException(status_code=401, detail="authority trust anchor not configured")
        try:
            if verify_authority_credential(
                authority_token, apub,
                requested_scope=scope,
                local_status_list_path=test_dir,
            ):
                return {"authorized": True, "used_authority": True, "authority_jti": get_jti(authority_token)}
        except Exception as e:  # noqa: BLE001
            if not raise_404_on_fail:
                raise HTTPException(status_code=401, detail=f"Authority credential invalid: {e}")
            
    # Path (i): Grant/Ownership
    if grant_token:
        try:
            if verify_sdjwt_grant(grant_token, pubkey, requested_geoid=geoid, requested_list_id=list_id, local_status_list_path=test_dir):
                return {"authorized": True, "used_authority": False}
        except Exception:  # noqa: BLE001, S110
            pass
    if raise_404_on_fail:
        raise HTTPException(status_code=404, detail="Artifact not found")
    else:
        raise HTTPException(status_code=403, detail="Not authorized for this seed")

@lru_cache
def get_jwks_client():
    jwks_url = os.getenv("JWKS_URL")
    return jwt.PyJWKClient(jwks_url, cache_keys=True)

def verify_token(token: str):
    try:
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        data = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"]
        )
        return data
    except Exception:  # noqa: BLE001
        return None

async def get_current_user(request: Request):

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    token = authorization.split(" ")[1]
    if not token:
        return None

    return verify_token(token)

async def require_hub_user(user = Depends(get_current_user)):

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hub authentication required"
        )
    return user
