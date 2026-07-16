"""Reconcile all migration heads

This merge migration reconciles multiple conflicting heads to a single lineage.

Revision ID: 00001_reconcile_heads
Revises: 8e5c69f96870, 20260401_repair
Create Date: 2026-07-16 00:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "00001_reconcile_heads"
down_revision = ("8e5c69f96870", "20260401_repair")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
