"""Bot de ofertas da Amazon Brasil (roda 24h no servidor).

- Lê as ofertas publicadas pelo coletor do GitHub (a cada 10 min) e avisa NA HORA as de URGENTE%+.
- Nos horários de RESUMO_HORAS manda um resumo das ofertas novas entre MIN_DESCONTO% e URGENTE%.
- Cobre todas as ofertas da Amazon que aceitam filtro de desconto (todos os departamentos).
- Quem manda /start no bot passa a receber; /stop para sair. Admins recebem avisos de erro e de inscritos.
"""
import base64, csv, html, io, json, logging, math, os, re, shutil, threading, time
from datetime import date, datetime
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
CANAL_HORAS = {int(h) for h in os.getenv("CANAL_HORAS", "8,10,12,14,16,18,20,22").split(",") if h.strip()}
CANAL_MAX = int(os.getenv("CANAL_MAX", "10"))
AVISO = "\n\n<i>Link de afiliado: como Associado da Amazon, ganho com compras qualificadas.</i>"
AVISO_WA = "\n\n_Links de afiliado: como Associado da Amazon, ganho com compras qualificadas._"
WA_URL = os.getenv("WA_URL", "").strip()            # ponte do WhatsApp, ex.: http://127.0.0.1:3399/send
FOTOS = os.getenv("FOTOS", "1") == "1"              # álbuns com foto no Telegram
MAX_MSGS = int(os.getenv("MAX_MSGS", "4"))          # trava: no máx. 4 mensagens por leva no Telegram
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
    rel = ("⚡" if it["rel"] else "") + ("🏆" if it.get("min30") else "") + ("📉" if it.get("baixou") else "")
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
    for g in albuns(com, titulo)[:MAX_MSGS]:
        for c in destinos:
            if not enviar_album(c, g, titulo):
                if c == CANAL:
                    log(f"Canal {CANAL} recusou o álbum (o bot é admin do canal?)")
                elif c in ST["inscritos"]:
                    with lock:
                        ST["inscritos"].remove(c)
                    log(f"Removido (bloqueou o bot): {c}")
    if sem:
        for m in em_blocos(titulo, [linha(a, it) for a, it in sem])[:MAX_MSGS]:
            para_todos(m, canal=canal)
    salvar()


# ---------- WhatsApp (ponte local wa.mjs) ----------
# Poucas mensagens: as ofertas vão se juntando e, no máximo a cada WA_INTERVALO_MIN,
# sai UM post com uma colagem das melhores fotos (numeradas) + a lista numerada embaixo.
WA_INTERVALO_MIN = int(os.getenv("WA_INTERVALO_MIN", "30"))
WA_MAX = int(os.getenv("WA_MAX", "9"))  # produtos por post (colagem 3x3)
WA_POSTS_MAX = int(os.getenv("WA_POSTS_MAX", "4"))  # máx. de posts por rodada (0 = todos); o resto é cancelado
WA_FOTO_MODO = os.getenv("WA_FOTO_MODO", "previa")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "").strip()  # se preenchido, posta pelo Whapi (foto grande no Canal)
WHAPI_PARA = os.getenv("WHAPI_PARA", "").strip()    # ex.: 120363432321331568@newsletter
WHAPI_ESTILO = os.getenv("WHAPI_ESTILO", "unica")  # "unica" (colagem + lista numa mensagem), "individual" ou "colagem"
WA_CANAL_LINK = os.getenv("WA_CANAL_LINK", "").strip()  # link de convite do Canal (vai no rodapé de todo post)
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def para_whatsapp(itens, titulo=None):
    """Guarda as ofertas para o próximo post do WhatsApp (não posta na hora)."""
    if not (WA_URL or WHAPI_TOKEN):
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
    selo = ""
    if it.get("baixou"):
        selo += f"📉 _baixou de novo (era {brl(it['baixou'])})_\n"
    if it.get("min30"):
        selo += "🏆 _menor preço em 30 dias_\n"
    if econ(it) >= 100:
        selo += f"💰 _economia de {brl(econ(it))}_\n"
    if (it.get("mercado") or {}).get("v") == "barato" and (MERCADO_MODO == "filtrar" or econ(it) >= VALOR_ECON):
        selo += "✅ _mais barato que nas outras lojas_\n"
    selo += selos_extra(it)
    return (f"{NUM[n]} *{it['pc']}% OFF*{rel}\n"
            f"{curto(it['t'], 70)}\n"
            f"{de}*{brl(it['p'])}*\n{selo}"
            f"🛒 {BASE}/dp/{asin}?tag={AMAZON_TAG}")


def whapi(tipo, payload):
    try:
        r = requests.post(f"https://gate.whapi.cloud/messages/{tipo}", json=payload, timeout=90,
                          headers={"Authorization": f"Bearer {WHAPI_TOKEN}", "Accept": "application/json"})
    except requests.RequestException as e:
        log(f"Whapi indisponível: {e}")
        stat("erros_wa")
        return False
    if not r.ok:
        log(f"Whapi {tipo} falhou ({r.status_code}): {r.text[:200]}")
        para_admins(f"⚠️ Whapi recusou o post ({r.status_code}): {html.escape(r.text[:150])}")
        stat("erros_wa")
    return r.ok


# ---------- comparação com outras lojas (DeepSeek: busca na web + IA) ----------
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()
MERCADO_MODO = os.getenv("MERCADO_MODO", "observar")   # "observar" (só anota), "filtrar" (barra e põe selo) ou "desligado"
DS = "https://api.deepseek.com"
ICONE_MERCADO = {"barato": "✅", "caro": "❌", "inflado": "🎈", "parecido": "≈", "incerto": "❓", "sem_dados": "❓", "erro": "⚠️"}


def _ds(caminho, payload):
    r = requests.post(DS + caminho, json=payload, timeout=90,
                      headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"})
    if r.status_code == 402 and not ST.get("ds_sem_saldo"):
        ST["ds_sem_saldo"] = True
        para_admins("⚠️ DeepSeek sem saldo: a comparação com outras lojas parou até você recarregar.")
    r.raise_for_status()
    ST["ds_sem_saldo"] = False
    return r.json()


def _num(x):
    try:
        v = float(str(x).replace("R$", "").replace(".", "").replace(",", ".")) if isinstance(x, str) else float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


LOJAS_FORA = ("amazon.", "aliexpress", "temu.", "shein.", "ebay.", "alibaba", "walmart.com", "bestbuy.",
              "wish.com", "banggood", "dhgate")


def loja_brasileira(url):
    """Só lojas/sites do Brasil: domínio .br ou páginas /br/ e /pt-br/ de marcas (ex.: samsung.com/br)."""
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", url.lower())
    if not m:
        return False
    host, caminho = m.group(1), (m.group(2) or "/")
    if any(x in host for x in LOJAS_FORA):
        return False
    return host.endswith(".br") or caminho.startswith(("/br/", "/pt-br/", "/pt_br/"))


def mercado_checar(asin, it):
    """Busca o produto em outras lojas e pede à IA o veredito. Resultado fica 3 dias em cache."""
    p = float(it["p"])
    with lock:
        c = (ST.get("mercado") or {}).get(asin)
    if c and c.get("v") != "erro" and time.time() - c["ts"] < 3 * 86400 and abs(c["p"] - p) <= p * 0.02:
        return c
    c = {"ts": time.time(), "p": p, "t": it["t"][:70], "pc": it["pc"]}
    try:
        q = re.sub(r"[^\w\s\-./,]", " ", it["t"])
        q = re.sub(r"\s+", " ", curto(q, 100)).strip() + " preço Brasil"
        res = [x for x in _ds("/v1/tools/search", {"query": q[:128], "count": 15}).get("results", [])
               if loja_brasileira(x.get("url") or "")]
        stat("mercado_buscas")
        if not res:
            c["v"] = "sem_dados"
        else:
            fontes = "\n".join(
                f"- [{x.get('site_name') or ''}] {(x.get('title') or '')[:120]} | "
                f"{(x.get('summary') or x.get('snippet') or '')[:500]}" for x in res[:8])
            d = it.get("d")
            prompt = (f'Produto na Amazon: "{it["t"]}"\nPreço atual na Amazon: R$ {p:.2f}\n'
                      f'Preço "de" anunciado pela Amazon: {f"R$ {float(d):.2f}" if d else "não informado"}\n\n'
                      f"Resultados de busca em outras lojas:\n{fontes}\n\n"
                      "Responda em JSON com as chaves:\n"
                      '"mesmo_produto": true só se algum resultado for claramente o MESMO produto '
                      "(mesmo modelo, capacidade, cor não importa, mesma quantidade);\n"
                      '"menor_preco": menor preço à vista em R$ do mesmo produto em outra loja (número) ou null;\n'
                      '"loja": nome dessa loja ou null;\n'
                      '"precos": lista de até 4 lojas brasileiras com o MESMO produto, no formato '
                      '[{"loja": "Magazine Luiza", "preco": 2399.90}], do mais barato para o mais caro;\n'
                      '"preco_tipico": preço normal desse produto no mercado em R$ (número) ou null;\n'
                      '"confianca": "alta", "media" ou "baixa".\n'
                      "Use apenas preços em reais (R$) de lojas brasileiras que aparecem nos resultados. "
                      "Ignore preços em dólar, euro ou de sites internacionais e de importação, "
                      "porque com impostos o produto chega ao Brasil custando muito mais. "
                      "Ignore também preços de produtos usados, recondicionados ou de outro modelo. Não invente.")
            out = _ds("/chat/completions", {
                "model": "deepseek-chat", "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Você compara preços no varejo brasileiro. Responda só JSON."},
                             {"role": "user", "content": prompt}]})
            j = json.loads(out["choices"][0]["message"]["content"])
            mn, tip = _num(j.get("menor_preco")), _num(j.get("preco_tipico"))
            de = _num(d) if d else None
            c.update(mn=mn, tip=tip, loja=(j.get("loja") or "")[:30])
            lista = []
            for x in (j.get("precos") or [])[:4]:
                if isinstance(x, dict) and _num(x.get("preco")) and x.get("loja"):
                    lista.append([str(x["loja"])[:25], _num(x["preco"])])
            if lista:
                c["precos"] = sorted(lista, key=lambda z: z[1])
            if not j.get("mesmo_produto") or j.get("confianca") == "baixa" or not (mn or tip):
                c["v"] = "incerto"
            elif mn and mn < p * 0.9:
                c["v"] = "caro"            # outra loja pelo menos 10% mais barata
            elif de and tip and de > tip * 1.5 and p > tip * 0.85:
                c["v"] = "inflado"         # "de" bem acima do mercado e o preço atual quase igual ao normal
            elif mn and mn >= p * 0.98:
                c["v"] = "barato"
            else:
                c["v"] = "parecido"
    except Exception as e:
        log(f"Comparação {asin} falhou: {e!r}")
        c["v"] = "erro"
    stat("mercado_" + c["v"])
    with lock:
        ST.setdefault("mercado", {})[asin] = c
        lg = ST.setdefault("mercado_log", [])
        lg.append([asin, c])
        del lg[:-30]
    return c


def mercado_lote(itens):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (a, it), c in zip(itens, ex.map(lambda x: mercado_checar(*x), itens)):
            it["mercado"] = c


def texto_mercado():
    lg = (ST.get("mercado_log") or [])[-12:]
    if not lg:
        return "Ainda não comparei nenhuma oferta com outras lojas."
    br = lambda v: brl(v) if v else "?"
    linhas = [f"{ICONE_MERCADO.get(c['v'], '?')} <b>{c['pc']}%</b> {html.escape(c['t'])}\n"
              f"   Amazon {brl(c['p'])} · fora {br(c.get('mn'))}{' (' + html.escape(c['loja']) + ')' if c.get('loja') else ''}"
              f" · normal {br(c.get('tip'))} · <a href=\"{BASE}/dp/{a}\">ver</a>" for a, c in reversed(lg)]
    return (f"🔍 <b>Últimas comparações</b> (modo: {MERCADO_MODO})\n"
            "✅ Amazon mais barata · ❌ outra loja mais barata · 🎈 preço \"de\" inflado · ≈ parecido · ❓ sem comparação\n\n"
            + "\n\n".join(linhas))


def wa_publicar(forcar=False):
    """Se já passou o intervalo, posta TODAS as pendentes, em posts de até WA_MAX ofertas."""
    if not (WA_URL or WHAPI_TOKEN):
        return
    pend = ST.get("wa_pend") or {}
    if not pend:
        return
    intervalo = WA_INTERVALO_BF if bf_fase()[0] == "semana" else WA_INTERVALO_MIN
    if not forcar and time.time() - ST.get("wa_ult", 0) < intervalo * 60:
        return
    todas = sorted(pend.items(), key=lambda x: -pontos(x[1]))
    if DEEPSEEK_KEY and MERCADO_MODO in ("observar", "filtrar"):
        n = WA_MAX * (WA_POSTS_MAX or 4)
        mercado_lote(todas[:n + (n // 2 if MERCADO_MODO == "filtrar" else 0)])
        if MERCADO_MODO == "filtrar":
            antes = len(todas)
            todas = [(a, it) for a, it in todas if (it.get("mercado") or {}).get("v") not in ("caro", "inflado")]
            if antes - len(todas):
                log(f"Outras lojas: {antes - len(todas)} oferta(s) barradas")
        todas.sort(key=lambda x: -pontos(x[1]))
    if DEEPSEEK_KEY and MERCADO_MODO != "desligado":
        # descontos grandes (economia >= VALOR_ECON) SEMPRE passam pela DeepSeek, em qualquer modo
        grandes = [(a, it) for a, it in todas if econ(it) >= VALOR_ECON][:30]
        mercado_lote(grandes)
        aprovadas = []
        for a, it in todas:
            v = (it.get("mercado") or {}).get("v")
            if econ(it) >= VALOR_ECON and v in ("caro", "inflado"):
                stat("valor_barrado")
                log(f"Desconto grande barrado pela DeepSeek ({v}): {it['t'][:50]}")
                continue
            if it.get("valor") and v not in ("barato", "parecido"):
                stat("valor_barrado")  # 40-49% só vai na hora se a DeepSeek confirmar o preço
                log(f"40-49% sem confirmação da DeepSeek ({v}), não enviado: {it['t'][:50]}")
                continue
            aprovadas.append((a, it))
        todas = aprovadas
        if WA_DESTAQUES and todas:
            dest = [(a, it) for a, it in todas if econ(it) >= VALOR_ECON
                    and (it.get("mercado") or {}).get("v") == "barato" and (it.get("mercado") or {}).get("mn")]
            dest = sorted(dest, key=lambda x: -pontos(x[1]))[:WA_DESTAQUES]
            for a, it in dest:
                if wa_destaque(a, it):
                    todas = [(b, x) for b, x in todas if b != a]
                    stat("destaques")
                    time.sleep(5)
    partes = [todas[k:k + WA_MAX] for k in range(0, len(todas), WA_MAX)]
    if WA_POSTS_MAX:
        partes = partes[:WA_POSTS_MAX]
    with lock:  # tira tudo da fila já: o que não couber nos posts fica cancelado
        ST["wa_pend"] = {}
        ST["wa_ult"] = time.time()
    salvar()
    postadas = 0
    for n, top in enumerate(partes):
        if n:
            time.sleep(8)  # respiro entre um post e outro
        rotulo = f" ({n + 1}/{len(partes)})" if len(partes) > 1 else ""
        extra = len(todas) - postadas - len(top) if n == len(partes) - 1 else 0
        if not wa_post(top, rotulo, extra, ultimo=(n == len(partes) - 1)):
            break
        postadas += len(top)
    with lock:
        dia = datetime.now(TZ).strftime("%Y-%m-%d")
        h = ST.get("hoje") or {}
        if h.get("d") != dia:
            h = {"d": dia, "itens": {}}
        h["itens"].update(dict(todas[:postadas]))
        ST["hoje"] = h
    registrar_postados(todas[:postadas])
    with lock:
        ST["wa_ult_top"] = dict(todas[:postadas] or todas[:WA_MAX])
        ST["wa_pend"] = {}
        ST["wa_ult"] = time.time()
    salvar()
    log(f"WhatsApp: {postadas} de {len(todas)} ofertas em {len(partes)} post(s)")
    if postadas:
        stat("posts_wa", -(-postadas // WA_MAX))
        stat("ofertas_wa", postadas)
        ST["wa_ult_post"] = time.time()
        ST["wa_alerta_parado"] = False


WA_DESTAQUES = int(os.getenv("WA_DESTAQUES", "1"))  # posts de destaque (com prova de preço) por rodada


def texto_destaque(asin, it):
    c = it.get("mercado") or {}
    de = f"~{brl(it['d'])}~ ➜ " if it["d"] else ""
    precos = c.get("precos") or ([[c.get("loja") or "outra loja", c["mn"]]] if c.get("mn") else [])
    linhas = "\n".join(f"• {loja}: {brl(p)}" for loja, p in precos)
    quando = datetime.fromtimestamp(c.get("ts", time.time()), TZ).strftime("%d/%m às %H:%M")
    rel = " ⚡ _relâmpago_" if it.get("rel") else ""
    if c.get("v") == "barato" and linhas:
        prova = (f"🔎 *Comparamos em lojas brasileiras:*\n{linhas}\n"
                 f"✅ *Na Amazon está mais barato*\n"
                 f"_(preços pesquisados em {quando}; podem mudar)_\n\n")
    else:
        prova = f"_🕒 Preço de {datetime.now(TZ).strftime('%d/%m às %H:%M')}; pode mudar._\n\n"
    selo = selos_extra(it)
    txt = (f"⭐ *DESTAQUE DO MOMENTO*{rel}\n━━━━━━━━━━━━━━\n\n"
           f"*{curto(it['t'], 90)}*\n\n"
           f"{de}*{brl(it['p'])}*  ({it['pc']}% OFF)\n"
           f"💰 Economia de *{brl(econ(it))}*\n{selo}\n"
           f"{prova}"
           f"🛒 {BASE}/dp/{asin}?tag={AMAZON_TAG}")
    if WA_CANAL_LINK:
        txt += f"\n\n📲 *Gostou? Receba ofertas assim todo dia:*\n👉 Entre no canal Caça-Descontos: {WA_CANAL_LINK}"
    return txt + AVISO_WA


def postar_manual(chat, pedido):
    """/postar ASIN  ou  /postar palavras do título: posta a melhor oferta encontrada como destaque."""
    try:
        todos = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
        m = re.search(r"\b([A-Z0-9]{10})\b", pedido) or re.search(r"/dp/([A-Z0-9]{10})", pedido)
        if m and m.group(1) in todos:
            cand = [(m.group(1), todos[m.group(1)])]
        else:
            palavras = [w for w in pedido.lower().split() if w]
            if not palavras:
                enviar(chat, "Use: /postar ASIN  ou  /postar palavras do produto (ex.: /postar polo aeropostale masculina)")
                return
            cand = sorted([(a, it) for a, it in todos.items() if all(w in it["t"].lower() for w in palavras)],
                          key=lambda x: (-x[1]["pc"], -(x[1].get("na") or 0)))
        if not cand:
            enviar(chat, "Não achei essa oferta nos dados de agora (precisa estar com 35%+ de desconto).")
            return
        a, it = cand[0]
        if DEEPSEEK_KEY:
            mercado_lote([(a, it)])
        ok = wa_destaque(a, it)
        v = (it.get("mercado") or {}).get("v", "sem comparação")
        enviar(chat, (f"✅ Postado no canal: {html.escape(it['t'][:80])} ({it['pc']}% OFF, {brl(it['p'])}). "
                      f"Comparação com outras lojas: {v}.") if ok else "⚠️ O Whapi não aceitou o post.")
        if ok:
            with lock:
                ST.setdefault("alertas", {})[a] = [float(it["p"]), time.time()]
            salvar()
    except Exception as e:
        log(f"postar manual: {e!r}")
        enviar(chat, f"Falhou: {html.escape(str(e)[:200])}")


def wa_destaque(asin, it):
    """Post só do produto: foto grande + preço + comparação com outras lojas."""
    if not (WHAPI_TOKEN and WHAPI_PARA):
        return False
    txt = texto_destaque(asin, it)
    ok = it.get("img") and whapi("image", {"to": WHAPI_PARA, "media": foto_url(it), "caption": txt})
    if not ok:
        ok = whapi("text", {"to": WHAPI_PARA, "body": txt, "typing_time": 0})
    log(f"WhatsApp: destaque {asin} ok={ok}")
    if ok:
        with lock:
            h = ST.get("hoje") or {}
            dia = datetime.now(TZ).strftime("%Y-%m-%d")
            if h.get("d") != dia:
                h = {"d": dia, "itens": {}}
            h["itens"][asin] = it
            ST["hoje"] = h
        registrar_postados([(asin, it)])
        stat("posts_wa")
        stat("ofertas_wa")
        ST["wa_ult_post"] = time.time()
    return bool(ok)


def wa_para_html(t):
    """Converte a formatação do WhatsApp (*negrito* _itálico_ ~riscado~) para HTML do Telegram."""
    t = html.escape(t, quote=False)
    t = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w/])_([^_\n]+)_(?![\w/])", r"<i>\1</i>", t)
    return re.sub(r"~([^~\n]+)~", r"<s>\1</s>", t)


def teste_destaque(chat):
    """Mostra no Telegram (só para o admin) como ficaria um destaque com uma oferta real de agora."""
    try:
        todos = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
        cand = sorted([(a, it) for a, it in todos.items() if econ(it) >= VALOR_ECON and not motivo_fora(it)],
                      key=lambda x: -pontos(x[1]))[:6]
        if not cand:
            enviar(chat, "Não achei agora nenhuma oferta com economia grande para o teste.")
            return
        if DEEPSEEK_KEY:
            mercado_lote(cand)
        esc = next(((a, it) for a, it in cand if (it.get("mercado") or {}).get("v") == "barato"), cand[0])
        a, it = esc
        v = (it.get("mercado") or {}).get("v", "sem DeepSeek")
        aviso = ("" if v == "barato" else
                 f"\n\n⚠️ <i>Teste: nenhuma das {len(cand)} melhores foi confirmada como mais barata "
                 f"(esta ficou como “{v}”), então não iria para o canal.</i>")
        legenda = "🧪 <b>TESTE — assim fica o destaque no WhatsApp:</b>\n\n" + wa_para_html(texto_destaque(a, it)) + aviso
        if it.get("img") and visivel(legenda) <= 1024:
            r = requests.post(TG + "/sendPhoto", data={"chat_id": chat, "photo": foto_url(it),
                                                       "caption": legenda, "parse_mode": "HTML"}, timeout=60)
            if r.ok:
                return
        enviar(chat, legenda)
    except Exception as e:
        log(f"teste destaque: {e!r}")
        enviar(chat, f"Teste do destaque falhou: {html.escape(str(e)[:200])}")


def wa_post(top, rotulo="", extra=0, ultimo=True, titulo=None, sub=""):
    """Um post do WhatsApp com até WA_MAX ofertas. Devolve True se saiu."""
    aviso = AVISO_WA if ultimo else ""
    if WA_CANAL_LINK:  # rodapé em TODO post: quem receber encaminhado consegue entrar no canal
        aviso = f"\n\n📲 *Gostou? Receba ofertas assim todo dia:*\n👉 Entre no canal Caça-Descontos: {WA_CANAL_LINK}" + aviso
    corpo = "\n\n".join(wa_item(i, a, it) for i, (a, it) in enumerate(top))
    fase, faltam = bf_fase()
    if not titulo and fase == "semana":
        titulo = f"🖤 *BLACK FRIDAY • {len(top)} OFERTAS*"
    elif not titulo and fase == "esquenta" and not sub:
        sub = f"⏳ Faltam {faltam} dia{'s' if faltam > 1 else ''} para a Black Friday"
    cab = (titulo or f"🔥 *CAÇA-DESCONTOS • {len(top)} OFERTAS*") + rotulo + (f"\n_{sub}_" if sub else "")
    texto = (f"{cab}\n"
             f"━━━━━━━━━━━━━━\n\n{corpo}\n\n━━━━━━━━━━━━━━")
    if extra > 0:
        texto += f"\n➕ _e mais {extra} ofertas com desconto menor_"
    texto += f"\n🕒 _Preços de {datetime.now(TZ).strftime('%d/%m às %H:%M')}; podem mudar._"
    texto += aviso
    if WHAPI_TOKEN and WHAPI_PARA and WHAPI_ESTILO == "individual":
        # cabeçalho, depois cada oferta = foto grande com o texto embaixo
        whapi("text", {"to": WHAPI_PARA, "typing_time": 0,
                       "body": f"{cab}\n━━━━━━━━━━━━━━\n"
                               f"Até *{top[0][1]['pc']}% OFF* na Amazon 👇"})
        oks = 0
        for i, (a, it) in enumerate(top):
            time.sleep(4)
            leg = wa_item(i, a, it) + (aviso if i == len(top) - 1 else "")
            ok = it.get("img") and whapi("image", {"to": WHAPI_PARA, "media": foto_url(it), "caption": leg})
            if not ok:
                ok = whapi("text", {"to": WHAPI_PARA, "body": leg, "typing_time": 0})
            oks += bool(ok)
        log(f"WhatsApp (Whapi individual){rotulo}: {oks}/{len(top)} ofertas")
        return oks > 0
    colagem = montar_colagem(top) if FOTOS else None
    if WHAPI_TOKEN and WHAPI_PARA:
        b64 = None
        if colagem:
            with open(colagem, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        if b64 and WHAPI_ESTILO == "unica":
            # UMA mensagem: colagem grande em cima, lista numerada com links como legenda
            ok = whapi("image", {"to": WHAPI_PARA, "caption": texto,
                                 "media": f"data:image/jpeg;name=ofertas.jpg;base64,{b64}"})
            if not ok:  # legenda recusada: manda a lista em texto logo depois
                ok = whapi("text", {"to": WHAPI_PARA, "body": texto, "typing_time": 0})
            log(f"WhatsApp (Whapi única){rotulo}: {len(top)} ofertas ok={ok}")
            return ok
        # 1) foto grande da colagem  2) logo depois, a lista com os links
        if b64:
            whapi("image", {"to": WHAPI_PARA, "media": f"data:image/jpeg;name=ofertas.jpg;base64,{b64}",
                            "caption": f"🔥 *{len(top)} ofertas com até {top[0][1]['pc']}% OFF*{rotulo} 👇"})
            time.sleep(3)
        ok = whapi("text", {"to": WHAPI_PARA, "body": texto, "typing_time": 0})
        log(f"WhatsApp (Whapi){rotulo}: {len(top)} ofertas ({'com' if b64 else 'sem'} foto) ok={ok}")
        return ok
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
        log(f"WhatsApp (ponte){rotulo}: {len(top)} ofertas ({'com' if colagem else 'sem'} colagem)")
        return True
    except requests.RequestException as e:
        log(f"Ponte WhatsApp indisponível: {e}")
        return False


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
            if m.get("document") and chat in ADMINS:
                threading.Thread(target=aprender_relatorio, args=(chat, m["document"]), daemon=True).start()
                enviar(chat, "📥 Recebi o relatório. Analisando as vendas…")
                continue
            if txt.startswith("/aprendizado") and chat in ADMINS:
                enviar(chat, texto_aprendizado())
                continue
            if txt.startswith("/blackfriday") and chat in ADMINS:
                threading.Thread(target=bf_posts, kwargs={"forcar": True}, daemon=True).start()
                enviar(chat, "🧪 Postando o post de Black Friday de hoje no WhatsApp…")
                continue
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
            elif txt.startswith("/testewa") and chat in ADMINS:
                ult = ST.get("wa_ult_top") or {}
                if not ult:  # ainda sem post: usa as melhores ofertas atuais
                    try:
                        urg = (baixar("urgentes.json") or {}).get("itens", {})
                        ult = dict(sorted(urg.items(), key=lambda x: -x[1]["pc"])[:WA_MAX])
                    except Exception as e:
                        log(f"testewa: {e}")
                if not ult:
                    enviar(chat, "Não achei ofertas para o teste agora.")
                else:
                    with lock:
                        ST.setdefault("wa_pend", {}).update(ult)
                    threading.Thread(target=wa_publicar, kwargs={"forcar": True}, daemon=True).start()
                    enviar(chat, "🧪 Repostando o último post no WhatsApp…")
            elif txt.startswith(("/testetop", "/testeachados")) and chat in ADMINS:
                qual = "top" if txt.startswith("/testetop") else "achados"
                threading.Thread(target=wa_tematicos, kwargs={"forcar": qual}, daemon=True).start()
                enviar(chat, f"🧪 Postando o {'Top 5 do dia' if qual == 'top' else 'Achadinhos'} no WhatsApp…")
            elif txt.startswith(("/bloquear", "/desbloquear")) and chat in ADMINS:
                des = txt.startswith("/desbloquear")
                pedido = (m.get("text") or "").split(None, 1)[1] if len((m.get("text") or "").split(None, 1)) > 1 else ""
                threading.Thread(target=bloquear, args=(chat, pedido, des), daemon=True).start()
            elif txt.startswith("/postar") and chat in ADMINS:
                pedido = (m.get("text") or "")[7:].strip()
                threading.Thread(target=postar_manual, args=(chat, pedido), daemon=True).start()
                enviar(chat, "🧪 Procurando a oferta para postar como destaque…")
            elif txt.startswith("/testedestaque") and chat in ADMINS:
                threading.Thread(target=teste_destaque, args=(chat,), daemon=True).start()
                enviar(chat, "🧪 Montando um destaque de teste com uma oferta real (leva até 1 min)…")
            elif txt.startswith("/mercado") and chat in ADMINS:
                enviar(chat, texto_mercado())
            elif txt.startswith("/relatorio") and chat in ADMINS:
                enviar(chat, texto_relatorio())
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
    rel = (" ⚡" if it["rel"] else "") + (" 🏆 menor preço em 30 dias" if it.get("min30") else "") \
        + (f" 📉 baixou de novo (era {brl(it['baixou'])})" if it.get("baixou") else "")
    return (f"<b>{it['pc']}%</b>{rel} · {de}<b>{brl(it['p'])}</b>\n"
            f"<a href=\"{BASE}/dp/{asin}?tag={AMAZON_TAG}\">{html.escape(it['t'][:100])}</a>")


# ---------- filtros de lixo e pontuação ----------
MIN_AVAL = int(os.getenv("MIN_AVAL", "20"))                # mínimo de avaliações para qualquer produto
MIN_AVAL_LIVRO = int(os.getenv("MIN_AVAL_LIVRO", "100"))   # livros precisam de mais
MIN_NOTA = float(os.getenv("MIN_NOTA", "4.0"))             # nota mínima (estrelas)
MIN_PRECO = float(os.getenv("MIN_PRECO", "15"))            # nada abaixo disso
VALOR_ECON = float(os.getenv("VALOR_ECON", "150"))         # 40-49% com economia >= isso vai na hora pro WhatsApp
MIN_AVAL_MARCA = int(os.getenv("MIN_AVAL_MARCA", "30"))    # marcas conhecidas precisam de menos avaliações
CAMPEAO_AVAL = int(os.getenv("CAMPEAO_AVAL", "1000"))      # campeões de venda: com tantas avaliações...
CAMPEAO_PRECO = float(os.getenv("CAMPEAO_PRECO", "15"))     # ...podem custar a partir disso (abaixo de MIN_PRECO)
PALAVRAS_FORA = [w.strip().lower() for w in os.getenv(
    "PALAVRAS_FORA", "capinha,capa para celular,capa celular,capa iphone,capa galaxy,película,pelicula,"
    "case para celular,adesivo,chaveiro,refil genérico").split(",") if w.strip()]


# tipos de produto (categoria fina da Amazon) que nunca vão para o canal; dá para somar pelo Telegram com /bloquear
TIPOS_FORA = [t.strip().upper() for t in os.getenv("TIPOS_FORA", (
    "BALLOON,PARTY_SUPPLIES,BACKDROP,CELLULAR_PHONE_CASE,PORTABLE_ELECTRONIC_DEVICE_COVER,SCREEN_PROTECTOR,"
    "TOILET_SEAT,SHOWERHEAD,SHOWER_ENCLOSURE,PLUMBING_FIXTURE,BUILDING_MATERIAL,CADDY,STORAGE_BOX,"
    "ITEM_CONTAINER,STORAGE_RACK,KEYCHAIN,STICKER_DECAL,CAKE_TOPPER")).split(",") if t.strip()]


def bloquear(chat, pedido, desfazer=False):
    """/bloquear palavras -> bloqueia o TIPO de produto dessas ofertas (ex.: assento sanitário -> TOILET_SEAT)."""
    pedido = pedido.strip().lower()
    if not pedido:
        tipos, pal = ST.get("tipos_fora") or [], ST.get("palavras_fora") or []
        enviar(chat, "🚫 <b>Bloqueados pelo Telegram</b>\nTipos: " + (", ".join(tipos) or "nenhum")
               + "\nPalavras: " + (", ".join(pal) or "nenhuma")
               + "\n\nUse /bloquear nome do produto  ·  /desbloquear nome ou TIPO")
        return
    todos = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
    with lock:
        tipos = ST.setdefault("tipos_fora", [])
        pal = ST.setdefault("palavras_fora", [])
        if desfazer:
            alvo = pedido.upper()
            if alvo in tipos:
                tipos.remove(alvo)
            elif pedido in pal:
                pal.remove(pedido)
            else:
                ach = [it.get("tp") for it in todos.values() if pedido in it["t"].lower() and it.get("tp") in tipos]
                if not ach:
                    enviar(chat, "Não achei esse bloqueio. Mande /bloquear para ver a lista.")
                    return
                tipos.remove(max(set(ach), key=ach.count))
            salvar()
            enviar(chat, "✅ Desbloqueado.")
            return
        palavras = pedido.split()
        achados = [it for it in todos.values() if all(w in it["t"].lower() for w in palavras) and it.get("tp")]
        if achados:
            cont = {}
            for it in achados:
                cont[it["tp"]] = cont.get(it["tp"], 0) + 1
            tp = max(cont, key=cont.get)
            if tp not in tipos:
                tipos.append(tp)
            ex = next(it["t"] for it in achados if it["tp"] == tp)
            n = sum(1 for it in todos.values() if it.get("tp") == tp)
            msg = (f"🚫 Bloqueei o tipo <b>{tp}</b> (ex.: {html.escape(ex[:60])}). "
                   f"São {n} ofertas a menos agora. Para desfazer: /desbloquear {tp}")
        else:
            if pedido not in pal:
                pal.append(pedido)
            msg = f"🚫 Não achei esse produto agora, então bloqueei a expressão “{html.escape(pedido)}” nos títulos."
        for k in ("wa_pend", "canal_pend"):
            d = ST.get(k) or {}
            for a in [a for a, it in d.items() if motivo_fora(it) == "palavra"]:
                d.pop(a)
    salvar()
    enviar(chat, msg)


# capas de celular em qualquer formato: "Capa para iPhone", "Capa Sanrio para iPhone 16", "Case Capinha Galaxy"...
CAPA_CELULAR = re.compile(r"\b(capas?|capinhas?|case|cases|bumper)\b.{0,60}\b(iphone|galaxy|samsung|motorola|moto g|"
                          r"xiaomi|redmi|poco|celular|smartphone|magsafe)\b|\bprotetor de lente\b")


MARCAS = [m.strip().lower() for m in os.getenv("MARCAS", (
    "aeropostale,nike,adidas,puma,mizuno,asics,olympikus,new balance,under armour,levi's,levis,lacoste,"
    "tommy hilfiger,calvin klein,ralph lauren,polo ralph lauren,hering,colcci,oakley,ray-ban,havaianas,"
    "crocs,vans,converse,timberland,samsonite,apple,samsung,xiaomi,motorola,lg,sony,jbl,philips,bose,"
    "electrolux,brastemp,consul,mondial,britânia,britania,oster,arno,walita,cadence,tramontina,stanley,"
    "owala,bosch,makita,dewalt,black+decker,black & decker,vonder,kärcher,karcher,wap,lego,mattel,hasbro,"
    "barbie,hot wheels,logitech,redragon,hyperx,razer,kingston,sandisk,intelbras,dell,lenovo,asus,acer,hp,"
    "canon,epson,l'oréal,loreal,wella,nivea,natura,boticário,boticario,dove,gillette,oral-b,colgate,pampers,"
    "huggies,kindle,echo,fire tv,alexa,garmin,amazfit,anker,baseus,ugreen,multilaser,positivo,nespresso,"
    "dolce gusto,tefal,rochedo,brinox,le creuset,kitchenaid,weber,coleman,nautika,mormaii,speedo")).split(",")
    if m.strip()]
MARCA_BONUS = float(os.getenv("MARCA_BONUS", "10"))
MARCA_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(m) for m in sorted(MARCAS, key=len, reverse=True))
                      + r")(?![\w])") if MARCAS else None


def marca(it):
    m = MARCA_RE.search(it["t"].lower()) if MARCA_RE else None
    return m.group(1) if m else None


COMPATIVEL = re.compile(r"compat[ií]vel|\bpara (o |a |os |as )?(iphone|ipad|apple|galaxy|samsung|xiaomi|redmi|motorola|"
                        r"kindle|echo|alexa|fire tv|nespresso|dolce gusto|lego|jbl|sony|lg)\b")


def marca_forte(it):
    """Marca conhecida que é o fabricante (no começo do título), não acessório 'para iPhone'/'compatível com'."""
    t = it["t"].lower()
    return bool(MARCA_RE and MARCA_RE.search(t[:45]) and not COMPATIVEL.search(t))


def econ(it):
    try:
        return max(0.0, float(it["d"] or 0) - float(it["p"]))
    except (TypeError, ValueError):
        return 0.0


def motivo_fora(it):
    """Devolve o motivo para descartar a oferta, ou None se ela presta."""
    if "na" not in it:          # dado antigo, sem avaliações: deixa passar
        return None
    titulo = it["t"].lower()
    if any(w in titulo for w in PALAVRAS_FORA + (ST.get("palavras_fora") or [])) or CAPA_CELULAR.search(titulo):
        return "palavra"
    if it.get("tp") and (it["tp"] in TIPOS_FORA or it["tp"] in (ST.get("tipos_fora") or [])):
        return "palavra"
    livro = "book" in (it.get("cat") or "") or "BOOK" in (it.get("tp") or "")
    if float(it["p"]) < MIN_PRECO and not (it["na"] >= CAMPEAO_AVAL and float(it["p"]) >= CAMPEAO_PRECO and not livro):
        return "barato"
    minimo = MIN_AVAL_LIVRO if livro else (min(MIN_AVAL, MIN_AVAL_MARCA) if marca_forte(it) else MIN_AVAL)
    if it["na"] < minimo:
        return "livro" if livro else "poucas_aval"
    if it.get("nota") and it["nota"] < MIN_NOTA:
        return "nota"
    return None


def pontos(it):
    """Nota da oferta: desconto + economia em R$ + prova social + selos."""
    s = it["pc"] + min(20, econ(it) / 15)            # R$ 300 de economia vale +20 pontos
    s += min(8, 2 * math.log10(1 + (it.get("na") or 0)))
    s += 5 if it.get("min30") else 0
    s += 3 if it.get("rel") else 0
    s += MARCA_BONUS if marca(it) else 0
    s += min(6, (it.get("vend") or 0) / 12)          # oferta que já está saindo (% comprado)
    s += 3 if "black friday" in (it.get("bdg") or "").lower() else 0
    s += (ST.get("aprendizado") or {}).get(it.get("cat") or "", 0)
    if MERCADO_MODO == "filtrar" and (it.get("mercado") or {}).get("v") in ("caro", "inflado"):
        s -= 15
    return s


# ---------- qualidade: histórico de preços, sem repetição, sem "maquiagem" ----------
PRECOS_FILE = os.getenv("PRECOS_FILE", "precos.json")
REPETIR_DIAS = int(os.getenv("REPETIR_DIAS", "7"))       # não repete o produto antes disso...
QUEDA_MIN = float(os.getenv("QUEDA_MIN", "5"))            # ...a não ser que o preço caia essa % ou mais
HIST_DIAS_MIN = int(os.getenv("HIST_DIAS_MIN", "7"))      # dias de histórico p/ o selo "menor preço"


def _carregar_precos():
    try:
        with open(PRECOS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


PRECOS = _carregar_precos()  # asin -> {"i": 1º dia visto, "h": {dia: menor preço do dia}}


def hoje():
    return (datetime.now(TZ).date() - datetime(1970, 1, 1).date()).days


def registrar_precos(itens):
    dia = hoje()
    d = str(dia)
    for a, it in itens.items():
        r = PRECOS.setdefault(a, {"i": dia, "h": {}})
        p = float(it["p"])
        if d not in r["h"] or p < r["h"][d]:
            r["h"][d] = p


def salvar_precos():
    corte = hoje() - 31
    for a in list(PRECOS):
        h = {k: v for k, v in PRECOS[a]["h"].items() if int(k) >= corte}
        if h:
            PRECOS[a]["h"] = h
        else:
            del PRECOS[a]
    al = ST.get("alertas") or {}
    for a in [a for a, (p, t) in al.items() if time.time() - t > 60 * 86400]:
        del al[a]
    mc = ST.get("mercado") or {}
    for a in [a for a, c in mc.items() if time.time() - c["ts"] > 7 * 86400]:
        del mc[a]
    tmp = PRECOS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(PRECOS, f, separators=(",", ":"))
    os.replace(tmp, PRECOS_FILE)


def avaliar(a, it):
    """Põe o selo de menor preço e devolve False se for 'maquiagem'
    (o produto esteve bem mais barato nos últimos 30 dias)."""
    p = float(it["p"])
    r = PRECOS.get(a)
    dia = hoje()
    vals = [v for k, v in (r or {}).get("h", {}).items() if dia - 30 <= int(k) < dia]
    it.pop("min30", None)
    if not vals:
        return True
    m = min(vals)
    if m < p * (1 - QUEDA_MIN / 100):
        return False
    if dia - r["i"] >= HIST_DIAS_MIN and p <= m:
        it["min30"] = True
    return True


def pode_avisar(a, it, urgente):
    """Não repete produto antes de REPETIR_DIAS, salvo se o preço cair QUEDA_MIN% ou mais."""
    al = ST.setdefault("alertas", {})
    p = float(it["p"])
    if a not in al:
        ja = ST["vistos"].get(a, 0) >= URGENTE if urgente else a in ST["vistos"]
        if ja:  # já tinha sido avisado antes deste controle existir: só registra
            al[a] = [p, time.time()]
            return False
        return True
    p0, t0 = al[a]
    if p <= p0 * (1 - QUEDA_MIN / 100):
        it["baixou"] = p0
        return True
    return time.time() - t0 >= REPETIR_DIAS * 86400


# ---------- posts temáticos no WhatsApp ----------
WA_TOP_HORA = int(os.getenv("WA_TOP_HORA", "20"))           # Top 5 do dia (-1 desliga)
WA_ACHADOS_HORA = int(os.getenv("WA_ACHADOS_HORA", "12"))   # Achadinhos baratos (-1 desliga)
WA_ACHADOS_ATE = float(os.getenv("WA_ACHADOS_ATE", "50"))
WA_ACHADOS_MIN = float(os.getenv("WA_ACHADOS_MIN", "10"))


def wa_tematicos(forcar=None):
    if not (WA_URL or WHAPI_TOKEN):
        return
    agora = datetime.now(TZ)
    dia = agora.strftime("%Y-%m-%d")
    if forcar == "top" or (forcar is None and agora.hour == WA_TOP_HORA and ST.get("top_dia_ult") != dia):
        ST["top_dia_ult"] = dia
        h = ST.get("hoje") or {}
        itens = h.get("itens", {}) if h.get("d") == dia else {}
        top = sorted(itens.items(), key=lambda x: -x[1]["pc"])[:5]
        if top:
            wa_post(top, titulo="🏆 *TOP 5 DO DIA*", sub="Os maiores descontos que passaram por aqui hoje")
            log(f"WhatsApp: Top 5 do dia ({len(top)})")
        salvar()
    if forcar == "achados" or (forcar is None and agora.hour == WA_ACHADOS_HORA and ST.get("achados_ult") != dia):
        ST["achados_ult"] = dia
        todos = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
        cand = [(a, it) for a, it in todos.items()
                if WA_ACHADOS_MIN <= float(it["p"]) <= WA_ACHADOS_ATE and it["pc"] >= MIN_DESCONTO and not motivo_fora(it) and avaliar(a, it)]
        top = sorted(cand, key=lambda x: -x[1]["pc"])[:WA_MAX]
        if top:
            wa_post(top, titulo=f"💸 *ACHADINHOS ATÉ R$ {WA_ACHADOS_ATE:.0f}*",
                    sub="Baratinhos com desconto de verdade")
            log(f"WhatsApp: Achadinhos ({len(top)} de {len(cand)})")
        salvar()


# ---------- relatório diário e vigia do canal ----------
RELATORIO_HORA = int(os.getenv("RELATORIO_HORA", "22"))      # -1 desliga
PARADO_HORAS = float(os.getenv("PARADO_HORAS", "3"))          # alerta se o canal ficar tanto tempo sem post


def stat(chave, n=1):
    dia = datetime.now(TZ).strftime("%Y-%m-%d")
    with lock:
        st = ST.get("stats") or {}
        if st.get("d") != dia:
            st = {"d": dia}
        st[chave] = st.get(chave, 0) + n
        ST["stats"] = st


def texto_relatorio():
    st = ST.get("stats") or {}
    g = lambda k: st.get(k, 0)
    ult = ST.get("wa_ult_post")
    ult_txt = f"há {int((time.time() - ult) // 60)} min" if ult else "ainda nenhum"
    col = ST.get("ult_gerado")
    col_txt = f"última leitura há {int(idade_min(col))} min" if col else "sem leitura"
    return (f"📊 <b>Relatório de hoje</b> ({datetime.now(TZ).strftime('%d/%m %H:%M')})\n\n"
            f"🔎 Ofertas novas: {g('novas_urg') + g('novas_com')} "
            f"({g('novas_urg')} com {URGENTE}%+ e {g('novas_com')} de {MIN_DESCONTO}% a {URGENTE - 1}%)\n"
            f"🚫 Barradas por desconto maquiado: {g('maquiadas')}\n"
            f"✂️ Canceladas por excesso: {g('canceladas')}\n"
            f"🧹 Lixo filtrado: {g('fora_livro')} livros pouco avaliados · {g('fora_poucas_aval')} com poucas avaliações · "
            f"{g('fora_nota')} nota baixa · {g('fora_barato')} baratos demais · {g('fora_palavra')} capinhas/películas etc.\n"
            f"💰 {MIN_DESCONTO}-{URGENTE - 1}% com economia grande: {g('valor')} encontradas · {g('valor_barrado')} descontos grandes barrados pela DeepSeek · {g('destaques')} destaques\n"
            f"📲 WhatsApp: {g('posts_wa')} posts com {g('ofertas_wa')} ofertas · último post {ult_txt}\n"
            f"⚠️ Erros do Whapi: {g('erros_wa')}\n"
            f"🤖 Coletor: {'Amazon bloqueada ⚠️' if ST.get('bloqueado') else 'ok'}, {col_txt}"
            + (f"\n💳 Saldo DeepSeek: US$ {ST['ds_saldo']:.2f}" if ST.get("ds_saldo") is not None else "")
            + (f"\n🔍 Outras lojas ({MERCADO_MODO}): {g('mercado_barato')} mais baratas na Amazon · "
               f"{g('mercado_caro')} mais caras · {g('mercado_inflado')} com \"de\" inflado · "
               f"{g('mercado_parecido')} parecidas · {g('mercado_incerto') + g('mercado_sem_dados')} sem comparação"
               f" · {g('mercado_erro')} erros  (detalhes: /mercado)" if DEEPSEEK_KEY else ""))


def vigiar():
    agora = datetime.now(TZ)
    dia = agora.strftime("%Y-%m-%d")
    if agora.hour == BACKUP_HORA and ST.get("backup_ult") != dia:
        ST["backup_ult"] = dia
        fazer_backup(dia)
    if DEEPSEEK_KEY and agora.hour == SALDO_HORA and ST.get("saldo_ult") != dia:
        ST["saldo_ult"] = dia
        checar_saldo_ds()
    if agora.hour == RELATORIO_HORA and ST.get("relatorio_ult") != dia:
        ST["relatorio_ult"] = dia
        para_admins(texto_relatorio())
    ult = ST.get("wa_ult_post")
    if (WHAPI_TOKEN or WA_URL) and ult and 9 <= agora.hour <= 23 and not ST.get("wa_alerta_parado") \
            and time.time() - ult > PARADO_HORAS * 3600:
        ST["wa_alerta_parado"] = True
        para_admins(f"⚠️ O canal do WhatsApp está há mais de {PARADO_HORAS:g}h sem post. "
                    "Pode ser falta de ofertas novas ou problema no Whapi. Mande /relatorio para ver os números.")


# ---------- Black Friday ----------
BLACK_FRIDAY = os.getenv("BLACK_FRIDAY", "2026-11-27")      # dia da Black Friday
BF_ESQUENTA_DIAS = int(os.getenv("BF_ESQUENTA_DIAS", "12"))  # contagem regressiva começa N dias antes
BF_SEMANA_ANTES = int(os.getenv("BF_SEMANA_ANTES", "4"))     # "semana da Black Friday" começa N dias antes...
BF_SEMANA_DEPOIS = int(os.getenv("BF_SEMANA_DEPOIS", "3"))   # ...e vai até N dias depois (Cyber Monday)
BF_HORA = int(os.getenv("BF_HORA", "10"))                    # post diário de esquenta / Black Friday (-1 desliga)
WA_INTERVALO_BF = int(os.getenv("WA_INTERVALO_BF", "8"))     # intervalo entre posts na semana da Black Friday


def bf_fase():
    """('esquenta', dias que faltam) / ('semana', dias até/depois) / (None, None)."""
    try:
        d = (date.fromisoformat(BLACK_FRIDAY) - datetime.now(TZ).date()).days
    except ValueError:
        return None, None
    if -BF_SEMANA_DEPOIS <= d <= BF_SEMANA_ANTES:
        return "semana", d
    if 0 < d <= BF_ESQUENTA_DIAS:
        return "esquenta", d
    return None, None


def selos_extra(it):
    s = ""
    bdg = it.get("bdg") or ""
    if "black friday" in bdg.lower():
        s += "🖤 _Oferta Black Friday da Amazon_\n"
    elif "cupom" in bdg.lower() or "coupon" in bdg.lower():
        s += f"🎟️ _{bdg}_ (aplique na página)\n"
    if (it.get("vend") or 0) >= 50:
        s += f"🔥 _{it['vend']}% da oferta já vendida_\n"
    return s


def bf_posts(forcar=False):
    """Post diário: esquenta (contagem regressiva) e, na semana, 'as melhores de hoje'."""
    if not (WA_URL or WHAPI_TOKEN) or BF_HORA < 0:
        return
    fase, d = bf_fase()
    agora = datetime.now(TZ)
    dia = agora.strftime("%Y-%m-%d")
    if not fase or (not forcar and (agora.hour != BF_HORA or ST.get("bf_ult") == dia)):
        if forcar and not fase:
            para_admins("Fora do período da Black Friday: nada a postar.")
        return
    ST["bf_ult"] = dia
    todos = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
    cand = [(a, it) for a, it in todos.items() if not motivo_fora(it) and avaliar(a, it)]
    top = sorted(cand, key=lambda x: -pontos(x[1]))[:WA_MAX]
    if not top:
        return
    if fase == "esquenta":
        titulo = f"⏳ *FALTAM {d} DIA{'S' if d > 1 else ''} PARA A BLACK FRIDAY*"
        sub = "Esquenta: as melhores ofertas de agora"
    elif d == 0:
        titulo, sub = "🖤 *HOJE É BLACK FRIDAY!*", "As melhores ofertas da manhã"
    elif d > 0:
        titulo, sub = "🖤 *SEMANA BLACK FRIDAY*", f"Faltam {d} dia{'s' if d > 1 else ''} • as melhores de hoje"
    else:
        titulo, sub = "🖤 *BLACK FRIDAY: ÚLTIMOS DIAS*", "Ofertas que ainda estão valendo"
    wa_post(top, titulo=titulo, sub=sub)
    registrar_postados(top)
    stat("posts_wa")
    log(f"WhatsApp: post Black Friday ({fase}, {d}) com {len(top)} ofertas")
    salvar()


# ---------- aprendizado com o relatório de vendas da Amazon ----------
def registrar_postados(itens):
    with lock:
        p = ST.setdefault("postados", {})
        agora = time.time()
        for a, it in itens:
            p[a] = [it.get("cat") or "", agora]
        for a in [a for a, (c, t) in p.items() if agora - t > 60 * 86400]:
            del p[a]


def nome_cat(c):
    return (c or "?").replace("gl_", "").replace("_", " ")


def ler_tabela(raw):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    linhas = [l for l in txt.splitlines() if l.strip()]
    for i, l in enumerate(linhas[:15]):          # o cabeçalho é a linha que tem uma coluna ASIN
        if "asin" in l.lower():
            sep = max([",", ";", "\t"], key=l.count)
            return list(csv.reader(io.StringIO("\n".join(linhas[i:])), delimiter=sep))
    return None


def aprender_relatorio(chat, doc):
    try:
        f = requests.get(TG + "/getFile", params={"file_id": doc["file_id"]}, timeout=30).json()["result"]
        raw = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}", timeout=60).content
        nome = (doc.get("file_name") or "").lower()
        if nome.endswith(".xlsx"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
                rows = [[("" if c is None else str(c)) for c in r] for r in wb.active.iter_rows(values_only=True)]
                i = next(k for k, r in enumerate(rows[:15]) if any("asin" in c.lower() for c in r))
                tab = rows[i:]
            except ImportError:
                enviar(chat, "Não consigo ler .xlsx aqui. Baixe o relatório como CSV e mande de novo.")
                return
        else:
            tab = ler_tabela(raw)
        if not tab:
            enviar(chat, "Não achei a coluna ASIN nesse arquivo. Mande o relatório de <b>Pedidos</b> "
                         "do Associados em CSV.")
            return
        cab = [c.strip().lower() for c in tab[0]]
        ia = next(i for i, c in enumerate(cab) if "asin" in c)
        iq = next((i for i, c in enumerate(cab) if c.startswith(("qtd", "quant", "itens envi", "itens ped"))), None)
        ic = next((i for i, c in enumerate(cab) if "categ" in c), None)
        vendas, cat_txt = {}, {}
        for r in tab[1:]:
            if len(r) <= ia or not re.fullmatch(r"[A-Z0-9]{10}", r[ia].strip()):
                continue
            try:
                q = int(float((r[iq] if iq is not None and len(r) > iq else "1").replace(",", ".") or 1))
            except ValueError:
                q = 1
            a = r[ia].strip()
            vendas[a] = vendas.get(a, 0) + max(q, 1)
            if ic is not None and len(r) > ic:
                cat_txt[r[ic].strip()] = cat_txt.get(r[ic].strip(), 0) + max(q, 1)
        if not vendas:
            enviar(chat, "O arquivo não tem pedidos que eu consiga ler.")
            return
        # categoria de cada ASIN vendido: do que postamos, ou dos dados atuais do coletor
        atuais = {**(baixar("comuns.json") or {}).get("itens", {}), **(baixar("urgentes.json") or {}).get("itens", {})}
        post = ST.get("postados") or {}
        vend_cat, post_cat = {}, {}
        for a, (c, t) in post.items():
            if c:
                post_cat[c] = post_cat.get(c, 0) + 1
        nossos = 0
        for a, q in vendas.items():
            c = (post.get(a) or [None])[0] or (atuais.get(a) or {}).get("cat")
            if a in post:
                nossos += q
            if c:
                vend_cat[c] = vend_cat.get(c, 0) + q
        total_v, total_p = sum(vend_cat.values()) or 1, sum(post_cat.values()) or 1
        media = total_v / total_p
        bonus = {}
        for c in set(vend_cat) | set(post_cat):
            if post_cat.get(c, 0) < 10 and vend_cat.get(c, 0) < 3:
                continue   # poucos dados nessa categoria: não mexe
            taxa = (vend_cat.get(c, 0) + 0.5 * media) / (post_cat.get(c, 0) + 0.5)
            bonus[c] = round(max(-6, min(8, 4 * math.log2(taxa / media))), 1) if media else 0
        with lock:
            ST["aprendizado"] = bonus
            ST["aprendizado_info"] = {"quando": datetime.now(TZ).strftime("%d/%m %H:%M"), "itens": sum(vendas.values()),
                                      "nossos": nossos, "vend_cat": vend_cat, "cat_txt": cat_txt}
        salvar()
        enviar(chat, texto_aprendizado())
    except Exception as e:
        log(f"aprendizado: {e!r}")
        enviar(chat, f"Não consegui ler o relatório: {html.escape(str(e)[:200])}")


def texto_aprendizado():
    inf, b = ST.get("aprendizado_info"), ST.get("aprendizado") or {}
    if not inf:
        return ("📚 Ainda não aprendi com nenhum relatório.\n\nNo Associados da Amazon, baixe o relatório de "
                "<b>Pedidos</b> (últimos 30 dias) em CSV e mande o arquivo aqui.")
    mais = sorted(inf["vend_cat"].items(), key=lambda x: -x[1])[:6]
    sobe = sorted([x for x in b.items() if x[1] > 0], key=lambda x: -x[1])[:5]
    desce = sorted([x for x in b.items() if x[1] < 0], key=lambda x: x[1])[:5]
    t = (f"📚 <b>Aprendizado</b> (relatório de {inf['quando']})\n"
         f"🛒 {inf['itens']} itens vendidos · {inf['nossos']} eram ofertas que postamos\n\n"
         "<b>O que mais vende:</b>\n" + "\n".join(f"• {nome_cat(c)}: {q}" for c, q in mais))
    if sobe:
        t += "\n\n⬆️ <b>Ganham prioridade:</b> " + ", ".join(f"{nome_cat(c)} (+{v:g})" for c, v in sobe)
    if desce:
        t += "\n⬇️ <b>Perdem prioridade:</b> " + ", ".join(f"{nome_cat(c)} ({v:g})" for c, v in desce)
    return t


# ---------- backup diário ----------
BACKUP_HORA = int(os.getenv("BACKUP_HORA", "3"))
BACKUP_DIAS = int(os.getenv("BACKUP_DIAS", "7"))
BACKUP_TELEGRAM = os.getenv("BACKUP_TELEGRAM", "1") == "1"   # cópia diária, sem som, no seu Telegram
HC_URL = os.getenv("HC_URL", "").strip()                    # Healthchecks.io: avisa se o bot parar
SALDO_HORA = int(os.getenv("SALDO_HORA", "9"))
DS_SALDO_MIN = float(os.getenv("DS_SALDO_MIN", "2"))        # alerta quando o saldo da DeepSeek ficar abaixo (US$)


def sinal_vida(falha=False):
    if not HC_URL:
        return
    try:
        requests.get(HC_URL + ("/fail" if falha else ""), timeout=10)
    except requests.RequestException as e:
        log(f"Healthchecks indisponível: {e}")


def checar_saldo_ds():
    try:
        r = requests.get(DS + "/user/balance", timeout=30,
                         headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"})
        r.raise_for_status()
        info = (r.json().get("balance_infos") or [{}])[0]
        saldo = float(info.get("total_balance") or 0)
        ST["ds_saldo"] = saldo
        if saldo < DS_SALDO_MIN:
            para_admins(f"💳 Saldo da DeepSeek baixo: US$ {saldo:.2f}. Recarregue em platform.deepseek.com "
                        "para a comparação com outras lojas não parar.")
    except Exception as e:
        log(f"Saldo DeepSeek: {e!r}")


def fazer_backup(dia):
    try:
        pasta = os.path.join("backups", dia)
        os.makedirs(pasta, exist_ok=True)
        with lock:
            salvar()
        for f in (STATE_FILE, PRECOS_FILE):
            if os.path.exists(f):
                shutil.copy2(f, pasta)
        for velho in sorted(os.listdir("backups"))[:-BACKUP_DIAS]:
            if os.path.isdir(os.path.join("backups", velho)):
                shutil.rmtree(os.path.join("backups", velho), ignore_errors=True)
        log(f"Backup diário salvo em {pasta}")
        if BACKUP_TELEGRAM and ADMINS:
            arq = shutil.make_archive(os.path.join("/tmp", f"backup-bot-{dia}"), "gztar", pasta)
            with open(arq, "rb") as fh:
                r = requests.post(TG + "/sendDocument", files={"document": (os.path.basename(arq), fh)},
                                  data={"chat_id": ADMINS[0], "disable_notification": "true",
                                        "caption": f"💾 Backup do bot {dia} (estado + histórico de preços)"},
                                  timeout=120)
            os.remove(arq)
            if not r.ok:
                raise RuntimeError(f"Telegram recusou o backup ({r.status_code})")
    except Exception as e:
        log(f"Backup falhou: {e!r}")
        para_admins(f"⚠️ Backup diário falhou: {html.escape(str(e)[:150])}")


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

    novos_urg = [a for a, it in urg.items() if it["pc"] >= URGENTE and pode_avisar(a, it, True)]
    novos_com = [a for a, it in comuns.items() if a not in urg and MIN_DESCONTO <= it["pc"] < URGENTE
                 and pode_avisar(a, it, False)]
    fracas = set()
    for a in novos_urg + novos_com:
        m = motivo_fora(urg[a] if a in urg else comuns[a])
        if m:
            fracas.add(a)
            stat("fora_" + m)
    novos_urg = [a for a in novos_urg if a not in fracas]
    novos_com = [a for a in novos_com if a not in fracas]
    maquiadas = {a for a in novos_urg if not avaliar(a, urg[a])} | {a for a in novos_com if not avaliar(a, comuns[a])}
    if maquiadas:
        log(f"{len(maquiadas)} oferta(s) ignoradas: já estiveram mais baratas nos últimos 30 dias")
    stat("maquiadas", len(maquiadas))
    novos_urg = [a for a in novos_urg if a not in maquiadas]
    novos_com = [a for a in novos_com if a not in maquiadas]
    if ST["base_feita"]:
        stat("novas_urg", len(novos_urg))
        stat("novas_com", len(novos_com))
    if ST["base_feita"]:  # descartadas não voltam a ser analisadas a cada ciclo
        with lock:
            al = ST.setdefault("alertas", {})
            for a in fracas | maquiadas:
                al[a] = [float((urg.get(a) or comuns.get(a))["p"]), time.time()]
    registrar_precos({**comuns, **urg})
    salvar_precos()
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

    # trava contra enxurrada: muitas ofertas de uma vez -> só as melhores, o resto é cancelado
    limite = WA_MAX * (WA_POSTS_MAX or 4)
    if len(novos_urg) > limite:
        canc = len(novos_urg) - limite
        novos_urg.sort(key=lambda a: -urg[a]["pc"])
        with lock:
            al = ST.setdefault("alertas", {})
            for a in novos_urg[limite:]:
                al[a] = [float(urg[a]["p"]), time.time()]
        novos_urg = novos_urg[:limite]
        stat("canceladas", canc)
        log(f"Enxurrada: {limite + canc} urgentes de uma vez; enviando {limite}, {canc} canceladas")
        para_admins(f"⚠️ Chegaram {limite + canc} ofertas de uma vez. Mandei só as {limite} melhores "
                    f"e cancelei as outras {canc}.")
    with lock:
        al = ST.setdefault("alertas", {})
        for a in novos_urg:
            al[a] = [float(urg[a]["p"]), time.time()]
        for a in novos_com:
            al[a] = [float(comuns[a]["p"]), time.time()]
    salvar()

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
            msgs = em_blocos(titulo, linhas)
            if len(msgs) > MAX_MSGS:
                log(f"Resumo grande ({len(novos_com)} ofertas): mandando só {MAX_MSGS} mensagens")
                msgs = msgs[:MAX_MSGS]
                msgs[-1] += "\n\n<i>(lista cortada: muitas ofertas de uma vez)</i>"
            for m in msgs:
                para_todos(m, canal=False)
            valor = [a for a in novos_com if econ(comuns[a]) >= VALOR_ECON]
            if valor:
                log(f"{len(valor)} oferta(s) de {MIN_DESCONTO}-{URGENTE - 1}% com economia >= R$ {VALOR_ECON:.0f}: WhatsApp na hora")
                para_whatsapp([(a, {**comuns[a], "valor": True}) for a in valor])
                stat("valor", len(valor))
            with lock:
                pend = ST.setdefault("canal_pend", {})
                for a in novos_com:
                    if a not in valor:
                        pend[a] = comuns[a]


    # canal: resumo curto (so as melhores) nos horarios de CANAL_HORAS
    agora = datetime.now(TZ)
    chave = agora.strftime("%Y-%m-%d %H")
    pend = ST.get("canal_pend") or {}
    if (CANAL or WA_URL or WHAPI_TOKEN) and pend and agora.hour in CANAL_HORAS and ST.get("canal_ult") != chave:
        top = sorted(pend, key=lambda a: -pontos(pend[a]))[:CANAL_MAX]   # melhores pela pontuação (marca, economia...)
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
    if not ST.get("teste_destaque_v1") and ADMINS:
        ST["teste_destaque_v1"] = True
        salvar()
        threading.Thread(target=teste_destaque, args=(ADMINS[0],), daemon=True).start()
    log(f"Bot iniciado: lendo {GITHUB_REPO} a cada {CHECAR_MIN} min, urgente {URGENTE}%+, resumo {RESUMO_HORAS}")
    erro_avisado = False
    while True:
        try:
            ciclo()
            wa_publicar()
            wa_tematicos()
            bf_posts()
            vigiar()
            sinal_vida()
            erro_avisado = False
        except Exception as e:
            log(f"Falha no ciclo: {e!r}")
            sinal_vida(falha=True)
            if not erro_avisado:
                erro_avisado = True
                para_admins(f"⚠️ Bot de ofertas: erro ao ler os dados ({html.escape(str(e)[:200])}).")
        salvar()
        time.sleep(CHECAR_MIN * 60)


if __name__ == "__main__":
    main()
