import os
import requests
from fastapi import Request, HTTPException
from jose import jwt
from jose.exceptions import JWTError

CLERK_JWT_ISSUER = os.getenv("CLERK_JWT_ISSUER")  # must match token "iss"
JWKS_URL = f"{CLERK_JWT_ISSUER}/.well-known/jwks.json" if CLERK_JWT_ISSUER else None

_JWKS_CACHE = None

def _get_jwks():
    global _JWKS_CACHE
    if _JWKS_CACHE is not None:
        return _JWKS_CACHE
    if not JWKS_URL:
        raise RuntimeError("CLERK_JWT_ISSUER not set")
    _JWKS_CACHE = requests.get(JWKS_URL, timeout=10).json()
    return _JWKS_CACHE

def get_current_clerk_user_id(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    print("AUTH HEADER:", auth[:120], flush=True)
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth.split(" ", 1)[1].strip()

    jwks = _get_jwks()
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = jwks.get("keys", [])
        key = next((k for k in keys if k.get("kid") == kid), None)
        if not key:
            raise HTTPException(status_code=401, detail="No matching JWKS key (kid)")

        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=CLERK_JWT_ISSUER,
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub")
    return user_id
