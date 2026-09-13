"""SQLAlchemy 2.0 async models for the FluxSwarm PostgreSQL schema.

Mirrors the exact SQLite schema from :mod:`db` (ten tables plus the additive
columns ``_migrate`` introduced over time), so a SQLite database migrated via
:mod:`scripts.migrate_sqlite_to_postgres` yields identical data shapes.
Postgres-specific choices:

* ``users.plan`` is a native PostgreSQL ENUM (``user_plan``).
* ``users.id`` uses ``IDENTITY`` (PostgreSQL 10+) instead of AUTOINCREMENT.
* REAL timestamps become ``DOUBLE PRECISION`` (same epoch-seconds semantics).
* JSON payload columns (``agents``, ``detail``) use ``sa.JSON``.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    BIGINT,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import ENUM

# Alembic autogenerate requires a stable naming convention so constraint names
# are deterministic and upgrade/revision diffs stay clean.
_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

user_plan_enum = ENUM(
    "demo", "starter", "pro", "scale",
    name="user_plan",
    create_type=True,
)


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=_CONVENTION)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    pw_hash: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(user_plan_enum, nullable=False, server_default="demo")
    credits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    ref_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    referred_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    logged_out_at: Mapped[float | None] = mapped_column(BIGINT, nullable=True)

    __table_args__ = (
        CheckConstraint("credits >= 0", name="credits_non_negative"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    board_slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    # Additive columns from `db._migrate()`.
    launch_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    launch_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    launch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    launch_refunded: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    launch_updated_at: Mapped[float | None] = mapped_column(BIGINT, nullable=True)


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    referrer_code: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    referred_email: Mapped[str] = mapped_column(Text, nullable=False)
    rewarded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class SquadTemplate(Base):
    __tablename__ = "squad_templates"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    agents: Mapped[list] = mapped_column(JSON, nullable=False)
    price_credits: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="10"
    )
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class CustomAgent(Base):
    """User-defined squad member (P4). Name + objective + skills describe what
    this agent should produce; owners attach rows via /api/projects' agent_ids."""

    __tablename__ = "custom_agents"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    skills: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class TemplatePurchase(Base):
    __tablename__ = "template_purchases"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    template_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("squad_templates.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    buyer_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    gateway: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, server_default="{}")
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    telegram_chat_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    linked_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class TelegramCode(Base):
    __tablename__ = "telegram_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class PasswordReset(Base):
    __tablename__ = "password_resets"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)


class DemoUsage(Base):
    __tablename__ = "demo_usage"

    who: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[str] = mapped_column(Text, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        UniqueConstraint("who", "day", name="uq_demo_usage_who_day"),
    )


class ProviderAgreement(Base):
    """Per-user acceptance of a model provider's terms (Phase 3).

    Launching a squad that relies on a BYOK provider requires the user to have
    accepted that provider's agreement first (recorded with ``agreed_at`` and
    the version they accepted). PK = (user_id, provider) — one acceptance row
    per provider per user, idempotently refreshed on re-acceptance.
    """

    __tablename__ = "provider_agreements"

    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    agreed_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1.0")

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_provider_agreements_user_provider"),
    )


class ProviderUsage(Base):
    """Append-only provider accounting ledger (Phase F).

    One row per launch attempt; ``ok`` starts NULL and is filled when the
    launch finalizes (outcome updates key on the unique board slug). This is
    observability only — spend gating lives in provider_guard's fail-closed
    budget gate, never here.
    """

    __tablename__ = "provider_usage"

    id: Mapped[int] = mapped_column(BIGINT, Identity(), primary_key=True)
    created_at: Mapped[float] = mapped_column(BIGINT, nullable=False)
    day: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    surface: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    runtime_source: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tasks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)


# Keep an explicit list of every model for Alembic autogenerate and tooling.
MODELS: tuple[type[Base], ...] = (
    User, Project, Referral, CustomAgent, SquadTemplate, TemplatePurchase,
    PaymentEvent, TelegramLink, TelegramCode, PasswordReset, DemoUsage,
    ProviderAgreement, ProviderUsage,
)