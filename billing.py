# ---------- imports ----------
import os
import json
import time
import hashlib
from pathlib import Path
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from auth_clerk import get_current_clerk_user_id
from emailer import send_license_email as send_license_email_sg
from fastapi import (
    FastAPI,
    Request,
    Form,
    HTTPException,
    Query,
    Depends,
)
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import stripe
from uuid import uuid4
from typing import Dict, Any

from sqlalchemy.orm import Session

from db import SessionLocal, engine
from models import Base, License

Base.metadata.create_all(bind=engine)
print("[DB] Tables created/verified")

# ---------- env / stripe ----------
load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# ---------- DB dependency ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- domain constants ----------
ALL_PAIRS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "XAUUSD",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "GBPJPY",
    "EURJPY",
    "AUDJPY",
]

PRICE_TO_PLAN = {
    "price_1SMxUT2L1OGIrdKU2l6P4yev": "Basic",
    "price_1SN1B82L1OGIrdKUxo1dQFI4": "Pro",
    "price_1SN1Hw2L1OGIrdKUZ0HUSf5Z": "Elite",
}

PLAN_CONFIG = {
    "Basic": {
        "max_pairs_user_selectable": 2,
        "optimizations_policy": "1_per_year",
        "priority": False,
    },
    "Pro": {
        "max_pairs_user_selectable": 5,
        "optimizations_policy": "unlimited_up_to_5_pairs",
        "priority": False,
    },
    "Elite": {
        "max_pairs_user_selectable": len(ALL_PAIRS),
        "optimizations_policy": "priority_queue",
        "priority": True,
    },
}


def default_pairs_for_plan(plan_cfg: dict) -> list[str]:
    return ALL_PAIRS[: plan_cfg["max_pairs_user_selectable"]]


# ---------- preset + jobs stores (still in-memory / JSON) ----------

PRESets: dict[str, list[dict]] = {
    "EURUSD": [
        {
            "id": "EURUSD_v1",
            "pair": "EURUSD",
            "version": 1,
            "params": {"ema_fast": 12, "ema_slow": 200, "rsi": 14},
            "metrics": {"pf": 1.28, "wr": 0.56, "maxdd": 0.18},
            "created_at": datetime.utcnow().isoformat(),
            "created_by": "system",
            "window": "2023-01-01..2024-12-31",
        }
    ],
    "XAUUSD": [
        {
            "id": "XAUUSD_v1",
            "pair": "XAUUSD",
            "version": 1,
            "params": {"ema_fast": 5, "ema_slow": 190, "rsi": 16},
            "metrics": {"pf": 1.21, "wr": 0.54, "maxdd": 0.22},
            "created_at": datetime.utcnow().isoformat(),
            "created_by": "system",
            "window": "2023-01-01..2024-12-31",
        }
    ],
}

# Which preset is assigned to each license per pair (in-memory)
LICENSE_PRESETS: dict[str, dict[str, str]] = {}  # lic -> { "EURUSD": "v1", ... }

# Optimization job queue (stub)
DATA_DIR = Path(os.getenv("LICENSE_DATA_DIR", "."))
JOBS_JSON = DATA_DIR / "jobs.json"
OPTJOBS: Dict[str, Dict[str, Any]] = {}  # job_id -> job


def load_jobs() -> None:
    global OPTJOBS
    if JOBS_JSON.exists():
        try:
            OPTJOBS = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
            print(f"[STORE] Loaded {len(OPTJOBS)} jobs from {JOBS_JSON}")
        except Exception as e:
            print(f"[STORE] Failed to load {JOBS_JSON}: {e}")


def save_jobs() -> None:
    try:
        JOBS_JSON.write_text(
            json.dumps(OPTJOBS, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[STORE] Failed to save {JOBS_JSON}: {e}")


# ---------- app ----------
app = FastAPI()
load_jobs()

# ---------- cors ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://algo-pro-portal.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- dev seed: TEST-LIC-1234 in DB (optional) ----------
@app.on_event("startup")
def seed_test_license():
    """Create TEST-LIC-1234 in the DB if it doesn't exist (dev helper)."""
    if os.getenv("SEED_TEST_LICENSE", "1") != "1":
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(License)
            .filter(License.license_key == "TEST-LIC-1234")
            .first()
        )
        if existing:
            return

        plan_cfg = PLAN_CONFIG["Pro"]
        pairs_csv = ",".join(
            ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "GBPJPY"]
        )
        expiry = datetime.utcnow() + timedelta(days=365 * 50)  # far future

        lic = License(
            license_key="TEST-LIC-1234",
            plan="Pro",
            status="active",
            stripe_customer_id="cus_dummy",
            stripe_subscription_id="sub_dummy",
            expires_at=expiry,
            pairs_csv=pairs_csv,
            max_pairs=plan_cfg["max_pairs_user_selectable"],
            optimizations_policy=plan_cfg["optimizations_policy"],
            priority_support=plan_cfg["priority"],
            account_locked_to=None,
            download_url=os.getenv(
                "BASE_DOWNLOAD_URL",
                "https://yourcdn/SubscribedAgent.mq5",
            ),
        )
        db.add(lic)
        db.commit()
        print("[SEED] Created TEST-LIC-1234 in DB")
    except Exception as e:
        print(f"[SEED] Failed to seed test license: {e}")
    finally:
        db.close()


# ---------- request models ----------
class CheckoutRequest(BaseModel):
    email: str
    price_id: str
    clerk_user_id: str | None = None

class UpdatePairsRequest(BaseModel):
    license_key: str
    pairs: list[str]


class ApplyPresetRequest(BaseModel):
    license_key: str
    pair: str
    version: str  # e.g. "v2.7"


class RunOptRequest(BaseModel):
    license_key: str
    pair: str
    objective: str | None = "pf"  # "pf" or "wr" etc.


class CreatePortalRequest(BaseModel):
    license_key: str


# ---------- email helper ----------
def send_license_email(to_email: str, license_obj: License):
    pairs = license_obj.pairs_csv or ""
    plan_name = license_obj.plan
    expiry = (
        license_obj.expires_at.date().isoformat()
        if license_obj.expires_at
        else "N/A"
    )
    download_url = (
        license_obj.download_url
        or os.getenv(
            "BASE_DOWNLOAD_URL", "https://yourcdn/SubscribedAgent.mq5"
        )
    )

    body = f"""
Welcome aboard 🎉

Your plan: {plan_name}
Pairs unlocked: {pairs}
License expires on: {expiry}

License Key:
{license_obj.license_key}

EA Download:
{download_url}

How to activate:
1. Open MT5.
2. Tools -> Options -> Expert Advisors -> tick "Allow WebRequest" and add the API URL we give you.
3. Drag SubscribedAgent.mq5 onto ANY chart.
4. In Inputs:
   LicenseKey = {license_obj.license_key}
   ServerBaseURL = http://127.0.0.1:8000  (or our live URL when deployed)
5. Click OK. The EA panel should show "Status: Valid ✅".

NOTE:
- Your license will lock to the FIRST MT5 account + machine (HWID) that runs it.
- Trying to share that key with someone else will fail activation.

If you need to move it to a new account/VPS, contact support.

Happy Trading,

Algo Pro
"""
    msg = MIMEText(body)
    msg["Subject"] = "Your Trading Bot License Key"
    msg["From"] = "support@yourbrand.com"
    msg["To"] = to_email
    print("=== EMAIL (preview) ===")
    print(msg.as_string())
    print("=======================")


# ---------- license issue/renew (DB version) ----------
def issue_or_renew_license(
    db: Session,
    *,
    email: str,
    stripe_customer_id: str,
    sub_id: str,
    price_id: str,
    plan_name: str,
    checkout_session_id: str | None = None,
) -> License:
    plan_cfg = PLAN_CONFIG.get(plan_name, PLAN_CONFIG["Basic"])
    selected_pairs_csv = ",".join(default_pairs_for_plan(plan_cfg))
    expires_at = datetime.utcnow() + timedelta(days=31)

    lic = (
        db.query(License)
        .filter(License.stripe_customer_id == stripe_customer_id)
        .first()
    )

    if lic:
        lic.plan = plan_name
        lic.status = "active"
        lic.expires_at = expires_at
        lic.max_pairs = plan_cfg["max_pairs_user_selectable"]
        lic.optimizations_policy = plan_cfg["optimizations_policy"]
        lic.priority_support = plan_cfg["priority"]
        lic.stripe_subscription_id = sub_id

        if checkout_session_id:
            lic.checkout_session_id = checkout_session_id

        if not lic.pairs_csv:
            lic.pairs_csv = selected_pairs_csv

        if hasattr(lic, "user_email"):
            setattr(lic, "user_email", email)

        if hasattr(lic, "billing_email"):
            lic.billing_email = email

    else:
        from secrets import token_hex

        license_key = f"LIC-{int(time.time())}-{token_hex(4).upper()}"

        lic = License(
            license_key=license_key,
            plan=plan_name,
            status="active",
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=sub_id,
            expires_at=expires_at,
            pairs_csv=selected_pairs_csv,
            max_pairs=plan_cfg["max_pairs_user_selectable"],
            optimizations_policy=plan_cfg["optimizations_policy"],
            priority_support=plan_cfg["priority"],
            account_locked_to=None,
            download_url=os.getenv(
                "BASE_DOWNLOAD_URL", "https://yourcdn/SubscribedAgent.mq5"
            ),
        )

        if checkout_session_id:
            lic.checkout_session_id = checkout_session_id

        if hasattr(lic, "user_email"):
            setattr(lic, "user_email", email)

        if hasattr(lic, "billing_email"):
            lic.billing_email = email

        db.add(lic)

    db.commit()
    db.refresh(lic)

    return lic

# ---------- Recompute presetVer string from in-memory presets ----------
def recompute_presetVer(license_obj: License) -> str:
    """
    Build a "XAUUSD:v1;EURUSD:v1" string from:
    - license_obj.pairs_csv
    - LICENSE_PRESETS mapping (version per pair)
    Fallback: v1 for any pair without explicit version.
    """
    mapping = LICENSE_PRESETS.get(license_obj.license_key, {})
    parts: list[str] = []

    if not license_obj.pairs_csv:
        return ""

    for p in license_obj.pairs_csv.split(","):
        p = p.strip()
        if not p:
            continue
        ver = mapping.get(p)  # e.g. "v2.4"
        if not ver:
            ver = "v1"
        parts.append(f"{p}:{ver}")

    return ";".join(parts)


# ---------- helper: sign_blob (not yet enforced) ----------
def sign_blob(blob: str) -> str:
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------- create checkout ----------

@app.post("/stripe/create-checkout")
def create_checkout(req: CheckoutRequest):

    frontend_base = os.getenv(
        "FRONTEND_BASE_URL",
        "http://localhost:3000"
    )

    customer = stripe.Customer.create(
        email=req.email
    )

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer["id"],
        line_items=[
            {
                "price": req.price_id,
                "quantity": 1
            }
        ],
        client_reference_id=req.clerk_user_id or None,
        metadata={
            "clerk_user_id": req.clerk_user_id or ""
        },

        success_url=(
            f"{frontend_base}/checkout/success"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),

        cancel_url=(
            f"{frontend_base}/checkout/cancel"
        ),

        allow_promotion_codes=True,
    )

    return {
        "checkout_url": session.url
    }
# ---------- stripe webhook ----------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            WEBHOOK_SECRET,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    event_raw = event._data if hasattr(event, "_data") else event

    etype = event_raw["type"]
    data_wrapper = event_raw["data"]
    data_obj = data_wrapper["object"]

    data = data_obj._data if hasattr(data_obj, "_data") else data_obj

    # ------------------------------
    # 1) Checkout completed
    # ------------------------------
    if etype == "checkout.session.completed":

        session_id = data.get("id")
        cust_id = data.get("customer")
        sub_id = data.get("subscription")

        metadata = data.get("metadata") or {}

        if hasattr(metadata, "_data"):
            metadata = metadata._data

        clerk_user_id = (
            data.get("client_reference_id")
            or metadata.get("clerk_user_id")
        )

        if not cust_id:
            raise HTTPException(
                status_code=400,
                detail="Missing Stripe customer id",
            )

        if not sub_id:
            print(
                f"[STRIPE] checkout.session.completed "
                f"but no subscription yet. session={session_id}"
            )
            return {"ok": True}

        sub_obj = stripe.Subscription.retrieve(sub_id)
        sub_data = sub_obj._data if hasattr(sub_obj, "_data") else sub_obj
        price_id = sub_data["items"]["data"][0]["price"]["id"]

        plan_name = PRICE_TO_PLAN.get(price_id, "Basic")

        customer_obj = stripe.Customer.retrieve(cust_id)
        customer_data = customer_obj._data if hasattr(customer_obj, "_data") else customer_obj
        email = customer_data.get("email")

        if not email:
            print(
                f"[STRIPE] No customer email found. "
                f"cust={cust_id} session={session_id}"
            )
            return {"ok": True}
        
        print(f"[DEBUG] session_id={session_id}")

        lic = issue_or_renew_license(
            db=db,
            email=email,
            stripe_customer_id=cust_id,
            sub_id=sub_id,
            price_id=price_id,
            plan_name=plan_name,
            checkout_session_id=session_id,
        )

        if clerk_user_id:
            lic.owner_clerk_user_id = clerk_user_id
            lic.owner_email = email

        lic.billing_email = email

        print(f"[DEBUG] session_id={session_id}")

        db.commit()
        db.refresh(lic)

        print(f"[DEBUG] persisted checkout_session_id={lic.checkout_session_id}")

        portal_url = (
            f"{os.getenv('FRONTEND_BASE_URL', 'http://localhost:3000')}"
            f"/license/{lic.license_key}"
        )

        try:
            status_code = send_license_email_sg(
                to_email=email,
                license_key=lic.license_key,
                portal_url=portal_url,
            )

            print(
                f"[EMAIL] Sent license email to {email} "
                f"status={status_code}"
            )

        except Exception as e:
            print(
                f"[EMAIL] Failed to send license email "
                f"to {email}: {e}"
            )

        print(
            f"[STRIPE] License {lic.license_key} for {email} "
            f"plan={plan_name} "
            f"exp={lic.expires_at} "
            f"session={session_id}"
        )

        return {"ok": True}

    # ------------------------------
    # 2) Payment succeeded
    # ------------------------------
    if etype == "invoice.payment_succeeded":

        sub_id = data.get("subscription")

        if sub_id:

            lic = (
                db.query(License)
                .filter(License.stripe_subscription_id == sub_id)
                .first()
            )

            if lic:
                lic.expires_at = datetime.utcnow() + timedelta(days=31)
                lic.status = "active"

                db.commit()

                print(
                    f"[STRIPE] Renewed license "
                    f"{lic.license_key} → {lic.expires_at}"
                )

        return {"ok": True}

    # ------------------------------
    # 3) Payment failed / cancelled
    # ------------------------------
    if etype in (
        "customer.subscription.deleted",
        "invoice.payment_failed",
    ):

        sub_id = (
            data.get("id")
            if etype == "customer.subscription.deleted"
            else data.get("subscription")
        )

        if sub_id:

            lic = (
                db.query(License)
                .filter(License.stripe_subscription_id == sub_id)
                .first()
            )

            if lic:
                lic.status = "suspended"

                db.commit()

                print(
                    f"[STRIPE] Suspended license "
                    f"{lic.license_key} (sub={sub_id})"
                )

        return {"ok": True}

    return {"ok": True}

# ---------- EA endpoints ----------
@app.get("/checkout/license-by-session")
def get_license_by_session(
    session_id: str,
    db: Session = Depends(get_db),
):
    lic = (
        db.query(License)
        .filter(License.checkout_session_id == session_id)
        .first()
    )

    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    return {
        "license_key": lic.license_key,
        "plan": lic.plan,
        "status": lic.status,
        "expires_at": str(lic.expires_at),
    }

@app.post("/license/activate", response_class=PlainTextResponse)
def activate(
    license: str = Form(...),
    account: str = Form(...),
    hwid: str = Form(...),
    ts: str = Form(...),
    db: Session = Depends(get_db),
):
    lic = (
        db.query(License)
        .filter(License.license_key == license)
        .first()
    )
    if not lic or lic.status != "active":
        return "status=ERR\nreason=invalid_or_inactive\n"

    # Bind to first account+hwid combo
    combo = f"{account}:{hwid}"
    if lic.account_locked_to is None:
        lic.account_locked_to = combo
        db.commit()
    elif lic.account_locked_to != combo:
        return "status=ERR\nreason=bound_to_other_account\n"

    # Compute presetVer string on the fly
    preset_ver = recompute_presetVer(lic)

    expiry_str = (
        lic.expires_at.date().isoformat() if lic.expires_at else "N/A"
    )
    blob = (
        f"status=OK\nplan={lic.plan}\n"
        f"pairs={lic.pairs_csv or ''}\n"
        f"presetVer={preset_ver}\n"
        f"expiry={expiry_str}\n"
    )
    # signature placeholder
    return blob + "sig=dummy\n"


@app.post("/license/heartbeat", response_class=PlainTextResponse)
def heartbeat(
    license: str = Form(...),
    account: str = Form(...),
    hwid: str = Form(...),
    ts: str = Form(...),
    db: Session = Depends(get_db),
):
    lic = (
        db.query(License)
        .filter(License.license_key == license)
        .first()
    )
    if not lic or lic.status != "active":
        return "continue=false\nreason=invalid\n"

    combo = f"{account}:{hwid}"
    if lic.account_locked_to != combo:
        return "continue=false\nreason=invalid\n"

    if lic.expires_at and datetime.utcnow() > lic.expires_at:
        return "continue=false\nreason=expired\n"

    return "continue=true\n"


# ---------- portal endpoints ----------
# ✅ TEMP STUB (we’ll wire real Clerk JWT verification next step)
import os
import time
import requests
from fastapi import Request, HTTPException
from jose import jwt
from jose.exceptions import JWTError

# Required
CLERK_JWT_ISSUER = os.getenv("CLERK_JWT_ISSUER")  # e.g. https://<your-clerk-domain>
# Optional (recommended if you set an audience in Clerk JWT template)
CLERK_JWT_AUDIENCE = os.getenv("CLERK_JWT_AUDIENCE")  # e.g. "algo-pro-api"

# Clerk JWKS is typically served from your issuer domain:
# https://<issuer>/.well-known/jwks.json
CLERK_JWKS_URL = f"{CLERK_JWT_ISSUER}/.well-known/jwks.json" if CLERK_JWT_ISSUER else None

# Simple in-memory cache (avoid fetching JWKS every request)
_JWKS_CACHE = {"jwks": None, "fetched_at": 0.0}
_JWKS_TTL_SECONDS = 60 * 10  # 10 minutes


def _get_jwks():
    if not CLERK_JWKS_URL:
        raise RuntimeError("CLERK_JWT_ISSUER not set (CLERK_JWKS_URL cannot be built)")

    now = time.time()
    if _JWKS_CACHE["jwks"] and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE["jwks"]

    jwks = requests.get(CLERK_JWKS_URL, timeout=10).json()
    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["fetched_at"] = now
    return jwks


def get_current_clerk_user_id(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    jwks = _get_jwks()

    try:
        # jose will select the correct key using the token's "kid"
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            issuer=CLERK_JWT_ISSUER,
            audience=CLERK_JWT_AUDIENCE if CLERK_JWT_AUDIENCE else None,
            options={
                "verify_aud": True if CLERK_JWT_AUDIENCE else False,
            },
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub")

    return str(user_id)


@app.get("/me/license/{license_key}")
def me_license(
    license_key: str,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(get_current_clerk_user_id),
):
    lic = (
        db.query(License)
        .filter(License.license_key == license_key)
        .first()
    )
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    # 1) Claim-on-first-access
    if not getattr(lic, "owner_clerk_user_id", None):
        lic.owner_clerk_user_id = clerk_user_id
        db.commit()
        db.refresh(lic)

    # 2) Enforce ownership
    if lic.owner_clerk_user_id != clerk_user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this license")

    expiry_str = lic.expires_at.date().isoformat() if lic.expires_at else None

    return {
        "plan": lic.plan,
        "status": lic.status,
        "expires_at": expiry_str,
        "pairs": lic.pairs_csv or "",
        "max_pairs": lic.max_pairs,
        "optimizations_policy": lic.optimizations_policy,
        "priority_support": lic.priority_support,
        "license_key": lic.license_key,
        "download_url": lic.download_url
        or os.getenv("BASE_DOWNLOAD_URL", "https://yourcdn/SubscribedAgent.mq5"),
        "account_locked_to": (
            (lic.account_locked_to or "").split(":")[0]
            if lic.account_locked_to
            else None
        ),
    }
    
@app.post("/me/update-pairs")
def update_pairs(
    req: UpdatePairsRequest,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(get_current_clerk_user_id),
):
    lic = db.query(License).filter(License.license_key == req.license_key).first()
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    if not getattr(lic, "owner_clerk_user_id", None):
        lic.owner_clerk_user_id = clerk_user_id
        db.commit()
        db.refresh(lic)

    if lic.owner_clerk_user_id != clerk_user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this license")

    if (lic.status or "").lower() != "active":
        raise HTTPException(status_code=403, detail="License not active")

    max_allowed = lic.max_pairs
    if len(req.pairs) > max_allowed:
        raise HTTPException(status_code=400, detail=f"Too many pairs selected (max {max_allowed})")

    for p in req.pairs:
        if p not in ALL_PAIRS:
            raise HTTPException(status_code=400, detail=f"Invalid pair: {p}")

    lic.pairs_csv = ",".join(req.pairs)
    db.commit()

    return {"ok": True, "pairs": lic.pairs_csv, "max_pairs": max_allowed}


# ---------- GET /portal/presets ----------
@app.get("/portal/presets")
def list_presets(pair: str):
    if pair not in ALL_PAIRS:
        raise HTTPException(status_code=400, detail="Invalid pair")
    return {"pair": pair, "presets": PRESets.get(pair, [])}


# ---------- helpers: presetVer get/set ----------
def get_preset_ver(s: str, pair: str) -> str | None:
    # "XAUUSD:v1;EURUSD:v1" -> version for pair
    if not s:
        return None
    parts = dict(p.split(":") for p in s.split(";") if ":" in p)
    return parts.get(pair)


def set_preset_ver(s: str, pair: str, version: str) -> str:
    parts = dict(p.split(":") for p in s.split(";") if ":" in p) if s else {}
    parts[pair] = version
    return ";".join(f"{k}:{v}" for k, v in sorted(parts.items()))


# ---------- /portal/presets/apply ----------
@app.post("/portal/presets/apply")
def apply_preset(req: ApplyPresetRequest, db: Session = Depends(get_db)):
    lic = (
        db.query(License)
        .filter(License.license_key == req.license_key)
        .first()
    )
    if not lic or lic.status != "active":
        raise HTTPException(
            status_code=404, detail="License not found or inactive"
        )
    if req.pair not in ALL_PAIRS:
        raise HTTPException(status_code=400, detail="Unsupported pair")

    mapping = LICENSE_PRESETS.setdefault(req.license_key, {})
    mapping[req.pair] = req.version  # e.g. "v2.7"

    preset_ver = recompute_presetVer(lic)
    # We don't persist presetVer in DB yet; we compute on the fly.

    return {"ok": True, "pair": req.pair, "presetVer": preset_ver}


# ---------- /portal/optimization/run ----------
@app.post("/portal/optimization/run")
def run_optimization(req: RunOptRequest, db: Session = Depends(get_db)):
    lic = (
        db.query(License)
        .filter(License.license_key == req.license_key)
        .first()
    )
    if not lic or lic.status != "active":
        raise HTTPException(
            status_code=404, detail="License not found or inactive"
        )

    if req.pair not in ALL_PAIRS:
        raise HTTPException(status_code=400, detail="Unsupported pair")

    # basic plan policy example: 1/yr not enforced here; we’ll add later
    job_id = (
        f"job-{int(time.time())}-"
        f"{hash(req.license_key + req.pair) & 0xffff:x}"
    )
    OPTJOBS[job_id] = {
        "job_id": job_id,
        "license_key": req.license_key,
        "pair": req.pair,
        "objective": req.objective,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "results": None,  # filled on "finish"
    }
    save_jobs()

    # simulate background finish in ~3s using a poor-man timer
    # (production: real worker/queue)
    import threading
    import random

    def _finish():
        time.sleep(3)
        # fake “top configs”
        tops = [
            {
                "version": f"v{random.randint(2,9)}.{random.randint(0,9)}",
                "pf": round(random.uniform(1.1, 1.8), 2),
                "wr": round(random.uniform(0.45, 0.62), 2),
            },
            {
                "version": f"v{random.randint(2,9)}.{random.randint(0,9)}",
                "pf": round(random.uniform(1.05, 1.6), 2),
                "wr": round(random.uniform(0.42, 0.58), 2),
            },
            {
                "version": f"v{random.randint(2,9)}.{random.randint(0,9)}",
                "pf": round(random.uniform(1.0, 1.5), 2),
                "wr": round(random.uniform(0.40, 0.55), 2),
            },
        ]
        OPTJOBS[job_id]["status"] = "finished"
        OPTJOBS[job_id]["results"] = {"top": tops}
        save_jobs()
        print(f"[OPT] Finished {job_id} for {req.pair}")

    threading.Thread(target=_finish, daemon=True).start()

    return {"job_id": job_id, "status": "queued"}


# ---------- /portal/optimization/result/{job_id} ----------
@app.get("/portal/optimization/result/{job_id}")
def get_opt_result(job_id: str):
    job = OPTJOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "results": job.get("results"),
    }


# ---------- POST /me/create-portal ----------
@app.post("/me/create-portal")
def create_portal(req: CreatePortalRequest, db: Session = Depends(get_db)):
    lic = (
        db.query(License)
        .filter(License.license_key == req.license_key)
        .first()
    )
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    if not lic.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="License is not Stripe-managed (no customer id)",
        )

    # Where Stripe should send them back after managing subscription
    return_url = os.getenv("PORTAL_RETURN_URL", "http://localhost:3000")

    session = stripe.billing_portal.Session.create(
        customer=lic.stripe_customer_id,
        return_url=return_url,
    )

    return {"url": session.url}


# ---------- download redirect (signed URL later) ----------
@app.get("/portal/download")
def portal_download(
    license_key: str = Query(...), db: Session = Depends(get_db)
):
    lic = (
        db.query(License)
        .filter(License.license_key == license_key)
        .first()
    )
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")
    if lic.status != "active":
        raise HTTPException(status_code=403, detail="License not active")

    url = lic.download_url or os.getenv(
        "BASE_DOWNLOAD_URL", "https://yourcdn/SubscribedAgent.mq5"
    )

    return {"url": url}

# ---------- POST /me/resend-license ----------
class ResendLicenseRequest(BaseModel):
    license_key: str

@app.post("/me/resend-license")
def resend_license(req: ResendLicenseRequest, db: Session = Depends(get_db)):
    key = (req.license_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing license_key")

    lic = db.query(License).filter(License.license_key == key).first()
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    status = (lic.status or "").lower().strip()
    if status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resend email for status '{status or 'unknown'}'"
        )

    # Prefer DB-stored email if present (best reliability)
    to_email = (
        getattr(lic, "billing_email", None)
        or getattr(lic, "owner_email", None)
        or getattr(lic, "user_email", None)
    )

    if to_email:
        to_email = to_email.strip()

    # Fallback: try Stripe customer email
    if not to_email and lic.stripe_customer_id:
        try:
            cust = stripe.Customer.retrieve(lic.stripe_customer_id)
            to_email = (cust.get("email") or "").strip() or None
        except Exception as e:
            # keep None; we'll return a helpful error below
            print(f"[EMAIL] Stripe email lookup failed for {lic.license_key}: {e}")
            to_email = None

    if not to_email:
        raise HTTPException(
            status_code=400,
            detail="No email found for this license yet. (Add user_email to the license record or ensure Stripe customer has an email.)"
        )

    # Send email (currently preview/print mode in your helper)
    portal_url = (
        f"{os.getenv('FRONTEND_BASE_URL', 'http://localhost:3000')}"
        f"/license/{lic.license_key}"
    )

    send_license_email_sg(
        to_email=to_email,
        license_key=lic.license_key,
        portal_url=portal_url,
    )
    return {"ok": True}

class ResolveCheckoutResp(BaseModel):
    ok: bool
    license_key: str
    sent_to: str | None = None
    status: str | None = None
    expires_at: str | None = None

@app.get("/stripe/checkout/resolve")
def resolve_checkout(session_id: str, db: Session = Depends(get_db)):
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="Missing session_id")

    lic = db.query(License).filter(License.checkout_session_id == sid).first()

    # fallback: resolve session from Stripe and match by subscription/customer
    if not lic:
        try:
            sess = stripe.checkout.Session.retrieve(sid)
            sub_id = sess.get("subscription")
            cust_id = sess.get("customer")
        except Exception:
            raise HTTPException(status_code=404, detail="Checkout session not found")

        q = db.query(License)
        if sub_id:
            lic = q.filter(License.stripe_subscription_id == sub_id).first()
        if not lic and cust_id:
            lic = q.filter(License.stripe_customer_id == cust_id).order_by(License.id.desc()).first()

        if lic:
            # backfill session id for future reliability
            lic.checkout_session_id = sid
            db.commit()

    if not lic:
        raise HTTPException(status_code=404, detail="License not found for this checkout")

    # pick email (db first, stripe fallback)
    to_email = getattr(lic, "user_email", None)
    if not to_email and lic.stripe_customer_id:
        try:
            cust = stripe.Customer.retrieve(lic.stripe_customer_id)
            to_email = cust.get("email")
        except Exception:
            to_email = None

    return {
        "ok": True,
        "license_key": lic.license_key,
        "sent_to": (to_email or None),
        "status": lic.status,
        "expires_at": lic.expires_at.isoformat() if getattr(lic, "expires_at", None) else None,
    }


class ResendCheckoutReq(BaseModel):
    session_id: str

@app.post("/stripe/checkout/resend")
def resend_from_checkout(req: ResendCheckoutReq, db: Session = Depends(get_db)):
    sid = (req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="Missing session_id")

    # use the resolver logic
    data = resolve_checkout(sid, db)  # returns dict
    lic_key = data["license_key"]

    lic = db.query(License).filter(License.license_key == lic_key).first()
    if not lic:
        raise HTTPException(status_code=404, detail="License not found")

    status = (lic.status or "").lower().strip()
    if status != "active":
        raise HTTPException(status_code=400, detail=f"Cannot resend for status '{status}'")

    to_email = getattr(lic, "user_email", None)
    if to_email:
        to_email = to_email.strip()

    if not to_email and lic.stripe_customer_id:
        cust = stripe.Customer.retrieve(lic.stripe_customer_id)
        to_email = (cust.get("email") or "").strip() or None

    if not to_email:
        raise HTTPException(status_code=400, detail="No email found for this license yet.")

    send_license_email(to_email, lic)
    print(f"[EMAIL] Resent via checkout session → {to_email} ({lic.license_key})")
    return {"ok": True, "license_key": lic.license_key, "sent_to": to_email}

# GET /checkout/license-by-session
@app.get("/checkout/license-by-session")
def get_license_by_session(session_id: str, db: Session = Depends(get_db)):
    lic = (
        db.query(License)
        .filter(License.checkout_session_id == session_id)
        .first()
    )
    if not lic:
        raise HTTPException(status_code=404, detail="License not ready yet")

    return {
        "license_key": lic.license_key,
        "status": lic.status,
        "expires_at": lic.expires_at.isoformat(),
        "plan": lic.plan,
    }
