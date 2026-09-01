# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

import base64
import hashlib
import json
import os
import time
import urllib.request
import zlib

import jwt as pyjwt

VCT = "agstack.org/credentials/field-access-grant/v1"
AUTHORITY_VCT = "agstack.org/credentials/traceforward-authority/v1"
CLOCK_SKEW_SECONDS = 300

class VerificationError(Exception):
    pass

def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _split(sd_jwt: str) -> tuple[str, list[str]]:
    if "~" not in sd_jwt:
        return sd_jwt, []
    parts = sd_jwt.split("~")
    return parts[0], [p for p in parts[1:] if p]

def verify_sdjwt_grant(
    sd_jwt: str,
    public_key_pem: bytes,
    requested_geoid: str | None = None,
    requested_list_id: str | None = None,
    local_status_list_path: str | None = None,
    now: int | None = None
    ) -> bool:

    token, disclosures = _split(sd_jwt)
    now = now if now is not None else int(time.time())

    try:
        claims = pyjwt.decode(
            token,
            public_key_pem,
            algorithms=["EdDSA"],
            leeway=CLOCK_SKEW_SECONDS,
            options={"verify_exp": False, "verify_iat": False},
        )
    except pyjwt.PyJWTError as e:
        raise VerificationError(f"signature verification failed: {e}") from e

    exp = claims.get("exp")
    if exp is None:
        raise VerificationError("credential has no exp")
    if now > int(exp):
        raise VerificationError("credential expired")
    
    iat = claims.get("iat")
    if iat is not None and int(iat) > now + CLOCK_SKEW_SECONDS:
        raise VerificationError("credential issued in the future")

    if claims.get("vct") != VCT:
        raise VerificationError(f"unexpected vct: {claims.get('vct')}")

    disclosed_geoids = []
    if disclosures:
        sd_digests = set(claims.get("_sd", []))
        if claims.get("_sd_alg", "sha-256") != "sha-256":
            raise VerificationError("unsupported _sd_alg")
        
        for encoded in disclosures:
            digest = _b64url(hashlib.sha256(encoded.encode("ascii")).digest())
            if digest not in sd_digests:
                raise VerificationError("disclosure digest not present in _sd")
            
            try:
                decoded_json = _b64url_decode(encoded)
                _salt, claim_name, value = json.loads(decoded_json)
            except Exception as e:
                raise VerificationError("invalid disclosure format") from e
                
            if claim_name.startswith("fields."):
                disclosed_geoids.append(value)

    if requested_list_id and claims.get("sub") != requested_list_id:
        raise VerificationError(f"requested list_id {requested_list_id} does not match grant subject")

    if requested_geoid and requested_geoid not in disclosed_geoids:
        raise VerificationError(f"requested GeoID {requested_geoid} not among disclosed GeoIDs")

    _verify_status_list(claims, local_status_list_path)
    return True

def _verify_status_list(claims: dict, local_status_list_path: str | None = None):
    status_claim = claims.get("status", {}).get("status_list", {})
    uri = status_claim.get("uri")
    idx = status_claim.get("idx")
    
    if uri is None or idx is None:
        raise VerificationError("missing status list uri or idx")

    status_list_data = None
    if local_status_list_path:
        filepath = os.path.join(local_status_list_path, "status_list.txt")
        if not os.path.exists(filepath):
            filename = uri.rstrip('/').split('/')[-1]
            filepath = os.path.join(local_status_list_path, filename)
            
        try:
            with open(filepath, "rb") as f:
                status_list_data = f.read()
        except Exception as e:  # noqa: BLE001
            raise VerificationError(f"failed to load local status list: {e}")
    else:
        try:
            req = urllib.request.Request(uri, headers={'Accept': 'application/statuslist+jwt'})
            with urllib.request.urlopen(req, timeout=10) as response:
                status_list_data = response.read()
        except Exception as e:  # noqa: BLE001
            raise VerificationError(f"failed to fetch status list: {e}")

    try:
        bitstring_encoded = None
        try:
            sl_json = json.loads(status_list_data)
            bitstring_encoded = sl_json.get("encoded")
        except json.JSONDecodeError:
            pass
            
        if not bitstring_encoded:
            try:
                decoded_sl = pyjwt.decode(status_list_data, options={"verify_signature": False})
                bitstring_encoded = decoded_sl.get("vc", {}).get("credentialSubject", {}).get("status_list", {}).get("lst")
            except pyjwt.PyJWTError:
                pass
                
        if not bitstring_encoded:
            bitstring_encoded = status_list_data.decode('utf-8').strip()

        bitstring_compressed = _b64url_decode(bitstring_encoded)
        bitstring = zlib.decompress(bitstring_compressed)
    except Exception as e:  # noqa: BLE001
        raise VerificationError(f"failed to decode/decompress status list: {e}")

    byte_idx = idx // 8
    bit_idx = idx % 8
    if byte_idx >= len(bitstring):
        raise VerificationError("status index out of bounds")
        
    is_revoked = (bitstring[byte_idx] & (1 << bit_idx)) != 0
    if is_revoked:
        raise VerificationError(f"credential is revoked (status bit {idx} set)")

def verify_authority_credential(
    sd_jwt: str,
    public_key_pem: bytes,
    requested_scope: str | None = None,
    local_status_list_path: str | None = None,
    now: int | None = None
) -> bool:
    token, _disclosures = _split(sd_jwt)
    now = now if now is not None else int(time.time())

    try:
        claims = pyjwt.decode(
            token,
            public_key_pem,
            algorithms=["EdDSA"],
            leeway=CLOCK_SKEW_SECONDS,
            options={"verify_exp": False, "verify_iat": False},
        )
    except pyjwt.PyJWTError as e:
        raise VerificationError(f"signature verification failed: {e}") from e

    exp = claims.get("exp")
    if exp is None:
        raise VerificationError("credential has no exp")
    if now > int(exp):
        raise VerificationError("credential expired")
    
    if claims.get("vct") != AUTHORITY_VCT:
        raise VerificationError(f"unexpected vct: {claims.get('vct')}")

    scope_claim = claims.get("scope")
    cred_scopes = scope_claim if isinstance(scope_claim, list) else [scope_claim] if scope_claim else []
    if not cred_scopes:
        raise VerificationError("authority credential has no scope")
    if requested_scope and requested_scope not in cred_scopes and "global" not in cred_scopes:
        raise VerificationError(
            f"requested scope {requested_scope} not in credential scopes {cred_scopes}")

    _verify_status_list(claims, local_status_list_path)
    return True
