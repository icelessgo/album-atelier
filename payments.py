"""Stripe test Checkout for a frozen, password-approved album proof."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PaymentError(ValueError):
    pass


def _secret(name: str) -> str:
    filename = os.environ.get(f"{name}_FILE")
    if filename:
        try:
            return Path(filename).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(name, "")


def configured() -> bool:
    return (_secret("ALBUM_STRIPE_SECRET_KEY").startswith(("sk_test_", "rk_test_")) and
            _secret("ALBUM_STRIPE_WEBHOOK_SECRET").startswith("whsec_") and
            os.environ.get("ALBUM_PUBLIC_REVIEW_BASE", "").startswith("https://"))


def checkout_session(proof: dict, delivery_countries: tuple[str, ...] = ("IE",)) -> dict:
    if not configured():
        raise PaymentError("Stripe test checkout is not configured yet")
    approved = proof.get("approved")
    if not approved or proof.get("feedback") or not proof.get("offers"):
        raise PaymentError("Approve the final book before checkout")
    offer = next((item for item in proof["offers"]
                  if item["design_format"] == approved.get("design_format")), None)
    if not offer or offer["product"] != approved["product"]:
        raise PaymentError("Approved book does not match this proof")
    # Imported here to avoid a module cycle: proofing owns the order contract.
    from proofing import companion_counts, companion_total
    counts = companion_counts(approved.get("companion_quantities"), approved.get("companion_option"))
    expected = offer["base_eur"] + companion_total(counts, offer)
    if expected != approved.get("total_eur"):
        raise PaymentError("Approved price no longer matches the offer")
    base = os.environ["ALBUM_PUBLIC_REVIEW_BASE"].rstrip("/")
    fields = {
        "mode": "payment", "client_reference_id": proof["id"],
        "success_url": f"{base}/{proof['id']}?checkout=success",
        "cancel_url": f"{base}/{proof['id']}?checkout=cancelled",
        # Stripe requires at least 30 minutes from its own creation timestamp.
        # Leave a little room for Pi clock skew and network latency.
        "expires_at": str(int(time.time()) + 1920),
        "billing_address_collection": "required",
        "phone_number_collection[enabled]": "true",
        "line_items[0][price_data][currency]": "eur",
        "line_items[0][price_data][unit_amount]": str(offer["base_eur"] * 100),
        "line_items[0][price_data][product_data][name]": f"{offer['product_label']} wedding album · {offer['closed_size']}",
        "line_items[0][quantity]": "1",
        "metadata[proof_id]": proof["id"],
        "metadata[design_format]": offer["design_format"],
        "metadata[project_revision]": str(proof["project_revision"]),
    }
    item_index = 1
    for key, quantity in (("one_mid", counts["one_mid"]), ("two_small", counts["two_small"] // 2)):
        if not quantity:
            continue
        label = offer["companions"][key]
        fields.update({
            f"line_items[{item_index}][price_data][currency]": "eur",
            f"line_items[{item_index}][price_data][unit_amount]": str(offer["companion_eur"] * 100),
            f"line_items[{item_index}][price_data][product_data][name]": label if key == "two_small" else label.replace("One ", "", 1),
            f"line_items[{item_index}][quantity]": str(quantity),
        })
        item_index += 1
    for index, country in enumerate(delivery_countries):
        fields[f"shipping_address_collection[allowed_countries][{index}]"] = country
    request = Request("https://api.stripe.com/v1/checkout/sessions",
                      data=urlencode(fields).encode(), method="POST",
                      headers={"Authorization": f"Bearer {_secret('ALBUM_STRIPE_SECRET_KEY')}",
                               "Content-Type": "application/x-www-form-urlencoded",
                               "Idempotency-Key": f"album-proof-{proof['id']}-{proof.get('checkout_attempt', 1)}"})
    try:
        with urlopen(request, timeout=20) as response:
            session = json.load(response)
    except Exception as exc:
        raise PaymentError("Stripe checkout could not be started; try again shortly") from exc
    if not str(session.get("id", "")).startswith("cs_test_") or not str(session.get("url", "")).startswith("https://checkout.stripe.com/"):
        raise PaymentError("Stripe returned an unexpected checkout session")
    return {"id": session["id"], "url": session["url"], "amount_eur": expected,
            "expires_at": int(session.get("expires_at") or 0)}


def verify_webhook(body: bytes, signature_header: str, secret: str | None = None,
                   now: int | None = None) -> dict:
    secret = secret or _secret("ALBUM_STRIPE_WEBHOOK_SECRET")
    if not secret.startswith("whsec_") or not body or len(body) > 65536:
        raise PaymentError("Invalid Stripe webhook")
    pieces = [part.strip().split("=", 1) for part in signature_header.split(",") if "=" in part]
    values: dict[str, list[str]] = {}
    for key, value in pieces:
        values.setdefault(key, []).append(value)
    try:
        timestamp = int(values["t"][0])
    except (KeyError, ValueError, IndexError) as exc:
        raise PaymentError("Missing Stripe webhook timestamp") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > 300:
        raise PaymentError("Expired Stripe webhook")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in values.get("v1", [])):
        raise PaymentError("Invalid Stripe webhook signature")
    event = json.loads(body)
    if not isinstance(event, dict) or event.get("livemode") is not False:
        raise PaymentError("Only Stripe test events are accepted")
    return event
