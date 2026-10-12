"""risk gate enforcement

Revision ID: 6be86390d388
Revises: 8cf298f10d2a
Create Date: 2026-10-12 15:08:15.447538+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6be86390d388"
down_revision: str | None = "8cf298f10d2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of RiskReason values at this revision (do not import app code).
KNOWN_REASONS_V2 = [
    "kill_switch_not_armed",
    "trading_disabled",
    "system_not_ready",
    "risk_state_unavailable",
    "reconciliation_unresolved",
    "market_data_stale",
    "valuation_unavailable",
    "exchange_error_rate",
    "market_not_allowed",
    "outside_schedule",
    "max_order_notional",
    "max_asset_exposure",
    "max_total_exposure",
    "max_open_orders",
    "max_daily_loss",
    "max_drawdown",
    "order_frequency",
    "balance_reserve",
    "spread_too_wide",
    "market_orders_disallowed",
    "insufficient_balance",
    "ticker_unavailable",
    "price_outside_band",
    "position_policy",
    "insufficient_position",
    "precision_violation",
    "below_minimum",
]
KNOWN_REASONS_V1 = KNOWN_REASONS_V2[: KNOWN_REASONS_V2.index("insufficient_balance")]


def _reasons_check(reasons: list[str]) -> str:
    values = ", ".join(f"'{r}'" for r in reasons)
    return f"reasons <@ ARRAY[{values}]::text[]"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_risk_decisions_known_reasons"), "risk_decisions", type_="check")
    op.create_check_constraint("known_reasons", "risk_decisions", _reasons_check(KNOWN_REASONS_V2))

    # An order can only reach risk_approved / risk_rejected with a matching
    # decision row, and can only be created in the 'created' state. This is
    # the database-level guarantee that no order is approved without risk.
    op.execute(
        """
        CREATE FUNCTION arbitron_enforce_risk_gate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'created' THEN
                    RAISE EXCEPTION 'orders must be inserted as created, not %', NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status = 'risk_approved' AND OLD.status IS DISTINCT FROM 'risk_approved'
               AND NOT EXISTS (
                   SELECT 1 FROM risk_decisions WHERE intent_id = NEW.id AND approved
               ) THEN
                RAISE EXCEPTION 'order % cannot be risk_approved without an approved risk decision', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'risk_rejected' AND OLD.status IS DISTINCT FROM 'risk_rejected'
               AND NOT EXISTS (
                   SELECT 1 FROM risk_decisions WHERE intent_id = NEW.id AND NOT approved
               ) THEN
                RAISE EXCEPTION 'order % cannot be risk_rejected without a rejecting risk decision', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER orders_risk_gate BEFORE INSERT OR UPDATE ON orders "
        "FOR EACH ROW EXECUTE FUNCTION arbitron_enforce_risk_gate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER orders_risk_gate ON orders")
    op.execute("DROP FUNCTION arbitron_enforce_risk_gate()")
    op.drop_constraint(op.f("ck_risk_decisions_known_reasons"), "risk_decisions", type_="check")
    op.create_check_constraint("known_reasons", "risk_decisions", _reasons_check(KNOWN_REASONS_V1))
