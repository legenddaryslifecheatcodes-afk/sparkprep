"""One door for every checkout: Stripe (the default) or Square, chosen by PAYMENTS_PROVIDER.

Every call site builds its checkout the way it always has (Stripe-shaped keyword arguments) and calls
create_session / retrieve_session / expire_session / retrieve_invoice here. With PAYMENTS_PROVIDER unset or
"stripe", each call goes straight to the stripe library exactly as before, so nothing changes. With
PAYMENTS_PROVIDER=square the same arguments become a Square payment link, and what comes back has the same
fields the call sites already read (id, url, payment_status, status, subscription, invoice, metadata).

Square settings (Render environment):
  SQUARE_ACCESS_TOKEN           production access token from the Square Developer dashboard
  SQUARE_LOCATION_ID            the location payments are taken at
  SQUARE_ENVIRONMENT            "production" (default) or "sandbox"
  SQUARE_WEBHOOK_SIGNATURE_KEY  signature key of the webhook subscription
  SQUARE_WEBHOOK_URL            the exact notification URL registered with Square (part of the signature)

A Square "session id" is the payment link id. Square sends the buyer back to the success URL with our
{CHECKOUT_SESSION_ID} placeholder already filled in, so the existing return pages keep working unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
import stripe
from dateutil.relativedelta import relativedelta

SQUARE_VERSION = "2025-01-23"
_PLACEHOLDER = "{CHECKOUT_SESSION_ID}"
_plan_variation_cache: dict = {}


class PaymentError(Exception):
    """A Square call failed. Call sites catch ERRORS, which covers this and Stripe's own errors."""


ERRORS = (stripe.error.StripeError, PaymentError)


class Obj(dict):
    """Dict that also answers attribute access, like a Stripe object."""
    __getattr__ = dict.get


def provider() -> str:
    return (os.environ.get("PAYMENTS_PROVIDER") or "stripe").strip().lower()


def using_square() -> bool:
    return provider() == "square"


def ready() -> bool:
    if using_square():
        return bool(os.environ.get("SQUARE_ACCESS_TOKEN") and os.environ.get("SQUARE_LOCATION_ID"))
    return bool(stripe.api_key) and stripe.api_key not in ("sk_test_not_configured", "")


def mode_label() -> str:
    """For the startup log line."""
    if using_square():
        env = (os.environ.get("SQUARE_ENVIRONMENT") or "production").lower()
        return f"Square ({env})" if ready() else "Square (NOT configured: token or location missing)"
    return "Stripe"


# ------------------------------------------------------------------ Square HTTP
def _base() -> str:
    env = (os.environ.get("SQUARE_ENVIRONMENT") or "production").lower()
    return "https://connect.squareupsandbox.com" if env == "sandbox" else "https://connect.squareup.com"


def _sq(method: str, path: str, body: Optional[dict] = None, *, allow_404: bool = False) -> Optional[dict]:
    headers = {"Authorization": f"Bearer {os.environ.get('SQUARE_ACCESS_TOKEN', '')}",
               "Square-Version": SQUARE_VERSION, "Content-Type": "application/json"}
    try:
        r = requests.request(method, _base() + path, json=body, headers=headers, timeout=20)
    except requests.RequestException as e:
        raise PaymentError(f"Square unreachable: {e}") from e
    if allow_404 and r.status_code == 404:
        return None
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    if r.status_code >= 400:
        errs = "; ".join(e.get("detail") or e.get("code", "") for e in data.get("errors", [])) or r.text[:200]
        raise PaymentError(f"Square error {r.status_code}: {errs}")
    return data


def _money(cents: int) -> dict:
    return {"amount": int(cents), "currency": "USD"}


def _square_metadata(md: Optional[dict]) -> dict:
    # Square order metadata: string values, at most 10 entries, short keys.
    out = {}
    for k, v in (md or {}).items():
        if v is not None and len(out) < 10:
            out[str(k)[:60]] = str(v)[:255]
    return out


def _plan_variation_id(name: str, amount_cents: int) -> str:
    """A monthly Square subscription plan variation for this price, created once and then reused."""
    label = f"{name} (${amount_cents / 100:.2f}/month)"
    if label in _plan_variation_cache:
        return _plan_variation_cache[label]
    found = _sq("POST", "/v2/catalog/search", {"object_types": ["SUBSCRIPTION_PLAN_VARIATION"],
                                               "query": {"exact_query": {"attribute_name": "name",
                                                                         "attribute_value": label}}})
    for o in (found or {}).get("objects", []) or []:
        if not o.get("is_deleted"):
            _plan_variation_cache[label] = o["id"]
            return o["id"]
    plan = _sq("POST", "/v2/catalog/object", {"idempotency_key": uuid.uuid4().hex, "object": {
        "type": "SUBSCRIPTION_PLAN", "id": "#plan", "subscription_plan_data": {"name": name}}})
    plan_id = plan["catalog_object"]["id"]
    var = _sq("POST", "/v2/catalog/object", {"idempotency_key": uuid.uuid4().hex, "object": {
        "type": "SUBSCRIPTION_PLAN_VARIATION", "id": "#variation",
        "subscription_plan_variation_data": {
            "name": label, "subscription_plan_id": plan_id,
            "phases": [{"cadence": "MONTHLY", "ordinal": 0,
                        "pricing": {"type": "STATIC", "price_money": _money(amount_cents)}}]}}})
    _plan_variation_cache[label] = var["catalog_object"]["id"]
    return _plan_variation_cache[label]


# ------------------------------------------------------------------ the one door
def create_session(*, credit_cents: int = 0, credit_name: str = "SparkPrep audit credit", **kw):
    """Same keyword arguments as stripe.checkout.Session.create. credit_cents takes money off once (the audit
    credit); on Stripe it becomes a one-time coupon exactly as before."""
    if not using_square():
        if credit_cents:
            coupon = stripe.Coupon.create(amount_off=credit_cents, currency="usd", duration="once",
                                          max_redemptions=1, name=credit_name)
            kw["discounts"] = [{"coupon": coupon.id}]
        return stripe.checkout.Session.create(**kw)
    return _square_create(credit_cents=credit_cents, credit_name=credit_name, **kw)


def _square_create(*, mode: str, line_items: list, success_url: str, cancel_url: str = "",
                   metadata: Optional[dict] = None, customer_email: Optional[str] = None,
                   credit_cents: int = 0, credit_name: str = "", **_ignored):
    pd = line_items[0]["price_data"]
    name = pd["product_data"]["name"]
    desc = pd["product_data"].get("description") or ""
    amount = int(pd["unit_amount"])
    location = os.environ.get("SQUARE_LOCATION_ID", "")
    body: dict = {"idempotency_key": uuid.uuid4().hex,
                  "checkout_options": {"redirect_url": success_url, "ask_for_shipping_address": False}}
    if customer_email:
        body["pre_populated_data"] = {"buyer_email": customer_email}
    if mode == "subscription":
        # Square only sells a subscription through a quick-pay link; it can't carry a one-time discount.
        body["quick_pay"] = {"name": name, "price_money": _money(amount), "location_id": location}
        body["checkout_options"]["subscription_plan_id"] = _plan_variation_id(name, amount)
    else:
        order = {"location_id": location, "metadata": _square_metadata(metadata),
                 "line_items": [{"name": name, "quantity": "1", "base_price_money": _money(amount),
                                 **({"note": desc[:500]} if desc else {})}]}
        if credit_cents:
            order["discounts"] = [{"name": credit_name, "amount_money": _money(credit_cents), "scope": "ORDER"}]
        body["order"] = order
    link = _sq("POST", "/v2/online-checkout/payment-links", body)["payment_link"]
    # The link id is only known now, so fill it into the return address the buyer is sent back to.
    if _PLACEHOLDER in success_url:
        updated = _sq("PUT", f"/v2/online-checkout/payment-links/{link['id']}", {"payment_link": {
            "version": link.get("version", 1),
            "checkout_options": {"redirect_url": success_url.replace(_PLACEHOLDER, link["id"]),
                                 "ask_for_shipping_address": False,
                                 **({"subscription_plan_id": body["checkout_options"]["subscription_plan_id"]}
                                    if mode == "subscription" else {})}}})
        link = (updated or {}).get("payment_link") or link
    return Obj(id=link["id"], url=link.get("long_url") or link.get("url"), order_id=link.get("order_id"),
               payment_status="unpaid", status="open", subscription=None, invoice=None,
               metadata=metadata or {}, mode=mode)


def _order_paid(order: dict) -> bool:
    if (order or {}).get("state") == "COMPLETED":
        return True
    due = ((order or {}).get("net_amount_due_money") or {}).get("amount")
    return bool((order or {}).get("tenders")) and due == 0


def _find_subscription(order: dict, plan_variation_id: Optional[str]) -> Optional[dict]:
    customer = (order or {}).get("customer_id")
    if not customer:
        return None
    res = _sq("POST", "/v2/subscriptions/search", {"query": {"filter": {
        "customer_ids": [customer], "location_ids": [os.environ.get("SQUARE_LOCATION_ID", "")]}}}) or {}
    subs = [s for s in res.get("subscriptions", []) or []
            if not plan_variation_id or s.get("plan_variation_id") == plan_variation_id]
    return max(subs, key=lambda s: s.get("created_at", ""), default=None)


def _period_invoice_id(sub_id: str, period_start: str) -> str:
    # The first month (paid at checkout) and Square's invoice for that same month share this id, so the
    # month's books are granted once no matter which confirmation arrives first.
    return f"sqsub:{sub_id}:{period_start[:10]}"


def retrieve_session(session_id: str):
    if not using_square():
        return stripe.checkout.Session.retrieve(session_id)
    link = (_sq("GET", f"/v2/online-checkout/payment-links/{session_id}", allow_404=True) or {}).get("payment_link")
    if not link:
        return Obj(id=session_id, payment_status="unpaid", status="expired", subscription=None, invoice=None,
                   metadata={})
    order = (_sq("GET", f"/v2/orders/{link['order_id']}", allow_404=True) or {}).get("order") or {}
    paid = _order_paid(order)
    sub_id = invoice = None
    plan_var = (link.get("checkout_options") or {}).get("subscription_plan_id")
    if paid and plan_var:
        sub = _find_subscription(order, plan_var)
        if sub:
            sub_id = sub["id"]
            invoice = _period_invoice_id(sub_id, sub.get("start_date") or order.get("created_at") or "")
    return Obj(id=session_id, url=link.get("long_url") or link.get("url"), order_id=link.get("order_id"),
               payment_status="paid" if paid else "unpaid", status="complete" if paid else "open",
               subscription=sub_id, invoice=invoice, metadata=order.get("metadata") or {})


def expire_session(session_id: str) -> None:
    if not using_square():
        stripe.checkout.Session.expire(session_id)
        return
    _sq("DELETE", f"/v2/online-checkout/payment-links/{session_id}", allow_404=True)


def retrieve_invoice(invoice_id: str):
    if not using_square():
        return stripe.Invoice.retrieve(invoice_id)
    # Square: our per-month id (see _period_invoice_id); it is only handed out once that month is paid.
    _, _, start = invoice_id.split(":", 2)
    return square_period_invoice(invoice_id.split(":")[1], start)


def square_period_invoice(sub_id: str, period_start: str) -> Obj:
    start = datetime.fromisoformat(period_start[:10]).replace(tzinfo=timezone.utc)
    end = start + relativedelta(months=1)
    return Obj(id=_period_invoice_id(sub_id, period_start), status="paid", paid=True,
               period_end=int(end.timestamp()))


def retrieve_subscription_metadata(sub_id: str) -> dict:
    if not using_square():
        s = stripe.Subscription.retrieve(sub_id)
        return (s.get("metadata") if isinstance(s, dict) else getattr(s, "metadata", None)) or {}
    return {}  # Square subscriptions carry no metadata; the checkout confirmation registers them instead


# ------------------------------------------------------------------ Square webhook
def square_signature_ok(body: bytes, signature: str) -> bool:
    key = os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
    url = os.environ.get("SQUARE_WEBHOOK_URL", "")
    if not key or not url or not signature:
        return False
    digest = hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
