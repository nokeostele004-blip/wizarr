from flask_wtf import FlaskForm
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Optional


class GeneralSettingsForm(FlaskForm):
    server_name = StringField("Display Name", validators=[DataRequired()])
    wizard_acl_enabled = BooleanField(
        "Protect Wizard Access", default=True, validators=[Optional()]
    )

    qris_enabled = BooleanField("Enable QRIS Subscription Gate", default=False, validators=[Optional()])
    qris_merchant_name = StringField("QRIS Merchant Name", validators=[Optional()])
    qris_payment_link = StringField("QRIS Payment Link", validators=[Optional()])
    qris_image_url = StringField("QRIS Image URL", validators=[Optional()])
    qris_plans_json = TextAreaField("QRIS Plans JSON", validators=[Optional()])
    qris_webhook_secret = StringField("QRIS Webhook Secret", validators=[Optional()])

    expiry_action = SelectField(
        "Expiry Action",
        choices=[
            ("delete", "Delete User"),
            ("disable", "Disable User (if supported)"),
        ],
        default="delete",
        validators=[DataRequired()],
    )
