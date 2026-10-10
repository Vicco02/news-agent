# 📰 Agente diario de noticias → Telegram

Resumen automático cada mañana de tech mundial (inglés), tech Chile (inglés), Chile y mundo (español). Corre gratis en GitHub Actions.

## Cómo funciona
1. Lee los feeds RSS y se queda con lo publicado en las últimas 36 horas.
2. **Clasifica** todas las noticias tech con Claude según la región REAL de la noticia (`chile` / `latam` / `global`) y el tipo de tema (`producto`, `ia`, `feature`, `empresa`, `finanzas`, `legal`, `otro`). Así "Tech Chile" solo incluye noticias que tratan de Chile, aunque vengan de un medio chileno que cubre tech global.
3. **Selecciona** con cupos fijos: 3 tech Chile, 5 tech mundial, 2 Chile, 2 mundo. Antes, Claude revisa las candidatas tech y descarta las que repiten un hecho ya enviado en los últimos 3 días o el de otra candidata del mismo día (dos notas del mismo lanzamiento, la misma noticia en inglés y en español). Si un día no hay suficiente tech chileno, la diferencia se rellena con más tech mundial. Las noticias tech de finanzas o legal/regulación no entran en las secciones tech; si son muy relevantes pasan como candidatas a las secciones generales.
4. **Hallazgos IA** 🧪 (sección ocasional): de un pool aparte (repos de GitHub creados en las últimas 2 semanas que ganan al menos 500 estrellas por día, uno por autor, y lo más votado de Hacker News) Claude detecta herramientas, skills, agentes y proyectos que la gente está construyendo con IA (también los que muy probablemente se hicieron con IA aunque no lo digan, como un clon completo de Photoshop) y los puntúa por novedad y popularidad. Solo entran los de score ≥ 4, máximo 2, así que la mayoría de los días la sección no debería aparecer. Lo ya enviado se recuerda 21 días, y de un autor ya enviado no entra otro repo en ese plazo (para que una familia de repos parecidos no ocupe la sección varios días).
5. **Redacta** el resumen con Claude y lo envía a Telegram.

## Resumen semanal de videojuegos 🎮
Además del diario, `gaming_agent.py` manda los lunes a las 09:00 de Chile (ver "Hora de envío") un resumen de la semana anterior (lunes a domingo) para un jugador de PlayStation, Nintendo y PC. Usa los mismos secrets y el mismo chat.

1. Lee feeds de medios de videojuegos (Eurogamer, PC Gamer, Push Square, Nintendo Life, PlayStation Blog, Steam, etc.) y búsquedas de Google News en español para ofertas y juegos gratis.
2. **Clasifica** cada noticia en lotes con Claude: tipo (`lanzamiento`, `actualizacion`, `review`, `oferta`, `gratis`, `hardware`, `industria`, `otro`), plataforma y relevancia 1-5. Lo exclusivo de Xbox o móvil, los rumores y las filtraciones quedan fuera.
3. **Arma cinco secciones** con cupos: 🎮 Lanzamientos y anuncios (4), 🔧 Actualizaciones y DLC (3), ⭐ Reviews destacadas (3), 🛒 Ofertas y juegos gratis (4), 🕹️ Consolas, tiendas e industria (2).
4. **Redacta** todo en español y lo envía a Telegram.

- **Probar sin enviar**: en Actions → "Resumen semanal de videojuegos" → "Run workflow" con `dry_run` marcado. El input `days` acota la ventana (p. ej. `3`) para probar entre semana.
- **Ajustar**: `FEEDS`, `SECTIONS` (cupos), `MIN_SCORE` y los prompts están en `gaming_agent.py`.

## Setup (una sola vez, ~10 minutos)

### 1. Crear el bot de Telegram
1. Abre Telegram y busca **@BotFather**.
2. Envía `/newbot`, elige nombre y username. Te dará un **token** tipo `123456:ABC-DEF...`. Guárdalo.
3. Búscate a ti mismo: abre tu bot recién creado y mándale cualquier mensaje (ej: "hola").

### 2. Obtener tu CHAT_ID
1. En el navegador, abre (reemplaza TU_TOKEN):
   `https://api.telegram.org/botTU_TOKEN/getUpdates`
2. Busca `"chat":{"id":XXXXXXXX`. Ese número es tu **TELEGRAM_CHAT_ID**.
   - Si sale vacío, mándale otro mensaje al bot y recarga.

### 3. API key de Anthropic
1. Entra a https://console.anthropic.com/ → **API Keys** → crea una.
2. Carga saldo (con USD 5 te dura muchísimos meses con Haiku).

### 4. Subir a GitHub
1. Crea un repo **privado** en GitHub (ej: `news-agent`).
2. Sube estos archivos (o con git):
   ```bash
   git init
   git add .
   git commit -m "news agent"
   git branch -M main
   git remote add origin git@github.com:TU_USUARIO/news-agent.git
   git push -u origin main
   ```

### 5. Configurar los secrets
En el repo → **Settings → Secrets and variables → Actions → New repository secret**. Crea 3:
- `ANTHROPIC_API_KEY`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

### 6. Probar
1. Ve a la pestaña **Actions** del repo.
2. Selecciona "Resumen diario de noticias" → **Run workflow**.
3. En ~1 min deberías recibir el mensaje en Telegram.

## Personalización
- **Hora de envío**: los crons de GitHub salen en este repo con 5 a 9 horas de atraso, variable, así que no sirven para una hora fija. El disparo principal es un cron externo que llama a la API de GitHub (`repository_dispatch`), que parte en segundos. Los `cron` de los workflows quedan de respaldo: si ese día el disparo externo ya envió el resumen, el job `ya-enviado` lo detecta y la corrida no hace nada; si no, envía (tarde, a mediodía). Configuración en [cron-job.org](https://cron-job.org) (gratis), una sola vez:
  1. En GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate. Repositorio: solo `news-agent`. Permiso: **Contents: Read and write** (es el que exige `repository_dispatch`). Expiración: la más larga que permita; anota cuándo vence.
  2. En cron-job.org crea un cronjob:
     - URL: `https://api.github.com/repos/Vicco02/news-agent/dispatches`
     - Horario: todos los días a las 09:00, zona horaria **America/Santiago** (así el cambio de hora se maneja solo).
     - En "Advanced": método **POST**, headers `Authorization: Bearer TU_TOKEN`, `Accept: application/vnd.github+json` y `Content-Type: application/json`, y body `{"event_type": "resumen-diario"}`.
  3. Crea otro igual para el semanal: solo los lunes a las 09:00, con body `{"event_type": "resumen-semanal"}`.
  4. Prueba con "Test run" en cron-job.org: debe responder HTTP 204 y aparecer una corrida en Actions.

  Si el token vence, cron-job.org empieza a recibir 401 (avisa por correo) y los resúmenes siguen llegando por el respaldo, tarde.
- **Fuentes**: edita `TECH_FEEDS` (un solo pool; la región la decide el clasificador) y `GENERAL_FEEDS` en `news_agent.py`. Solo necesitas la URL del RSS. `google_news("consulta")` genera un feed de búsqueda de Google News Chile, útil para captar tech local por contenido.
- **Cantidad por sección**: cambia `QUOTA_TECH_CHILE`, `QUOTA_TECH_MUNDIAL`, `QUOTA_CHILE_GENERAL`, `QUOTA_MUNDO_GENERAL`.
- **Calidad de Tech Chile**: `MIN_TECH_CHILE_SCORE` (por defecto 3) filtra las noticias de esa sección; el clasificador puntúa lo chileno con la vara del ecosistema local y manda entrevistas y columnas a "otro". Si no hay suficientes, el cupo que falte se rellena con tech mundial, pero solo con noticias de score ≥ `MIN_FILL_SCORE` (3). El log de cada corrida muestra cuántas noticias chilenas se detectaron y con qué scores.
- **Idioma**: cada noticia se redacta en el idioma de su fuente (detectado automáticamente) y se le indica a Claude con una etiqueta `IDIOMA`. Las de TechCrunch/The Verge salen en inglés; las de medios chilenos, en español.
- **Links de Google News**: los links de redirección (`news.google.com/rss/articles/...`) se convierten a la URL real de la nota antes de redactar. Si Google cambia el mecanismo, queda el link largo original y se avisa en el log.
- **Qué temas tech entran**: `PREFERRED_TECH_TOPICS` (van a las secciones tech) y `GENERAL_TECH_TOPICS` (van a las generales si son relevantes).
- **Criterios de clasificación**: ajusta `CLASSIFY_PROMPT`. **Estilo del resumen**: ajusta el prompt en `build_prompt()`.
- **Hallazgos IA**: fuentes en `fetch_github_new()` (`GITHUB_NEW_DAYS`, `GITHUB_MIN_STARS`, y el ritmo mínimo `GITHUB_MIN_STARS_PER_DAY`, que es lo que más define qué tan seguido sale la sección) y `HN_SEARCHES`, vara en `MIN_HALLAZGO_SCORE` (4) y cupo en `QUOTA_HALLAZGOS` (2); el criterio está en `HALLAZGOS_PROMPT`. El log muestra los candidatos con su score (`🧪?`) para calibrar si sale muy seguido o nunca. La memoria de lo enviado vive en `hallazgos_enviados.json`, que en Actions se conserva entre corridas con `actions/cache`; si se pierde, a lo más se repite un hallazgo. Un dry run no la actualiza.
- **Noticias repetidas**: las noticias tech enviadas se recuerdan `NEWS_MEMORY_DAYS` (3) días en `noticias_enviadas.json`, con el mismo mecanismo de cache. El criterio está en `REPEATS_PROMPT`; el log marca con 🔁 lo que se descartó por repetido.
- **Revisar lo enviado**: el resumen completo queda impreso en el log de cada corrida, también en las que se envían a Telegram.
- **Probar sin enviar**: `python news_agent.py --dry-run` imprime el resumen en consola (solo necesita `ANTHROPIC_API_KEY`).
- **Verificar un feed nuevo**: `python probe_feeds.py URL` dice si es RSS válido y muestra sus titulares. El agente también registra en el log de cada corrida el estado HTTP y la cantidad de entradas por feed.
- **Medios sin RSS o que bloquean bots** (Emol, DF): se leen con `google_news("site:emol.com tecnología")`.
- **Modelo**: cambia `MODEL` a `claude-sonnet-4-6` si quieres más análisis (cuesta más).

## Costo
- GitHub Actions: gratis (ilimitado en repos públicos; en privados, 2000 min/mes y esto usa ~2 min/día).
- Telegram: gratis.
- Claude Haiku: centavos al mes (cuatro llamadas por día: clasificar tech, revisar repetidas, clasificar hallazgos IA y redactar).
