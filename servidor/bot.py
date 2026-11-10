"""Bot de ofertas da Amazon Brasil (roda 24h no servidor).

- Lê as ofertas publicadas pelo coletor do GitHub (a cada 10 min) e avisa NA HORA as de URGENTE%+.
- Nos horários de RESUMO_HORAS manda um resumo das ofertas novas entre MIN_DESCONTO% e URGENTE%.
- Cobre todas as ofertas da Amazon que aceitam filtro de desconto (todos os departamentos).
- Quem manda /start no bot passa a receber; /stop para sair. Admins recebem avisos de erro e de inscritos.
"""
import html, json, logging, os, re, threading, time
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
AVISO_WA = "\n\n_Links de afiliado: como Associado da Amazon, ganho com compras qualificadas._"
WA_URL = os.getenv("WA_URL", "").strip()            # ponte do WhatsApp, ex.: http://127.0.0.1:3399/send
FOTOS = os.getenv("FOTOS", "1") == "1"              # álbuns com foto no Telegram
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


def foto_url(it):
    return f"https://m.media-amazon.com/images/I/{it['img']}._AC_SL500_.jpg" if it.get("img") else None


def visivel(t):
    return len(html.unescape(re.sub(r"<[^>]+>", "", t)))


def linha_curta(n, asin, it):
    de = f"<s>{brl(it['d'])}</s> → " if it["d"] else ""
    rel = "⚡" if it["rel"] else ""
    return (f"<b>{n}.</b> <b>{it['pc']}%</b>{rel} {de}<b>{brl(it['p'])}</b>\n"
            f"<a href=\"{BASE}/dp/{asin}?tag={AMAZON_TAG}\">{html.escape(it['t'][:55])}</a>")


def legenda_album(titulo, grupo):
    return (titulo + "\n\n" + "\n".join(linha_curta(i + 1, a, it) for i, (a, it) in enumerate(grupo))
            + AVISO)


def albuns(itens, titulo):
    """Agrupa ofertas com foto em álbuns de até 10; legenda numerada na ordem das fotos."""
    grupos, atual = [], []
    for par in itens:
        teste = atual + [par]
        if len(teste) > 10 or visivel(legenda_album(titulo, teste)) > 1010:
            grupos.append(atual)
            atual = [par]
        else:
            atual = teste
    if atual:
        grupos.append(atual)
    return grupos


def enviar_album(chat, grupo, titulo):
    legenda = legenda_album(titulo, grupo)
    if len(grupo) == 1:
        metodo, data = "/sendPhoto", {"chat_id": chat, "photo": foto_url(grupo[0][1]),
                                      "caption": legenda, "parse_mode": "HTML"}
    else:
        media = [{"type": "photo", "media": foto_url(it)} for _, it in grupo]
        media[0].update(caption=legenda, parse_mode="HTML")
        metodo, data = "/sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}
    try:
        r = requests.post(TG + metodo, data=data, timeout=60)
    except requests.RequestException as e:
        log(f"Erro Telegram álbum {chat}: {e}")
        return enviar(chat, legenda)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return enviar_album(chat, grupo, titulo)
    if r.status_code == 403:
        return False
    if not r.ok:  # alguma foto inválida: manda a lista em texto
        log(f"Álbum falhou em {chat} ({r.status_code}): {r.text[:150]}")
        return enviar(chat, legenda)
    time.sleep(1)
    return True


def para_todos_albuns(itens, titulo, canal=True):
    """Ofertas com foto em álbuns (privado + canal); sem foto vão em texto."""
    com = [(a, it) for a, it in itens if it.get("img")]
    sem = [(a, it) for a, it in itens if not it.get("img")]
    with lock:
        destinos = list(dict.fromkeys(ADMINS + ST["inscritos"]))
    if CANAL and canal:
        destinos = [CANAL] + destinos
    for g in albuns(com, titulo):
        for c in destinos:
            if not enviar_album(c, g, titulo):
                if c == CANAL:
                    log(f"Canal {CANAL} recusou o álbum (o bot é admin do canal?)")
                elif c in ST["inscritos"]:
                    with lock:
                        ST["inscritos"].remove(c)
                    log(f"Removido (bloqueou o bot): {c}")
    if sem:
        for m in em_blocos(titulo, [linha(a, it) for a, it in sem]):
            para_todos(m, canal=canal)
    salvar()


# ---------- WhatsApp (ponte local wa.mjs) ----------
# Poucas mensagens: as ofertas vão se juntando e, no máximo a cada WA_INTERVALO_MIN,
# sai UM post com uma colagem das melhores fotos (numeradas) + a lista numerada embaixo.
WA_INTERVALO_MIN = int(os.getenv("WA_INTERVALO_MIN", "30"))
WA_MAX = int(os.getenv("WA_MAX", "9"))  # produtos por post (colagem 3x3)
WA_FOTO_MODO = os.getenv("WA_FOTO_MODO", "previa")  # previa (colagem como prévia do texto) | imagem | texto
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def para_whatsapp(itens, titulo=None):
    """Guarda as ofertas para o próximo post do WhatsApp (não posta na hora)."""
    if not WA_URL:
        return
    with lock:
        pend = ST.setdefault("wa_pend", {})
        for a, it in itens:
            pend[a] = it


def montar_colagem(itens):
    """Colagem com as fotos numeradas (até 3 colunas). Devolve o caminho do JPG ou None."""
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow não instalado: WhatsApp vai só com texto")
        return None
    lado, gap = 420, 12
    cols = min(3, len(itens))
    linhas = (len(itens) + cols - 1) // cols
    W, H = cols * lado + (cols + 1) * gap, linhas * lado + (linhas + 1) * gap
    tela = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(tela)
    try:
        fonte = ImageFont.load_default(size=40)
        fonte_p = ImageFont.load_default(size=34)
    except TypeError:
        fonte = fonte_p = ImageFont.load_default()
    ok = 0
    for i, (asin, it) in enumerate(itens):
        x = gap + (i % cols) * (lado + gap)
        y = gap + (i // cols) * (lado + gap)
        d.rounded_rectangle([x, y, x + lado, y + lado], radius=24, fill=(255, 255, 255))
        try:
            r = requests.get(foto_url(it), timeout=15)
            img = Image.open(BytesIO(r.content)).convert("RGB")
            img.thumbnail((lado - 60, lado - 60))
            tela.paste(img, (x + (lado - img.width) // 2, y + (lado - img.height) // 2))
            ok += 1
        except Exception as e:
            log(f"Foto {asin} falhou na colagem: {e}")
        # número no canto e selo de desconto
        d.ellipse([x + 10, y + 10, x + 70, y + 70], fill=(229, 57, 53))
        d.text((x + 40, y + 40), str(i + 1), fill="white", font=fonte, anchor="mm")
        selo = f"-{it['pc']}%"
        d.rounded_rectangle([x + lado - 135, y + lado - 62, x + lado - 10, y + lado - 10],
                            radius=14, fill=(46, 125, 50))
        d.text((x + lado - 72, y + lado - 36), selo, fill="white", font=fonte_p, anchor="mm")
    if not ok:
        return None
    caminho = os.path.abspath("wa_colagem.jpg")
    tela.save(caminho, "JPEG", quality=85)
    # miniatura leve: vai como "prévia" de uma mensagem de texto (o canal recusa imagem pura)
    mini = tela.copy()
    mini.thumbnail((480, 480))
    mini.save(os.path.abspath("wa_mini.jpg"), "JPEG", quality=70)
    return caminho


def curto(t, n):
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def wa_item(n, asin, it):
    de = f"~{brl(it['d'])}~ ➜ " if it["d"] else ""
    rel = " ⚡ _relâmpago_" if it["rel"] else ""
    return (f"{NUM[n]} *{it['pc']}% OFF*{rel}\n"
            f"{curto(it['t'], 70)}\n"
            f"{de}*{brl(it['p'])}*\n"
            f"🛒 {BASE}/dp/{asin}?tag={AMAZON_TAG}")


def wa_publicar(forcar=False):
    """Se já passou o intervalo, junta as melhores pendentes num único post bonito."""
    if not WA_URL:
        return
    pend = ST.get("wa_pend") or {}
    if not pend:
        return
    if not forcar and time.time() - ST.get("wa_ult", 0) < WA_INTERVALO_MIN * 60:
        return
    top = sorted(pend.items(), key=lambda x: -x[1]["pc"])[:WA_MAX]
    extra = len(pend) - len(top)
    corpo = "\n\n".join(wa_item(i, a, it) for i, (a, it) in enumerate(top))
    texto = (f"🔥 *CAÇA-DESCONTOS • {len(top)} OFERTAS*\n"
             f"━━━━━━━━━━━━━━\n\n{corpo}\n\n━━━━━━━━━━━━━━")
    if extra > 0:
        texto += f"\n➕ _e mais {extra} ofertas com desconto menor_"
    texto += AVISO_WA
    colagem = montar_colagem(top) if FOTOS else None
    if colagem and WA_FOTO_MODO == "imagem":
        payload = {"image": colagem, "caption": texto}
    elif colagem and WA_FOTO_MODO == "previa":
        link = f"{BASE}/dp/{top[0][0]}?tag={AMAZON_TAG}"
        payload = {"text": texto, "thumb": os.path.abspath("wa_mini.jpg"), "link": link,
                   "title": f"🔥 {len(top)} ofertas com até {top[0][1]['pc']}% OFF",
                   "desc": "Caça-Descontos • Amazon"}
    else:
        payload = {"text": texto}
    try:
        requests.post(WA_URL, json=payload, timeout=10)
        log(f"WhatsApp: post com {len(top)} ofertas ({'com' if colagem else 'sem'} colagem)")
    except requests.RequestException as e:
        log(f"Ponte WhatsApp indisponível: {e}")
        return
    with lock:
        ST["wa_pend"] = {}
        ST["wa_ult"] = time.time()
    salvar()


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
        titulo = f"🔥 <b>{len(novos_urg)} oferta(s) com {URGENTE}%+ agora</b>"
        itens = [(a, urg[a]) for a in novos_urg]
        if FOTOS:
            para_todos_albuns(itens, titulo)
        else:
            for m in em_blocos(titulo, [linha(a, it) for a, it in itens]):
                para_todos(m)
        para_whatsapp(itens, f"🔥 {len(novos_urg)} oferta(s) com {URGENTE}%+ agora")

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
    if (CANAL or WA_URL) and pend and agora.hour in CANAL_HORAS and ST.get("canal_ult") != chave:
        top = sorted(pend, key=lambda a: -pend[a]["pc"])[:CANAL_MAX]
        titulo = f"🛒 <b>Top {len(top)} ofertas de {MIN_DESCONTO}% a {URGENTE - 1}%</b>"
        com = [(a, pend[a]) for a in top if pend[a].get("img")]
        if CANAL and FOTOS and com:
            for g in albuns(com, titulo):
                if not enviar_album(CANAL, g, titulo):
                    log(f"Canal {CANAL} recusou o resumo")
        sem = [a for a in top if not (FOTOS and pend[a].get("img"))]
        if CANAL and sem:
            for m in em_blocos(titulo, [linha(a, pend[a]) for a in sem]):
                if not enviar(CANAL, m + AVISO):
                    log(f"Canal {CANAL} recusou o resumo")
        para_whatsapp([(a, pend[a]) for a in top], f"🛒 Top {len(top)} ofertas de {MIN_DESCONTO}% a {URGENTE - 1}%")
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
            wa_publicar()
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
