#!/usr/bin/env python3
"""Extraction en LECTURE SEULE de la configuration Brevo CEPAC.

- N'utilise que des requêtes GET (aucune création, modification, suppression ni envoi).
- N'écrit aucune donnée personnelle : seuls des volumes agrégés et des éléments
  de configuration (noms d'attributs, de listes, de segments, de campagnes) sont produits.

Usage :
    export BREVO_API_KEY=xkeysib-...
    python3 scripts/extract_brevo_config.py > brevo_snapshot.json
"""
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

API = "https://api.brevo.com/v3"
KEY = os.environ.get("BREVO_API_KEY")

# Attributs contact dont on veut la répartition des valeurs (agrégée).
AGG_ATTRIBUTES = [
    "CIBLE", "SOUS_CIBLE", "REGION_FRANCE", "DEPARTEMENT",
    "SOURCE", "TELECHARGEMENT_RESSOURCE",
    "OPT_IN", "DOUBLE_OPT-IN", "_PIXEL_TRACKING_CONSENT",
    "_PIXEL_TRACKING_CONSENT_SOURCE", "TEST_FONCTIONNEL", "UTM_SOURCE", "UTM_MEDIUM",
]
# Attributs dont on veut seulement savoir s'ils sont renseignés (jamais leur valeur).
FILLED_ATTRIBUTES = [
    "JOB_TITLE", "COMPANY_NAME", "MESSAGE", "SMS", "LINKEDIN",
    "_PIXEL_TRACKING_CONSENT_DATE", "_LAST_EMAIL_OPEN_DATE",
    "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN",
]
# Croisements utiles (agrégés) : attribut A x attribut B.
CROSSES = [
    ("CIBLE", "REGION_FRANCE"),
    ("SOURCE", "OPT_IN"),
    ("SOURCE", "DOUBLE_OPT-IN"),
    ("OPT_IN", "_BLACKLISTED"),
    ("DOUBLE_OPT-IN", "_BLACKLISTED"),
    ("_LISTS", "OPT_IN"),
    ("_LISTS", "_BLACKLISTED"),
]


def get(path, params=None):
    url = API + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method="GET", headers={
        "api-key": KEY, "accept": "application/json",
        # Sans User-Agent explicite, Cloudflare bloque certains endpoints (erreur 1010).
        "user-agent": "cepac-brevo-audit/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} sur GET {path}", "_detail": e.read().decode()[:300]}


def paginate(path, key, limit=50, extra=None):
    items, offset = [], 0
    while True:
        page = get(path, {"limit": limit, "offset": offset, **(extra or {})})
        if "_error" in page:
            return items, page
        batch = page.get(key) or []
        items.extend(batch)
        if len(batch) < limit:
            return items, None
        offset += limit


def values_of(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def main():
    if not KEY:
        sys.exit("BREVO_API_KEY manquante")

    out = {}

    account = get("/account")
    out["account"] = {
        "companyName": account.get("companyName"),
        "plan": account.get("plan"),
        "marketingAutomation": account.get("marketingAutomation"),
        "_error": account.get("_error"),
    }
    senders = get("/senders")
    out["senders"] = [
        {"name": s.get("name"), "domain": (s.get("email") or "").split("@")[-1], "active": s.get("active")}
        for s in senders.get("senders", [])
    ] or senders
    out["domains"] = get("/senders/domains")

    out["contact_attributes"] = get("/contacts/attributes")
    out["company_attributes"] = get("/companies/attributes")

    folders, err = paginate("/contacts/folders", "folders")
    out["folders"] = err or folders
    lists, err = paginate("/contacts/lists", "lists")
    out["lists"] = err or lists
    segments, err = paginate("/contacts/segments", "segments")
    out["segments"] = err or segments

    companies = get("/companies", {"limit": 1})
    out["companies_count"] = companies.get("pager", {}).get("total", companies.get("_error"))

    campaigns, err = paginate("/emailCampaigns", "campaigns",
                              extra={"excludeHtmlContent": "false", "statistics": "globalStats"})
    out["email_campaigns"] = err or [
        {
            "name": c.get("name"), "status": c.get("status"), "sentDate": c.get("sentDate"),
            "scheduledAt": c.get("scheduledAt"), "recipients": c.get("recipients"),
            "has_revoke_pixel_tag": "revoke_open_pixel_tracking" in (c.get("htmlContent") or ""),
            "has_pixel_condition": "PIXEL_TRACKING_CONSENT" in (c.get("htmlContent") or ""),
            "has_unsubscribe": "unsubscribe" in (c.get("htmlContent") or "").lower(),
            "stats": (c.get("statistics") or {}).get("globalStats"),
        }
        for c in campaigns
    ]
    sms, err = paginate("/smsCampaigns", "campaigns")
    out["sms_campaigns"] = err or [{"name": c.get("name"), "status": c.get("status")} for c in sms]

    templates, err = paginate("/smtp/templates", "templates")
    out["templates"] = err or [
        {
            "name": t.get("name"), "isActive": t.get("isActive"),
            "has_revoke_pixel_tag": "revoke_open_pixel_tracking" in (t.get("htmlContent") or ""),
            "has_pixel_condition": "PIXEL_TRACKING_CONSENT" in (t.get("htmlContent") or ""),
            "has_unsubscribe": "unsubscribe" in (t.get("htmlContent") or "").lower(),
        }
        for t in templates
    ]

    # Les attributs de type Catégorie sont renvoyés sous forme d'identifiant numérique :
    # on les traduit via l'énumération déclarée sur l'attribut.
    enum_labels = {
        a.get("name"): {str(e.get("value")): e.get("label") for e in a.get("enumeration") or []}
        for a in out["contact_attributes"].get("attributes", [])
    }

    # Contacts : uniquement des agrégats, aucune donnée nominative conservée.
    contacts, err = paginate("/contacts", "contacts", limit=1000)
    agg = {a: Counter() for a in AGG_ATTRIBUTES}
    filled = Counter()
    per_list = Counter()
    list_combos = Counter()
    crosses = {f"{a} x {b}": Counter() for a, b in CROSSES}
    created_by_month = Counter()
    blacklisted = 0

    def labelled(attrs, a):
        labels = enum_labels.get(a) or {}
        return [labels.get(v, v) for v in values_of(attrs.get(a))] or ["(vide)"]

    for c in contacts:
        attrs = dict(c.get("attributes") or {})
        attrs["_BLACKLISTED"] = "oui" if c.get("emailBlacklisted") else "non"
        attrs["_LISTS"] = "+".join(str(i) for i in sorted(c.get("listIds") or [])) or "aucune"
        for a in AGG_ATTRIBUTES:
            for v in labelled(attrs, a):
                agg[a][v] += 1
        for a in FILLED_ATTRIBUTES:
            if values_of(attrs.get(a)):
                filled[a] += 1
        for a, b in CROSSES:
            for va in labelled(attrs, a):
                for vb in labelled(attrs, b):
                    crosses[f"{a} x {b}"][f"{va} | {vb}"] += 1
        for lid in c.get("listIds") or []:
            per_list[lid] += 1
        list_combos[attrs["_LISTS"]] += 1
        created_by_month[(c.get("createdAt") or "")[:7] or "(inconnu)"] += 1
        if c.get("emailBlacklisted"):
            blacklisted += 1
    out["contacts_aggregates"] = {
        "_error": err,
        "total": len(contacts),
        "email_blacklisted": blacklisted,
        "by_attribute": {a: dict(v.most_common()) for a, v in agg.items()},
        "filled_count": dict(filled),
        "by_list_id": dict(per_list),
        "by_list_combination": dict(list_combos),
        "created_by_month": dict(sorted(created_by_month.items())),
        "crosses": {k: dict(v.most_common()) for k, v in crosses.items()},
    }

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
