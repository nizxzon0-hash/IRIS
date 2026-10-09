# Iris — bot de Discord para vigilar acciones

Iris vigila tu lista de acciones, publica noticias cada hora, te avisa
cuando el precio sube o baja más de un umbral y responde con personalidad.

> Nada de lo que publica Iris es asesoramiento financiero. Las alertas, el
> sentimiento y las lecturas de IA son datos de contexto, no predicciones.

## Comandos

**Mi lista** (aceptan varios símbolos separados por coma)
- `/add AAPL, NVDA` — agrega acciones y avisa si Finnhub no tiene datos de alguna
- `/remove AAPL` — las quita (con autocompletado)
- `/list` — muestra la lista

**Precios y mercado** (Finnhub)
- `/precio AAPL` — precio, variación del día, apertura, máx. y mín.
- `/resumen` — tabla de toda tu lista con 📈/📉, mejor y peor del día
- `/humor` — cómo se siente Iris hoy según el promedio de tu lista
- `/mercado` — titulares generales del mercado

**Alertas y control** (no necesitan nada extra)
- `/umbral 5` — % de movimiento diario que dispara una alerta (por defecto 3)
- `/alertas activar|desactivar` — enciende o apaga las alertas de precio
- `/pausa` y `/reanudar` — pausa o reactiva todos los mensajes automáticos
- `/estado` — modo, umbral, última revisión, tiempo en línea y fuentes activas
- `/checknow` — fuerza una revisión de noticias (últimas 24 h) y explica qué encontró

**Opcionales**
- `/sentiment AAPL` — sentimiento de las noticias (necesita Alpha Vantage)
- `/opinion AAPL` — lectura de IA de las noticias (necesita API de Anthropic)

**Para divertirse**
- `/oraculo ¿pregunta?` — respuestas al azar de Iris. Es un juego, no una señal.
- `/ayuda` — resumen de todo lo anterior

## Qué publica solo

- **Cada hora:** noticias nuevas de cada acción (Finnhub) y, si configuraste
  Alpha Vantage, un resumen de sentimiento con 🔺/🔻.
- **Cada 15 minutos:** compara el cambio diario de cada acción contra el
  umbral. Si lo supera, publica una alerta 📈/📉. No repite la misma alerta;
  vuelve a avisar solo si el movimiento se duplica, cambia de signo o es otro día.
- **Cada 5 horas (opcional):** lectura de IA de las noticias recientes.

## Importante: cobertura de Finnhub

El plan gratuito de Finnhub solo cubre acciones de **EE.UU. y Canadá**.
Para acciones de otras bolsas (Tokio, Corea, Milán...) no habrá precio,
noticias ni alertas. `/add` te avisa cuando pasa esto.

## 1. Crear el bot en Discord

1. Andá a https://discord.com/developers/applications → **New Application**.
2. En **Bot**, copiá el **Token** (`DISCORD_TOKEN`). Si lo pegás en algún
   lado por error, usá **Reset Token** y cargá el nuevo.
3. En **OAuth2 → URL Generator**, marcá los scopes `bot` y
   `applications.commands`, y los permisos `Send Messages` y `Embed Links`.
   Abrí la URL e invitá el bot a tu servidor.
4. Con el modo desarrollador activado (Configuración → Avanzado), clic
   derecho sobre el canal → **Copiar ID de canal** (`DISCORD_CHANNEL_ID`).
   Ojo: es el ID del **canal**, no el del bot.

## 2. API keys

- **Finnhub (obligatoria):** https://finnhub.io/register → `FINNHUB_API_KEY`.
  Gratis, sin tarjeta.
- **Alpha Vantage (opcional):** https://www.alphavantage.co/support/#api-key
  → `ALPHAVANTAGE_API_KEY`. El plan gratuito permite unas 25 consultas por día.
- **Anthropic (opcional):** https://console.anthropic.com → `ANTHROPIC_API_KEY`.
  Se cobra por uso.

## 3. Variables de entorno

| Variable | Obligatoria | Para qué |
|---|---|---|
| `DISCORD_TOKEN` | sí | Token del bot |
| `DISCORD_CHANNEL_ID` | sí | Canal donde publica Iris |
| `FINNHUB_API_KEY` | sí | Precios y noticias |
| `ALPHAVANTAGE_API_KEY` | no | Sentimiento |
| `ANTHROPIC_API_KEY` | no | Lectura de IA |
| `INITIAL_TICKERS` | no | Lista inicial, ej: `AAPL,NVDA,TSLA` |
| `DATA_DIR` | no | Carpeta de los archivos guardados, ej: `/data` |

## 4. Probarlo localmente

```bash
pip install -r requirements.txt

export DISCORD_TOKEN="tu_token"
export DISCORD_CHANNEL_ID="1234567890"
export FINNHUB_API_KEY="tu_api_key"

python bot.py
```

## 5. Desplegarlo en Railway

1. Subí los cuatro archivos (`bot.py`, `requirements.txt`, `Procfile`,
   `README.md`) a un repo de GitHub.
2. En https://railway.app → **New Project → Deploy from GitHub repo**.
3. En **Variables**, cargá las de la tabla de arriba.
4. Railway detecta el `Procfile` y arranca el bot.

Al actualizar `bot.py` en GitHub, Railway redespliega solo. Si los
comandos nuevos no aparecen enseguida en Discord, esperá unos minutos o
reiniciá la app de Discord.

## 6. Que no se pierdan tus datos

Los archivos `tickers.json`, `settings.json` y `seen_news.json` viven en
el disco del servicio. En Railway, sin un volumen, ese disco se borra en
cada redespliegue, y perderías la lista, el umbral y la pausa. Dos formas
de evitarlo:

- **Gratis:** cargá `INITIAL_TICKERS` con tu lista. Si el archivo no
  existe, Iris arranca con esa lista. (El umbral y la pausa sí se pierden.)
- **Completa:** agregá un Volume al servicio con punto de montaje `/data`
  y cargá `DATA_DIR=/data`. Tiene un costo pequeño por GB al mes.
