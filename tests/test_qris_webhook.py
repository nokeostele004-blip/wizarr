import json

from app.extensions import db
from app.models import QrisPayment, Settings


def _set_setting(key: str, value: str):
    setting = Settings.query.filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        setting = Settings(key=key, value=value)
        db.session.add(setting)
    db.session.commit()


def test_qris_webhook_get_health(client, session):
    response = client.get('/webhooks/qris')
    assert response.status_code == 200
    data = response.get_json()
    assert data['ok'] is True


def test_qris_webhook_accepts_json_payload(client, session):
    payment = QrisPayment(order_id='ORD-12345', invite_code='abc123', status='pending')
    db.session.add(payment)
    db.session.commit()

    payload = {
        'event': 'payment.paid',
        'transaction_id': 'TRX-123ABC',
        'order_id': 'ORD-12345',
        'amount': 50000,
        'customer_name': 'John Doe',
        'customer_phone': '+6281234567890',
        'status': 'paid',
        'paid_at': '2025-11-18 15:30:45',
        'created_at': '2025-11-18 15:25:00',
        'qr_image_url': 'https://qris.pw/qr/example',
        'merchant_id': '206',
        'merchant_name': 'REDD',
    }

    response = client.post('/webhooks/qris', json=payload)
    assert response.status_code == 200

    updated = QrisPayment.query.filter_by(order_id='ORD-12345').first()
    assert updated is not None
    assert updated.status == 'paid'
    assert updated.transaction_id == 'TRX-123ABC'
    assert updated.amount == 50000


def test_qris_webhook_accepts_text_plain_json(client, session):
    payment = QrisPayment(order_id='ORD-TEXT-1', invite_code='abc123', status='pending')
    db.session.add(payment)
    db.session.commit()

    payload = {'event': 'payment.pending', 'order_id': 'ORD-TEXT-1', 'status': 'pending'}
    response = client.post(
        '/webhooks/qris',
        data=json.dumps(payload),
        headers={'Content-Type': 'text/plain'},
    )
    assert response.status_code == 200

    updated = QrisPayment.query.filter_by(order_id='ORD-TEXT-1').first()
    assert updated is not None
    assert updated.status == 'pending'


def test_qris_webhook_secret_supports_bearer(client, session):
    _set_setting('qris_webhook_secret', 'supersecret')

    payment = QrisPayment(order_id='ORD-SEC-1', invite_code='abc123', status='pending')
    db.session.add(payment)
    db.session.commit()

    payload = {'event': 'payment.paid', 'order_id': 'ORD-SEC-1'}

    unauthorized = client.post('/webhooks/qris', json=payload)
    assert unauthorized.status_code == 401

    authorized = client.post(
        '/webhooks/qris',
        json=payload,
        headers={'Authorization': 'Bearer supersecret'},
    )
    assert authorized.status_code == 200

    updated = QrisPayment.query.filter_by(order_id='ORD-SEC-1').first()
    assert updated is not None
    assert updated.status == 'paid'
