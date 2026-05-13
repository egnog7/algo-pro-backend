# licensing/models.py
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.sql import func
from db import Base
from db import engine
from models import Base

Base.metadata.create_all(bind=engine)

class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, index=True)

    license_key = Column(String(64), unique=True, index=True, nullable=False)

    plan = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)

    stripe_customer_id = Column(String(64), index=True, nullable=True)
    stripe_subscription_id = Column(String(64), index=True, nullable=True)
    checkout_session_id = Column(String(128), nullable=True, unique=True, index=True)

    # ✅ NEW: owner identity (Clerk)
    owner_clerk_user_id = Column(String(64), index=True, nullable=True)
    owner_email = Column(String(255), nullable=True)     # optional display
    billing_email = Column(String(255), nullable=True)   # optional from Stripe

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)

    pairs_csv = Column(Text, nullable=False, default="")
    max_pairs = Column(Integer, nullable=False, default=2)
    optimizations_policy = Column(String(64), nullable=False, default="basic")
    priority_support = Column(Boolean, nullable=False, default=False)

    account_locked_to = Column(String(64), nullable=True)

    download_url = Column(Text, nullable=True)
    agent_version = Column(String(32), nullable=True)
