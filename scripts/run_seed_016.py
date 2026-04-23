"""
Run migration 016_seed_mock_firms.sql via Lambda.

Deploy as firmos-seed-016 (temporary), invoke once, then delete.
This Lambda must run inside the VPC to reach RDS.

Layer required: firmos-shared (provides shared_db)
"""
import sys

sys.path.insert(0, "/opt/python")

from shared_db import get_connection  # type: ignore[import]

SQL = """
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
"""


def lambda_handler(event: dict, context: object) -> dict:
    """Execute seed migration 016 against firm_os schema.

    Args:
        event: Lambda event payload (unused).
        context: Lambda context object (unused).

    Returns:
        HTTP-style response dict with statusCode and body.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL)
        conn.commit()
        return {"statusCode": 200, "body": "Seed 016 applied successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Seed 016 failed: {exc}") from exc
    finally:
        conn.close()
