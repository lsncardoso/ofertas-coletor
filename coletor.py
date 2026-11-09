"""Coletor de ofertas da Amazon Brasil (roda no GitHub Actions).
Grava em <pasta>/: urgentes.json (sempre), comuns.json (1x por hora) e status.json.
Só dados públicos de ofertas: nenhum token ou dado pessoal."""
import json, os, random, sys, time, urllib.parse
from datetime import datetime, timezone

import requests

MIN = int(os.getenv("MIN_DESCONTO", "40"))
URG = int(os.getenv("URGENTE", "50"))
OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
BASE = "https://www.amazon.com.br"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
CAP = 480


class Bloqueado(Exception):
    pass


S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
try:
    S.get(BASE + "/deals", headers={"Accept": "text/html"}, timeout=30)
except requests.RequestException:
    pass


def api(lo, hi, dep=None, idx=0, refin=False):
    rank = json.dumps({"pageTypeId": "deals", "rankGroup": "PARENT_ASIN_RANKING"})
    url = (BASE + f"/d2b/api/v1/products/search?pageSize=60&startIndex={idx}"
           f"&calculateRefinements={'true' if refin else 'false'}"
           f"&rankingContext={urllib.parse.quote(rank)}&rangeRefinementFilters="
           + urllib.parse.quote(json.dumps([{"id": "percentOff", "min": lo, "max": hi}])))
    if dep:
        url += "&refinementFilters=" + urllib.parse.quote(json.dumps([{"id": "departments", "value": [dep]}]))
    time.sleep(random.uniform(0.3, 0.7))
    for tentativa in range(3):
        r = S.get(url, headers={"Accept": "application/json", "Referer": BASE + "/deals"}, timeout=30)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                pass
        time.sleep(5 * (tentativa + 1))
    raise Bloqueado(f"API respondeu {r.status_code}")


def item(p):
    pr = (p.get("price") or {}).get("priceToPay", {}).get("price")
    de = ((p.get("price") or {}).get("basisPrice") or {}).get("price")
    frags = (((p.get("dealBadge") or {}).get("label") or {}).get("content") or {}).get("fragments") or []
    badge = frags[0].get("text", "") if frags else ""
    pc = None
    if badge.split("%")[0].strip().isdigit():
        pc = int(badge.split("%")[0])
    elif pr and de:
        pc = round((1 - float(pr) / float(de)) * 100)
    if pc is None or not pr:
        return None
    return {"t": p.get("title", "")[:120], "p": pr, "d": de, "pc": pc,
            "rel": (p.get("dealDetails") or {}).get("type") == "LIGHTNING_DEAL"}


def faixa(lo, hi, dep):
    itens, idx = {}, 0
    for _ in range(12):
        j = api(lo, hi, dep, idx)
        ps = j.get("products") or []
        for p in ps:
            it = item(p)
            if it:
                itens[p["asin"]] = it
        nxt = j.get("nextIndex")
        if not ps or nxt is None or nxt <= idx:
            break
        idx = nxt
    if len(itens) >= CAP and hi > lo:
        meio = (lo + hi) // 2
        itens = {**faixa(lo, meio, dep), **faixa(meio + 1, hi, dep)}
    return itens


def varrer(faixas, deps):
    """Faixas pequenas de desconto evitam bater no limite de 500 (e refazer consultas)."""
    tudo = {}
    for dep in deps:
        for lo, hi in faixas:
            tudo.update(faixa(lo, hi, dep))
    return tudo


def fatiar(lo, hi, passo):
    return [(a, min(a + passo - 1, hi)) for a in range(lo, hi + 1, passo)]


def salvar(nome, dados):
    with open(os.path.join(OUT, nome), "w") as f:
        json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))


def main():
    os.makedirs(OUT, exist_ok=True)
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = {"gerado": agora, "ok": True}
    try:
        with open(os.path.join(OUT, "status.json")) as f:
            status["comuns_gerado"] = json.load(f).get("comuns_gerado")
    except (OSError, ValueError):
        pass
    completo = (os.getenv("COMPLETO") == "true" or datetime.now(timezone.utc).minute < 10
                or not os.path.exists(os.path.join(OUT, "comuns.json")))
    try:
        j = api(MIN, 100, refin=True)
        dep_ref = next((x for x in j.get("refinements", []) if x.get("id") == "departments"), {})
        deps = [o["value"] for o in dep_ref.get("options", [])]
        if not deps:
            raise Bloqueado("lista de departamentos vazia")
        urg = varrer(fatiar(URG, 69, 5) + [(70, 100)], deps)
        salvar("urgentes.json", {"gerado": agora, "itens": urg})
        print(f"urgentes: {len(urg)}", flush=True)
        if completo:
            com = varrer(fatiar(MIN, URG - 1, 4), deps)
            salvar("comuns.json", {"gerado": agora, "itens": com})
            status["comuns_gerado"] = agora
            print(f"comuns: {len(com)}")
    except (Bloqueado, requests.RequestException) as e:
        status.update(ok=False, erro=str(e))
        print("Falhou:", e)
    salvar("status.json", status)


if __name__ == "__main__":
    main()
