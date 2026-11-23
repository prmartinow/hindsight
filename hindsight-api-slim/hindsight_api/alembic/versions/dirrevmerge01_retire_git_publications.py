"""Retire the previous fork writer without dropping an enrolled bank's Git receipt."""

import sqlalchemy as sa
from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect
from hindsight_api.alembic.versions import b68dc79d5a2f_directive_git_publications as git_receipts

revision = "dirrevmerge01"
down_revision = ("dirrev1a2b3c", "b68dc79d5a2f")
branch_labels = None
depends_on = None


def _drop_empty_receipts(schema: str | None = None) -> None:
    receipts = sa.table("directive_git_publications", schema=schema)
    if op.get_bind().execute(sa.select(sa.literal(1)).select_from(receipts).limit(1)).first():
        raise RuntimeError(
            "Reconcile pending Git changes and detach enrolled directive repositories "
            "using the previous release before upgrading to database revision history."
        )
    op.drop_table(receipts.name, schema=schema)


def _pg_upgrade() -> None:
    _drop_empty_receipts(context.config.get_main_option("target_schema") or None)


def _oracle_upgrade() -> None:
    _drop_empty_receipts()


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=git_receipts._pg_upgrade, oracle=git_receipts._oracle_upgrade)
