CREATE SCHEMA IF NOT EXISTS ontology;

CREATE TABLE IF NOT EXISTS ontology.ontology (
    workspace text NOT NULL,
    id text NOT NULL,
    active_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, id)
);
ALTER TABLE ontology.ontology ADD COLUMN IF NOT EXISTS active_version text;

CREATE TABLE IF NOT EXISTS ontology.ontology_version (
    workspace text NOT NULL,
    ontology_id text NOT NULL,
    version text NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'published')),
    definition jsonb NOT NULL,
    content_hash text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_by text,
    published_at timestamptz,
    PRIMARY KEY (workspace, ontology_id, version),
    FOREIGN KEY (workspace, ontology_id) REFERENCES ontology.ontology(workspace, id)
);
CREATE OR REPLACE FUNCTION ontology.prevent_published_version_mutation() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'published' THEN
        RAISE EXCEPTION 'published ontology versions are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS ontology_published_version_immutable ON ontology.ontology_version;
CREATE TRIGGER ontology_published_version_immutable
    BEFORE UPDATE OR DELETE ON ontology.ontology_version
    FOR EACH ROW EXECUTE FUNCTION ontology.prevent_published_version_mutation();
CREATE INDEX IF NOT EXISTS ontology_version_lookup
    ON ontology.ontology_version (workspace, ontology_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ontology.fact (
    workspace text NOT NULL,
    id text NOT NULL,
    ontology_id text NOT NULL,
    ontology_version text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('entity', 'relation')),
    record jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, id),
    FOREIGN KEY (workspace, ontology_id, ontology_version)
        REFERENCES ontology.ontology_version(workspace, ontology_id, version)
);
CREATE INDEX IF NOT EXISTS fact_ontology_lookup ON ontology.fact(workspace, ontology_id);

CREATE TABLE IF NOT EXISTS ontology.quarantine (
    workspace text NOT NULL,
    id text NOT NULL,
    ontology_id text NOT NULL,
    record jsonb NOT NULL,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    mode text NOT NULL CHECK (mode IN ('observe', 'quarantine')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, id, created_at)
);

CREATE TABLE IF NOT EXISTS ontology.audit (
    sequence bigserial PRIMARY KEY,
    workspace text NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    subject text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ontology_audit_workspace ON ontology.audit(workspace, sequence DESC);

CREATE TABLE IF NOT EXISTS ontology.outbox (
    id bigserial PRIMARY KEY,
    workspace text NOT NULL,
    fact_id text NOT NULL,
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    leased_until timestamptz,
    lease_token text,
    delivered_at timestamptz,
    last_error text,
    UNIQUE (workspace, fact_id)
);
CREATE INDEX IF NOT EXISTS ontology_outbox_ready
    ON ontology.outbox(available_at, id) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS ontology.migration_plan (
    workspace text NOT NULL,
    id text NOT NULL,
    ontology_id text NOT NULL,
    from_version text NOT NULL,
    target_definition jsonb NOT NULL,
    target_hash text NOT NULL,
    renames jsonb NOT NULL,
    plan jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('ready', 'blocked', 'applied', 'rolled_back')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    PRIMARY KEY (workspace, id)
);
ALTER TABLE ontology.migration_plan DROP CONSTRAINT IF EXISTS migration_plan_state_check;
ALTER TABLE ontology.migration_plan ADD CONSTRAINT migration_plan_state_check
    CHECK (state IN ('ready', 'blocked', 'applied', 'rolled_back'));
CREATE TABLE IF NOT EXISTS ontology.migration_snapshot (
    workspace text NOT NULL,
    migration_id text NOT NULL,
    fact_id text NOT NULL,
    record jsonb NOT NULL,
    applied_record jsonb,
    PRIMARY KEY (workspace, migration_id, fact_id),
    FOREIGN KEY (workspace, migration_id) REFERENCES ontology.migration_plan(workspace, id)
);
ALTER TABLE ontology.migration_snapshot ADD COLUMN IF NOT EXISTS applied_record jsonb;
