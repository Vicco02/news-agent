# 📰 Agente diario de noticias → Telegram

Resumen automático cada mañana de tech mundial (inglés), tech Chile, Chile general y mundo general (español). Corre gratis en GitHub Actions.

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
- **Fuentes**: edita el diccionario `FEEDS` en `news_agent.py`. Solo necesitas la URL del RSS.
- **Cantidad/estilo**: ajusta el prompt en `build_prompt()`.
- **Modelo**: cambia `MODEL` a `claude-sonnet-4-6` si quieres más análisis (cuesta más).

## Costo
- GitHub Actions: gratis (2000 min/mes en repos privados; esto usa ~1 min/día).
- Telegram: gratis.
- Claude Haiku: centavos al mes con ~20 noticias/día.
