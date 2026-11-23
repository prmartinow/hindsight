"""Record the active Git revision and its explicitly managed directives per bank."""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "b68dc79d5a2f"
down_revision: str | Sequence[str] | None = "kbtok1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    schema = _pg_schema_prefix()
    op.execute(f"""
        CREATE TABLE {schema}directive_git_publications (
            bank_id TEXT PRIMARY KEY REFERENCES {schema}banks(bank_id) ON DELETE CASCADE,
            revision VARCHAR(64) NOT NULL,
            managed_ids TEXT NOT NULL
        )
    """)


def _oracle_upgrade() -> None:
    op.execute("""
        CREATE TABLE directive_git_publications (
            bank_id VARCHAR2(256) PRIMARY KEY REFERENCES banks(bank_id) ON DELETE CASCADE,
            revision VARCHAR2(64) NOT NULL,
            managed_ids CLOB NOT NULL
        )
    """)


def _pg_downgrade() -> None:
    op.execute(f"DROP TABLE {_pg_schema_prefix()}directive_git_publications")


def _oracle_downgrade() -> None:
    op.execute("DROP TABLE directive_git_publications")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
