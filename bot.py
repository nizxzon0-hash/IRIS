"""
Stock News Discord Bot
-----------------------
Monitorea acciones (tickers) y manda actualizaciones de noticias a un canal
de Discord cada hora. Permite agregar/quitar tickers con comandos slash.

Requisitos:
    pip install -r requirements.txt

Variables de entorno necesarias (ver README.md):
    DISCORD_TOKEN       -> token del bot de Discord
    DISCORD_CHANNEL_ID  -> ID del canal donde se publican las noticias
    FINNHUB_API_KEY     -> API key gratuita de https://finnhub.io
    ALPHAVANTAGE_API_KEY -> API key gratuita de https://www.alphavantage.co
                            (se usa para el resumen de sentimiento)

Nota: el "resumen de sentimiento" es una heurística basada en cómo viene
siendo la cobertura de noticias reciente, no una predicción financiera.
No garantiza hacia dónde se va a mover el precio.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stock-news-bot")

# ---------- Configuración ----------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")  # opcional
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # opcional
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5")
AI_OPINION_INTERVAL_HOURS = 5

AI_SYSTEM_PROMPT = (
    "Sos un analista que resume noticias bursátiles para un canal privado de Discord. "
    "Dado un listado de titulares recientes sobre una acción, escribí un párrafo breve "
    "(máximo 4-5 oraciones, en español) que explique qué está pasando y qué factores "
    "podrían presionar el precio al alza o a la baja, y por qué. "
    "No dés una predicción categórica de si el precio va a subir o bajar, no des precios "
    "objetivo, y no dés recomendaciones de compra/venta. Hablá en términos de factores y "
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

TICKERS_FILE = "tickers.json"
SEEN_FILE = "seen_news.json"
CHECK_INTERVAL_HOURS = 1

# ---------- Almacenamiento simple en JSON ----------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_tickers():
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


# ---------- Cliente de Discord ----------
class StockNewsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        self.check_news.start()
        if ANTHROPIC_API_KEY:
            self.ai_opinion_loop.start()

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def check_news(self):
        tickers = get_tickers()
        if not tickers:
            return

        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("No se encontró el canal %s", CHANNEL_ID)
            return

        seen_ids = get_seen_ids()
        new_seen_ids = set(seen_ids)

        async with aiohttp.ClientSession() as session:
            for ticker in tickers:
                # Fuente 1: Finnhub (titulares)
                articles = await fetch_news(session, ticker)
                for article in articles:
                    article_id = str(article.get("id"))
                    if article_id in seen_ids:
                        continue
                    new_seen_ids.add(article_id)
                    await channel.send(embed=build_embed(ticker, article))

                # Fuente 2: Alpha Vantage (sentimiento) -> resumen tipo "puede subir/bajar"
                if ALPHAVANTAGE_API_KEY:
                    sentiment_articles = await fetch_alphavantage_sentiment(session, ticker)
                    if sentiment_articles:
                        summary_embed = build_sentiment_embed(ticker, sentiment_articles)
                        if summary_embed:
                            await channel.send(embed=summary_embed)

        save_seen_ids(new_seen_ids)

    @check_news.before_loop
    async def before_check_news(self):
        await self.wait_until_ready()

    @tasks.loop(hours=AI_OPINION_INTERVAL_HOURS)
    async def ai_opinion_loop(self):
        tickers = get_tickers()
        if not tickers:
            return

        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("No se encontró el canal %s", CHANNEL_ID)
            return

        async with aiohttp.ClientSession() as session:
            for ticker in tickers:
                # Juntamos titulares recientes de las últimas horas como contexto
                articles = await fetch_news(session, ticker, lookback_hours=AI_OPINION_INTERVAL_HOURS + 1)
                headlines = [a.get("headline") for a in articles if a.get("headline")]

                opinion = await fetch_ai_opinion(session, ticker, headlines)
                if opinion:
                    await channel.send(embed=build_ai_opinion_embed(ticker, opinion))

    @ai_opinion_loop.before_loop
    async def before_ai_opinion_loop(self):
        await self.wait_until_ready()


async def fetch_news(session, ticker, lookback_hours=CHECK_INTERVAL_HOURS + 1):
    """Trae noticias recientes de un ticker desde Finnhub."""
    to_date = datetime.now(timezone.utc).date()
    from_date = (datetime.now(timezone.utc) - timedelta(days=2)).date()

    url = (
        "https://finnhub.io/api/v1/company-news"
        f"?symbol={ticker}&from={from_date}&to={to_date}&token={FINNHUB_API_KEY}"
    )

    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                log.warning("Finnhub devolvió %s para %s", resp.status, ticker)
                return []
            data = await resp.json()
    except Exception as e:
        log.error("Error consultando Finnhub para %s: %s", ticker, e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent = [
        a for a in data
        if datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc) >= cutoff
    ]
    return recent


async def fetch_alphavantage_sentiment(session, ticker, lookback_hours=CHECK_INTERVAL_HOURS + 1):
    """Trae noticias + puntaje de sentimiento por artículo desde Alpha Vantage."""
    if not ALPHAVANTAGE_API_KEY:
        return []

    url = (
        "https://www.alphavantage.co/query"
        f"?function=NEWS_SENTIMENT&tickers={ticker}&apikey={ALPHAVANTAGE_API_KEY}"
    )

    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                log.warning("Alpha Vantage devolvió %s para %s", resp.status, ticker)
                return []
            data = await resp.json()
    except Exception as e:
        log.error("Error consultando Alpha Vantage para %s: %s", ticker, e)
        return []

    feed = data.get("feed", [])
    if not feed:
        # Alpha Vantage devuelve un dict con "Note"/"Information" cuando se
        # excede el límite gratuito (25 req/día) en vez de un error HTTP.
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

        # Buscar el score específico para este ticker dentro del artículo
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
    if not ANTHROPIC_API_KEY:
        return None
    if not headlines:
        return None

    headlines_text = "\n".join(f"- {h}" for h in headlines[:15])
    user_prompt = f"Ticker: {ticker}\n\nTitulares recientes:\n{headlines_text}"

    payload = {
        "model": AI_MODEL,
        "max_tokens": 300,
        "system": AI_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=30
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("Anthropic API devolvió %s para %s: %s", resp.status, ticker, body[:200])
                return None
            data = await resp.json()
    except Exception as e:
        log.error("Error consultando la API de Claude para %s: %s", ticker, e)
        return None

    parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
    return "".join(parts).strip() or None


def build_ai_opinion_embed(ticker, opinion_text):
    embed = discord.Embed(
        title=f"🧠 Lectura de IA — ${ticker}",
        description=opinion_text[:4000],
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Generado por IA a partir de titulares recientes — no es asesoramiento financiero.")
    return embed


def classify_sentiment(avg_score):
    for threshold, label in SENTIMENT_BUCKETS:
        if avg_score >= threshold:
            return label
    return SENTIMENT_BUCKETS[-1][1]


def build_sentiment_embed(ticker, articles):
    """Arma un embed resumen tipo 'el mercado puede subir/bajar por X motivo'."""
    if not articles:
        return None

    avg_score = sum(a["score"] for a in articles) / len(articles)
    label = classify_sentiment(avg_score)

    # Tomamos el artículo con score más extremo (positivo o negativo) como "motivo" principal
    driver = max(articles, key=lambda a: abs(a["score"]))

    embed = discord.Embed(
        title=f"{label} — ${ticker}",
        description=(
            f"Basado en {len(articles)} noticia(s) reciente(s), la cobertura sobre "
            f"**{ticker}** viene con un tono {('positivo' if avg_score > 0 else 'negativo' if avg_score < 0 else 'mixto')}.\n\n"
            f"**Posible motivo principal:** {driver['title']}"
        ),
        color=discord.Color.green() if avg_score > 0.15 else discord.Color.red() if avg_score < -0.15 else discord.Color.greyple(),
        url=driver.get("url"),
    )
    embed.set_footer(text="Heurística de sentimiento de noticias — no es asesoramiento financiero ni garantía de movimiento de precio.")
    return embed


def build_embed(ticker, article):
    embed = discord.Embed(
        title=article.get("headline", "Sin título")[:256],
        url=article.get("url"),
        description=(article.get("summary") or "")[:300],
        color=discord.Color.blue(),
        timestamp=datetime.fromtimestamp(article.get("datetime", 0), tz=timezone.utc),
    )
    embed.set_author(name=f"${ticker}")
    if article.get("source"):
        embed.set_footer(text=article["source"])
    if article.get("image"):
        embed.set_thumbnail(url=article["image"])
    return embed


bot = StockNewsBot()


# ---------- Comandos ----------
@bot.tree.command(name="add", description="Agregar un ticker a la lista de monitoreo")
@app_commands.describe(ticker="Símbolo de la acción, ej: AAPL")
async def add_command(interaction: discord.Interaction, ticker: str):
    added = add_ticker(ticker)
    if added:
        await interaction.response.send_message(f"✅ Agregado **{ticker.upper()}** a la lista.")
    else:
        await interaction.response.send_message(f"⚠️ **{ticker.upper()}** ya estaba en la lista.")


@bot.tree.command(name="remove", description="Quitar un ticker de la lista de monitoreo")
@app_commands.describe(ticker="Símbolo de la acción, ej: AAPL")
async def remove_command(interaction: discord.Interaction, ticker: str):
    removed = remove_ticker(ticker)
    if removed:
        await interaction.response.send_message(f"🗑️ Quitado **{ticker.upper()}** de la lista.")
    else:
        await interaction.response.send_message(f"⚠️ **{ticker.upper()}** no estaba en la lista.")


@bot.tree.command(name="list", description="Ver los tickers monitoreados actualmente")
async def list_command(interaction: discord.Interaction):
    tickers = get_tickers()
    if tickers:
        await interaction.response.send_message("📋 Monitoreando: " + ", ".join(f"**{t}**" for t in tickers))
    else:
        await interaction.response.send_message("La lista está vacía. Usá `/add TICKER` para empezar.")


@bot.tree.command(name="checknow", description="Forzar una revisión de noticias ahora mismo")
async def checknow_command(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Revisando noticias...")
    await bot.check_news()


@bot.tree.command(name="sentiment", description="Ver el sentimiento actual de noticias para un ticker")
@app_commands.describe(ticker="Símbolo de la acción, ej: AAPL")
async def sentiment_command(interaction: discord.Interaction, ticker: str):
    if not ALPHAVANTAGE_API_KEY:
        await interaction.response.send_message(
            "⚠️ Este comando necesita configurar `ALPHAVANTAGE_API_KEY`."
        )
        return

    await interaction.response.defer()
    ticker = ticker.upper()
    async with aiohttp.ClientSession() as session:
        articles = await fetch_alphavantage_sentiment(session, ticker, lookback_hours=48)

    if not articles:
        await interaction.followup.send(f"No encontré noticias recientes para **{ticker}**.")
        return

    embed = build_sentiment_embed(ticker, articles)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="opinion", description="Pedirle a la IA una lectura de las noticias recientes de un ticker")
@app_commands.describe(ticker="Símbolo de la acción, ej: AAPL")
async def opinion_command(interaction: discord.Interaction, ticker: str):
    if not ANTHROPIC_API_KEY:
        await interaction.response.send_message(
            "⚠️ Este comando necesita configurar `ANTHROPIC_API_KEY`."
        )
        return

    await interaction.response.defer()
    ticker = ticker.upper()
    async with aiohttp.ClientSession() as session:
        articles = await fetch_news(session, ticker, lookback_hours=48)
        headlines = [a.get("headline") for a in articles if a.get("headline")]
        opinion = await fetch_ai_opinion(session, ticker, headlines)

    if not opinion:
        await interaction.followup.send(f"No tengo suficientes noticias recientes de **{ticker}** para armar una lectura.")
        return

    await interaction.followup.send(embed=build_ai_opinion_embed(ticker, opinion))


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
