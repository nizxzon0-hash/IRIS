"""
Iris — bot de Discord para vigilar acciones
-------------------------------------------
Vigila una lista de acciones (tickers), publica noticias cada hora, avisa
cuando el precio sube o baja más de un umbral y responde comandos con
personalidad propia.

Requisitos:
    pip install -r requirements.txt

Variables de entorno (ver README.md):
    DISCORD_TOKEN         token del bot de Discord (obligatoria)
    DISCORD_CHANNEL_ID    ID del canal donde Iris publica (obligatoria)
    FINNHUB_API_KEY       API key gratuita de https://finnhub.io (obligatoria)
    ALPHAVANTAGE_API_KEY  resumen de sentimiento (opcional)
    ANTHROPIC_API_KEY     lectura de IA cada 5 horas (opcional)
    DATA_DIR              carpeta donde se guardan los archivos JSON (opcional;
                          en Railway apuntala a un volumen, ej: /data)
    INITIAL_TICKERS       lista inicial separada por comas (opcional; se usa
                          solo si todavía no existe la lista guardada)

Importante: nada de lo que publica Iris es asesoramiento financiero. El
sentimiento, las alertas y las lecturas de IA son datos de contexto, no
predicciones ni recomendaciones.
"""

import os
import re
import json
import random
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("iris")

# =====================================================================
# Configuración
# =====================================================================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")  # opcional
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # opcional
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5")

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
TICKERS_FILE = os.path.join(DATA_DIR, "tickers.json")
SEEN_FILE = os.path.join(DATA_DIR, "seen_news.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

CHECK_INTERVAL_HOURS = 1  # noticias
AI_OPINION_INTERVAL_HOURS = 5  # lectura de IA
PRICE_CHECK_MINUTES = 15  # alertas de subida/bajada
MAX_ADD_CHECKS = 10  # cuántos tickers nuevos verifica /add contra Finnhub

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
AI_TIMEOUT = aiohttp.ClientTimeout(total=30)

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
BOT_START = datetime.now(timezone.utc)

DEFAULT_SETTINGS = {
    "threshold": 3.0,  # % de cambio diario que dispara una alerta
    "alerts": True,  # alertas de precio activadas
    "paused": False,  # pausa todos los mensajes automáticos
    "alerted": {},  # estado interno para no repetir alertas
    "last_news_check": None,
}

AI_SYSTEM_PROMPT = (
    "Sos un analista que resume noticias bursátiles para un canal privado de Discord. "
    "Dado un listado de titulares recientes sobre una acción, escribí un párrafo breve "
    "(máximo 4-5 oraciones, en español) que explique qué está pasando y qué factores "
    "podrían presionar el precio al alza o a la baja, y por qué. "
    "No des una predicción categórica de si el precio va a subir o bajar, no des precios "
    "objetivo, y no des recomendaciones de compra/venta. Hablá en términos de factores y "
    "posibilidades, no de certezas. Si los titulares no alcanzan para decir nada útil, "
    "decilo directamente en vez de inventar contenido."
)

# Umbrales para clasificar el sentimiento agregado (misma escala que Alpha Vantage)
SENTIMENT_BUCKETS = [
    (0.35, "🔺 Señales alcistas"),
    (0.15, "↗️ Señales levemente positivas"),
    (-0.15, "➖ Sentimiento neutral / mixto"),
    (-0.35, "↘️ Señales levemente negativas"),
    (float("-inf"), "🔻 Señales bajistas"),
]

# =====================================================================
# Personalidad de Iris (frases originales)
# =====================================================================
ADD_OK = [
    "¡Anotado! 📝 Desde ahora le echo el ojo a {x}.",
    "Listo, {x} ya está en mi radar. 👀",
    "Trato hecho: vigilo {x}. ✨",
]
ADD_DUP = "Ya conocía a {x}, pero gracias por insistir. 😄"
REMOVE_OK = [
    "Chau {x}, fue lindo mientras duró. 👋",
    "Listo, dejo de vigilar {x}. 🗑️",
    "{x} salió de mi lista. Sin rencores. 🫡",
]
EMPTY_LIST = [
    "Mi lista está vacía y me aburro. 🫥 Probá con `/add AAPL`.",
    "No estoy vigilando nada todavía. Dame un ticker con `/add`. 🔭",
]
UP_LINES = [
    "¡Mirá cómo sube! 🚀",
    "Alguien amaneció de buen humor. ✨",
    "Se está moviendo, y para arriba. 👀",
    "Verde por todos lados. 🌱",
]
DOWN_LINES = [
    "Uf, se está cayendo. Respirá hondo. 🫠",
    "Día complicado para este ticker. 🌧️",
    "Rojo en pantalla. Ojo con las decisiones apuradas. 🧯",
    "Bajó fuerte; todavía no sabemos si es ruido o algo más. 🔍",
]
ORACLE_ANSWERS = [
    "Los astros dicen que sí… pero los astros nunca leyeron un balance. 🔮",
    "Mi bola de cristal está actualizándose. Probá de nuevo en un rato. 🌀",
    "Todo apunta a que sí. Todo, menos la realidad. 😅",
    "Rotundamente no. Aunque yo también me equivoqué de ticker alguna vez. 🙈",
    "Quizás. Yo miraría los fundamentos antes que una bola de cristal. 🧐",
    "Sí, sí, sí… (son las hojas de té, no me hagas caso). 🍵",
    "No me convence. Pero soy un bot: mi opinión vale lo que una moneda al aire. 🪙",
    "Pregunta interesante, respuesta aburrida: depende. 😌",
    "Las señales son mixtas, como mi playlist. 🎧",
    "¡Qué audacia! Que te responda tu yo del futuro, que seguro sabe más. ⏳",
]
MOODS = [
    (2.0, "🤩 Eufórica", "Hoy el tablero está de fiesta. Yo contenta, pero con los pies en la tierra."),
    (0.5, "😊 Contenta", "Más verde que rojo. Buen día para ser una bot vigilante."),
    (-0.5, "😌 Tranquila", "Todo bastante parejo. Un día de esos en que no pasa nada, y está bien."),
    (-2.0, "😟 Preocupada", "Más rojo que verde. Nada de pánico: mirá el cuadro completo."),
    (float("-inf"), "🫠 En modo drama", "Hoy el tablero duele un poco. Respirá, tomá agua y no decidas con calor."),
]
FOOTER_NOT_ADVICE = "Iris no da asesoramiento financiero — son datos de contexto."


# =====================================================================
# Almacenamiento simple en JSON
# =====================================================================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("No pude leer %s: %s", path, e)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_tickers():
    if not os.path.exists(TICKERS_FILE):
        seed = [
            t.strip().upper()
            for t in os.environ.get("INITIAL_TICKERS", "").split(",")
            if t.strip()
        ]
        if seed:
            save_json(TICKERS_FILE, seed)
            return seed
        return []
    return load_json(TICKERS_FILE, [])


def add_ticker(ticker):
    tickers = get_tickers()
    ticker = ticker.upper()
    if ticker not in tickers:
        tickers.append(ticker)
        save_json(TICKERS_FILE, tickers)
        return True
    return False


def remove_ticker(ticker):
    tickers = get_tickers()
    ticker = ticker.upper()
    if ticker in tickers:
        tickers.remove(ticker)
        save_json(TICKERS_FILE, tickers)
        return True
    return False


def get_seen_ids():
    return set(load_json(SEEN_FILE, []))


def save_seen_ids(seen_ids):
    # Guardamos solo los últimos 500 para que el archivo no crezca sin límite
    save_json(SEEN_FILE, list(seen_ids)[-500:])


def get_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # copia profunda
    settings.update(load_json(SETTINGS_FILE, {}))
    return settings


def update_settings(**changes):
    settings = get_settings()
    settings.update(changes)
    save_json(SETTINGS_FILE, settings)
    return settings


# =====================================================================
# Helpers de formato
# =====================================================================
def arrow(dp):
    if dp is None:
        return "➖"
    return "📈" if dp > 0 else "📉" if dp < 0 else "➖"


def fmt_pct(dp):
    return "s/d" if dp is None else f"{dp:+.2f}%"


def fmt_price(value):
    return "s/d" if value is None else f"${value:,.2f}"


def trend_color(dp):
    if dp is None or dp == 0:
        return discord.Color.greyple()
    return discord.Color.green() if dp > 0 else discord.Color.red()


def human_delta(delta):
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def split_tickers(text):
    return [t.strip().upper() for t in re.split(r"[,\s;]+", text) if t.strip()]


# =====================================================================
# Fuentes de datos
# =====================================================================
async def finnhub_get(session, path, params, errors=None, label=""):
    """GET a Finnhub con manejo de errores. Devuelve el JSON o None."""
    def report(msg):
        log.warning(msg)
        if errors is not None:
            errors.append(msg)

    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"https://finnhub.io/api/v1/{path}?{query}&token={FINNHUB_API_KEY}"
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                report(f"Finnhub devolvió {resp.status}{(' para ' + label) if label else ''}")
                return None
            return await resp.json()
    except Exception as e:
        report(f"Error consultando Finnhub{(' para ' + label) if label else ''}: {e}")
        return None


async def fetch_quote(session, ticker, errors=None):
    """Precio actual y variación diaria (Finnhub). None si no hay datos."""
    q = await finnhub_get(session, "quote", {"symbol": ticker}, errors, ticker)
    if not isinstance(q, dict):
        return None
    # Finnhub devuelve todo en cero cuando no conoce el símbolo
    if not q.get("c") and not q.get("pc"):
        return None
    return q


async def fetch_news(session, ticker, lookback_hours=CHECK_INTERVAL_HOURS + 1, errors=None):
    """Noticias recientes de un ticker desde Finnhub."""
    to_date = datetime.now(timezone.utc).date()
    from_date = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    data = await finnhub_get(
        session, "company-news",
        {"symbol": ticker, "from": from_date, "to": to_date},
        errors, ticker,
    )
    if data is None:
        return []
    if not isinstance(data, list):
        if errors is not None:
            errors.append(f"Respuesta inesperada de Finnhub para {ticker}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    return [
        a for a in data
        if datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc) >= cutoff
    ]


async def fetch_market_news(session, limit=6, errors=None):
    """Titulares generales del mercado (Finnhub)."""
    data = await finnhub_get(session, "news", {"category": "general"}, errors, "mercado")
    if not isinstance(data, list):
        return []
    return data[:limit]


async def fetch_alphavantage_sentiment(session, ticker, lookback_hours=CHECK_INTERVAL_HOURS + 1):
    """Noticias + puntaje de sentimiento por artículo desde Alpha Vantage."""
    if not ALPHAVANTAGE_API_KEY:
        return []

    url = (
        "https://www.alphavantage.co/query"
        f"?function=NEWS_SENTIMENT&tickers={quote_plus(ticker)}&apikey={ALPHAVANTAGE_API_KEY}"
    )
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("Alpha Vantage devolvió %s para %s", resp.status, ticker)
                return []
            data = await resp.json()
    except Exception as e:
        log.error("Error consultando Alpha Vantage para %s: %s", ticker, e)
        return []

    feed = data.get("feed", [])
    if not feed:
        # Alpha Vantage avisa el límite diario con un dict "Note"/"Information"
        if "Note" in data or "Information" in data:
            log.warning("Alpha Vantage: %s", data.get("Note") or data.get("Information"))
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent = []
    for item in feed:
        try:
            published = datetime.strptime(item["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if published < cutoff:
            continue

        ticker_score = None
        for ts in item.get("ticker_sentiment", []):
            if ts.get("ticker") == ticker:
                ticker_score = float(ts.get("ticker_sentiment_score", 0))
                break

        recent.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "source": item.get("source"),
            "published": published,
            "score": ticker_score if ticker_score is not None else float(item.get("overall_sentiment_score", 0)),
        })
    return recent


async def fetch_ai_opinion(session, ticker, headlines):
    """Le pide a la API de Claude un resumen ponderado en base a titulares recientes."""
    if not ANTHROPIC_API_KEY or not headlines:
        return None

    headlines_text = "\n".join(f"- {h}" for h in headlines[:15])
    payload = {
        "model": AI_MODEL,
        "max_tokens": 300,
        "system": AI_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Ticker: {ticker}\n\nTitulares recientes:\n{headlines_text}"}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=AI_TIMEOUT
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("Anthropic API devolvió %s para %s: %s", resp.status, ticker, body[:200])
                return None
            data = await resp.json()
    except Exception as e:
        log.error("Error consultando la API de Claude para %s: %s", ticker, e)
        return None

    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip() or None


# =====================================================================
# Embeds
# =====================================================================
def build_embed(ticker, article):
    embed = discord.Embed(
        title=article.get("headline", "Sin título")[:256],
        url=article.get("url"),
        description=(article.get("summary") or "")[:300],
        color=discord.Color.blue(),
        timestamp=datetime.fromtimestamp(article.get("datetime", 0), tz=timezone.utc),
    )
    embed.set_author(name=f"📰 ${ticker}")
    if article.get("source"):
        embed.set_footer(text=article["source"])
    if article.get("image"):
        embed.set_thumbnail(url=article["image"])
    return embed


def classify_sentiment(avg_score):
    for threshold, label in SENTIMENT_BUCKETS:
        if avg_score >= threshold:
            return label
    return SENTIMENT_BUCKETS[-1][1]


def build_sentiment_embed(ticker, articles):
    """Resumen tipo 'señales alcistas/bajistas' a partir del tono de las noticias."""
    if not articles:
        return None

    avg_score = sum(a["score"] for a in articles) / len(articles)
    label = classify_sentiment(avg_score)
    driver = max(articles, key=lambda a: abs(a["score"]))
    tone = "positivo" if avg_score > 0 else "negativo" if avg_score < 0 else "mixto"

    embed = discord.Embed(
        title=f"{label} — ${ticker}",
        description=(
            f"Con {len(articles)} noticia(s) reciente(s) en la mano, la cobertura de "
            f"**{ticker}** viene con tono {tone}.\n\n"
            f"**Posible motivo principal:** {driver['title']}"
        ),
        color=discord.Color.green() if avg_score > 0.15 else discord.Color.red() if avg_score < -0.15 else discord.Color.greyple(),
        url=driver.get("url"),
    )
    embed.set_footer(text="Heurística de sentimiento de noticias — no es asesoramiento financiero ni garantía de movimiento de precio.")
    return embed


def build_ai_opinion_embed(ticker, opinion_text):
    embed = discord.Embed(
        title=f"🧠 Lectura de IA — ${ticker}",
        description=opinion_text[:4000],
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Generado por IA a partir de titulares recientes — no es asesoramiento financiero.")
    return embed


def build_quote_embed(ticker, q):
    dp, d = q.get("dp"), q.get("d")
    change_txt = f"**{fmt_pct(dp)}**" + (f" ({d:+.2f} USD)" if d is not None else "") + " vs. cierre anterior"
    embed = discord.Embed(
        title=f"{arrow(dp)} {ticker} — {fmt_price(q.get('c'))}",
        description=change_txt,
        color=trend_color(dp),
    )
    embed.add_field(name="Apertura", value=fmt_price(q.get("o")))
    embed.add_field(name="Máx. del día", value=fmt_price(q.get("h")))
    embed.add_field(name="Mín. del día", value=fmt_price(q.get("l")))
    embed.add_field(name="Cierre anterior", value=fmt_price(q.get("pc")))
    if q.get("t"):
        embed.timestamp = datetime.fromtimestamp(q["t"], tz=timezone.utc)
    embed.set_footer(text="Datos vía Finnhub, pueden tener retraso · " + FOOTER_NOT_ADVICE)
    return embed


def build_alert_embed(ticker, q, threshold):
    dp = q.get("dp") or 0
    up = dp > 0
    verb = "sube" if up else "baja"
    embed = discord.Embed(
        title=f"{arrow(dp)} {ticker} {verb} {fmt_pct(dp)} hoy",
        description=(
            f"{random.choice(UP_LINES if up else DOWN_LINES)}\n\n"
            f"Precio actual: **{fmt_price(q.get('c'))}** · cierre anterior: {fmt_price(q.get('pc'))}\n"
            f"Rango del día: {fmt_price(q.get('l'))} – {fmt_price(q.get('h'))}"
        ),
        color=trend_color(dp),
    )
    if q.get("t"):
        embed.timestamp = datetime.fromtimestamp(q["t"], tz=timezone.utc)
    embed.set_footer(text=f"Alerta por movimiento ≥ {threshold:g}% · " + FOOTER_NOT_ADVICE)
    return embed


# =====================================================================
# Cliente de Discord
# =====================================================================
class Iris(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        self.check_news.start()
        self.price_alert_loop.start()
        if ANTHROPIC_API_KEY:
            self.ai_opinion_loop.start()

    async def on_ready(self):
        log.info("Iris conectada como %s", self.user)
        await self.refresh_presence()

    async def refresh_presence(self):
        n = len(get_tickers())
        text = f"{n} {'acción' if n == 1 else 'acciones'} 👀" if n else "esperando tickers 🔭"
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=text))

    # ---- Noticias (cada hora) ----
    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def check_news(self):
        if get_settings()["paused"]:
            return
        await self.run_check()

    @check_news.before_loop
    async def before_check_news(self):
        await self.wait_until_ready()

    async def run_check(self, lookback_hours=CHECK_INTERVAL_HOURS + 1):
        """Revisa noticias y las publica. Devuelve estadísticas para diagnóstico."""
        stats = {
            "tickers": 0, "found": 0, "sent": 0, "already_seen": 0,
            "per_ticker": {}, "errors": [], "channel_ok": True,
        }

        tickers = get_tickers()
        stats["tickers"] = len(tickers)
        if not tickers:
            return stats

        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("No se encontró el canal %s", CHANNEL_ID)
            stats["channel_ok"] = False
            return stats

        seen_ids = get_seen_ids()
        new_seen_ids = set(seen_ids)

        async with aiohttp.ClientSession() as session:
            for ticker in tickers:
                articles = await fetch_news(
                    session, ticker, lookback_hours=lookback_hours, errors=stats["errors"]
                )
                stats["found"] += len(articles)
                stats["per_ticker"][ticker] = len(articles)
                for article in articles:
                    article_id = str(article.get("id"))
                    if article_id in seen_ids:
                        stats["already_seen"] += 1
                        continue
                    new_seen_ids.add(article_id)
                    await channel.send(embed=build_embed(ticker, article))
                    stats["sent"] += 1

                if ALPHAVANTAGE_API_KEY:
                    sentiment_articles = await fetch_alphavantage_sentiment(
                        session, ticker, lookback_hours=lookback_hours
                    )
                    if sentiment_articles:
                        summary_embed = build_sentiment_embed(ticker, sentiment_articles)
                        if summary_embed:
                            await channel.send(embed=summary_embed)

        save_seen_ids(new_seen_ids)
        update_settings(last_news_check=datetime.now(timezone.utc).isoformat())
        return stats

    # ---- Alertas de subida/bajada (cada 15 min) ----
    @tasks.loop(minutes=PRICE_CHECK_MINUTES)
    async def price_alert_loop(self):
        settings = get_settings()
        if settings["paused"] or not settings["alerts"]:
            return
        tickers = get_tickers()
        if not tickers:
            return
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("No se encontró el canal %s", CHANNEL_ID)
            return

        threshold = float(settings["threshold"])
        alerted = settings.get("alerted", {})
        changed = False

        async with aiohttp.ClientSession() as session:
            for ticker in tickers:
                q = await fetch_quote(session, ticker)
                if not q or q.get("dp") is None:
                    continue
                dp = q["dp"]
                if abs(dp) < threshold:
                    continue

                # La fecha sale de la última operación, así un fin de semana
                # no repite la alerta del viernes.
                trade_date = (
                    datetime.fromtimestamp(q["t"], tz=timezone.utc).date().isoformat()
                    if q.get("t") else datetime.now(timezone.utc).date().isoformat()
                )
                sign = 1 if dp > 0 else -1
                level = int(abs(dp) // threshold)  # 1 = primer umbral, 2 = el doble...

                prev = alerted.get(ticker)
                if prev and prev.get("date") == trade_date and prev.get("sign") == sign and prev.get("level", 0) >= level:
                    continue  # ya avisé de este nivel de movimiento

                alerted[ticker] = {"date": trade_date, "sign": sign, "level": level}
                changed = True
                await channel.send(embed=build_alert_embed(ticker, q, threshold))

        if changed:
            update_settings(alerted=alerted)

    @price_alert_loop.before_loop
    async def before_price_alert_loop(self):
        await self.wait_until_ready()

    # ---- Lectura de IA (cada 5 horas) ----
    @tasks.loop(hours=AI_OPINION_INTERVAL_HOURS)
    async def ai_opinion_loop(self):
        if get_settings()["paused"]:
            return
        tickers = get_tickers()
        if not tickers:
            return
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("No se encontró el canal %s", CHANNEL_ID)
            return

        async with aiohttp.ClientSession() as session:
            for ticker in tickers:
                articles = await fetch_news(session, ticker, lookback_hours=AI_OPINION_INTERVAL_HOURS + 1)
                headlines = [a.get("headline") for a in articles if a.get("headline")]
                opinion = await fetch_ai_opinion(session, ticker, headlines)
                if opinion:
                    await channel.send(embed=build_ai_opinion_embed(ticker, opinion))

    @ai_opinion_loop.before_loop
    async def before_ai_opinion_loop(self):
        await self.wait_until_ready()


bot = Iris()


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Error en un comando: %s", error)
    msg = "Uy, algo me salió mal por dentro. 😵 Probá de nuevo en un momentito."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def watchlist_autocomplete(interaction: discord.Interaction, current: str):
    current = current.upper()
    return [
        app_commands.Choice(name=t, value=t) for t in get_tickers() if current in t
    ][:25]


# =====================================================================
# Comandos: lista de seguimiento
# =====================================================================
@bot.tree.command(name="add", description="Agregá una o varias acciones a mi lista (separadas por coma)")
@app_commands.describe(tickers="Símbolos, ej: AAPL o AAPL, NVDA, TSLA")
async def add_command(interaction: discord.Interaction, tickers: str):
    await interaction.response.defer()
    raw = split_tickers(tickers)
    if not raw:
        await interaction.followup.send("No vi ningún símbolo. Probá con `/add AAPL`. 🤔")
        return

    added, existing, invalid = [], [], []
    for t in raw:
        if not TICKER_RE.match(t):
            invalid.append(t)
        elif add_ticker(t):
            added.append(t)
        else:
            existing.append(t)

    # Verifico contra Finnhub para avisar si el símbolo no tiene datos
    no_data = []
    if added:
        async with aiohttp.ClientSession() as session:
            for t in added[:MAX_ADD_CHECKS]:
                if await fetch_quote(session, t) is None:
                    no_data.append(t)

    lines = []
    if added:
        lines.append(random.choice(ADD_OK).format(x=", ".join(f"**{t}**" for t in added)))
    if existing:
        lines.append(ADD_DUP.format(x=", ".join(f"**{t}**" for t in existing)))
    if invalid:
        lines.append("Estos símbolos no me cierran: " + ", ".join(f"`{t}`" for t in invalid) + ".")
    if no_data:
        lines.append(
            "⚠️ Finnhub no me da precio de " + ", ".join(f"**{t}**" for t in no_data)
            + ". Lo guardo igual, pero con el plan gratuito solo cubro acciones de EE.UU. y Canadá, "
            "así que probablemente no vea noticias ni alertas de eso."
        )
    await interaction.followup.send("\n".join(lines))
    await bot.refresh_presence()


@bot.tree.command(name="remove", description="Quitá una o varias acciones de mi lista")
@app_commands.describe(tickers="Símbolos, ej: AAPL o AAPL, NVDA")
async def remove_command(interaction: discord.Interaction, tickers: str):
    raw = split_tickers(tickers)
    removed = [t for t in raw if remove_ticker(t)]
    missing = [t for t in raw if t not in removed]

    lines = []
    if removed:
        lines.append(random.choice(REMOVE_OK).format(x=", ".join(f"**{t}**" for t in removed)))
    if missing:
        lines.append("No tenía anotado a " + ", ".join(f"**{t}**" for t in missing) + ". 🤷")
    if not raw:
        lines.append("No vi ningún símbolo para quitar.")
    await interaction.response.send_message("\n".join(lines))
    await bot.refresh_presence()


@remove_command.autocomplete("tickers")
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    return await watchlist_autocomplete(interaction, current.split(",")[-1].strip())


@bot.tree.command(name="list", description="Mirá qué acciones estoy vigilando")
async def list_command(interaction: discord.Interaction):
    tickers = get_tickers()
    if not tickers:
        await interaction.response.send_message(random.choice(EMPTY_LIST))
        return
    embed = discord.Embed(
        title=f"👀 Estoy vigilando {len(tickers)} {'acción' if len(tickers) == 1 else 'acciones'}",
        description="\n".join(f"• **{t}**" for t in tickers),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Usá /resumen para ver precios y variaciones.")
    await interaction.response.send_message(embed=embed)


# =====================================================================
# Comandos: precios y mercado (Finnhub)
# =====================================================================
@bot.tree.command(name="precio", description="Precio actual y variación del día de una acción")
@app_commands.describe(ticker="Símbolo, ej: AAPL")
async def precio_command(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    ticker = ticker.strip().upper()
    errors = []
    async with aiohttp.ClientSession() as session:
        q = await fetch_quote(session, ticker, errors)
    if q is None:
        extra = f"\n⚠️ {errors[0]}" if errors else ""
        await interaction.followup.send(
            f"No encuentro precio de **{ticker}**. 🤔 Con el plan gratuito de Finnhub solo veo "
            f"acciones de EE.UU. y Canadá.{extra}"
        )
        return
    await interaction.followup.send(embed=build_quote_embed(ticker, q))


@precio_command.autocomplete("ticker")
async def precio_autocomplete(interaction: discord.Interaction, current: str):
    return await watchlist_autocomplete(interaction, current)


async def collect_quotes(tickers):
    """Devuelve {ticker: quote_o_None} para toda la lista."""
    result = {}
    async with aiohttp.ClientSession() as session:
        for t in tickers:
            result[t] = await fetch_quote(session, t)
    return result


@bot.tree.command(name="resumen", description="Tabla con precio y variación de toda mi lista")
async def resumen_command(interaction: discord.Interaction):
    tickers = get_tickers()
    if not tickers:
        await interaction.response.send_message(random.choice(EMPTY_LIST))
        return
    await interaction.response.defer()
    quotes = await collect_quotes(tickers)

    with_data = {t: q for t, q in quotes.items() if q and q.get("dp") is not None}
    without_data = [t for t in tickers if t not in with_data]
    ordered = sorted(with_data.items(), key=lambda kv: kv[1]["dp"], reverse=True)

    rows = [
        f"{arrow(q['dp'])} {t:<8} {fmt_price(q.get('c')):>10}  {fmt_pct(q['dp']):>8}"
        for t, q in ordered
    ]
    desc = "```\n" + "\n".join(rows) + "\n```" if rows else "No pude traer precios de ninguna acción. 😕"
    if without_data:
        desc += "\nSin datos: " + ", ".join(f"**{t}**" for t in without_data)

    if len(ordered) >= 2:
        best, worst = ordered[0], ordered[-1]
        desc += (
            f"\n🏆 Mejor del día: **{best[0]}** ({fmt_pct(best[1]['dp'])})"
            f" · 🥶 Peor: **{worst[0]}** ({fmt_pct(worst[1]['dp'])})"
        )

    avg = sum(q["dp"] for q in with_data.values()) / len(with_data) if with_data else 0
    embed = discord.Embed(title="📊 Resumen de mi lista", description=desc, color=trend_color(avg if with_data else None))
    embed.set_footer(text="Variación vs. cierre anterior · " + FOOTER_NOT_ADVICE)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="humor", description="Preguntale a Iris cómo se siente hoy según tu lista")
async def humor_command(interaction: discord.Interaction):
    tickers = get_tickers()
    if not tickers:
        await interaction.response.send_message(random.choice(EMPTY_LIST))
        return
    await interaction.response.defer()
    quotes = await collect_quotes(tickers)
    changes = [q["dp"] for q in quotes.values() if q and q.get("dp") is not None]
    if not changes:
        await interaction.followup.send("No veo precios ahora, así que hoy no sé cómo me siento. 🤷")
        return

    avg = sum(changes) / len(changes)
    ups = sum(1 for c in changes if c > 0)
    downs = sum(1 for c in changes if c < 0)
    for threshold, title, line in MOODS:
        if avg >= threshold:
            break
    embed = discord.Embed(
        title=f"Hoy estoy: {title}",
        description=(
            f"{line}\n\n"
            f"Promedio de tu lista: **{fmt_pct(avg)}** · 📈 {ups} arriba · 📉 {downs} abajo"
        ),
        color=trend_color(avg),
    )
    embed.set_footer(text="Es solo mi humor, no una señal de nada. " + FOOTER_NOT_ADVICE)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="mercado", description="Titulares generales del mercado ahora mismo")
async def mercado_command(interaction: discord.Interaction):
    await interaction.response.defer()
    errors = []
    async with aiohttp.ClientSession() as session:
        items = await fetch_market_news(session, limit=6, errors=errors)
    if not items:
        extra = f" ({errors[0]})" if errors else ""
        await interaction.followup.send(f"No pude traer titulares ahora. 😕{extra}")
        return

    lines = []
    for item in items:
        headline = (item.get("headline") or "Sin título")[:140]
        url = item.get("url")
        source = item.get("source") or ""
        lines.append(f"• [{headline}]({url}) — *{source}*" if url else f"• {headline} — *{source}*")
    embed = discord.Embed(
        title="🌎 Qué está pasando en el mercado",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Titulares vía Finnhub")
    await interaction.followup.send(embed=embed)


# =====================================================================
# Comandos: control del monitoreo
# =====================================================================
@bot.tree.command(name="umbral", description="Cambiá el % de movimiento diario que dispara una alerta")
@app_commands.describe(porcentaje="Entre 0.5 y 50 (por defecto 3)")
async def umbral_command(interaction: discord.Interaction, porcentaje: app_commands.Range[float, 0.5, 50.0]):
    update_settings(threshold=float(porcentaje), alerted={})
    await interaction.response.send_message(
        f"Hecho. 🎯 Te aviso cuando alguna acción se mueva **{porcentaje:g}%** o más en el día "
        f"(reviso cada {PRICE_CHECK_MINUTES} min)."
    )


@bot.tree.command(name="alertas", description="Activá o desactivá las alertas de subida y bajada")
@app_commands.choices(estado=[
    app_commands.Choice(name="activar", value="on"),
    app_commands.Choice(name="desactivar", value="off"),
])
async def alertas_command(interaction: discord.Interaction, estado: app_commands.Choice[str]):
    on = estado.value == "on"
    update_settings(alerts=on)
    threshold = get_settings()["threshold"]
    await interaction.response.send_message(
        f"Alertas de precio **activadas** 🔔 (umbral ±{threshold:g}%)." if on
        else "Alertas de precio **apagadas** 🔕. Las noticias siguen igual."
    )


@bot.tree.command(name="pausa", description="Pausá todos mis mensajes automáticos")
async def pausa_command(interaction: discord.Interaction):
    update_settings(paused=True)
    await interaction.response.send_message(
        "Me pongo en pausa. ⏸️ No publico nada solo hasta que uses `/reanudar` "
        "(los comandos manuales siguen funcionando)."
    )


@bot.tree.command(name="reanudar", description="Volvé a activar mis mensajes automáticos")
async def reanudar_command(interaction: discord.Interaction):
    update_settings(paused=False)
    await interaction.response.send_message("¡Vuelvo al ruedo! ▶️ Retomo noticias y alertas.")


@bot.tree.command(name="estado", description="Mirá cómo estoy funcionando")
async def estado_command(interaction: discord.Interaction):
    s = get_settings()
    last = s.get("last_news_check")
    last_txt = "todavía no" if not last else f"<t:{int(datetime.fromisoformat(last).timestamp())}:R>"
    embed = discord.Embed(title="🩺 Estado de Iris", color=discord.Color.blurple())
    embed.add_field(name="Acciones vigiladas", value=str(len(get_tickers())))
    embed.add_field(name="Modo", value="⏸️ En pausa" if s["paused"] else "▶️ Activa")
    embed.add_field(
        name="Alertas de precio",
        value=f"🔔 ±{s['threshold']:g}% · cada {PRICE_CHECK_MINUTES} min" if s["alerts"] else "🔕 Apagadas",
    )
    embed.add_field(name="Última revisión de noticias", value=last_txt)
    embed.add_field(name="En línea hace", value=human_delta(datetime.now(timezone.utc) - BOT_START))
    embed.add_field(
        name="Fuentes",
        value=(
            "Finnhub ✅ · "
            f"Alpha Vantage {'✅' if ALPHAVANTAGE_API_KEY else '❌'} · "
            f"IA {'✅' if ANTHROPIC_API_KEY else '❌'}"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


def format_check_summary(stats):
    if stats["tickers"] == 0:
        return "⚠️ La lista está vacía. Usá `/add TICKER` primero."
    if not stats["channel_ok"]:
        return (
            f"⚠️ No encuentro el canal con ID `{CHANNEL_ID}`. Revisá la variable "
            "`DISCORD_CHANNEL_ID` en Railway y que yo tenga acceso a ese canal."
        )

    lines = [f"✅ Listo, revisé {stats['tickers']} ticker(s) (noticias de las últimas 24 h):"]
    for ticker, n in stats["per_ticker"].items():
        lines.append(f"• **{ticker}**: {n} noticia(s) encontradas")
    lines.append(
        f"Publiqué en <#{CHANNEL_ID}>: {stats['sent']} · Ya enviadas antes: {stats['already_seen']}"
    )
    if stats["errors"]:
        lines.append("⚠️ Problemas: " + "; ".join(stats["errors"][:3]))
    elif stats["found"] == 0:
        lines.append(
            "Finnhub no devolvió noticias. Si tus acciones no son de EE.UU. o Canadá, es lo "
            "esperable: el plan gratuito no las cubre."
        )
    return "\n".join(lines)


@bot.tree.command(name="checknow", description="Forzá una revisión de noticias ahora mismo")
async def checknow_command(interaction: discord.Interaction):
    await interaction.response.defer()
    stats = await bot.run_check(lookback_hours=24)
    await interaction.followup.send(format_check_summary(stats))


# =====================================================================
# Comandos: sentimiento e IA (opcionales)
# =====================================================================
@bot.tree.command(name="sentiment", description="Sentimiento actual de las noticias de una acción")
@app_commands.describe(ticker="Símbolo, ej: AAPL")
async def sentiment_command(interaction: discord.Interaction, ticker: str):
    if not ALPHAVANTAGE_API_KEY:
        await interaction.response.send_message("⚠️ Para esto necesito configurar `ALPHAVANTAGE_API_KEY`.")
        return

    await interaction.response.defer()
    ticker = ticker.strip().upper()
    async with aiohttp.ClientSession() as session:
        articles = await fetch_alphavantage_sentiment(session, ticker, lookback_hours=48)

    if not articles:
        await interaction.followup.send(f"No encontré noticias recientes de **{ticker}**. 🤔")
        return
    await interaction.followup.send(embed=build_sentiment_embed(ticker, articles))


@sentiment_command.autocomplete("ticker")
async def sentiment_autocomplete(interaction: discord.Interaction, current: str):
    return await watchlist_autocomplete(interaction, current)


@bot.tree.command(name="opinion", description="Pedile a la IA una lectura de las noticias recientes de una acción")
@app_commands.describe(ticker="Símbolo, ej: AAPL")
async def opinion_command(interaction: discord.Interaction, ticker: str):
    if not ANTHROPIC_API_KEY:
        await interaction.response.send_message("⚠️ Para esto necesito configurar `ANTHROPIC_API_KEY`.")
        return

    await interaction.response.defer()
    ticker = ticker.strip().upper()
    async with aiohttp.ClientSession() as session:
        articles = await fetch_news(session, ticker, lookback_hours=48)
        headlines = [a.get("headline") for a in articles if a.get("headline")]
        opinion = await fetch_ai_opinion(session, ticker, headlines)

    if not opinion:
        await interaction.followup.send(
            f"No tengo suficientes noticias recientes de **{ticker}** para armar una lectura. 🤔"
        )
        return
    await interaction.followup.send(embed=build_ai_opinion_embed(ticker, opinion))


@opinion_command.autocomplete("ticker")
async def opinion_autocomplete(interaction: discord.Interaction, current: str):
    return await watchlist_autocomplete(interaction, current)


# =====================================================================
# Comandos: diversión y ayuda (sin APIs)
# =====================================================================
@bot.tree.command(name="oraculo", description="Hacele una pregunta al oráculo de Iris (es un juego)")
@app_commands.describe(pregunta="Lo que quieras saber, ej: ¿hoy es buen día para mirar el celular?")
async def oraculo_command(interaction: discord.Interaction, pregunta: str):
    embed = discord.Embed(
        title="🔮 El oráculo de Iris",
        description=f"**Tu pregunta:** {pregunta[:300]}\n\n{random.choice(ORACLE_ANSWERS)}",
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Esto es un juego, no un consejo de inversión. Las decisiones reales, con datos y cabeza fría.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ayuda", description="Qué puede hacer Iris")
async def ayuda_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✨ Hola, soy Iris",
        description="Vigilo tus acciones, te traigo noticias y te aviso cuando algo se mueve. Esto es lo que sé hacer:",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📋 Mi lista",
        value="`/add` · `/remove` · `/list`\n(aceptan varios símbolos separados por coma)",
        inline=False,
    )
    embed.add_field(
        name="📈 Precios y mercado",
        value="`/precio` · `/resumen` · `/humor` · `/mercado`",
        inline=False,
    )
    embed.add_field(
        name="🔔 Alertas y control",
        value="`/umbral` · `/alertas` · `/pausa` · `/reanudar` · `/estado` · `/checknow`",
        inline=False,
    )
    embed.add_field(
        name="🧠 Extras opcionales",
        value="`/sentiment` (Alpha Vantage) · `/opinion` (IA)",
        inline=False,
    )
    embed.add_field(name="🎲 Para divertirse", value="`/oraculo`", inline=False)
    embed.set_footer(text=FOOTER_NOT_ADVICE)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
