#!/usr/bin/env python3
"""
Agente diario de noticias -> Telegram.

Pipeline:
  1. Lee feeds RSS y filtra las noticias del día anterior.
  2. Clasifica las noticias tech con Claude (región REAL de la noticia y tipo
     de tema) para que "Tech Chile" sea realmente chileno, sin depender de
     qué medio la publicó.
  3. Selecciona con cupos fijos por sección (si falta tech chileno, se
     compensa con más tech mundial).
  4. Redacta el resumen con Claude y lo envía a Telegram.

Uso:
  python news_agent.py            # corre completo y envía a Telegram
  python news_agent.py --dry-run  # imprime el resumen en consola, no envía
"""

import os
import re
import sys
import html
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import feedparser
import requests
import anthropic

# ------------------------------------------------------------------
# Configuración
# ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MODEL = "claude-haiku-4-5-20251001"   # barato y suficiente. Sube a sonnet si quieres más análisis.
MAX_ITEMS_PER_FEED = 12                # cuántos titulares tomar por feed antes de filtrar por fecha
HOURS_WINDOW = 36                      # ventana: cubre "ayer" con margen por husos horarios
MAX_DESC_CHARS = 400                   # cuánta descripción del RSS pasar a Claude por noticia

# Cupos por sección del resumen. Si "Tech Chile" no alcanza su cupo, la
# diferencia se suma a "Tech mundial" para que el total tech se mantenga.
QUOTA_TECH_CHILE = 3
QUOTA_TECH_LATAM = 1                   # extra en Tech Chile para una noticia latam, solo si es relevante
MIN_LATAM_SCORE = 4
QUOTA_TECH_MUNDIAL = 5
QUOTA_CHILE_GENERAL = 2
QUOTA_MUNDO_GENERAL = 2

# Temas tech que quieres leer (producto, IA, features, empresas). Lo que
# quede fuera (finanzas, legal/regulación) no entra en las secciones tech:
# si es muy relevante, pasa como candidato a las secciones generales.
PREFERRED_TECH_TOPICS = {"producto", "ia", "feature", "empresa"}
GENERAL_TECH_TOPICS = {"finanzas", "legal"}
MIN_TECH_SCORE = 2                     # descarta ruido (score 1 = irrelevante/duplicado)

# Zona horaria Chile (UTC-3 en horario de verano, -4 invierno). Usamos -3 fijo para simplicidad.
CHILE_TZ = timezone(timedelta(hours=-3))

# User-Agent de navegador: varios medios (La Tercera, etc.) bloquean requests sin él.
UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def google_news(query):
    """RSS de Google News (edición Chile) para una búsqueda. Sirve para
    captar noticias tech chilenas por CONTENIDO, no por medio."""
    from urllib.parse import quote_plus
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=es-419&gl=CL&ceid=CL:es-419"
    )


# Todos los feeds tech van a un mismo pool: la región (Chile / mundo) la
# decide el clasificador mirando el contenido, no la fuente.
TECH_FEEDS = [
    # Medios globales
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://hnrss.org/frontpage",
    # Medios chilenos de tech (cubren mucho tech global; el clasificador separa)
    "https://www.pisapapeles.net/feed/",
    "https://www.fayerwayer.com/feed/",
    "https://www.latercera.com/arcio/rss/category/tecnologia/",
    # Búsquedas en Google News Chile para captar ecosistema tech local
    google_news("startup chilena"),
    google_news("inteligencia artificial Chile empresa"),
    google_news("fintech Chile lanzamiento"),
]

GENERAL_FEEDS = {
    "🇨🇱 Chile": [
        "https://www.latercera.com/arcio/rss/category/nacional/",
        "https://www.ex-ante.cl/feed/",
    ],
    "🗞️ Mundo": [
        "https://feeds.bbci.co.uk/mundo/rss.xml",
        "https://www.latercera.com/arcio/rss/category/mundo/",
    ],
}

SECTION_TECH_MUNDIAL = "🌐 Tech mundial"
SECTION_TECH_CHILE = "🇨🇱 Tech Chile (+ Latam)"
SECTION_CHILE = "🇨🇱 Chile"
SECTION_MUNDO = "🗞️ Mundo"


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


def source_name(entry, feed_url):
    """Nombre corto de la fuente (dominio o, en Google News, el medio)."""
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return src.title
    return urlparse(feed_url).netloc.replace("www.", "")


def fetch_feed(url, cutoff):
    """Devuelve la lista de items recientes de un feed."""
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"  ! Error leyendo {url}: {e}", file=sys.stderr)
        return []
    is_gnews = "news.google.com" in url
    items = []
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        dt = entry_datetime(entry)
        if dt is not None and dt < cutoff:
            continue
        title = html.unescape(getattr(entry, "title", "").strip())
        if not title:
            continue
        if is_gnews:
            # Google News pone " - Medio" al final del título; lo quitamos.
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
        items.append({
            "title": title,
            "link": getattr(entry, "link", ""),
            "desc": clean_description(entry),
            "source": source_name(entry, url),
        })
    return items


def dedup(items):
    seen, unique = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9áéíóúñ ]", "", it["title"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def collect_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    tech = []
    for url in TECH_FEEDS:
        tech.extend(fetch_feed(url, cutoff))
    tech = dedup(tech)
    print(f"  tech (pool): {len(tech)} noticias")

    general = {}
    for section, urls in GENERAL_FEEDS.items():
        items = []
        for url in urls:
            items.extend(fetch_feed(url, cutoff))
        general[section] = dedup(items)
        print(f"  {section}: {len(general[section])} noticias")

    return tech, general


# ------------------------------------------------------------------
# Clasificación de noticias tech con Claude
# ------------------------------------------------------------------
def format_items(items, with_link=False):
    lines = []
    for i, it in enumerate(items, 1):
        line = f"[{i}] TÍTULO: {it['title']}"
        if it.get("source"):
            line += f" (fuente: {it['source']})"
        if it.get("desc"):
            line += f"\n    CONTEXTO: {it['desc']}"
        if with_link and it.get("link"):
            line += f"\n    LINK: {it['link']}"
        lines.append(line)
    return "\n".join(lines)


CLASSIFY_PROMPT = """Eres un editor de tecnología. Clasifica cada noticia de la lista.

Para cada noticia devuelve:
- "region": "chile" | "latam" | "global"
  * "chile" SOLO si la noticia trata de algo chileno: empresa/startup chilena, producto o servicio lanzado en Chile o hecho por chilenos, decisión de una empresa o del Estado de Chile en tech, evento tech en Chile. Que el medio sea chileno NO la hace chilena: una nota de Pisapapeles sobre el nuevo iPhone es "global".
  * "latam" si trata de otro país de Latinoamérica.
  * "global" para todo lo demás.
- "topic": "producto" | "ia" | "feature" | "empresa" | "finanzas" | "legal" | "otro"
  * producto: lanzamientos de hardware/software/servicios, reviews.
  * ia: modelos, herramientas y avances de inteligencia artificial.
  * feature: nuevas funciones o actualizaciones de productos existentes.
  * empresa: movimientos estratégicos (nuevos negocios, alianzas, adquisiciones, fundadores, rondas de inversión de startups).
    Rondas de inversión: son "empresa", pero dales score 4-5 SOLO si es una ronda grande o de una startup conocida; una ronda chica o de una startup desconocida es score 2.
  * finanzas: resultados trimestrales, acciones, valorización, despidos por costos, macro.
  * legal: juicios, multas, regulación, antimonopolio, privacidad/legislación.
  * otro: tutoriales, opinión, ofertas, ciencia general, gaming casual, ruido.
- "score": 1-5 importancia/relevancia para alguien que trabaja en tech y le interesan productos, IA, nuevas funciones y empresas. Usa 1 para clickbait, ofertas, tutoriales y para DUPLICADOS (si dos noticias cuentan lo mismo, deja 1 en todas menos la mejor).

Responde SOLO con un array JSON, sin texto adicional, con un objeto por noticia en el mismo orden:
[{"id": 1, "region": "global", "topic": "ia", "score": 4}, ...]

Noticias:
{items}
"""


def classify_tech(client, items):
    """Devuelve una lista de dicts {region, topic, score} alineada con items.
    Si algo falla, devuelve None y el llamador usa un fallback."""
    if not items:
        return []
    prompt = CLASSIFY_PROMPT.replace("{items}", format_items(items))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    try:
        start, end = text.index("["), text.rindex("]") + 1
        data = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  ! No pude parsear la clasificación: {e}", file=sys.stderr)
        return None
    by_id = {}
    for d in data:
        try:
            by_id[int(d["id"])] = d
        except (KeyError, TypeError, ValueError):
            continue
    result = []
    for i in range(1, len(items) + 1):
        d = by_id.get(i, {})
        result.append({
            "region": str(d.get("region", "global")).lower(),
            "topic": str(d.get("topic", "otro")).lower(),
            "score": int(d.get("score", 3) or 3),
        })
    return result


def select_tech(items, labels):
    """Aplica los cupos. Devuelve (tech_chile, tech_mundial, extras_general)
    donde extras_general = {sección: [items]} con noticias tech de
    finanzas/legal relevantes que pueden entrar en las secciones generales."""
    for it, lab in zip(items, labels):
        it.update(lab)

    def ranked(pred):
        return sorted(
            (it for it in items if it["score"] >= MIN_TECH_SCORE and pred(it)),
            key=lambda it: -it["score"],
        )

    chile = ranked(lambda it: it["region"] == "chile" and it["topic"] in PREFERRED_TECH_TOPICS)
    latam = ranked(lambda it: it["region"] == "latam" and it["topic"] in PREFERRED_TECH_TOPICS
                   and it["score"] >= MIN_LATAM_SCORE)

    tech_chile = chile[:QUOTA_TECH_CHILE]
    # Hasta QUOTA_TECH_LATAM noticias latam relevantes se suman a Tech Chile.
    tech_chile += latam[:QUOTA_TECH_LATAM]
    chosen = {id(it) for it in tech_chile}

    # Lo que no entró en Tech Chile (incluido latam) compite en Tech mundial.
    mundo = ranked(lambda it: it["region"] != "chile" and it["topic"] in PREFERRED_TECH_TOPICS
                   and id(it) not in chosen)
    faltan = QUOTA_TECH_CHILE - len(chile[:QUOTA_TECH_CHILE])
    tech_mundial = mundo[:QUOTA_TECH_MUNDIAL + faltan]

    extras = {SECTION_CHILE: [], SECTION_MUNDO: []}
    for it in ranked(lambda it: it["topic"] in GENERAL_TECH_TOPICS and it["score"] >= 4):
        extras[SECTION_CHILE if it["region"] == "chile" else SECTION_MUNDO].append(it)

    print(
        f"  clasificación: {len(chile)} tech Chile, {len(latam)} latam relevantes, "
        f"{len(mundo)} tech mundial, "
        f"{sum(len(v) for v in extras.values())} tech finanzas/legal -> general"
    )
    return tech_chile, tech_mundial, extras


# ------------------------------------------------------------------
# Resumen con Claude
# ------------------------------------------------------------------
def build_prompt(tech_chile, tech_mundial, general, extras):
    def block(title, items, instruction):
        if not items:
            return ""
        return f"\n## {title}\n({instruction})\n{format_items(items, with_link=True)}\n"

    fixed = "escribe TODAS estas noticias, en este orden; ya están seleccionadas"
    raw = block(SECTION_TECH_CHILE, tech_chile, fixed)
    raw += block(SECTION_TECH_MUNDIAL, tech_mundial, fixed)

    for section, quota in ((SECTION_CHILE, QUOTA_CHILE_GENERAL),
                           (SECTION_MUNDO, QUOTA_MUNDO_GENERAL)):
        pool = general.get(section, []) + extras.get(section, [])
        raw += block(section, pool, f"elige SOLO las {quota} más importantes; descarta el resto")

    quota_note = (
        f"Las secciones tech ya vienen seleccionadas. En {SECTION_CHILE} elige "
        f"{QUOTA_CHILE_GENERAL} noticias y en {SECTION_MUNDO} elige {QUOTA_MUNDO_GENERAL}: "
        "prioriza hechos de peso (política, economía, seguridad, grandes empresas) "
        "y descarta farándula, deportes, clickbait y duplicados."
    )

    return f"""Eres un editor de noticias. Abajo tienes noticias crudas de RSS del día anterior, agrupadas por sección. Cada una trae título, un contexto (extracto de la nota) y su link.

Genera un resumen diario para Telegram con estas reglas:
- Usa las secciones tal cual (mismo emoji + nombre como encabezado en <b>negrita</b>), en el mismo orden. Omite una sección solo si no tiene noticias.
- {quota_note}
- Cada noticia debe ir DESARROLLADA en 2-3 frases: qué pasó, el dato o detalle clave, y por qué importa o qué implica. Apóyate en el CONTEXTO provisto, no te quedes solo en el título. No inventes datos que no estén en el material.
- Formato de cada noticia: el titular en <b>negrita</b>, seguido de las frases de desarrollo, y el link entre paréntesis al final.
- IDIOMA: cada noticia se escribe en el idioma de su fuente: si el TÍTULO/CONTEXTO está en inglés, escríbela en inglés; si está en español, en español. No traduzcas. Los encabezados de sección van tal cual.
- Sé claro y sustancioso pero sin relleno. Empieza directo con la primera sección, sin introducción.
- Usa SOLO formato HTML de Telegram: <b>negrita</b>. NADA de markdown (nada de ** ni ##).
- Los links van como texto plano entre paréntesis, no como etiqueta <a>.
- Deja una línea en blanco entre noticias para que se lea cómodo.

Noticias crudas:
{raw}
"""


def summarize(client, prompt):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        print("  ! El resumen se cortó por max_tokens; súbelo más.",
              file=sys.stderr)
    return resp.content[0].text.strip()


# ------------------------------------------------------------------
# Envío a Telegram
# ------------------------------------------------------------------
def _split_for_telegram(full, limit=4000):
    """Parte el mensaje respetando saltos de línea para no cortar una
    etiqueta <b>…</b> por la mitad (Telegram rechaza HTML mal formado)."""
    chunks = []
    current = ""
    for line in full.split("\n"):
        # Si una sola línea excede el límite, la partimos a lo bruto.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text):
    fecha = datetime.now(CHILE_TZ).strftime("%A %d/%m/%Y")
    header = f"<b>📰 Resumen de noticias — {fecha}</b>\n\n"
    full = header + text

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in _split_for_telegram(full):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, data=payload)
        # Fallback: si el HTML viene mal formado (etiqueta suelta, < o &
        # crudos), reintentamos como texto plano en vez de perder el mensaje.
        if r.status_code == 400 and "can't parse entities" in r.text.lower():
            print(f"  ! HTML inválido, reenviando como texto plano: {r.text}",
                  file=sys.stderr)
            plain = re.sub(r"<[^>]+>", "", chunk)
            payload.pop("parse_mode")
            payload["text"] = plain
            r = requests.post(url, data=payload)
        if not r.ok:
            print(f"  ! Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            r.raise_for_status()
        time.sleep(0.5)


# ------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    if not dry_run and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (o usa --dry-run).")

    print("Recolectando noticias...")
    tech, general = collect_news()
    total = len(tech) + sum(len(v) for v in general.values())
    if total == 0:
        print("No se encontraron noticias. Saliendo.")
        if not dry_run:
            send_telegram("No encontré noticias nuevas en las fuentes hoy. Revisa los feeds.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"Clasificando {len(tech)} noticias tech con {MODEL}...")
    labels = classify_tech(client, tech)
    if labels is None:
        # Fallback: sin clasificación tratamos todo como tech mundial.
        print("  ! Clasificación falló; usando todo como tech mundial.", file=sys.stderr)
        labels = [{"region": "global", "topic": "producto", "score": 3} for _ in tech]
    tech_chile, tech_mundial, extras = select_tech(tech, labels)
    print(f"  seleccionadas: {len(tech_chile)} tech Chile + {len(tech_mundial)} tech mundial")
    for it in tech_chile:
        print(f"    🇨🇱 [{it['score']}/{it['topic']}] {it['title']}")
    for it in tech_mundial:
        print(f"    🌐 [{it['score']}/{it['topic']}] {it['title']}")

    print(f"Resumiendo con {MODEL}...")
    summary = summarize(client, build_prompt(tech_chile, tech_mundial, general, extras))

    if dry_run:
        print("\n" + "=" * 60 + "\n" + summary + "\n" + "=" * 60)
        return
    print("Enviando a Telegram...")
    send_telegram(summary)
    print("Listo ✅")


if __name__ == "__main__":
    main()
