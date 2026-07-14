#!/usr/bin/env python3
"""
Agente diario de noticias -> Telegram.
Lee feeds RSS, filtra las del día anterior, resume con Claude Haiku y envía a Telegram.
"""

import os
import re
import sys
import html
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import anthropic

# ------------------------------------------------------------------
# Configuración
# ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MODEL = "claude-haiku-4-5-20251001"   # barato y suficiente. Sube a sonnet si quieres más análisis.
MAX_ITEMS_PER_FEED = 10                # cuántos titulares tomar por feed antes de filtrar por fecha
HOURS_WINDOW = 36                      # ventana: cubre "ayer" con margen por husos horarios
MAX_DESC_CHARS = 400                   # cuánta descripción del RSS pasar a Claude por noticia

# Zona horaria Chile (UTC-3 en horario de verano, -4 invierno). Usamos -3 fijo para simplicidad.
CHILE_TZ = timezone(timedelta(hours=-3))

# User-Agent de navegador: varios medios (La Tercera, etc.) bloquean requests sin él.
UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Feeds agrupados por categoría. Edita libremente.
FEEDS = {
    "🌐 Tech mundial": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://hnrss.org/frontpage",
    ],
    "🇨🇱 Tech (medios chilenos)": [
        "https://www.pisapapeles.net/feed/",
        "https://www.fayerwayer.com/feed/",
        "https://www.latercera.com/arcio/rss/category/tecnologia/",
    ],
    "🇨🇱 Chile general": [
        "https://www.latercera.com/arcio/rss/category/nacional/",
        "https://www.ex-ante.cl/feed/",
    ],
    "🗞️ Mundo general": [
        "https://feeds.bbci.co.uk/mundo/rss.xml",
        "https://www.latercera.com/arcio/rss/category/mundo/",
    ],
}


# ------------------------------------------------------------------
# Recolección de noticias
# ------------------------------------------------------------------
def entry_datetime(entry):
    """Devuelve la fecha del entry como datetime aware, o None."""
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if val:
            try:
                dt = parsedate_to_datetime(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (TypeError, ValueError):
                pass
    # feedparser a veces expone struct_time
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None


def clean_description(entry):
    """Extrae y limpia la descripción/resumen del entry (sin HTML)."""
    raw = getattr(entry, "summary", "") or ""
    if not raw and getattr(entry, "content", None):
        try:
            raw = entry.content[0].value
        except (IndexError, AttributeError, KeyError):
            raw = ""
    # quitar etiquetas HTML
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # algunos feeds (Pisapapeles) meten "Lee la nota original..." al inicio; lo quitamos
    text = re.sub(r"^Lee la nota original.*?link:\s*", "", text, flags=re.IGNORECASE)
    return text[:MAX_DESC_CHARS]


def collect_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    grouped = {}
    for category, urls in FEEDS.items():
        items = []
        for url in urls:
            try:
                resp = requests.get(url, headers=UA_HEADERS, timeout=15)
                feed = feedparser.parse(resp.content)
            except Exception as e:
                print(f"  ! Error leyendo {url}: {e}", file=sys.stderr)
                continue
            for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
                dt = entry_datetime(entry)
                if dt is None or dt >= cutoff:
                    title = html.unescape(getattr(entry, "title", "").strip())
                    link = getattr(entry, "link", "")
                    desc = clean_description(entry)
                    if title:
                        items.append({"title": title, "link": link, "desc": desc})
        # dedup por título
        seen, unique = set(), []
        for it in items:
            key = it["title"].lower()
            if key not in seen:
                seen.add(key)
                unique.append(it)
        grouped[category] = unique
        print(f"  {category}: {len(unique)} noticias")
    return grouped


# ------------------------------------------------------------------
# Resumen con Claude
# ------------------------------------------------------------------
def build_prompt(grouped):
    lines = []
    for category, items in grouped.items():
        if not items:
            continue
        lines.append(f"\n## {category}")
        for it in items:
            entry = f"- TÍTULO: {it['title']}"
            if it.get("desc"):
                entry += f"\n  CONTEXTO: {it['desc']}"
            entry += f"\n  LINK: {it['link']}"
            lines.append(entry)
    raw = "\n".join(lines)

    return f"""Eres un editor de noticias. Abajo tienes noticias crudas de RSS del día anterior, agrupadas por categoría. Cada una trae título, un contexto (extracto de la nota) y su link.

Genera un resumen diario para Telegram con estas reglas:
- Mantén las categorías (usa el mismo emoji + nombre como encabezado en <b>negrita</b>).
- Por cada categoría, elige SOLO las 3-4 noticias más importantes/relevantes. Descarta ruido, clickbait y duplicados.
- Cada noticia debe ir DESARROLLADA en 2-3 frases: qué pasó, el dato o detalle clave, y por qué importa o qué implica. Apóyate en el CONTEXTO provisto, no te quedes solo en el título. No inventes datos que no estén en el material.
- Formato de cada noticia: el titular en <b>negrita</b>, seguido de las frases de desarrollo, y el link entre paréntesis al final.
- IDIOMA: las categorías de tecnología (🌐 Tech mundial, 🇨🇱 Tech) escríbelas en inglés; las categorías generales (Chile general, Mundo general) en español.
- Sé claro y sustancioso pero sin relleno. Empieza directo con la primera categoría, sin introducción.
- Usa SOLO formato HTML de Telegram: <b>negrita</b>. NADA de markdown (nada de ** ni ##).
- Los links van como texto plano entre paréntesis, no como etiqueta <a>.
- Deja una línea en blanco entre noticias para que se lea cómodo.

Noticias crudas:
{raw}
"""


def summarize(grouped):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": build_prompt(grouped)}],
    )
    return resp.content[0].text.strip()


# ------------------------------------------------------------------
# Envío a Telegram
# ------------------------------------------------------------------
def send_telegram(text):
    fecha = datetime.now(CHILE_TZ).strftime("%A %d/%m/%Y")
    header = f"<b>📰 Resumen de noticias — {fecha}</b>\n\n"
    full = header + text

    # Telegram limita a 4096 chars por mensaje: partimos si hace falta.
    chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in chunks:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if not r.ok:
            print(f"  ! Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            r.raise_for_status()
        time.sleep(0.5)


# ------------------------------------------------------------------
def main():
    print("Recolectando noticias...")
    grouped = collect_news()
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        print("No se encontraron noticias. Saliendo.")
        send_telegram("No encontré noticias nuevas en las fuentes hoy. Revisa los feeds.")
        return
    print(f"Total: {total} noticias. Resumiendo con {MODEL}...")
    summary = summarize(grouped)
    print("Enviando a Telegram...")
    send_telegram(summary)
    print("Listo ✅")


if __name__ == "__main__":
    main()
