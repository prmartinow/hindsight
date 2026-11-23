"""Record a directive's published versions in the same transaction as its live row.

Revision ID: dirrev1a2b3c
Revises: kbtok1a2b3c4
"""

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision = "dirrev1a2b3c"
down_revision = "kbtok1a2b3c4"
branch_labels = None
depends_on = None


def _pg_schema_prefix() -> str:
    schema = (
        context.config.get_main_option("target_schema")
        or op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one()
    )
    return '"' + schema.replace('"', '""') + '".'


def _pg_upgrade() -> None:
    s = _pg_schema_prefix()
    op.execute(f"ALTER TABLE {s}directives ADD COLUMN revision BIGINT NOT NULL DEFAULT 1")
    op.execute(f"""
        CREATE TABLE {s}directive_revisions (
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            directive_id UUID NOT NULL,
            revision BIGINT NOT NULL CHECK (revision > 0),
            operation TEXT NOT NULL CHECK (operation IN ('baseline','create','update','delete','revert')),
            deleted BOOLEAN NOT NULL,
            restored_from_revision BIGINT,
            snapshot JSONB NOT NULL,
            actor_id TEXT,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (bank_id, directive_id, revision)
        )
    """)
    # This observes the current state; the migration cannot reconstruct older edits.
    op.execute(f"""
        INSERT INTO {s}directive_revisions(bank_id, directive_id, revision, operation, deleted, snapshot)
        SELECT d.bank_id, d.id, d.revision, 'baseline', FALSE, to_jsonb(d) FROM {s}directives d
    """)
    # BEFORE assigns the version returned by INSERT/UPDATE ... RETURNING. AFTER only
    # journals rows actually written (restore uses INSERT ... ON CONFLICT DO NOTHING).
    op.execute(f"""
        CREATE FUNCTION {s}assign_directive_revision() RETURNS trigger LANGUAGE plpgsql AS $body$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.bank_id, NEW.id) IS DISTINCT FROM (OLD.bank_id, OLD.id) THEN
                    RAISE EXCEPTION 'A directive cannot change its bank or ID';
                END IF;
                NEW.revision := OLD.revision + 1;
            ELSE
                SELECT COALESCE(MAX(h.revision), 0) + 1 INTO NEW.revision
                FROM {s}directive_revisions h WHERE h.bank_id = NEW.bank_id AND h.directive_id = NEW.id;
            END IF;
            RETURN NEW;
        END
        $body$
    """)
    op.execute(f"""
        CREATE TRIGGER directive_assign_revision BEFORE INSERT OR UPDATE ON {s}directives
        FOR EACH ROW EXECUTE FUNCTION {s}assign_directive_revision()
    """)
    op.execute(f"""
        CREATE FUNCTION {s}record_directive_revision() RETURNS trigger LANGUAGE plpgsql AS $body$
        DECLARE
            state jsonb;
            n bigint;
            action text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                -- A bank deletion cascades both tables. Never resurrect its history.
                IF NOT EXISTS (SELECT 1 FROM {s}banks WHERE bank_id = OLD.bank_id) THEN
                    RETURN NULL;
                END IF;
                n := OLD.revision + 1;
                state := to_jsonb(OLD) || jsonb_build_object('revision', n);
                action := 'delete';
            ELSE
                state := to_jsonb(NEW);
                n := NEW.revision;
                action := CASE TG_OP WHEN 'INSERT' THEN 'create' ELSE 'update' END;
            END IF;
            INSERT INTO {s}directive_revisions
                (bank_id, directive_id, revision, operation, deleted, snapshot, actor_id, restored_from_revision)
            VALUES (state->>'bank_id', (state->>'id')::uuid, n,
                    CASE WHEN NULLIF(current_setting('hindsight.directive_revert', TRUE), '') IS NOT NULL
                         THEN 'revert' ELSE action END,
                    TG_OP = 'DELETE', state, NULLIF(current_setting('hindsight.directive_actor', TRUE), ''),
                    NULLIF(current_setting('hindsight.directive_revert', TRUE), '')::bigint);
            RETURN NULL;
        END
        $body$
    """)
    op.execute(f"""
        CREATE TRIGGER directive_record_revision AFTER INSERT OR UPDATE OR DELETE ON {s}directives
        FOR EACH ROW EXECUTE FUNCTION {s}record_directive_revision()
    """)
    # Published revisions are append-only over the bank's lifetime. Bank erasure must
    # still cascade to historical rows, including tombstones holding removed content.
    op.execute(f"""
        CREATE FUNCTION {s}protect_directive_revision() RETURNS trigger LANGUAGE plpgsql AS $body$
        BEGIN
            IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM {s}banks WHERE bank_id = OLD.bank_id) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Published directive revisions are append-only';
        END
        $body$
    """)
    op.execute(f"""
        CREATE TRIGGER directive_protect_revision BEFORE UPDATE OR DELETE ON {s}directive_revisions
        FOR EACH ROW EXECUTE FUNCTION {s}protect_directive_revision()
    """)


def _pg_downgrade() -> None:
    s = _pg_schema_prefix()
    op.execute(f"DROP TRIGGER directive_record_revision ON {s}directives")
    op.execute(f"DROP TRIGGER directive_assign_revision ON {s}directives")
    op.execute(f"DROP FUNCTION {s}record_directive_revision()")
    op.execute(f"DROP FUNCTION {s}assign_directive_revision()")
    op.execute(f"DROP TABLE {s}directive_revisions")
    op.execute(f"DROP FUNCTION {s}protect_directive_revision()")
    op.execute(f"ALTER TABLE {s}directives DROP COLUMN revision")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade)  # Oracle retains its native unversioned directive CRUD.


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade)
