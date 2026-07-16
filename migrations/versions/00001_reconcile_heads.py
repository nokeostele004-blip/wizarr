"""Reconcile all migration heads - FINAL

This merge migration reconciles ALL conflicting heads into a single linear chain.

The problem: Multiple independent branches developed from e6155a91eb50:
- LDAP branch: 20251226_add_ldap → 8e5c69f96870 (merge ldap + main)
- Password branch: c854ad44aad5 → eecad7c18ac3 (merge password + main)

These need to be merged into one head to fix:
"ERROR Multiple head revisions are present for given argument 'head'"

This migration acts as the final reconciliation point.

Revision ID: 00001_reconcile_heads
Revises: 8e5c69f96870, eecad7c18ac3
Create Date: 2026-07-16 00:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "00001_reconcile_heads"
down_revision = ("8e5c69f96870", "eecad7c18ac3")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
