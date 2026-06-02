"""
Webhook systeme.io → Abby
Regroupe offre principale + order bump + upsell en une seule facture Abby.
"""

import os
import json
import time
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ABBY_API_KEY  = os.environ.get("ABBY_API_KEY", "")   # Clé API Abby
ABBY_BASE_URL = "https://api.abby.fr"                 # URL de base Abby
DELAY_SECONDS = 60                                    # Attendre 1 min avant de créer la facture
PORT          = int(os.environ.get("PORT", 8080))

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── STOCKAGE TEMPORAIRE ─────────────────────────────────────────────────────
# { order_id: { "items": [...], "customer": {...}, "timer": Timer } }
pending_orders: dict = {}
lock = threading.Lock()


# ─── HELPERS API ABBY ─────────────────────────────────────────────────────────

def abby_headers():
    return {
        "Authorization": f"Bearer {ABBY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def abby_get(path: str) -> dict | list | None:
    r = requests.get(f"{ABBY_BASE_URL}{path}", headers=abby_headers(), timeout=15)
    if r.ok:
        return r.json()
    log.error("GET %s → %s %s", path, r.status_code, r.text[:300])
    return None


def abby_post(path: str, body: dict) -> dict | None:
    r = requests.post(f"{ABBY_BASE_URL}{path}", headers=abby_headers(), json=body, timeout=15)
    if r.ok:
        return r.json()
    log.error("POST %s → %s %s", path, r.status_code, r.text[:300])
    return None


# ─── LOGIQUE ABBY ─────────────────────────────────────────────────────────────

def find_organization_by_email(email: str) -> dict | None:
    """Cherche une organisation existante par e-mail."""
    result = abby_get("/api/v1/organizations")
    if not result:
        return None
    orgs = result if isinstance(result, list) else result.get("data", [])
    for org in orgs:
        if org.get("email", "").lower() == email.lower():
            return org
    return None


def create_organization(customer: dict) -> dict | None:
    """Crée une nouvelle organisation dans Abby."""
    fields = customer.get("fields", {})
    body = {
        "name": fields.get("company_name") or f"{fields.get('first_name', '')} {fields.get('surname', '')}".strip(),
        "email": customer.get("email", ""),
        "type": "company",
        "country": fields.get("country", "FR"),
        "vatNumber": fields.get("tax_number", ""),
    }
    log.info("Création organisation : %s", body["name"])
    return abby_post("/api/v1/organizations", body)


def get_or_create_organization(customer: dict) -> str | None:
    """Retourne l'ID de l'organisation (existante ou créée)."""
    email = customer.get("email", "")
    org = find_organization_by_email(email)
    if org:
        log.info("Organisation trouvée : %s (id=%s)", org.get("name"), org.get("id"))
        return org["id"]
    org = create_organization(customer)
    if org:
        log.info("Organisation créée : id=%s", org.get("id"))
        return org["id"]
    return None


def create_contact_for_org(org_id: str, customer: dict) -> str | None:
    """Crée un contact rattaché à l'organisation."""
    fields = customer.get("fields", {})
    body = {
        "organizationId": org_id,
        "firstName": fields.get("first_name", ""),
        "lastName": fields.get("surname", ""),
        "email": customer.get("email", ""),
    }
    result = abby_post("/api/v1/contacts", body)
    if result:
        log.info("Contact créé : id=%s", result.get("id"))
        return result["id"]
    return None


def create_invoice_draft(org_id: str, contact_id: str | None, items: list) -> dict | None:
    """Crée une facture en brouillon avec toutes les lignes."""
    lines = []
    for item in items:
        lines.append({
            "description": item["name"],
            "quantity": 1,
            "unitPrice": item["unit_price_eur"],  # en euros (float)
            "vatRate": 0,                          # TVA 0% (ajustez si besoin)
        })

    body = {
        "organizationId": org_id,
        "status": "draft",
        "lines": lines,
    }
    if contact_id:
        body["contactId"] = contact_id

    log.info("Création facture brouillon (%d ligne(s))…", len(lines))
    return abby_post("/api/v1/invoices", body)


# ─── TRAITEMENT COMMANDE ──────────────────────────────────────────────────────

def process_order(order_id: str):
    """
    Appelée après le délai de 1 minute.
    Regroupe tous les items reçus et crée une seule facture Abby.
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

    # 1. Org
    org_id = get_or_create_organization(customer)
    if not org_id:
        log.error("Impossible de trouver/créer l'organisation — abandon.")
        return

    # 2. Contact
    contact_id = create_contact_for_org(org_id, customer)

    # 3. Facture brouillon
    invoice = create_invoice_draft(org_id, contact_id, items)
    if invoice:
        log.info("✅ Facture créée : id=%s", invoice.get("id"))
    else:
        log.error("❌ Échec création facture.")


# ─── PARSING WEBHOOK ──────────────────────────────────────────────────────────

def parse_webhook(payload: dict) -> tuple[str, dict, dict] | None:
    """
    Retourne (order_id, customer, item) depuis un webhook systeme.io.
    item = { name, unit_price_eur }
    """
    try:
        data = payload.get("data", {})
        order_id = str(data["order"]["id"])
        customer = data["customer"]

        offer = data.get("offer_price_plan") or data.get("price_plan", {})
        name  = offer.get("name", "Produit")
        # direct_charge_amount est en centimes
        amount_cents = offer.get("direct_charge_amount") or offer.get("amount", 0)
        unit_price   = round(amount_cents / 100, 2)

        # Si discount couvre tout → prix réel 0 € mais on garde le nom
        discount = data.get("order", {}).get("discount_amount") or 0
        total    = data.get("order", {}).get("total_price")
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
        pass  # On gère nos propres logs

    def do_GET(self):
        """Page d'accueil pour vérifier que le serveur tourne."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Serveur webhook systeme.io -> Abby actif OK".encode("utf-8"))

    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length)

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
                log.info("Nouvelle commande reçue : %s — démarrage timer %ds", order_id, DELAY_SECONDS)
                timer = threading.Timer(DELAY_SECONDS, process_order, args=[order_id])
                pending_orders[order_id] = {
                    "customer": customer,
                    "items": [],
                    "timer": timer,
                }
                timer.start()

            pending_orders[order_id]["items"].append(item)
            log.info("Produit ajouté à la commande %s : %s (%.2f €)",
                     order_id, item["name"], item["unit_price_eur"])


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ABBY_API_KEY:
        log.warning("⚠️  ABBY_API_KEY non définie ! Positionne la variable d'environnement.")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info("Serveur démarré sur le port %d (délai facture : %ds)", PORT, DELAY_SECONDS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Arrêt du serveur.")
