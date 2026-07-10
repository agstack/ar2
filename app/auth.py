# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os
import jwt
from fastapi import Request, HTTPException, status, Depends
from functools import lru_cache

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
