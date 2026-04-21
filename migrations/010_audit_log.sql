CREATE TABLE firm_os.audit_log (
    log_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id      UUID REFERENCES firm_os.organizations(org_id) ON DELETE SET NULL,
    actor       TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_log_org_id ON firm_os.audit_log(org_id);
CREATE INDEX idx_audit_log_event_type ON firm_os.audit_log(event_type);
CREATE INDEX idx_audit_log_severity ON firm_os.audit_log(severity);
CREATE INDEX idx_audit_log_created_at ON firm_os.audit_log(created_at);
