"""Bot de ofertas da Amazon Brasil (roda 24h no servidor).

- Lê as ofertas publicadas pelo coletor do GitHub (a cada 10 min) e avisa NA HORA as de URGENTE%+.
- Nos horários de RESUMO_HORAS manda um resumo das ofertas novas entre MIN_DESCONTO% e URGENTE%.
- Cobre todas as ofertas da Amazon que aceitam filtro de desconto (todos os departamentos).
- Quem manda /start no bot passa a receber; /stop para sair. Admins recebem avisos de erro e de inscritos.
"""
import html, json, logging, os, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ---------- configuração (arquivo .env) ----------
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMINS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
AMAZON_TAG = os.getenv("AMAZON_TAG", "lsncardoso-20")
MIN_DESCONTO = int(os.getenv("MIN_DESCONTO", "40"))
URGENTE = int(os.getenv("URGENTE", "60"))
RESUMO_HORAS = os.getenv("RESUMO_HORAS", "todas")      # "todas" ou ex.: "9,12,15,18,21"
RESUMO_MAX = int(os.getenv("RESUMO_MAX", "0"))          # máx. de ofertas por resumo (0 = todas)
STATE_FILE = os.getenv("STATE_FILE", "state.json")
CANAL = os.getenv("TELEGRAM_CANAL", "").strip()  # ex.: @cacadescontos40
CANAL_HORAS = {int(h) for h in os.getenv("CANAL_HORAS", "9,12,15,18,21").split(",") if h.strip()}
CANAL_MAX = int(os.getenv("CANAL_MAX", "10"))
AVISO = "\n\n<i>Link de afiliado: como Associado da Amazon, ganho com compras qualificadas.</i>"
TZ = ZoneInfo("America/Sao_Paulo")

BASE = "https://www.amazon.com.br"
TG = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.info
lock = threading.RLock()


# ---------- estado ----------
def carregar():
    st = {"vistos": {}, "inscritos": [], "nomes": {}, "offset": 0, "bloqueado": False,
          "base_feita": False}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st.update(json.load(f))
    # importa inscritos do monitor antigo do GitHub, se o arquivo existir
    if os.path.exists("seen_github.json"):
        with open("seen_github.json") as f:
            old = json.load(f)
        for c in old.get("inscritos", []):
            if c not in st["inscritos"]:
                st["inscritos"].append(c)
        st["nomes"].update(old.get("nomes", {}))
        os.rename("seen_github.json", "seen_github.importado.json")
        log(f"Importados {len(old.get('inscritos', []))} inscritos do GitHub")
    return st


def salvar():
    with lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ST, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)


ST = carregar()


# ---------- Telegram ----------
def enviar(chat, texto):
    try:
        r = requests.post(TG + "/sendMessage", data={
            "chat_id": chat, "text": texto, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}, timeout=30)
    except requests.RequestException as e:
        log(f"Erro Telegram {chat}: {e}")
        return True
    if r.status_code == 429:  # muitas mensagens: espera e tenta de novo
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return enviar(chat, texto)
    if r.status_code == 403:
        return False
    if not r.ok:
        log(f"Erro Telegram {chat}: {r.status_code} {r.text[:200]}")
    time.sleep(0.4)  # ritmo seguro para listas longas
    return True


def para_admins(texto):
    for c in ADMINS:
        enviar(c, texto)


def para_todos(texto, canal=True):
    texto = texto + AVISO
    if CANAL and canal:
        if not enviar(CANAL, texto):
            log(f"Canal {CANAL} recusou a mensagem (o bot e admin do canal?)")
    with lock:
        destinos = list(dict.fromkeys(ADMINS + ST["inscritos"]))
    for c in destinos:
        if not enviar(c, texto) and c in ST["inscritos"]:
            with lock:
                ST["inscritos"].remove(c)
            log(f"Removido (bloqueou o bot): {c}")
    salvar()


def em_blocos(titulo, linhas):
    msgs, msg = [], titulo
    for l in linhas:
        if len(msg) + len(l) + 2 > 3700:
            msgs.append(msg)
            msg = ""
        msg += "\n\n" + l
    msgs.append(msg)
    return msgs


def nome_de(m):
    frm = m.get("from") or {}
    n = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x) or "?"
    return n + (f" (@{frm['username']})" if frm.get("username") else "")


def ouvir_telegram():
    """Fica escutando /start e /stop o tempo todo (resposta imediata)."""
    while True:
        try:
            r = requests.get(TG + "/getUpdates", params={
                "offset": ST["offset"], "timeout": 50,
                "allowed_updates": json.dumps(["message"])}, timeout=70)
            ups = r.json().get("result", [])
        except Exception as e:
            log(f"Erro getUpdates: {e}")
            time.sleep(10)
            continue
        for u in ups:
            with lock:
                ST["offset"] = u["update_id"] + 1
            m = u.get("message") or {}
            chat = str((m.get("chat") or {}).get("id", ""))
            if not chat or (m.get("chat") or {}).get("type") != "private":
                continue
            txt = (m.get("text") or "").strip().lower()
            nome = nome_de(m)
            if txt.startswith("/start"):
                novo = False
                with lock:
                    if chat not in ST["inscritos"]:
                        ST["inscritos"].append(chat)
                        ST["nomes"][chat] = nome
                        novo = True
                enviar(chat, f"✅ Pronto! Você vai receber as ofertas da Amazon:\n\n"
                             f"🔥 <b>{URGENTE}% ou mais</b>: na hora em que aparecem\n"
                             f"🛒 <b>{MIN_DESCONTO}% a {URGENTE - 1}%</b>: em resumos periódicos\n\n"
                             "Para parar, mande /stop.")
                if novo:
                    para_admins(f"👤 Novo inscrito: {html.escape(nome)} — total: {len(ST['inscritos'])}")
            elif txt.startswith("/stop"):
                saiu = False
                with lock:
                    if chat in ST["inscritos"]:
                        ST["inscritos"].remove(chat)
                        saiu = True
                enviar(chat, "Você não vai mais receber as ofertas. Para voltar, mande /start.")
                if saiu:
                    para_admins(f"👋 Saiu: {html.escape(nome)} — total: {len(ST['inscritos'])}")
            elif txt.startswith("/status") and chat in ADMINS:
                enviar(chat, f"🤖 Online. Inscritos: {len(ST['inscritos'])} · "
                             f"produtos conhecidos: {len(ST['vistos'])} · "
                             f"Amazon {'BLOQUEADA ⚠️' if ST['bloqueado'] else 'ok'}")
        if ups:
            salvar()


# ---------- dados (coletados pelo GitHub) ----------
GITHUB_REPO = os.getenv("GITHUB_REPO", "lsncardoso/ofertas-coletor")
CHECAR_MIN = int(os.getenv("CHECAR_MIN", "3"))


def baixar(nome):
    """Lê um arquivo do ramo 'dados' do repositório público do coletor."""
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{nome}",
                     params={"ref": "dados"}, timeout=60,
                     headers={"Accept": "application/vnd.github.raw", "User-Agent": "bot-ofertas"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def brl(v):
    s = f"{float(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def linha(asin, it):
    de = f"<s>{brl(it['d'])}</s> → " if it["d"] else ""
    rel = " ⚡" if it["rel"] else ""
    return (f"<b>{it['pc']}%</b>{rel} · {de}<b>{brl(it['p'])}</b>\n"
            f"<a href=\"{BASE}/dp/{asin}?tag={AMAZON_TAG}\">{html.escape(it['t'][:100])}</a>")


# ---------- ciclo principal ----------
def hora_de_resumo(agora):
    if RESUMO_HORAS == "todas":
        return True
    return agora.hour in {int(h) for h in RESUMO_HORAS.split(",") if h.strip()}


def idade_min(iso):
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
    return (datetime.now(ZoneInfo("UTC")) - t).total_seconds() / 60


def ciclo():
    st = baixar("status.json")
    if not st:
        log("Coletor ainda não publicou dados")
        return
    if st["gerado"] == ST.get("ult_gerado"):
        if idade_min(st["gerado"]) > 45 and not ST.get("atrasado"):
            ST["atrasado"] = True
            para_admins("⚠️ O coletor do GitHub não publica ofertas há mais de 45 min. "
                        "Confira o cron-job.org e a aba Actions.")
        return
    ST["ult_gerado"] = st["gerado"]
    if ST.get("atrasado"):
        ST["atrasado"] = False
        para_admins("✅ Coletor voltou a publicar ofertas.")
    if not st.get("ok"):
        if not ST["bloqueado"]:
            ST["bloqueado"] = True
            para_admins(f"⚠️ Coletor: falha ao consultar a Amazon ({html.escape(st.get('erro', '?'))}). "
                        "Aviso quando voltar.")
        return
    if ST["bloqueado"]:
        ST["bloqueado"] = False
        para_admins("✅ Amazon voltou a responder.")

    urg = (baixar("urgentes.json") or {}).get("itens", {})
    comuns = {}
    cg = st.get("comuns_gerado")
    if cg and cg != ST.get("ult_comuns") and (hora_de_resumo(datetime.now(TZ)) or not ST["base_feita"]):
        comuns = (baixar("comuns.json") or {}).get("itens", {})
        ST["ult_comuns"] = cg

    novos_urg = [a for a, it in urg.items() if it["pc"] >= URGENTE and ST["vistos"].get(a, 0) < URGENTE]
    novos_com = [a for a, it in comuns.items() if a not in ST["vistos"] and a not in urg
                 and MIN_DESCONTO <= it["pc"] < URGENTE]
    with lock:
        for a, it in {**comuns, **urg}.items():
            ST["vistos"][a] = max(ST["vistos"].get(a, 0), it["pc"])

    if not ST["base_feita"]:
        if comuns:
            ST["base_feita"] = True
            salvar()
            para_admins(f"✅ Bot de ofertas online. Base criada: {len(urg)} ofertas com {URGENTE}%+ "
                        f"e {len(comuns)} entre {MIN_DESCONTO}% e {URGENTE - 1}%. "
                        "A partir de agora aviso só as novas.")
        return

    if novos_urg:
        novos_urg.sort(key=lambda a: -urg[a]["pc"])
        log(f"{len(novos_urg)} urgentes novas")
        for m in em_blocos(f"🔥 <b>{len(novos_urg)} oferta(s) com {URGENTE}%+ agora</b>",
                           [linha(a, urg[a]) for a in novos_urg]):
            para_todos(m)

    if comuns:
        log(f"resumo: {len(novos_com)} novas comuns")
        if novos_com:
            novos_com.sort(key=lambda a: -comuns[a]["pc"])
            limite = RESUMO_MAX or len(novos_com)
            extra = len(novos_com) - limite
            titulo = f"🛒 <b>Resumo: {len(novos_com)} oferta(s) nova(s) de {MIN_DESCONTO}% a {URGENTE - 1}%</b>"
            linhas = [linha(a, comuns[a]) for a in novos_com[:limite]]
            if extra > 0:
                linhas.append(f"<i>+ {extra} outras com desconto menor</i>")
            for m in em_blocos(titulo, linhas):
                para_todos(m, canal=False)
            with lock:
                pend = ST.setdefault("canal_pend", {})
                for a in novos_com:
                    pend[a] = comuns[a]


    # canal: resumo curto (so as melhores) nos horarios de CANAL_HORAS
    agora = datetime.now(TZ)
    chave = agora.strftime("%Y-%m-%d %H")
    pend = ST.get("canal_pend") or {}
    if CANAL and pend and agora.hour in CANAL_HORAS and ST.get("canal_ult") != chave:
        top = sorted(pend, key=lambda a: -pend[a]["pc"])[:CANAL_MAX]
        titulo = f"🛒 <b>Top {len(top)} ofertas de {MIN_DESCONTO}% a {URGENTE - 1}%</b>"
        for m in em_blocos(titulo, [linha(a, pend[a]) for a in top]):
            if not enviar(CANAL, m + AVISO):
                log(f"Canal {CANAL} recusou o resumo")
        with lock:
            ST["canal_pend"] = {}
            ST["canal_ult"] = chave
        salvar()
        log(f"canal: resumo com {len(top)} de {len(pend)} ofertas")


def main():
    threading.Thread(target=ouvir_telegram, daemon=True).start()
    log(f"Bot iniciado: lendo {GITHUB_REPO} a cada {CHECAR_MIN} min, urgente {URGENTE}%+, resumo {RESUMO_HORAS}")
    erro_avisado = False
    while True:
        try:
            ciclo()
            erro_avisado = False
        except Exception as e:
            log(f"Falha no ciclo: {e!r}")
            if not erro_avisado:
                erro_avisado = True
                para_admins(f"⚠️ Bot de ofertas: erro ao ler os dados ({html.escape(str(e)[:200])}).")
        salvar()
        time.sleep(CHECAR_MIN * 60)


if __name__ == "__main__":
    main()
