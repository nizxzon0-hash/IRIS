# Stock News Discord Bot

Bot que monitorea acciones (tickers) y publica noticias nuevas en un canal
de Discord cada hora, con comandos para administrar la lista.

## Comandos

- `/add TICKER` — agrega una acción a la lista (ej: `/add AAPL`)
- `/remove TICKER` — la quita de la lista
- `/list` — muestra las acciones monitoreadas
- `/checknow` — fuerza una revisión inmediata (sin esperar la hora)
- `/sentiment TICKER` — muestra el resumen de sentimiento al toque, sin
  esperar al chequeo horario
- `/opinion TICKER` — le pide a la IA una lectura de las noticias
  recientes, al toque

## Fuentes de noticias

- **Finnhub** — titulares y links de noticias por empresa.
- **Alpha Vantage** (opcional pero recomendado) — además de titulares,
  trae un puntaje de sentimiento por noticia. Con esto el bot arma un
  resumen tipo "🔺 Señales alcistas — posiblemente por X motivo" o
  "🔻 Señales bajistas — posiblemente por Y motivo", agregando el
  sentimiento de las noticias recientes de cada ticker.

  ⚠️ **Importante:** esto es una heurística basada en el tono de la
  cobertura de noticias reciente, no una predicción financiera. Ninguna
  señal de sentimiento garantiza hacia dónde se va a mover el precio —
  úsalo como un dato más, no como una recomendación de inversión.

  El plan gratuito de Alpha Vantage tiene un límite de 25 consultas por
  día, así que si monitoreás muchos tickers a la vez podés llegar al
  límite; en ese caso el bot simplemente omite el resumen de sentimiento
  para esa vuelta y sigue funcionando con las noticias de Finnhub.

- **IA (opcional)** — cada 5 horas, si hay noticias nuevas, el bot le
  pasa los titulares recientes de cada ticker a la API de Claude y pide
  un párrafo breve explicando qué factores podrían presionar el precio
  al alza o a la baja. El prompt está armado explícitamente para que
  **no** dé predicciones categóricas ni recomendaciones de compra/venta
  — es una lectura de contexto, no una señal de trading.

  Para activarlo necesitás una cuenta en https://console.anthropic.com
  (distinta de tu cuenta normal de claude.ai) y generar una API key ahí.
  Esto tiene costo por uso (facturado por Anthropic), aunque para el
  volumen de este bot es un gasto mínimo. Configurá `ANTHROPIC_API_KEY`
  para activarlo; sin esa variable, el bot simplemente no manda esta
  parte y sigue funcionando igual con noticias + sentimiento.

## 1. Crear el bot en Discord

1. Andá a https://discord.com/developers/applications → **New Application**.
2. En **Bot**, creá un bot y copiá el **Token** (esto es `DISCORD_TOKEN`).
3. En **Bot**, activá el permiso *Send Messages* (no hace falta ningún
   Privileged Intent para este bot).
4. En **OAuth2 → URL Generator**, marcá los scopes `bot` y `applications.commands`,
   y en permisos marcá `Send Messages` y `Embed Links`. Abrí la URL generada
   e invitá el bot a tu servidor.
5. Con "modo desarrollador" activado en Discord (Configuración → Avanzado),
   click derecho sobre el canal donde querés las noticias → **Copiar ID**.
   Eso es `DISCORD_CHANNEL_ID`.

## 2. Obtener API keys de noticias

**Finnhub (obligatorio, titulares):**
1. Creá una cuenta en https://finnhub.io/register
2. Copiá tu API key del dashboard → esto es `FINNHUB_API_KEY`.
   El plan gratuito alcanza sin problema para consultas cada hora.

**Alpha Vantage (opcional, sentimiento):**
1. Pedí una key gratis en https://www.alphavantage.co/support/#api-key
2. Esto es `ALPHAVANTAGE_API_KEY`. Si no la configurás, el bot sigue
   funcionando normal, solo que sin el resumen de sentimiento ni el
   comando `/sentiment`.

## 3. Probarlo localmente (opcional)

```bash
pip install -r requirements.txt

export DISCORD_TOKEN="tu_token"
export DISCORD_CHANNEL_ID="1234567890"
export FINNHUB_API_KEY="tu_api_key"
export ALPHAVANTAGE_API_KEY="tu_api_key"  # opcional
export ANTHROPIC_API_KEY="tu_api_key"     # opcional

python bot.py
```

## 4. Desplegarlo gratis (para que quede corriendo 24/7)

**Opción recomendada: Railway**

1. Subí esta carpeta a un repo de GitHub (puede ser privado).
2. Andá a https://railway.app → **New Project → Deploy from GitHub repo**.
3. Elegí el repo. Railway detecta el `Procfile` y arranca el worker solo.
4. En **Variables**, agregá `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`,
   `FINNHUB_API_KEY` y, si los usás, `ALPHAVANTAGE_API_KEY` y
   `ANTHROPIC_API_KEY`.
5. Railway te da créditos gratis mensuales que alcanzan de sobra para un
   bot chico como este.

**Alternativa: Fly.io** — funciona parecido, usando `fly launch` y
`fly secrets set` para las variables de entorno en vez del panel web.

## Cómo funciona

- Los tickers se guardan en `tickers.json` (se crea solo al usar `/add`).
- Cada hora, el bot revisa noticias de las últimas 2 horas por cada ticker
  vía Finnhub y publica solo las que no mandó antes (registradas en
  `seen_news.json`).
- `/checknow` corre esa misma revisión al toque, útil para probar que todo
  funciona sin esperar una hora entera.

## Nota sobre el almacenamiento

Los archivos `tickers.json` y `seen_news.json` viven en el disco del
hosting. En Railway/Fly.io con un solo servicio esto persiste entre
reinicios normales, pero si el proyecto crece te conviene migrar a una
base de datos (ej. SQLite con un volumen persistente, o Postgres gratis
de Railway) para no depender del filesystem.
