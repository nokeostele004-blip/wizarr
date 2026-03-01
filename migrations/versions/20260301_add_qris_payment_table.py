"""add qris payment table

Revision ID: 20260301_add_qris_payment_table
Revises: eecad7c18ac3
Create Date: 2026-03-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260301_add_qris_payment_table'
down_revision = 'eecad7c18ac3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'qris_payment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.String(), nullable=False),
        sa.Column('invite_code', sa.String(), nullable=False),
        sa.Column('plan_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=True),
        sa.Column('transaction_id', sa.String(), nullable=True),
        sa.Column('customer_name', sa.String(), nullable=True),
        sa.Column('customer_phone', sa.String(), nullable=True),
        sa.Column('merchant_id', sa.String(), nullable=True),
        sa.Column('merchant_name', sa.String(), nullable=True),
        sa.Column('qr_image_url', sa.String(), nullable=True),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_id')
    )
    op.create_index(op.f('ix_qris_payment_invite_code'), 'qris_payment', ['invite_code'], unique=False)
    op.create_index(op.f('ix_qris_payment_order_id'), 'qris_payment', ['order_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_qris_payment_order_id'), table_name='qris_payment')
    op.drop_index(op.f('ix_qris_payment_invite_code'), table_name='qris_payment')
    op.drop_table('qris_payment')
