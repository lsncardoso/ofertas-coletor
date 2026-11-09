// Ponte WhatsApp para o bot de ofertas (roda 24h no servidor).
// Recebe textos do bot.py em http://127.0.0.1:3399/send e publica no Canal do WhatsApp.
// Config (.env): WA_PHONE (so na 1a conexao, p/ codigo de pareamento), WA_CANAL_LINK
import makeWASocket, { useMultiFileAuthState, DisconnectReason, Browsers, fetchLatestBaileysVersion } from "baileys";
import pino from "pino";
import http from "node:http";
import fs from "node:fs";

const PORT = Number(process.env.WA_PORT || 3399);
const PHONE = (process.env.WA_PHONE || "").replace(/\D/g, "");
const LINK = process.env.WA_CANAL_LINK || "";
const INTERVALO_MS = Number(process.env.WA_INTERVALO_MS || 4000); // ritmo calmo entre posts
const logger = pino({ level: "warn" });
const log = (...a) => console.log(new Date().toISOString(), ...a);

let sock = null, pronto = false, canalJid = process.env.WA_CANAL_JID || "";
const fila = [];
let enviando = false;

async function resolverCanal() {
  if (canalJid || !LINK) return canalJid;
  const code = LINK.trim().split("/").filter(Boolean).pop();
  const meta = await sock.newsletterMetadata("invite", code);
  canalJid = meta.id;
  log("Canal do WhatsApp:", canalJid);
  return canalJid;
}

async function processarFila() {
  if (enviando) return;
  enviando = true;
  try {
    while (fila.length && pronto) {
      const texto = fila[0];
      try {
        const jid = await resolverCanal();
        if (!jid) { log("Sem WA_CANAL_LINK configurado; descartando"); fila.shift(); continue; }
        await sock.sendMessage(jid, { text: texto });
        fila.shift();
        log("Postado no canal (" + texto.length + " caracteres). Na fila:", fila.length);
      } catch (e) {
        log("Falha ao postar:", e?.message || e);
        break; // tenta de novo no proximo ciclo
      }
      await new Promise(r => setTimeout(r, INTERVALO_MS));
    }
  } finally {
    enviando = false;
  }
}
setInterval(processarFila, 30000);

async function conectar() {
  const { state, saveCreds } = await useMultiFileAuthState("auth");
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({ version, auth: state, logger, browser: Browsers.ubuntu("Chrome"),
                        markOnlineOnConnect: false, syncFullHistory: false });
  sock.ev.on("creds.update", saveCreds);

  if (!state.creds.registered) {
    if (!PHONE) { log("Defina WA_PHONE no .env para gerar o codigo de pareamento"); }
    else {
      setTimeout(async () => {
        try {
          const code = await sock.requestPairingCode(PHONE);
          log("CODIGO DE PAREAMENTO:", code);
          fs.writeFileSync("pareamento.txt", code + "\n");
        } catch (e) { log("Erro ao pedir codigo:", e?.message || e); }
      }, 4000);
    }
  }

  sock.ev.on("connection.update", ({ connection, lastDisconnect }) => {
    if (connection === "open") {
      pronto = true;
      log("WhatsApp conectado");
      try { fs.unlinkSync("pareamento.txt"); } catch {}
      processarFila();
    }
    if (connection === "close") {
      pronto = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        log("Sessao encerrada pelo WhatsApp (deslogado ou numero bloqueado). Precisa parear de novo.");
        process.exit(2);
      }
      log("Conexao caiu (" + code + "), reconectando em 10s");
      setTimeout(conectar, 10000);
    }
  });
}

http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/status") {
    res.end(JSON.stringify({ conectado: pronto, canal: canalJid || null, fila: fila.length }));
    return;
  }
  if (req.method === "POST" && req.url === "/send") {
    let body = "";
    req.on("data", c => { body += c; if (body.length > 100000) req.destroy(); });
    req.on("end", () => {
      try {
        const { text } = JSON.parse(body);
        if (text) { fila.push(text); if (fila.length > 200) fila.shift(); processarFila(); }
        res.end('{"ok":true}');
      } catch { res.statusCode = 400; res.end('{"ok":false}'); }
    });
    return;
  }
  res.statusCode = 404; res.end();
}).listen(PORT, "127.0.0.1", () => log("Ponte WhatsApp ouvindo em 127.0.0.1:" + PORT));

conectar();
