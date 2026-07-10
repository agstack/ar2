# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os
import jwt
from fastapi import Request, HTTPException, status, Depends
from functools import lru_cache

from app.grant_verifier import verify_sdjwt_grant

@lru_cache()
def get_issuer_pubkey():
    key_path = os.getenv("AR_TRUSTED_ISSUER_PUBKEY")
    if not key_path or not os.path.exists(key_path):
        return None
    with open(key_path, "rb") as f:
        return f.read()

def verify_field_grant(grant_token: str, requested_geoid: str) -> bool:
    pubkey = get_issuer_pubkey()
    if not pubkey:
        return False
    try:
        test_dir = os.getenv("TEST_STATUS_LIST_DIR")
        return verify_sdjwt_grant(
            sd_jwt=grant_token,
            public_key_pem=pubkey,
            requested_geoid=requested_geoid,
            local_status_list_path=test_dir
        )
    except Exception as e:
        print(f"Grant verification failed ({e}). Falling back to L0.")
        return False

@lru_cache()
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
    except Exception:
        return None

async def get_current_user(request: Request):

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    token = authorization.split(" ")[1]
    if not token:
        return None

    return verify_token(token)

async def require_l1(user = Depends(get_current_user)):

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="L1 authorization required"
        )
    return user
