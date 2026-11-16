"""Teste rápido da Creators API da Amazon (Brasil). Lê AMZ_ID / AMZ_SECRET / AMAZON_TAG do .env.
Não imprime as credenciais."""
import json
import sys

import requests


def env(nome):
    for linha in open(".env", encoding="utf-8"):
        if linha.startswith(nome + "="):
            return linha.split("=", 1)[1].strip()
    return ""


ID, SECRET, TAG = env("AMZ_ID"), env("AMZ_SECRET"), env("AMAZON_TAG") or "lsncardoso-20"
VERSAO = env("AMZ_VERSAO") or "3.1"
MKT = "www.amazon.com.br"
print(f"Credenciais: id {len(ID)} caracteres, secret {len(SECRET)} caracteres, versão {VERSAO}, tag {TAG}")
if not ID or not SECRET:
    sys.exit("Faltam AMZ_ID e/ou AMZ_SECRET no .env")

r = requests.post("https://api.amazon.com/auth/o2/token", timeout=30, json={
    "grant_type": "client_credentials", "client_id": ID, "client_secret": SECRET, "scope": "creatorsapi::default"})
print("Token:", r.status_code, "" if r.ok else r.text[:300])
if not r.ok:
    sys.exit(1)
tok = r.json()["access_token"]
H = {"Authorization": f"Bearer {tok}, Version {VERSAO}", "Content-Type": "application/json", "x-marketplace": MKT}
RES = ["itemInfo.title", "offersV2.listings.price", "offersV2.listings.availability",
       "offersV2.listings.dealDetails", "images.primary.large", "customerReviews.count", "customerReviews.starRating",
       "browseNodeInfo.websiteSalesRank"]


def chamar(op, corpo):
    for auth in (f"Bearer {tok}", f"Bearer {tok}, Version {VERSAO}"):
        h = dict(H, Authorization=auth)
        r = requests.post(f"https://creatorsapi.amazon/catalog/v1/{op}", headers=h, json=corpo, timeout=30)
        if r.status_code not in (400, 401, 403):
            break
        print(f"  ({op} com '{auth.split(',')[-1].strip()[:7]}…': {r.status_code}, tentando outro formato)")
    print(f"{op}:", r.status_code)
    if not r.ok:
        print(r.text[:500])
        return None
    return r.json()


def mostrar(itens):
    for it in itens[:5]:
        l = ((it.get("offersV2") or {}).get("listings") or [{}])[0]
        pr = l.get("price") or {}
        sv = (pr.get("savings") or {})
        print("  -", ((it.get("itemInfo") or {}).get("title") or {}).get("displayValue", "")[:60],
              "|", (pr.get("money") or {}).get("displayAmount"),
              "| de", ((pr.get("savingBasis") or {}).get("money") or {}).get("displayAmount"),
              "|", sv.get("percentage"), "% |", (pr.get("savingBasis") or {}).get("savingBasisType"),
              "| aval", (it.get("customerReviews") or {}).get("count"))


j = chamar("searchItems", {"keywords": "fone de ouvido", "minSavingPercent": 40, "partnerTag": TAG,
                           "marketplace": MKT, "itemCount": 10, "resources": RES})
if j:
    itens = (j.get("searchResult") or j.get("SearchResult") or {}).get("items") or \
            (j.get("searchResult") or j.get("SearchResult") or {}).get("Items") or []
    print(f"  {len(itens)} itens; chaves da resposta: {list(j.keys())}")
    mostrar(itens)
    if itens:
        asin = itens[0].get("asin") or itens[0].get("ASIN")
        g = chamar("getItems", {"itemIds": [asin], "itemIdType": "ASIN", "partnerTag": TAG,
                                "marketplace": MKT, "resources": RES})
        if g:
            print("  getItems ok:", json.dumps(g)[:300])
