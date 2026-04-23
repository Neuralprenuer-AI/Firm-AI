-- 016_seed_mock_firms.sql
-- TEST DATA ONLY — two mock firms for firm-admin dashboard testing
-- Safe to run multiple times (ON CONFLICT DO NOTHING)
-- DO NOT run in production. These are placeholder orgs for local/staging QA only.

BEGIN;
SET search_path TO firm_os, public;

INSERT INTO firm_os.organizations
  (org_id, name, practice_area, city, state, billing_status, status, intake_extra)
VALUES
  (
    'a1b2c3d4-0001-0001-0001-000000000001',
    'Ruiz Personal Injury',
    'Personal Injury',
    'Houston',
    'TX',
    'trial',
    'active',
    '{}'
  ),
  (
    'a1b2c3d4-0002-0002-0002-000000000002',
    'Vega Immigration Law',
    'Immigration',
    'Houston',
    'TX',
    'trial',
    'active',
    '{}'
  )
ON CONFLICT (org_id) DO NOTHING;

INSERT INTO firm_os.org_users
  (org_id, name, email, org_role, escalation_routing)
VALUES
  (
    'a1b2c3d4-0001-0001-0001-000000000001',
    'Carlos Ruiz',
    'admin@ruizpi.test',
    'partner',
    TRUE
  ),
  (
    'a1b2c3d4-0002-0002-0002-000000000002',
    'Maria Vega',
    'admin@vegalaw.test',
    'partner',
    TRUE
  )
ON CONFLICT DO NOTHING;

COMMIT;
