"""
Webhook systeme.io → Abby
Regroupe offre principale + order bump + upsell en une seule facture Abby (brouillon).
"""

import os
import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ABBY_API_KEY  = os.environ.get("ABBY_API_KEY", "")
ABBY_BASE_URL = "https://api.app-abby.com"
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", 60))
PORT          = int(os.environ.get("PORT", 8080))

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── STOCKAGE TEMPORAIRE ─────────────────────────────────────────────────────
pending_orders: dict = {}
lock = threading.Lock()


# ─── HELPERS API ABBY ─────────────────────────────────────────────────────────

def abby_headers():
    return {
        "Authorization": f"Bearer {ABBY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def abby_get(path: str, params: dict = None) -> dict | list | None:
    r = requests.get(
        f"{ABBY_BASE_URL}{path}",
        headers=abby_headers(),
        params=params,
        timeout=15
    )
    if r.ok:
        return r.json()
    log.error("GET %s → %s %s", path, r.status_code, r.text[:300])
    return None


def abby_post(path: str, body: dict) -> dict | None:
    r = requests.post(
        f"{ABBY_BASE_URL}{path}",
        headers=abby_headers(),
        json=body,
        timeout=15
    )
    if r.ok:
        return r.json()
    log.error("POST %s → %s %s", path, r.status_code, r.text[:300])
    return None


def abby_patch(path: str, body: dict) -> dict | None:
    r = requests.patch(
        f"{ABBY_BASE_URL}{path}",
        headers=abby_headers(),
        json=body,
        timeout=15
    )
    if r.ok:
        return r.json()
    log.error("PATCH %s → %s %s", path, r.status_code, r.text[:300])
    return None


# ─── LOGIQUE ABBY ─────────────────────────────────────────────────────────────

def find_contact_by_email(email: str) -> dict | None:
    """Cherche un contact existant par e-mail.
    Route correcte : GET /contacts (pluriel, sans /v2/)
    Requiert page obligatoire.
    """
    result = abby_get("/contacts", params={"search": email, "page": 1, "limit": 50})
    if not result:
        return None
    docs = result.get("docs", [])
    for c in docs:
        emails = c.get("emails", [])
        if isinstance(emails, list):
            if any(e.lower() == email.lower() for e in emails):
                return c
        elif isinstance(emails, str) and emails.lower() == email.lower():
            return c
    return None


def find_organization_by_email(email: str) -> dict | None:
    """Cherche une organisation existante par e-mail.
    Route correcte : GET /organizations (pluriel, sans /v2/)
    Requiert page obligatoire.
    """
    result = abby_get("/organizations", params={"search": email, "page": 1, "limit": 50})
    if not result:
        return None
    docs = result.get("docs", [])
    for org in docs:
        emails = org.get("emails", [])
        if isinstance(emails, list):
            if any(e.lower() == email.lower() for e in emails):
                return org
        elif isinstance(emails, str) and emails.lower() == email.lower():
            return org
    return None


def create_organization(customer: dict) -> dict | None:
    fields = customer.get("fields", {})
    company = fields.get("company_name") or f"{fields.get('first_name', '')} {fields.get('surname', '')}".strip()
    email   = customer.get("email", "")

    vat_number = fields.get("tax_number", "") or ""

    siren = ""
    if vat_number.upper().startswith("FR") and len(vat_number) >= 13:
        siren = vat_number[4:]  # enlève "FR" + les 2 chiffres de clé TVA

    body = {
        "name": company,
        "emails": [email] if email else [],
        "vatNumber": vat_number,
        "siren": siren,
    }
    address_line = fields.get("address", "")
    city         = fields.get("city", "")
    zip_code     = fields.get("zip_code", fields.get("zipcode", ""))
    country      = fields.get("country", "FR") or "FR"

    if address_line and city and zip_code:
        body["billingAddress"] = {
            "address":  address_line,
            "city":     city,
            "zipCode":  zip_code,
            "country":  country.upper()[:2],
        }

    log.info("Création organisation : %s", company)
    return abby_post("/organization", body)  # ← singulier, sans /v2/


def get_or_create_customer_id(customer: dict) -> str | None:
    """
    Retourne l'ID client Abby (organisation ou contact) pour la facturation.
    """
    email = customer.get("email", "")

    # 1. Cherche une organisation
    org = find_organization_by_email(email)
    if org:
        log.info("Organisation trouvée : %s (id=%s)", org.get("name"), org.get("id"))
        return org["id"]

    # 2. Cherche un contact simple
    contact = find_contact_by_email(email)
    if contact:
        log.info("Contact trouvé : %s (id=%s)", contact.get("fullname", ""), contact.get("id"))
        return contact["id"]

    # 3. Crée l'organisation ou le contact
    fields = customer.get("fields", {})
    company = fields.get("company_name", "")
    if company:
        new_org = create_organization(customer)
        if new_org:
            log.info("Organisation créée : id=%s", new_org.get("id"))
            return new_org["id"]
    else:
        first  = fields.get("first_name", "")
        last   = fields.get("surname", fields.get("last_name", ""))
        body   = {
            "firstname": first or "Client",
            "lastname":  last or first or "Client",
            "emails":    [email] if email else [],
        }
        # Route correcte : POST /contact (singulier, sans /v2/)
        new_contact = abby_post("/contact", body)
        if new_contact:
            log.info("Contact créé : id=%s", new_contact.get("id"))
            return new_contact["id"]

    return None


def create_invoice_with_lines(customer_id: str, items: list) -> dict | None:
    """
    Crée une facture brouillon puis y ajoute toutes les lignes via PATCH.

    Étape 1 : POST /v2/billing/invoice/{customerId}  → crée la facture vide
    Étape 2 : PATCH /v2/billing/{billingId}/lines    → ajoute les lignes

    Montants en centimes. vatCode FR_00HT = TVA non applicable (auto-entrepreneur).
    """
    # Étape 1 : créer la facture vide
    log.info("Création facture brouillon pour customerId=%s…", customer_id)
    invoice = abby_post(f"/v2/billing/invoice/{customer_id}", {})
    if not invoice:
        return None

    billing_id = invoice.get("id")
    log.info("Facture créée : id=%s — ajout de %d ligne(s)…", billing_id, len(items))

    # Étape 2 : ajouter les lignes
    lines = []
    for item in items:
        unit_price_cents = round(item["unit_price_eur"] * 100)
        lines.append({
            "designation":  item["name"],
            "quantity":     1,
            "quantityUnit": "unit",
            "unitPrice":    unit_price_cents,
            "type":         "sale_of_goods",   # ← string en minuscules
            "vatCode":      "FR_00HT",
            "isDeliveryOfGoods": False,
        })
    updated = abby_patch(f"/v2/billing/{billing_id}/lines", {"lines": lines})
    if updated:
        log.info("✅ Lignes ajoutées à la facture id=%s", billing_id)
    else:
        log.error("❌ Échec ajout des lignes (facture id=%s créée mais vide)", billing_id)

    return updated or invoice


# ─── TRAITEMENT COMMANDE ──────────────────────────────────────────────────────

def process_order(order_id: str):
    """
    Appelée après le délai de DELAY_SECONDS.
    Regroupe tous les items et crée une seule facture Abby en brouillon.
    """
    with lock:
        order_data = pending_orders.pop(order_id, None)

    if not order_data:
        log.warning("Commande %s introuvable (déjà traitée ?)", order_id)
        return

    customer = order_data["customer"]
    items    = order_data["items"]

    log.info("=== Traitement commande %s — %d produit(s) ===", order_id, len(items))
    for it in items:
        log.info("  • %s (%.2f €)", it["name"], it["unit_price_eur"])

    # 1. Client Abby
    customer_id = get_or_create_customer_id(customer)
    if not customer_id:
        log.error("Impossible de trouver/créer le client — abandon.")
        return

    # 2. Facture brouillon avec lignes
    invoice = create_invoice_with_lines(customer_id, items)
    if invoice:
        log.info("✅ Facture brouillon finalisée : id=%s", invoice.get("id"))
    else:
        log.error("❌ Échec création facture.")


# ─── PARSING WEBHOOK ──────────────────────────────────────────────────────────

def parse_webhook(payload: dict) -> tuple[str, dict, dict] | None:
    """
    Retourne (order_id, customer, item) depuis un webhook systeme.io.
    """
    try:
        data     = payload.get("data", {})
        order_id = str(data["order"]["id"])
        customer = data["customer"]

        offer        = data.get("offer_price_plan") or data.get("price_plan", {})
        name         = offer.get("name", "Produit")
        amount_cents = offer.get("direct_charge_amount") or offer.get("amount", 0)
        unit_price   = round(amount_cents / 100, 2)

        total = data.get("order", {}).get("total_price")
        if total is not None and total == 0:
            unit_price = 0.0

        item = {"name": name, "unit_price_eur": unit_price}
        return order_id, customer, item
    except (KeyError, TypeError) as e:
        log.error("Erreur parsing webhook : %s", e)
        return None


# ─── SERVEUR HTTP ─────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Serveur webhook systeme.io → Abby actif ✓".encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Payload JSON invalide")
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        result = parse_webhook(payload)
        if not result:
            return

        order_id, customer, item = result

        with lock:
            if order_id not in pending_orders:
                log.info("Nouvelle commande : %s — timer %ds", order_id, DELAY_SECONDS)
                timer = threading.Timer(DELAY_SECONDS, process_order, args=[order_id])
                pending_orders[order_id] = {
                    "customer": customer,
                    "items":    [],
                    "timer":    timer,
                }
                timer.start()

            pending_orders[order_id]["items"].append(item)
            log.info("Produit ajouté à %s : %s (%.2f €)",
                     order_id, item["name"], item["unit_price_eur"])


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ABBY_API_KEY:
        log.warning("⚠️  ABBY_API_KEY non définie !")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info("Serveur démarré port %d (délai : %ds)", PORT, DELAY_SECONDS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Arrêt.")
