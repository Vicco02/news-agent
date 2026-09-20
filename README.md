# 📰 Agente diario de noticias → Telegram

Resumen automático cada mañana de tech mundial (inglés), tech Chile (inglés), Chile y mundo (español). Corre gratis en GitHub Actions.

## Cómo funciona
1. Lee los feeds RSS y se queda con lo publicado en las últimas 36 horas.
2. **Clasifica** todas las noticias tech con Claude según la región REAL de la noticia (`chile` / `latam` / `global`) y el tipo de tema (`producto`, `ia`, `feature`, `empresa`, `finanzas`, `legal`, `otro`). Así "Tech Chile" solo incluye noticias que tratan de Chile, aunque vengan de un medio chileno que cubre tech global.
3. **Selecciona** con cupos fijos: 3 tech Chile, 5 tech mundial, 2 Chile, 2 mundo. Si un día no hay suficiente tech chileno, la diferencia se rellena con más tech mundial. Las noticias tech de finanzas o legal/regulación no entran en las secciones tech; si son muy relevantes pasan como candidatas a las secciones generales.
4. **Redacta** el resumen con Claude y lo envía a Telegram.

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
- **Hora de envío**: edita el `cron` en `.github/workflows/daily.yml`. Está en UTC. 11:00 UTC ≈ 08:00 Chile verano.
- **Fuentes**: edita `TECH_FEEDS` (un solo pool; la región la decide el clasificador) y `GENERAL_FEEDS` en `news_agent.py`. Solo necesitas la URL del RSS. `google_news("consulta")` genera un feed de búsqueda de Google News Chile, útil para captar tech local por contenido.
- **Cantidad por sección**: cambia `QUOTA_TECH_CHILE`, `QUOTA_TECH_MUNDIAL`, `QUOTA_CHILE_GENERAL`, `QUOTA_MUNDO_GENERAL`.
- **Calidad de Tech Chile**: `MIN_TECH_CHILE_SCORE` (por defecto 4) exige que las noticias de esa sección sean de peso; si no hay suficientes, el cupo que falte se rellena con tech mundial, pero solo con noticias de score ≥ `MIN_FILL_SCORE` (3). Baja `MIN_TECH_CHILE_SCORE` a 3 si prefieres la sección siempre llena.
- **Idioma**: cada noticia se redacta en el idioma de su fuente (detectado automáticamente) y se le indica a Claude con una etiqueta `IDIOMA`. Las de TechCrunch/The Verge salen en inglés; las de medios chilenos, en español.
- **Links de Google News**: los links de redirección (`news.google.com/rss/articles/...`) se convierten a la URL real de la nota antes de redactar. Si Google cambia el mecanismo, queda el link largo original y se avisa en el log.
- **Qué temas tech entran**: `PREFERRED_TECH_TOPICS` (van a las secciones tech) y `GENERAL_TECH_TOPICS` (van a las generales si son relevantes).
- **Criterios de clasificación**: ajusta `CLASSIFY_PROMPT`. **Estilo del resumen**: ajusta el prompt en `build_prompt()`.
- **Probar sin enviar**: `python news_agent.py --dry-run` imprime el resumen en consola (solo necesita `ANTHROPIC_API_KEY`).
- **Verificar un feed nuevo**: `python probe_feeds.py URL` dice si es RSS válido y muestra sus titulares. El agente también registra en el log de cada corrida el estado HTTP y la cantidad de entradas por feed.
- **Medios sin RSS o que bloquean bots** (Emol, DF): se leen con `google_news("site:emol.com tecnología")`.
- **Modelo**: cambia `MODEL` a `claude-sonnet-4-6` si quieres más análisis (cuesta más).

## Costo
- GitHub Actions: gratis (2000 min/mes en repos privados; esto usa ~1 min/día).
- Telegram: gratis.
- Claude Haiku: centavos al mes (dos llamadas por día: clasificar + redactar).
