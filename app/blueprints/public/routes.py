import json
import secrets
from base64 import b64decode
from datetime import UTC, datetime
from pathlib import Path

import requests
from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from app.extensions import db, limiter
from app.models import Invitation, MediaServer, QrisPayment, Settings, User
from app.services.invites import is_invite_valid
from app.services.media.plex import PlexInvitationError, handle_oauth_token

public_bp = Blueprint("public", __name__)


def _get_qris_subscription_config() -> dict:
    """Load QRIS subscription settings from DB settings table."""
    settings = {s.key: s.value for s in Settings.query.all()}

    enabled = str(settings.get("qris_enabled", "false")).lower() == "true"
    merchant_name = settings.get("qris_merchant_name") or "Wizarr Subscription"
    payment_link = settings.get("qris_payment_link") or ""
    qris_image_url = settings.get("qris_image_url") or ""

    default_plans = [
        {"id": "basic", "name": "Basic", "price": "Rp25.000/bulan"},
        {"id": "standard", "name": "Standard", "price": "Rp50.000/bulan"},
        {"id": "premium", "name": "Premium", "price": "Rp100.000/bulan"},
    ]

    plans = default_plans
    plans_raw = settings.get("qris_plans_json")
    if plans_raw:
        try:
            parsed = json.loads(plans_raw)
            if isinstance(parsed, list) and parsed:
                plans = [p for p in parsed if isinstance(p, dict) and p.get("id")]
        except (json.JSONDecodeError, TypeError):
            plans = default_plans

    return {
        "enabled": enabled,
        "merchant_name": merchant_name,
        "payment_link": payment_link,
        "qris_image_url": qris_image_url,
        "plans": plans,
    }


def _subscription_session_key(code: str) -> str:
    return f"qris_subscription_selected_{code.lower()}"


def _build_order_id(code: str) -> str:
    nonce = secrets.token_hex(4)
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"WIZ-{code.upper()}-{ts}-{nonce}"


def _build_payment_link(base_link: str, order_id: str, plan_id: str) -> str:
    if not base_link:
        return ""
    link = base_link
    link = link.replace("{order_id}", order_id)
    link = link.replace("{plan_id}", plan_id)
    return link


def _get_payment_for_invite(code: str) -> QrisPayment | None:
    selected = session.get(_subscription_session_key(code)) or {}
    order_id = selected.get("order_id")
    if not order_id:
        return None
    return QrisPayment.query.filter_by(order_id=order_id).first()




def _extract_webhook_secret() -> str:
    """Read webhook secret from common header styles."""
    header_secret = request.headers.get("X-Webhook-Secret", "").strip()
    if header_secret:
        return header_secret

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    if auth_header.lower().startswith("basic "):
        try:
            decoded = b64decode(auth_header[6:].strip()).decode("utf-8", errors="ignore")
            # Accept either "secret" or "user:secret"
            if ":" in decoded:
                return decoded.split(":", 1)[1].strip()
            return decoded.strip()
        except Exception:
            return ""

    query_secret = request.args.get("secret", "").strip()
    return query_secret


def _parse_qris_webhook_payload() -> dict:
    """Parse webhook payload in JSON or form-encoded body."""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and payload:
        return payload

    if request.form:
        return request.form.to_dict()

    raw = request.get_data(cache=False, as_text=True) or ""
    raw = raw.strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return {}

# ─── Landing “/” ──────────────────────────────────────────────────────────────
@public_bp.route("/")
def root():
    # check if admin_username exists
    admin_setting = Settings.query.filter_by(key="admin_username").first()
    if not admin_setting:
        return redirect("/setup/")  # installation wizard
    return redirect("/admin")


# ─── Favicon ─────────────────────────────────────────────────────────────────
@public_bp.route("/favicon.ico")
def favicon():
    return send_from_directory(
        public_bp.root_path.replace("blueprints/public", "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


# ─── Invite link  /j/<code> ─────────────────────────────────────────────────
@public_bp.route("/j/<code>")
@limiter.limit("50 per minute")
def invite(code):
    qris_config = _get_qris_subscription_config()
    if qris_config["enabled"]:
        selected = session.get(_subscription_session_key(code))
        payment = _get_payment_for_invite(code)
        if not selected:
            return render_template(
                "subscription-select.html",
                code=code,
                plans=qris_config["plans"],
                merchant_name=qris_config["merchant_name"],
                qris_image_url=qris_config["qris_image_url"],
                payment_link=qris_config["payment_link"],
            )

        if not payment or payment.status != "paid":
            current_payment_link = _build_payment_link(
                qris_config["payment_link"],
                selected.get("order_id", ""),
                selected.get("plan_id", ""),
            )
            return render_template(
                "subscription-select.html",
                code=code,
                plans=qris_config["plans"],
                merchant_name=qris_config["merchant_name"],
                qris_image_url=payment.qr_image_url if payment and payment.qr_image_url else qris_config["qris_image_url"],
                payment_link=current_payment_link,
                selected_plan_id=selected.get("plan_id"),
                order_id=selected.get("order_id"),
                payment_status=payment.status if payment else "pending",
                waiting_for_webhook=True,
            )

    from app.services.invitation_flow import InvitationFlowManager

    manager = InvitationFlowManager()
    result = manager.process_invitation_display(code)
    return result.to_flask_response()


@public_bp.route("/j/<code>/subscription", methods=["POST"])
@limiter.limit("20 per minute")
def select_subscription(code):
    """Persist selected plan and create pending order before invitation flow."""
    qris_config = _get_qris_subscription_config()
    if not qris_config["enabled"]:
        return redirect(url_for("public.invite", code=code))

    selected_plan = request.form.get("plan_id", "").strip()
    valid_plan_ids = {str(plan.get("id")) for plan in qris_config["plans"]}

    if not selected_plan or selected_plan not in valid_plan_ids:
        return render_template(
            "subscription-select.html",
            code=code,
            plans=qris_config["plans"],
            merchant_name=qris_config["merchant_name"],
            qris_image_url=qris_config["qris_image_url"],
            payment_link=qris_config["payment_link"],
            error="Silakan pilih paket langganan terlebih dahulu.",
        )

    existing = _get_payment_for_invite(code)
    if existing and existing.status != "paid":
        payment = existing
    else:
        order_id = _build_order_id(code)
        payment = QrisPayment(
            order_id=order_id,
            invite_code=code,
            plan_id=selected_plan,
            status="pending",
            payload_json=json.dumps({"source": "wizarr_subscription_select"}),
        )
        db.session.add(payment)
        db.session.commit()

    session[_subscription_session_key(code)] = {
        "plan_id": selected_plan,
        "payment_method": "qris",
        "order_id": payment.order_id,
    }

    return redirect(url_for("public.invite", code=code))


@public_bp.route("/j/<code>/continue", methods=["POST"])
@limiter.limit("20 per minute")
def continue_after_payment(code):
    """Continue invite flow only when webhook marks payment as paid."""
    payment = _get_payment_for_invite(code)
    if payment and payment.status == "paid":
        return redirect(url_for("public.invite", code=code))

    qris_config = _get_qris_subscription_config()
    return render_template(
        "subscription-select.html",
        code=code,
        plans=qris_config["plans"],
        merchant_name=qris_config["merchant_name"],
        qris_image_url=payment.qr_image_url if payment and payment.qr_image_url else qris_config["qris_image_url"],
        payment_link=_build_payment_link(
            qris_config["payment_link"],
            payment.order_id if payment else "",
            payment.plan_id if payment else "",
        ),
        selected_plan_id=payment.plan_id if payment else None,
        order_id=payment.order_id if payment else None,
        payment_status=payment.status if payment else "pending",
        waiting_for_webhook=True,
        error="Pembayaran belum terkonfirmasi. Pastikan webhook payment.paid sudah terkirim.",
    )


@public_bp.route("/webhooks/qris", methods=["GET", "POST"])
@limiter.limit("120 per minute")
def qris_webhook():
    """Receive QRIS payment webhook and update order status."""
    if request.method == "GET":
        return jsonify({"ok": True, "message": "QRIS webhook endpoint ready"}), 200

    secret_setting = Settings.query.filter_by(key="qris_webhook_secret").first()
    expected_secret = secret_setting.value if secret_setting and secret_setting.value else ""
    if expected_secret:
        incoming_secret = _extract_webhook_secret()
        if incoming_secret != expected_secret:
            return jsonify({"ok": False, "error": "Invalid webhook secret"}), 401

    payload = _parse_qris_webhook_payload()
    event = payload.get("event")
    order_id = payload.get("order_id")

    if not order_id:
        return jsonify({"ok": False, "error": "Missing order_id"}), 400

    payment = QrisPayment.query.filter_by(order_id=order_id).first()
    if not payment:
        payment = QrisPayment(order_id=order_id, invite_code="unknown", status="pending")
        db.session.add(payment)

    status_map = {
        "payment.paid": "paid",
        "payment.pending": "pending",
        "payment.expired": "expired",
    }
    payment.status = status_map.get(event, payload.get("status") or payment.status)
    payment.transaction_id = payload.get("transaction_id") or payment.transaction_id
    payment.amount = payload.get("amount") or payment.amount
    payment.customer_name = payload.get("customer_name") or payment.customer_name
    payment.customer_phone = payload.get("customer_phone") or payment.customer_phone
    payment.merchant_id = payload.get("merchant_id") or payment.merchant_id
    payment.merchant_name = payload.get("merchant_name") or payment.merchant_name
    payment.qr_image_url = payload.get("qr_image_url") or payment.qr_image_url
    paid_at_raw = payload.get("paid_at")
    if paid_at_raw:
        try:
            payment.paid_at = datetime.strptime(paid_at_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    payment.payload_json = json.dumps(payload)

    if payment.invite_code == "unknown":
        # keep compatibility if webhook received before app-created pending row
        embedded_code = str(order_id).split("-")
        if len(embedded_code) >= 2 and embedded_code[0] == "WIZ":
            payment.invite_code = embedded_code[1].lower()

    db.session.commit()

    return jsonify({"ok": True, "event": event, "order_id": order_id, "status": payment.status})


# ─── Unified invitation processing ─────────────────────────────────────────
@public_bp.route("/invitation/process", methods=["POST"])
@limiter.limit("20 per minute")
def process_invitation():
    """Unified route for processing all invitation types"""
    from app.services.invitation_flow import InvitationFlowManager

    manager = InvitationFlowManager()
    form_data = request.form.to_dict()
    result = manager.process_invitation_submission(form_data)
    return result.to_flask_response()


# ─── POST /join  (Legacy Plex OAuth route - kept for compatibility) ────────
@public_bp.route("/join", methods=["POST"])
@limiter.limit("20 per minute")
def join():
    code = request.form.get("code")
    token = request.form.get("token")

    invitation = None
    if code:
        invitation = Invitation.query.filter(
            db.func.lower(Invitation.code) == code.lower()
        ).first()
    valid, msg = (
        is_invite_valid(code) if code else (False, "No invitation code provided")
    )
    if not valid:
        # Resolve server name for rendering error
        from app.services.server_name_resolver import resolve_invitation_server_name

        # Try to get servers from invitation for error display
        servers = []
        if invitation and invitation.servers:
            servers = list(invitation.servers)
        elif invitation and invitation.server:
            servers = [invitation.server]

        server_name = resolve_invitation_server_name(servers)

        return render_template(
            "user-plex-login.html", server_name=server_name, code=code, code_error=msg
        )

    # Get the appropriate server for this invitation
    server = None
    if invitation:
        # Prioritize new many-to-many relationship
        if hasattr(invitation, "servers") and invitation.servers:
            # For legacy /join route, prioritize Plex servers first (backward compatibility)
            plex_servers = [s for s in invitation.servers if s.server_type == "plex"]
            server = plex_servers[0] if plex_servers else invitation.servers[0]
        # Fallback to legacy single server relationship
        elif invitation.server:
            server = invitation.server

    # Final fallback to any server (maintain existing behavior)
    if not server:
        server = MediaServer.query.first()
    server_type = server.server_type if server else None

    from flask import current_app

    if server_type == "plex":
        # run Plex OAuth invite immediately (blocking – we need the DB row afterwards)
        if token and code:
            try:
                handle_oauth_token(current_app, token, code)
            except PlexInvitationError as e:
                # Show user-friendly error message from Plex API
                name_setting = Settings.query.filter_by(key="server_name").first()
                server_name = name_setting.value if name_setting else None

                return render_template(
                    "user-plex-login.html",
                    server_name=server_name,
                    code=code,
                    code_error=f"Plex invitation failed: {e.message}",
                )
            except Exception as e:
                # Handle any other unexpected errors
                import logging

                logging.error(f"Unexpected error during Plex OAuth: {e}")

                name_setting = Settings.query.filter_by(key="server_name").first()
                server_name = name_setting.value if name_setting else None

                return render_template(
                    "user-plex-login.html",
                    server_name=server_name,
                    code=code,
                    code_error="An unexpected error occurred during invitation. Please try again or contact support.",
                )

        # Determine if there are additional servers attached to the invite
        extra = [
            s
            for s in (invitation.servers if invitation else [])
            if s.server_type != "plex"
        ]

        if extra:
            # Stash the token & email lookup hint in session so we can provision others later
            session["invite_code"] = code
            session["invite_token"] = token
            return redirect(url_for("public.password_prompt", code=code))

        # No other servers → continue to wizard as before
        session["wizard_access"] = code
        return redirect(url_for("wizard.start"))
    if server_type in (
        "jellyfin",
        "emby",
        "audiobookshelf",
        "romm",
        "kavita",
        "komga",
    ):
        from app.forms.join import JoinForm

        # Get server name for the invitation using the new resolver if available
        try:
            from app.services.server_name_resolver import resolve_invitation_server_name

            servers = []
            if invitation and invitation.servers:
                servers = list(invitation.servers)
            elif invitation and invitation.server:
                servers = [invitation.server]
            elif server:
                servers = [server]

            server_name = resolve_invitation_server_name(servers)
        except ImportError:
            # Fallback to legacy approach if resolver not available
            name_setting = Settings.query.filter_by(key="server_name").first()
            server_name = name_setting.value if name_setting else "Media Server"

        form = JoinForm()
        form.code.data = code
        return render_template(
            "welcome-jellyfin.html",
            code=code,
            server_type=server_type,
            server_name=server_name,
            form=form,
        )

    # fallback if server_type missing/unsupported
    return render_template("invalid-invite.html", error="Configuration error.")


@public_bp.route("/health", methods=["GET"])
def health():
    # If you need to check DB connectivity, do it here.
    return jsonify(status="ok"), 200


@public_bp.route("/cinema-posters")
def cinema_posters():
    """Get movie poster URLs for cinema background display."""
    try:
        import time

        from flask import current_app

        from app.models import MediaServer
        from app.services.media.service import get_client_for_media_server

        # Cache key for poster URLs
        cache_key = "cinema_posters"
        cache_duration = 1800  # 30 minutes

        # Check cache first
        cached_data = current_app.config.get("POSTER_CACHE", {})
        cached_entry = cached_data.get(cache_key)

        if cached_entry and (time.time() - cached_entry["timestamp"]) < cache_duration:
            return jsonify(cached_entry["data"])

        # Get the primary media server (or first available)
        server = MediaServer.query.first()
        if not server:
            return jsonify([])

        # Get media client for the server
        client = get_client_for_media_server(server)

        # Check if client has get_movie_posters method
        poster_urls = []
        if hasattr(client, "get_movie_posters"):
            poster_urls = client.get_movie_posters(limit=80)

        # Cache the results
        if "POSTER_CACHE" not in current_app.config:
            current_app.config["POSTER_CACHE"] = {}
        current_app.config["POSTER_CACHE"][cache_key] = {
            "data": poster_urls,
            "timestamp": time.time(),
        }

        return jsonify(poster_urls)

    except Exception as e:
        import logging

        logging.warning(f"Failed to fetch cinema posters: {e}")
        return jsonify([])


@public_bp.route("/static/manifest.json")
def manifest():
    """Serve the PWA manifest file with correct content type"""
    return send_from_directory(
        Path(current_app.root_path) / "static",
        "manifest.json",
        mimetype="application/manifest+json",
    )


# ─── Password prompt for multi-server invites ───────────────────────────────


@public_bp.route("/j/<code>/password", methods=["GET", "POST"])
def password_prompt(code):
    invitation = Invitation.query.filter(
        db.func.lower(Invitation.code) == code.lower()
    ).first()

    if not invitation:
        return render_template("invalid-invite.html", error="Invalid invite")

    # ensure Plex has been processed
    # (a user row with this code and plex server_id should exist)
    plex_server = next((s for s in invitation.servers if s.server_type == "plex"), None)

    plex_user = None
    if plex_server:
        plex_user = User.query.filter_by(code=code, server_id=plex_server.id).first()

    if request.method == "POST":
        pw = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if pw != confirm or len(pw) < 8:
            return render_template(
                "choose-password.html",
                code=code,
                error="Passwords do not match or too short (8 chars).",
            )

        # Fallback: generate strong password if checkbox ticked or blank
        if request.form.get("generate") or pw.strip() == "":
            import secrets
            import string

            pw = "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
            )

        # Provision accounts on remaining servers
        from app.services.expiry import calculate_user_expiry
        from app.services.invites import mark_server_used
        from app.services.media.service import get_client_for_media_server

        # Calculate expiry will be done per-server to allow server-specific expiry

        for srv in invitation.servers:
            if srv.server_type == "plex":
                continue  # already done

            client = get_client_for_media_server(srv)

            username = (
                plex_user.username
                if plex_user
                else (plex_user.email.split("@")[0] if plex_user else "wizarr")
            )
            email = plex_user.email if plex_user else "user@example.com"

            try:
                if srv.server_type in ("jellyfin", "emby"):
                    uid = client.create_user(username, pw)
                elif srv.server_type in ("audiobookshelf", "romm"):
                    uid = client.create_user(username, pw, email=email)
                else:
                    continue  # unknown server type

                # set library permissions (simplified: full access)

                # Calculate server-specific expiry for this user
                user_expires = calculate_user_expiry(invitation, srv.id)

                # store local DB row with proper expiry
                new_user = User()
                new_user.username = username
                new_user.email = email
                new_user.token = uid
                new_user.code = code
                new_user.server_id = srv.id
                new_user.expires = user_expires  # Set expiry based on invitation duration (server-specific)
                db.session.add(new_user)
                db.session.commit()

                invitation.used_by = invitation.used_by or new_user
                mark_server_used(invitation, srv.id)
            except Exception as exc:
                db.session.rollback()
                import logging

                logging.error("Failed to provision user on %s: %s", srv.name, exc)

        session["wizard_access"] = code
        return redirect(url_for("wizard.start"))

    # GET request – show form
    return render_template("choose-password.html", code=code)


# ─── Image proxy to allow internal artwork URLs ─────────────────────────────
@public_bp.route("/image-proxy")
def image_proxy():
    """
    Secure image proxy using opaque tokens instead of URLs.

    This prevents SSRF attacks by not exposing the underlying URL.
    Only accepts signed tokens generated by ImageProxyService.
    """
    from app.services.image_proxy import ImageProxyService

    token = request.args.get("token")
    if not token:
        return Response(status=400)

    # Check image cache first
    cached_image = ImageProxyService.get_cached_image(token)
    if cached_image:
        resp = Response(cached_image["data"], content_type=cached_image["content_type"])
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    # Validate token and get URL
    mapping = ImageProxyService.validate_token(token)
    if not mapping:
        return Response(status=403)  # Invalid or expired token

    url = mapping["url"]
    server_id = mapping.get("server_id")

    try:
        # Prepare headers for authenticated requests (cached per server)
        headers = ImageProxyService.get_server_headers(server_id).copy()

        # Fetch the image using a pooled session to reuse TCP/TLS handshakes
        session = ImageProxyService.get_session(url, server_id)
        r = session.get(url, headers=headers, timeout=(5, 15))
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "image/jpeg")
        image_data = r.content

        # Cache the image
        ImageProxyService.cache_image(token, image_data, content_type)

        resp = Response(image_data, content_type=content_type)
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    except requests.RequestException:
        return Response(status=502)
    except Exception:
        return Response(status=502)


# ─── Password Reset ──────────────────────────────────────────────────────────
@public_bp.route("/reset/<code>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(code):
    """Handle password reset via token link."""
    from app.services.password_reset import get_reset_token, use_reset_token

    # Validate the reset token
    token, error = get_reset_token(code)

    if not token:
        return render_template("password-reset-error.html", error=error, code=code)

    # GET request - show the password reset form
    if request.method == "GET":
        return render_template(
            "password-reset-form.html",
            code=code,
            username=token.user.username,
            expires_at=token.expires_at,
        )

    # POST request - process the password reset
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    # Validate passwords match
    if new_password != confirm_password:
        return render_template(
            "password-reset-form.html",
            code=code,
            username=token.user.username,
            expires_at=token.expires_at,
            error="Passwords do not match",
        )

    # Validate password length
    if not (8 <= len(new_password) <= 128):
        return render_template(
            "password-reset-form.html",
            code=code,
            username=token.user.username,
            expires_at=token.expires_at,
            error="Password must be between 8 and 128 characters",
        )

    # Use the reset token to change the password
    success, message = use_reset_token(code, new_password)

    if success:
        return render_template(
            "password-reset-success.html", username=token.user.username
        )
    return render_template(
        "password-reset-form.html",
        code=code,
        username=token.user.username,
        expires_at=token.expires_at,
        error=message,
    )
