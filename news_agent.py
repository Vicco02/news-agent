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
  4. Busca hallazgos IA (herramientas y proyectos populares hechos con IA)
     en un pool aparte; la sección solo aparece si algo pasa la vara.
  5. Redacta el resumen con Claude y lo envía a Telegram.

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
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, quote

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
# Score mínimo para Tech Chile. Con 4 la sección quedó vacía seis días
# seguidos (el clasificador rara vez da 4 a una noticia chilena); con 3
# entran los hechos locales y las entrevistas/columnas igual quedan fuera
# porque se clasifican como "otro". El cupo que falte pasa a tech mundial.
MIN_TECH_CHILE_SCORE = 3
# Los cupos que Tech Chile no llena pasan a Tech mundial, pero solo con
# noticias de al menos este score: mejor un resumen más corto que rellenar
# con notas menores.
MIN_FILL_SCORE = 3

# Hallazgos IA: herramientas, skills y proyectos hechos con IA que se están
# volviendo populares. Es una sección ocasional: solo aparece cuando algo
# pasa la vara, así que la mayoría de los días no sale.
QUOTA_HALLAZGOS = 2
MIN_HALLAZGO_SCORE = 4
# Lo ya enviado se recuerda para no repetir un repo que sigue entre los más
# estrellados varios días. En GitHub Actions el archivo persiste vía actions/cache.
HALLAZGOS_MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "hallazgos_enviados.json")
HALLAZGOS_MEMORY_DAYS = 21

# Noticias tech ya enviadas: la ventana de 36 horas se traslapa con la del
# día anterior, así que sin esto un mismo hecho sale dos días seguidos (a
# veces con otro titular o en otro idioma). Persiste igual que la de hallazgos.
NEWS_MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "noticias_enviadas.json")
NEWS_MEMORY_DAYS = 3

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
    "https://www.fayerwayer.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://www.latercera.com/arcio/rss/category/tecnologia/",
    # Búsquedas en Google News Chile para captar ecosistema tech local
    google_news("startup chilena"),
    google_news("inteligencia artificial Chile empresa"),
    google_news("fintech Chile lanzamiento"),
    # Negocios / ecosistema chileno y latam (el clasificador filtra lo tech)
    "https://www.latercera.com/arcio/rss/category/pulso/",
    "https://www.trendtic.cl/feed/",
    "https://contxto.com/en/feed/",
    # Emol bloquea requests directos y DF no tiene RSS: los leemos vía Google News
    google_news("site:emol.com tecnología"),
    google_news("site:df.cl tecnología OR startup OR inteligencia artificial"),
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

# Fuentes de hallazgos IA: donde aparecen primero las herramientas y proyectos
# que la gente construye (no los anuncios de empresas, que ya van en tech).
# Pool aparte para que este ruido no entre en Tech mundial. Cada candidato
# trae un número de popularidad (estrellas o puntos): sin eso, Haiku le da 4
# a cualquier repo de GitHub Trending y la sección saldría todos los días.

# Repos de GitHub creados en los últimos días, ordenados por estrellas: ahí
# aparecen las skills, agentes y herramientas que están explotando.
GITHUB_NEW_DAYS = 14
GITHUB_MIN_STARS = 500
# Ritmo mínimo para ser candidato. Sin este piso el pool de 14 días cambia
# poco de un día a otro: al enviar los mejores, los que venían detrás pasaban
# a ser "4" y la sección salía todos los días con repos de ~350 estrellas por
# día. Los que valieron la pena la primera semana iban de 600 a 3900 por día.
GITHUB_MIN_STARS_PER_DAY = 500

# Hacker News vía la API de búsqueda de Algolia (los feeds filtrados de hnrss
# devuelven 502 a ratos). Sin filtrar por palabras: lo muy votado que no tiene
# que ver con IA lo descarta el clasificador. Los puntos son la mejor señal de
# popularidad que tenemos.
HN_SEARCHES = [
    {"tags": "show_hn", "min_points": 100},
    {"tags": "story", "min_points": 200},
]

# Nombres legibles para fuentes cuyo dominio no dice nada.
SOURCE_NAMES = {
    "hnrss.org": "Hacker News",
}

SECTION_TECH_MUNDIAL = "🌐 Tech mundial"
SECTION_TECH_CHILE = "🇨🇱 Tech Chile (+ Latam)"
SECTION_CHILE = "🇨🇱 Chile"
SECTION_MUNDO = "🗞️ Mundo"
SECTION_HALLAZGOS = "🧪 Hallazgos IA"


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
    domain = urlparse(feed_url).netloc.replace("www.", "")
    return SOURCE_NAMES.get(domain, domain)


# Palabras muy frecuentes de cada idioma; basta contar cuáles aparecen más.
_ES_WORDS = {
    "el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una", "por",
    "para", "con", "se", "su", "sus", "al", "es", "más", "como", "pero", "lo",
    "este", "esta", "han", "ha", "fue", "son", "sobre", "entre", "tras", "hasta",
    "también", "ya", "según", "nuevo", "nueva", "años", "millones",
}
_EN_WORDS = {
    "the", "of", "and", "to", "in", "is", "for", "on", "with", "that", "as",
    "it", "its", "by", "from", "at", "this", "are", "be", "has", "have", "was",
    "will", "new", "an", "or", "but", "not", "after", "into", "how", "what",
    "says", "more", "than", "about", "you", "your", "we", "can", "up", "out",
}


def detect_lang(text):
    """Devuelve "en" o "es" según qué palabras frecuentes dominan el texto."""
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    es = sum(w in _ES_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    return "en" if en > es else "es"


_GNEWS_CACHE = {}


def _gnews_article_id(url):
    parts = urlparse(url).path.rstrip("/").split("/")
    if "news.google.com" in url and len(parts) >= 2 and parts[-2] in ("articles", "read"):
        return parts[-1]
    return None


def resolve_gnews_link(url):
    """Convierte un link de redirección de Google News (news.google.com/rss/
    articles/...) en la URL real de la nota. Si algo falla, devuelve el link
    original: nunca rompe el resumen, solo queda el link largo."""
    art_id = _gnews_article_id(url)
    if not art_id:
        return url
    if art_id in _GNEWS_CACHE:
        return _GNEWS_CACHE[art_id]
    real = url
    try:
        # Formato antiguo: el id es base64 con la URL embebida.
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", raw)
        if m and not raw.startswith(b"AU_yqL"):
            real = m.group(0).decode("utf-8", "ignore")
        else:
            # Formato nuevo (2024+): hay que pedirle a Google que lo decodifique.
            page = requests.get(f"https://news.google.com/articles/{art_id}",
                                headers=UA_HEADERS, timeout=10).text
            sg = re.search(r'data-n-a-sg="([^"]+)"', page)
            ts = re.search(r'data-n-a-ts="(\d+)"', page)
            if sg and ts:
                req = (
                    '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                    'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                    f'"{art_id}",{ts.group(1)},"{sg.group(1)}"]'
                )
                body = "f.req=" + quote(json.dumps([[["Fbv4je", req, None, "generic"]]]))
                r = requests.post(
                    "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                    headers={**UA_HEADERS,
                             "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                    data=body, timeout=10,
                )
                outer = json.loads(r.text.split("\n\n", 1)[1])
                for row in outer:
                    if isinstance(row, list) and len(row) > 2 and isinstance(row[2], str):
                        cand = json.loads(row[2])
                        if isinstance(cand, list) and len(cand) > 1 and str(cand[1]).startswith("http"):
                            real = cand[1]
                            break
    except Exception as e:
        print(f"  ! No pude resolver link de Google News ({e}); dejo el original",
              file=sys.stderr)
    _GNEWS_CACHE[art_id] = real
    return real


def resolve_links(items):
    """Reemplaza in-place los links de Google News por la URL real."""
    n = 0
    for it in items:
        if _gnews_article_id(it.get("link", "")):
            new = resolve_gnews_link(it["link"])
            if new != it["link"]:
                it["link"] = new
                n += 1
    if n:
        print(f"  links de Google News resueltos: {n}")


def fetch_feed(url, cutoff, max_items=None):
    """Devuelve la lista de items recientes de un feed."""
    max_items = max_items or MAX_ITEMS_PER_FEED
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"  ! Error leyendo {url}: {e}", file=sys.stderr)
        return []
    if not feed.entries:
        # Diagnóstico: si no es RSS, listamos links que parezcan feeds.
        hrefs = sorted(set(re.findall(r'href="([^"]*(?:rss|feed)[^"]*)"', resp.text, re.I)))
        print(f"  ! {url}: HTTP {resp.status_code}, sin entradas RSS. "
              f"Links rss/feed en la página: {hrefs[:15]}", file=sys.stderr)
        return []
    is_gnews = "news.google.com" in url
    items = []
    for entry in feed.entries[:max_items]:
        dt = entry_datetime(entry)
        if dt is not None and dt < cutoff:
            continue
        title = html.unescape(getattr(entry, "title", "").strip())
        if not title:
            continue
        if is_gnews:
            # Google News pone " - Medio" al final del título; lo quitamos.
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
        desc = clean_description(entry)
        if "hnrss.org" in url:
            # Los puntos vienen al final del summary y el recorte los pierde;
            # van al inicio porque son la mejor señal de popularidad.
            m = re.search(r"Points:\s*(\d+)", getattr(entry, "summary", "") or "")
            if m:
                desc = f"[{m.group(1)} puntos en HN] {desc}"
        items.append({
            "title": title,
            "link": getattr(entry, "link", ""),
            "desc": desc,
            "source": source_name(entry, url),
            "lang": detect_lang(f"{title} {desc}"),
        })
    print(f"  feed {urlparse(url).netloc.replace('www.', '')}: HTTP {resp.status_code}, "
          f"{len(feed.entries)} entradas, {len(items)} recientes"
          + (f" [{url[:70]}]" if is_gnews or len(feed.entries) == 0 else ""))
    return items


def github_owner(link):
    """Dueño (usuario u organización) de un link de GitHub, o None."""
    p = urlparse(link)
    if p.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    owner = p.path.strip("/").split("/")[0]
    return owner.lower() or None


def fetch_github_new():
    """Repos creados en los últimos GITHUB_NEW_DAYS días que ganan al menos
    GITHUB_MIN_STARS_PER_DAY estrellas por día, uno por autor (el de más
    estrellas, con los demás mencionados en el contexto)."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=GITHUB_NEW_DAYS)).date().isoformat()
    headers = {**UA_HEADERS, "Accept": "application/vnd.github+json"}
    # Sin token la búsqueda permite 10 consultas/min por IP, compartida en Actions.
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    params = {
        "q": f"created:>={since} stars:>={GITHUB_MIN_STARS}",
        "sort": "stars",
        "order": "desc",
        "per_page": 100,
    }
    try:
        resp = requests.get("https://api.github.com/search/repositories",
                            params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        repos = resp.json()["items"]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  ! Error leyendo repos nuevos de GitHub: {e}", file=sys.stderr)
        return []
    # Agrupados por autor: algunos publican una familia de repos parecidos
    # (storytold sacó una decena de clones de Adobe la misma semana) que, si
    # no, ocupan la sección varios días seguidos.
    by_owner = {}
    for r in repos:
        try:
            created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            age = max(1.0, (now - created).total_seconds() / 86400)
        except (KeyError, ValueError):
            age = float(GITHUB_NEW_DAYS)
        per_day = r["stargazers_count"] / age
        if per_day < GITHUB_MIN_STARS_PER_DAY:
            continue
        by_owner.setdefault(r["full_name"].split("/")[0].lower(), []).append((r, age, per_day))
    items = []
    for group in by_owner.values():
        r, age, per_day = group[0]
        desc = (r.get("description") or "").strip()[:MAX_DESC_CHARS]
        siblings = [s["full_name"].split("/")[1] for s, _, _ in group[1:]]
        if siblings:
            desc += (f" (el mismo autor tiene otros {len(siblings)} repos nuevos "
                     f"populares: {', '.join(siblings[:8])})")
        items.append({
            "title": r["full_name"],
            "link": r["html_url"],
            "desc": (f"[{r['stargazers_count']} estrellas en GitHub en {round(age)} días, "
                     f"~{round(per_day)} por día] {desc}"),
            "source": "GitHub",
            "lang": detect_lang(f"{r['full_name']} {desc}"),
        })
    print(f"  GitHub (repos de {GITHUB_NEW_DAYS} días, >= {GITHUB_MIN_STARS} estrellas): "
          f"{len(repos)} repos, {len(items)} autores con >= {GITHUB_MIN_STARS_PER_DAY} "
          "estrellas por día")
    return items


def fetch_hn(cutoff, tags, min_points):
    """Historias de HN publicadas desde cutoff con al menos min_points."""
    url = "https://hn.algolia.com/api/v1/search"
    params = {
        "tags": tags,
        "numericFilters": f"points>={min_points},created_at_i>{int(cutoff.timestamp())}",
        "hitsPerPage": 50,
    }
    try:
        resp = requests.get(url, params=params, headers=UA_HEADERS, timeout=15)
        resp.raise_for_status()
        hits = resp.json()["hits"]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  ! Error leyendo HN ({tags}, >= {min_points} puntos): {e}", file=sys.stderr)
        return []
    items = []
    for h in hits:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        text = re.sub(r"<[^>]+>", " ", html.unescape(h.get("story_text") or ""))
        text = re.sub(r"\s+", " ", text).strip()[:MAX_DESC_CHARS]
        points = int(h.get("points") or 0)
        desc = f"[{points} puntos en HN, {h.get('num_comments') or 0} comentarios] {text}"
        items.append({
            "title": title,
            "link": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "desc": desc.strip(),
            "source": "Hacker News",
            "lang": detect_lang(f"{title} {text}"),
        })
    print(f"  HN ({tags}, >= {min_points} puntos): {len(items)} historias")
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

    ai = fetch_github_new()
    for search in HN_SEARCHES:
        ai.extend(fetch_hn(cutoff, **search))
    ai = dedup(ai)
    print(f"  hallazgos IA (pool): {len(ai)} candidatos")

    return tech, general, ai


# ------------------------------------------------------------------
# Clasificación de noticias tech con Claude
# ------------------------------------------------------------------
def format_items(items, with_link=False, with_lang=True):
    lines = []
    for i, it in enumerate(items, 1):
        line = f"[{i}] TÍTULO: {it['title']}"
        if it.get("source"):
            line += f" (fuente: {it['source']})"
        if it.get("desc"):
            line += f"\n    CONTEXTO: {it['desc']}"
        if with_link:
            if with_lang:
                line += f"\n    IDIOMA: {'inglés' if it.get('lang') == 'en' else 'español'}"
            if it.get("link"):
                line += f"\n    LINK: {it['link']}"
        lines.append(line)
    return "\n".join(lines)


CLASSIFY_PROMPT = """Eres un editor de tecnología. Clasifica cada noticia de la lista.

Para cada noticia devuelve:
- "region": "chile" | "latam" | "global"
  * "chile" SOLO si la noticia trata de algo chileno: empresa/startup chilena, producto o servicio lanzado en Chile o hecho por chilenos, decisión de una empresa o del Estado de Chile en tech, evento tech en Chile. La región es DÓNDE ocurre o impacta la noticia, no la nacionalidad de la empresa: una empresa extranjera (Rappi, Mercado Libre, Uber) que se expande, lanza algo o toma decisiones en Chile es "chile". Que el medio sea chileno NO la hace chilena: una nota de Pisapapeles sobre el nuevo iPhone es "global".
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
  * otro: tutoriales, opinión, entrevistas, columnas, notas panorámicas o de análisis general, listas y explicadores ("8 claves para entender...", "todo lo que debes saber de...", "qué esperar de..."), guías de compra, ofertas, ciencia general, gaming casual, ruido. Una entrevista o columna sobre IA es "otro", no "ia"; un explicador de un producto ya anunciado es "otro", no "producto": las secciones tech son para HECHOS (lanzamientos, avances, movimientos de empresas).
- "score": 1-5 importancia/relevancia para alguien que trabaja en tech y le interesan productos, IA, nuevas funciones y empresas. Usa 1 para clickbait, ofertas, tutoriales y para DUPLICADOS: si dos o más noticias tratan el MISMO hecho (aunque desde distinto ángulo o medio, p. ej. "startup X entra a Y Combinator" y "los chilenos que llegaron a Y Combinator"), deja score 1 en todas menos la más completa.
  * Para las noticias con region "chile" la vara es el ecosistema chileno, no el mundial: el lector vive en Chile y quiere saber qué pasa en la tech local. Una ronda, un lanzamiento, una alianza o una expansión de una startup o empresa chilena (o de una extranjera operando en Chile) es score 3 si es un hecho concreto y 4-5 si es relevante dentro de Chile, aunque a escala global sea pequeña. Reserva el 2 para hechos menores o empresas sin trayectoria.

Responde SOLO con un array JSON, sin texto adicional, con un objeto por noticia en el mismo orden:
[{"id": 1, "region": "global", "topic": "ia", "score": 4}, ...]

Noticias:
{items}
"""


VALID_REGIONS = {"chile", "latam", "global"}
VALID_TOPICS = PREFERRED_TECH_TOPICS | GENERAL_TECH_TOPICS | {"otro"}


def _classify(client, prompt):
    """Pide a Claude un array JSON de etiquetas con "id" y lo devuelve como
    {id: dict}. Si la respuesta no se puede parsear, devuelve None."""
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
    return by_id


def classify_tech(client, items):
    """Devuelve una lista de dicts {region, topic, score} alineada con items.
    Si algo falla, devuelve None y el llamador usa un fallback."""
    if not items:
        return []
    by_id = _classify(client, CLASSIFY_PROMPT.replace("{items}", format_items(items)))
    if by_id is None:
        return None
    result = []
    for i in range(1, len(items) + 1):
        d = by_id.get(i, {})
        region = str(d.get("region", "global")).lower()
        topic = str(d.get("topic", "otro")).lower()
        result.append({
            "region": region if region in VALID_REGIONS else "global",
            # Haiku a veces inventa etiquetas ("outro"); lo desconocido es "otro".
            "topic": topic if topic in VALID_TOPICS else "otro",
            "score": int(d.get("score", 3) or 3),
        })
    return result


REPEATS_PROMPT = """Eres un editor. Abajo hay noticias candidatas para el resumen de hoy, ordenadas de más a menos importante, y los titulares que ya se enviaron en los últimos días.

Marca como repetida cada candidata que:
- trate del MISMO hecho que un titular YA ENVIADO, aunque cambie el medio, el idioma, el titular o el ángulo; o
- trate del MISMO hecho que otra candidata que aparece ANTES en la lista.
Mismo hecho = el mismo anuncio, lanzamiento, operación o evento. Ejemplos: dos notas del lanzamiento del mismo teléfono; "Paramount closes historic Warner Bros. merger" y "Paramount and Warner Bros. Discovery complete $110 billion megamerger"; "Chileno vende su startup Pimento a Mistral AI" y "Chileno que trabajó en Apple vendió su startup de IA a gigante francés". Dos noticias distintas de la misma empresa NO son repetidas.

YA ENVIADOS (días anteriores):
{sent}

Responde SOLO con un array JSON con los números de las candidatas repetidas, sin texto adicional ([] si no hay ninguna):
[3, 7]

Candidatas:
{items}
"""


def mark_repeats(client, items, labels, memory):
    """Baja a score 1 las noticias tech que repiten un hecho ya enviado en
    días anteriores o el de otra candidata más importante del mismo día. El
    clasificador debería hacerlo, pero con 100+ noticias se le escapan. Solo
    revisa las que pueden entrar en las secciones tech; si la llamada falla,
    no cambia nada."""
    pool = sorted(
        (i for i, lab in enumerate(labels)
         if lab["topic"] in PREFERRED_TECH_TOPICS and lab["score"] >= MIN_TECH_SCORE),
        key=lambda i: -labels[i]["score"],
    )
    if not pool:
        return
    sent = "\n".join(f"- {d['title']}" for d in memory) or "(ninguno)"
    prompt = (REPEATS_PROMPT.replace("{sent}", sent)
              .replace("{items}", format_items([items[i] for i in pool])))
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        ids = json.loads(text[text.index("["):text.rindex("]") + 1])
    except (anthropic.APIError, ValueError) as e:
        print(f"  ! No pude revisar repetidas ({e}); sigo sin ese filtro", file=sys.stderr)
        return
    for n in ids:
        if isinstance(n, int) and 1 <= n <= len(pool):
            i = pool[n - 1]
            labels[i]["score"] = 1
            print(f"    🔁 repetida: {items[i]['title'][:90]}")


def select_tech(items, labels):
    """Aplica los cupos. Devuelve (tech_chile, tech_mundial, extras_general)
    donde extras_general = {sección: [items]} con noticias tech de
    finanzas/legal relevantes que pueden entrar en las secciones generales."""
    for it, lab in zip(items, labels):
        it.update(lab)

    # Diagnóstico: qué llegó de Chile y cómo se puntuó, para no adivinar
    # cuando la sección salga vacía.
    chile_all = [it for it in items if it["region"] == "chile"]
    scores = Counter(it["score"] for it in chile_all)
    topics = Counter(it["topic"] for it in chile_all)
    print(f"  noticias chilenas detectadas: {len(chile_all)} | scores: "
          + ", ".join(f"{s}:{n}" for s, n in sorted(scores.items(), reverse=True))
          + " | temas: " + ", ".join(f"{t}:{n}" for t, n in topics.most_common()))
    for it in sorted(chile_all, key=lambda it: -it["score"])[:6]:
        print(f"    🇨🇱? [{it['score']}/{it['topic']}] {it['title'][:90]}")

    def ranked(pred):
        return sorted(
            (it for it in items if it["score"] >= MIN_TECH_SCORE and pred(it)),
            key=lambda it: -it["score"],
        )

    chile = ranked(lambda it: it["region"] == "chile" and it["topic"] in PREFERRED_TECH_TOPICS
                   and it["score"] >= MIN_TECH_CHILE_SCORE)
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
    tech_mundial = mundo[:QUOTA_TECH_MUNDIAL]
    tech_mundial += [it for it in mundo[QUOTA_TECH_MUNDIAL:QUOTA_TECH_MUNDIAL + faltan]
                     if it["score"] >= MIN_FILL_SCORE]

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
# Hallazgos IA (sección ocasional)
# ------------------------------------------------------------------
HALLAZGOS_PROMPT = """Eres un curador de novedades de inteligencia artificial. El lector trabaja en tech y quiere enterarse, de vez en cuando, de las cosas nuevas que la gente está HACIENDO con IA: herramientas, skills y plugins para agentes (Claude Code, Codex, Cursor...), agentes open source, apps, mods y proyectos que muestran algo que antes no se podía hacer. Ejemplos del tipo de cosa que busca: OpenClaw cuando apareció, o la herramienta con IA que permitió descomprimir y mezclar juegos para meter Minecraft dentro de Skyrim.

Para cada noticia devuelve:
- "hallazgo": true si es algo CONCRETO que se puede usar, probar o ver: una herramienta, repo, skill, agente, app, mod, demo o proyecto hecho con IA o para IA. false para: anuncios de modelos o de grandes empresas ("OpenAI lanza X"), financiamiento, política, regulación, seguridad e incidentes, opinión, debates, preguntas, quejas, memes, benchmarks, papers sin herramienta usable, y proyectos que no tienen que ver con IA. Excepción: un proyecto que muy probablemente se construyó con agentes de IA aunque su descripción no lo diga (p. ej. la reimplementación completa de un programa conocido, como Photoshop o Premiere, aparecida en pocas semanas; o un port o ingeniería inversa enorme hecho por una sola persona) SÍ es hallazgo: muestra lo que hoy se puede hacer con IA. Repos que parecen spam o malware (activadores, cracks, bots para raidear o farmear Discord, cheats) son false.
- "score": 1-5, qué tanto vale la pena enterarse:
  * 5: se está volviendo viral o abre una posibilidad realmente nueva; se va a hablar de esto por semanas.
  * 4: popular y útil o sorprendente; vale la pena probarlo o conocerlo.
  * 3: interesante pero de nicho, o una variación más de algo que ya existe.
  * 1-2: menor, demasiado específico, o DUPLICADO.
  Señales de popularidad (cada noticia trae una entre corchetes):
  * "[N estrellas en GitHub en D días, ~R por día]": repo nuevo. Todos los repos de la lista ya ganan al menos 500 estrellas por día, así que eso solo no basta para un 4: decide por lo que el proyecto es. 1500 o más por día es viral.
  * "[N puntos en HN, M comentarios]": 300 o más puntos es mucho; 600 o más es viral.
  Sé exigente: la mayoría de los días NINGUNA noticia debería tener 4 o más.
  DUPLICADOS: si varias noticias tratan del mismo proyecto, deja score 1 en todas menos la más completa. También score 1 si el proyecto ya está en la lista YA ENVIADOS, aunque cambie el título o la fuente, o si es otro repo de la misma familia de uno ya enviado (mismo autor, misma idea).

YA ENVIADOS (días anteriores):
{sent}

Responde SOLO con un array JSON, sin texto adicional, con un objeto por noticia en el mismo orden:
[{"id": 1, "hallazgo": true, "score": 4}, ...]

Noticias:
{items}
"""


def _link_key(link):
    return link.strip().rstrip("/").lower()


def load_memory(path, days):
    """Lo enviado en los últimos `days` días: [{title, link, date}]. Sin
    archivo (primera vez o cache perdido) es []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"  ! No pude leer {path} ({e}); empiezo sin memoria", file=sys.stderr)
        return []
    since = (datetime.now(CHILE_TZ) - timedelta(days=days)).date().isoformat()
    return [d for d in data if isinstance(d, dict) and d.get("date", "") >= since]


def save_memory(path, memory, sent):
    today = datetime.now(CHILE_TZ).date().isoformat()
    memory = memory + [{"title": it["title"], "link": it["link"], "date": today}
                       for it in sent]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=1)


def classify_hallazgos(client, items, memory):
    """Devuelve una lista de dicts {hallazgo, score} alineada con items, o
    None si la clasificación falla."""
    if not items:
        return []
    sent = "\n".join(f"- {d['title']}" for d in memory) or "(ninguno)"
    prompt = (HALLAZGOS_PROMPT.replace("{sent}", sent)
              .replace("{items}", format_items(items)))
    by_id = _classify(client, prompt)
    if by_id is None:
        return None
    result = []
    for i in range(1, len(items) + 1):
        d = by_id.get(i, {})
        try:
            score = int(d.get("score", 1) or 1)
        except (TypeError, ValueError):
            score = 1
        result.append({"hallazgo": str(d.get("hallazgo")).lower() == "true", "score": score})
    return result


def select_hallazgos(items, labels, memory, taken):
    """Hasta QUOTA_HALLAZGOS hallazgos con score >= MIN_HALLAZGO_SCORE que no
    se hayan enviado antes ni estén ya en las secciones tech (taken). De un
    autor de GitHub ya enviado no entra otro repo mientras dure la memoria."""
    sent_links = {_link_key(d.get("link", "")) for d in memory}
    sent_owners = {github_owner(d.get("link", "")) for d in memory} - {None}
    taken_links = {_link_key(it["link"]) for it in taken}
    taken_titles = {it["title"].lower() for it in taken}
    candidates = []
    for it, lab in zip(items, labels):
        if not lab["hallazgo"]:
            continue
        it = {**it, "score": lab["score"]}
        key = _link_key(it["link"])
        if key in sent_links or key in taken_links or it["title"].lower() in taken_titles:
            continue
        if github_owner(it["link"]) in sent_owners:
            continue
        candidates.append(it)
    candidates.sort(key=lambda it: -it["score"])
    # Log de calibración: si la sección sale muy seguido o nunca, mira aquí.
    print(f"  hallazgos candidatos: {len(candidates)} (vara: score >= {MIN_HALLAZGO_SCORE})")
    for it in candidates[:6]:
        print(f"    🧪? [{it['score']}] {it['title'][:90]} ({it['source']})")
    chosen, owners = [], set()
    for it in candidates:
        owner = github_owner(it["link"])
        if it["score"] < MIN_HALLAZGO_SCORE or len(chosen) == QUOTA_HALLAZGOS:
            break
        if owner and owner in owners:
            continue
        chosen.append(it)
        owners.add(owner)
    return chosen


# ------------------------------------------------------------------
# Resumen con Claude
# ------------------------------------------------------------------
def build_prompt(tech_chile, tech_mundial, general, extras, hallazgos):
    def block(title, items, instruction):
        if not items:
            return ""
        return f"\n## {title}\n({instruction})\n{format_items(items, with_link=True)}\n"

    # Con "escribe TODAS" a secas Haiku a veces se saltaba noticias (escribió
    # 2 de 5 en Tech mundial); el número explícito se respeta mejor.
    def fixed(items):
        return (f"escribe las {len(items)} noticias, TODAS, en este orden; ya están "
                "seleccionadas y no se puede omitir ninguna")
    raw = block(SECTION_TECH_CHILE, tech_chile, fixed(tech_chile))
    raw += block(SECTION_TECH_MUNDIAL, tech_mundial, fixed(tech_mundial))
    raw += block(SECTION_HALLAZGOS, hallazgos,
                 fixed(hallazgos) + "; para cada una explica qué es, qué se puede hacer con ella "
                 "y qué tan popular está (estrellas en GitHub o puntos en Hacker News, "
                 "según el CONTEXTO)")

    for section, quota in ((SECTION_CHILE, QUOTA_CHILE_GENERAL),
                           (SECTION_MUNDO, QUOTA_MUNDO_GENERAL)):
        pool = general.get(section, []) + extras.get(section, [])
        raw += block(section, pool, f"elige SOLO las {quota} más importantes; descarta el resto")

    counts = ", ".join(f"{len(items)} en {section}" for section, items in (
        (SECTION_TECH_CHILE, tech_chile), (SECTION_TECH_MUNDIAL, tech_mundial),
        (SECTION_HALLAZGOS, hallazgos)) if items)
    quota_note = (
        f"Las secciones tech ya vienen seleccionadas: escribe exactamente {counts}. "
        f"En {SECTION_CHILE} elige "
        f"{QUOTA_CHILE_GENERAL} noticias y en {SECTION_MUNDO} elige {QUOTA_MUNDO_GENERAL}: "
        "prioriza hechos de peso (política, economía, seguridad, grandes empresas) "
        "y descarta farándula, deportes, clickbait y duplicados."
    )

    return f"""Eres un editor de noticias. Abajo tienes noticias crudas de RSS del día anterior, agrupadas por sección. Cada una trae título, un contexto (extracto de la nota) y su link.

Genera un resumen diario para Telegram con estas reglas:
- Usa las secciones tal cual (mismo emoji + nombre como encabezado en <b>negrita</b>), en el mismo orden. Omite una sección solo si no tiene noticias.
- Cada noticia va en la sección donde aparece abajo. NUNCA muevas una noticia a otra sección, aunque por su contenido te parezca que encaja mejor en otra: la asignación ya está decidida.
- {quota_note}
- Cada noticia debe ir DESARROLLADA en 2-3 frases: qué pasó, el dato o detalle clave, y por qué importa o qué implica. Apóyate en el CONTEXTO provisto, no te quedes solo en el título. No inventes datos que no estén en el material.
- Formato de cada noticia: el titular en <b>negrita</b>, seguido de las frases de desarrollo, y el link entre paréntesis al final.
- IDIOMA: cada noticia trae una etiqueta IDIOMA (inglés o español). Escribe el titular Y las frases de desarrollo de esa noticia exactamente en ese idioma. NUNCA traduzcas: una noticia marcada "inglés" va completa en inglés aunque las demás del resumen estén en español, y viceversa. Los encabezados de sección van tal cual.
- Sé claro y sustancioso pero sin relleno. Empieza directo con la primera sección, sin introducción.
- Usa SOLO formato HTML de Telegram: <b>negrita</b>. NADA de markdown (nada de ** ni ##).
- Los links van como texto plano entre paréntesis, no como etiqueta <a>.
- Deja una línea en blanco entre noticias para que se lea cómodo.

Noticias crudas:
{raw}

Recordatorio final: respeta la etiqueta IDIOMA de cada noticia. Las marcadas "inglés" se escriben en inglés (titular y desarrollo); las marcadas "español", en español.
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


def send_telegram(text, header=None):
    if header is None:
        fecha = datetime.now(CHILE_TZ).strftime("%A %d/%m/%Y")
        header = f"📰 Resumen de noticias — {fecha}"
    full = f"<b>{header}</b>\n\n" + text

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
    sys.stdout.reconfigure(line_buffering=True)
    dry_run = "--dry-run" in sys.argv
    if not dry_run and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (o usa --dry-run).")

    print("Recolectando noticias...")
    tech, general, ai = collect_news()
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
    news_memory = load_memory(NEWS_MEMORY_FILE, NEWS_MEMORY_DAYS)
    print(f"Revisando repetidas ({len(news_memory)} noticias tech enviadas en los últimos "
          f"{NEWS_MEMORY_DAYS} días)...")
    mark_repeats(client, tech, labels, news_memory)
    tech_chile, tech_mundial, extras = select_tech(tech, labels)
    print(f"  seleccionadas: {len(tech_chile)} tech Chile + {len(tech_mundial)} tech mundial")
    for it in tech_chile:
        print(f"    🇨🇱 [{it['score']}/{it['topic']}] {it['title']}")
    for it in tech_mundial:
        print(f"    🌐 [{it['score']}/{it['topic']}] {it['title']}")

    # Hallazgos IA: sección opcional; si algo falla, el resumen sale sin ella.
    memory = load_memory(HALLAZGOS_MEMORY_FILE, HALLAZGOS_MEMORY_DAYS)
    hallazgos = []
    print(f"Clasificando {len(ai)} candidatos a hallazgos IA "
          f"({len(memory)} enviados en los últimos {HALLAZGOS_MEMORY_DAYS} días)...")
    try:
        ai_labels = classify_hallazgos(client, ai, memory)
    except anthropic.APIError as e:
        print(f"  ! Clasificación de hallazgos falló ({e}); sin sección.", file=sys.stderr)
        ai_labels = None
    if ai_labels is not None:
        hallazgos = select_hallazgos(ai, ai_labels, memory, tech_chile + tech_mundial)
    for it in hallazgos:
        print(f"    🧪 [{it['score']}] {it['title']}")

    # Links de Google News: resolvemos solo los que van al resumen.
    resolve_links(tech_chile + tech_mundial
                  + [it for pool in extras.values() for it in pool]
                  + [it for pool in general.values() for it in pool])

    print(f"Resumiendo con {MODEL}...")
    summary = summarize(client, build_prompt(tech_chile, tech_mundial, general, extras,
                                             hallazgos))

    # Siempre al log, para poder revisar después qué se envió.
    print("\n" + "=" * 60 + "\n" + summary + "\n" + "=" * 60)
    if dry_run:
        return
    print("Enviando a Telegram...")
    send_telegram(summary)
    # Se recuerda solo después de enviar: un dry run no "gasta" nada.
    save_memory(NEWS_MEMORY_FILE, news_memory, tech_chile + tech_mundial)
    if hallazgos:
        save_memory(HALLAZGOS_MEMORY_FILE, memory, hallazgos)
    print("Listo ✅")


if __name__ == "__main__":
    main()
