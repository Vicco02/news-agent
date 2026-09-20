#!/usr/bin/env python3
"""
Resumen semanal de videojuegos -> Telegram.

Corre los lunes: junta lo publicado desde el lunes anterior hasta el domingo,
clasifica cada noticia con Claude (tipo, plataforma, relevancia), arma
secciones con cupos fijos y redacta todo en español.

Plataformas que se siguen: PlayStation, Nintendo y PC (Steam, Epic, GOG).
Lo exclusivo de Xbox, los rumores y las filtraciones se descartan.

Uso:
  python gaming_agent.py              # corre completo y envía a Telegram
  python gaming_agent.py --dry-run    # imprime el resumen en consola, no envía
  python gaming_agent.py --days 3     # ventana de N días en vez de la semana
"""

import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

import anthropic

from news_agent import (
    ANTHROPIC_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, MODEL, CHILE_TZ,
    google_news, fetch_feed, dedup, resolve_links, format_items, summarize,
    send_telegram,
)

# ------------------------------------------------------------------
# Configuración
# ------------------------------------------------------------------
MAX_ITEMS_PER_FEED = 100       # Eurogamer o RPS publican 100+ notas por semana
CLASSIFY_BATCH = 50            # noticias por llamada de clasificación
MIN_SCORE = 3                  # bajo esto no se considera candidata
MAX_CANDIDATES_PER_SECTION = 12  # cuántas candidatas ve el redactor por sección

# Medios en inglés (RSS directo) y búsquedas en Google News en español para
# ofertas, juegos gratis y cobertura latina. Verifica un feed nuevo con
# `python probe_feeds.py URL` o con el workflow "Verificar feeds RSS".
FEEDS = [
    # Generalistas
    "https://www.eurogamer.net/feed",
    "https://www.gematsu.com/feed",
    "https://kotaku.com/rss",
    "https://feeds.feedburner.com/ign/games-all",
    # PC / Steam
    "https://www.pcgamer.com/rss/",
    "https://www.rockpapershotgun.com/feed",
    "https://store.steampowered.com/feeds/news/",
    # PlayStation
    "https://www.pushsquare.com/feeds/latest",
    "https://blog.playstation.com/feed/",
    # Nintendo
    "https://www.nintendolife.com/feeds/latest",
    # Ofertas, gratis y cobertura en español vía Google News
    google_news("PS Plus juegos del mes"),
    google_news("PlayStation Store ofertas"),
    google_news("Epic Games Store juego gratis"),
    google_news("Nintendo eShop ofertas"),
    google_news("Steam ofertas rebajas"),
    google_news("Nintendo Direct"),
    google_news("análisis videojuego review"),
    google_news("videojuegos lanzamientos semana"),
]

# (título de sección, tipos que entran, cupo)
SECTIONS = [
    ("🎮 Lanzamientos y anuncios", {"lanzamiento"}, 4),
    ("🔧 Actualizaciones y DLC", {"actualizacion"}, 3),
    ("⭐ Reviews destacadas", {"review"}, 3),
    ("🛒 Ofertas y juegos gratis", {"oferta", "gratis"}, 4),
    ("🕹️ Consolas, tiendas e industria", {"hardware", "industria"}, 2),
]


# ------------------------------------------------------------------
# Ventana de tiempo
# ------------------------------------------------------------------
def week_window(days=None):
    """Devuelve (inicio, fin) en hora de Chile.
    - Un lunes (la corrida programada): desde el lunes de la semana pasada
      a las 00:00 hasta ahora, o sea la semana completa hasta el domingo.
    - Cualquier otro día (corrida manual): desde el lunes de esta semana.
    - Con --days N: los últimos N días."""
    now = datetime.now(CHILE_TZ)
    if days:
        return now - timedelta(days=days), now
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if now.weekday() == 0:
        return this_monday - timedelta(days=7), now
    return this_monday, now


def collect(cutoff):
    items = []
    for url in FEEDS:
        items.extend(fetch_feed(url, cutoff, max_items=MAX_ITEMS_PER_FEED))
    items = dedup(items)
    print(f"  pool: {len(items)} noticias en la ventana")
    return items


# ------------------------------------------------------------------
# Clasificación por lotes
# ------------------------------------------------------------------
CLASSIFY_PROMPT = """Eres editor de una newsletter semanal de videojuegos para un jugador de PlayStation, Nintendo y PC (Steam, Epic, GOG). Clasifica cada noticia.

Para cada noticia devuelve:
- "tipo": "lanzamiento" | "actualizacion" | "review" | "oferta" | "gratis" | "hardware" | "industria" | "otro"
  * lanzamiento: juego que salió esta semana, anuncio de juego nuevo, fecha de lanzamiento confirmada, tráiler o gameplay de un juego esperado, retrasos.
  * actualizacion: parche o actualización importante, DLC o expansión, temporada nueva, contenido relevante para un juego ya lanzado.
  * review: análisis/reseñas, recepción crítica (Metacritic/OpenCritic), "el mejor juego de...". Incluye indies si la recepción es muy buena.
  * oferta: rebajas y descuentos en PS Store, eShop, Steam, Epic, GOG; eventos de ofertas (Steam Sale, Days of Play, etc.).
  * gratis: juegos gratis: lineup mensual de PS Plus, juego gratis de Epic, fines de semana gratis, free-to-play nuevos.
  * hardware: consolas, mandos, accesorios, PS5/Switch/Steam Deck, precios de consolas.
  * industria: tiendas y plataformas (Steam, PS Store, eShop, Epic), políticas, estudios, adquisiciones, cierres, Nintendo Direct/State of Play como evento.
  * otro: rumores, filtraciones, opinión, columnas, listas genéricas, guías, trucos, esports, cine/series, ruido.
- "plataforma": "ps" | "nintendo" | "pc" | "xbox" | "multi" | "otra"
- "score": 1-5 relevancia para ese jugador.
  * 5: gran lanzamiento o anuncio, juego muy esperado, PS Plus del mes, rebaja fuerte de un juego conocido, Nintendo Direct.
  * 4: noticia importante de un juego o estudio conocido, indie con reviews excelentes, oferta buena.
  * 3: interesante pero menor.
  * 2: juego o estudio poco conocido, oferta menor, exclusivo de Xbox o móvil.
  * 1: ruido, clickbait, rumor, DUPLICADO. Si varias noticias tratan el MISMO hecho (aunque en distinto medio o idioma), deja score 1 en todas menos la más completa.
  * Lo exclusivo de Xbox o móvil no pasa de 2. Los rumores y filtraciones son "otro" con score 1.
  * El jugador compra en las tiendas de Chile/Latinoamérica y EE.UU. Las ofertas, promociones o lanzamientos limitados a otras regiones (Sudeste Asiático, Japón, Reino Unido, Europa, etc.) son score 1.

Responde SOLO con un array JSON, sin texto adicional, con un objeto por noticia en el mismo orden:
[{"id": 1, "tipo": "lanzamiento", "plataforma": "multi", "score": 4}, ...]

Noticias:
{items}
"""

VALID_TIPOS = {"lanzamiento", "actualizacion", "review", "oferta", "gratis",
               "hardware", "industria", "otro"}


def classify_batch(client, items):
    prompt = CLASSIFY_PROMPT.replace("{items}", format_items(items))
    resp = client.messages.create(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    try:
        data = json.loads(text[text.index("["):text.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  ! No pude parsear un lote de clasificación: {e}", file=sys.stderr)
        return [{"tipo": "otro", "plataforma": "otra", "score": 1} for _ in items]
    by_id = {}
    for d in data:
        try:
            by_id[int(d["id"])] = d
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for i in range(1, len(items) + 1):
        d = by_id.get(i, {})
        tipo = str(d.get("tipo", "otro")).lower()
        out.append({
            "tipo": tipo if tipo in VALID_TIPOS else "otro",
            "plataforma": str(d.get("plataforma", "otra")).lower(),
            "score": int(d.get("score", 1) or 1),
        })
    return out


def classify_all(client, items):
    labels = []
    for start in range(0, len(items), CLASSIFY_BATCH):
        batch = items[start:start + CLASSIFY_BATCH]
        print(f"  clasificando {start + 1}-{start + len(batch)} de {len(items)}...")
        labels.extend(classify_batch(client, batch))
    for it, lab in zip(items, labels):
        it.update(lab)
    return items


def select(items):
    """Candidatas por sección, ordenadas por score y recortadas."""
    chosen = {}
    for title, tipos, _quota in SECTIONS:
        cands = sorted(
            (it for it in items if it["tipo"] in tipos and it["score"] >= MIN_SCORE),
            key=lambda it: -it["score"],
        )
        chosen[title] = cands[:MAX_CANDIDATES_PER_SECTION]
    return chosen


# ------------------------------------------------------------------
# Redacción
# ------------------------------------------------------------------
def build_prompt(chosen, start, end):
    today = datetime.now(CHILE_TZ)
    raw = ""
    for title, _tipos, quota in SECTIONS:
        cands = chosen.get(title, [])
        if not cands:
            continue
        raw += (f"\n## {title}\n(elige como máximo {quota}, las más relevantes; "
                f"descarta el resto)\n{format_items(cands, with_link=True, with_lang=False)}\n")

    return f"""Eres el editor de una newsletter semanal de videojuegos para un jugador de PlayStation, Nintendo y PC. Hoy es {today:%d/%m/%Y}. Abajo tienes noticias crudas de RSS de la semana del {start:%d/%m} al {end:%d/%m}, agrupadas por sección. Cada una trae título, contexto (extracto de la nota) y link.

Genera el resumen semanal para Telegram con estas reglas:
- Usa las secciones tal cual (mismo emoji + nombre como encabezado en <b>negrita</b>), en el mismo orden. Omite una sección solo si no tiene noticias que valgan la pena.
- En cada sección elige hasta el máximo indicado. Prioriza lo que le importa a un jugador: juegos esperados, actualizaciones grandes, juegos con muy buenas reviews (indies incluidos), ofertas de verdad buenas y juegos gratis que valgan la pena. Descarta lo exclusivo de Xbox o móvil, rumores, filtraciones y listas genéricas.
- NUNCA repitas un hecho: si varias noticias cubren lo mismo (aunque estén en secciones distintas o en distinto idioma), escríbelo una sola vez usando la fuente más completa.
- Cada noticia va en la sección donde aparece abajo; no muevas noticias entre secciones.
- Cada noticia DESARROLLADA en 2-3 frases: qué pasó, el dato clave (fecha de lanzamiento, plataformas, precio o porcentaje de descuento, hasta cuándo dura la oferta, nota de reviews) y por qué importa. Apóyate en el CONTEXTO, no inventes datos que no estén en el material. Si una oferta o juego gratis tiene fecha límite y está en el material, dila.
- Fechas: compara con la fecha de hoy. Si un juego sale después de hoy, di que "sale el X", nunca que "ya está disponible".
- En 🛒 Ofertas y juegos gratis prioriza variedad: primero los juegos gratis (Epic, PS Plus, fines de semana gratis), después las ofertas, y no más de dos ofertas de la misma tienda. Si el material trae precios en euros o libras, no los repitas: da solo el porcentaje de descuento (el jugador compra en dólares o pesos chilenos).
- Formato de cada noticia: el titular en <b>negrita</b>, seguido de las frases de desarrollo, y al final, entre paréntesis, el LINK COMPLETO tal cual aparece en el material (la URL entera que empieza con https://). Nunca pongas solo el dominio ni abrevies la URL. Si usaste varias fuentes, pon el link de la más completa.
- No menciones ofertas ni promociones limitadas a otras regiones (Sudeste Asiático, Japón, Reino Unido, etc.): el jugador compra en tiendas de Chile/Latinoamérica y EE.UU.
- IDIOMA: TODO en español, aunque la fuente esté en inglés. Traduce los titulares; los nombres de juegos, estudios y tiendas se dejan tal cual.
- Sé claro y sustancioso pero sin relleno. Empieza directo con la primera sección, sin introducción.
- Usa SOLO formato HTML de Telegram: <b>negrita</b>. NADA de markdown (nada de ** ni ##).
- Los links van como texto plano entre paréntesis, no como etiqueta <a>.
- Deja una línea en blanco entre noticias.

Noticias crudas:
{raw}

Recordatorio final: cada noticia termina con su URL completa entre paréntesis, copiada del campo LINK; todo en español; nada de ofertas de otras regiones.
"""


# ------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="imprime en consola, no envía")
    ap.add_argument("--days", type=int, default=None, help="ventana de N días en vez de la semana")
    args = ap.parse_args()
    if not args.dry_run and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (o usa --dry-run).")

    start, end = week_window(args.days)
    # Para el título: la semana termina el domingo aunque se corra el lunes.
    label_end = end if args.days else min(end, start + timedelta(days=6))
    print(f"Recolectando noticias del {start:%d/%m %H:%M} al {end:%d/%m %H:%M} (Chile)...")
    items = collect(start.astimezone(timezone.utc))
    if not items:
        print("No se encontraron noticias. Saliendo.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print(f"Clasificando {len(items)} noticias con {MODEL}...")
    classify_all(client, items)

    chosen = select(items)
    for title, cands in chosen.items():
        print(f"  {title}: {len(cands)} candidatas")
        for it in cands[:5]:
            print(f"    [{it['score']}/{it['plataforma']}] {it['title'][:90]}")
    if not any(chosen.values()):
        print("Nada relevante esta semana. Saliendo.")
        return

    resolve_links([it for cands in chosen.values() for it in cands])

    print(f"Redactando con {MODEL}...")
    summary = summarize(client, build_prompt(chosen, start, label_end))

    header = f"🎮 Resumen gamer de la semana — {start:%d/%m} al {label_end:%d/%m}"
    if args.dry_run:
        print("\n" + "=" * 60 + f"\n<b>{header}</b>\n\n" + summary + "\n" + "=" * 60)
        return
    print("Enviando a Telegram...")
    send_telegram(summary, header=header)
    print("Listo ✅")


if __name__ == "__main__":
    main()
