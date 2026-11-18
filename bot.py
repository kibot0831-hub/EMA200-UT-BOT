import asyncio
import os
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import ccxt.async_support as ccxt


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "si", "on")


# ---------------- CONFIG (variables de entorno) ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEFRAMES = [t.strip() for t in os.getenv("TIMEFRAMES", "3m,5m").split(",") if t.strip()]
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "200"))
CANDLES = int(os.getenv("CANDLES", "500"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "0"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))
CLOSE_DELAY_SEC = int(os.getenv("CLOSE_DELAY_SEC", "3"))
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap")  # swap o spot

# Modulo 1: toque a la EMA200
ENABLE_EMA_TOUCH = env_bool("ENABLE_EMA_TOUCH", True)
TOUCH_TOLERANCE_PCT = float(os.getenv("TOUCH_TOLERANCE_PCT", "0.0"))

# Modulo 2: UT Bot Alerts + filtro EMA200
ENABLE_UT_BOT = env_bool("ENABLE_UT_BOT", True)
UT_KEY_VALUE = float(os.getenv("UT_KEY_VALUE", "1"))       # "Key Value" (sensibilidad)
UT_ATR_PERIOD = int(os.getenv("UT_ATR_PERIOD", "10"))      # ATR Period
UT_HEIKIN_ASHI = env_bool("UT_HEIKIN_ASHI", False)         # senales desde velas Heikin Ashi
UT_STRICT_ABOVE = env_bool("UT_STRICT_ABOVE", False)       # True = toda la vela (low/high) fuera de la EMA

# Resumen diario
REPORT_TZ = os.getenv("REPORT_TZ", "Europe/Madrid")        # zona horaria del "fin de dia"
REPORT_TIME = os.getenv("REPORT_TIME", "23:59")            # hora local del resumen (HH:MM)
FEE_PCT_ROUNDTRIP = float(os.getenv("FEE_PCT_ROUNDTRIP", "0.1"))  # comision entrada+salida en %

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ema-bot")

# posiciones virtuales abiertas por senal UT: (symbol, tf) -> {"side", "entry"}
POSITIONS = {}
# operaciones cerradas desde el ultimo resumen diario
CLOSED_TRADES = []


def tf_seconds(tf: str) -> int:
    unit, n = tf[-1], int(tf[:-1])
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def calc_ema(values, period):
    """EMA con semilla SMA. Lista alineada con values (None al inicio)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [None] * (period - 1)
    prev = sum(values[:period]) / period
    ema.append(prev)
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        ema.append(prev)
    return ema


def ut_bot(candles):
    """
    Port fiel de 'UT Bot Alerts' (Pine v4).
    Devuelve (buy, sell, trailing_stop) evaluado en la ULTIMA vela de la lista.
    """
    n = len(candles)
    c = UT_ATR_PERIOD
    if n < c + 5:
        return None
    opens = [x[1] for x in candles]
    highs = [x[2] for x in candles]
    lows = [x[3] for x in candles]
    closes = [x[4] for x in candles]

    # ATR (RMA de Wilder, igual que atr() de Pine)
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = [None] * n
    atr[c - 1] = sum(tr[:c]) / c
    for i in range(c, n):
        atr[i] = (atr[i - 1] * (c - 1) + tr[i]) / c

    if UT_HEIKIN_ASHI:
        src = [(opens[i] + highs[i] + lows[i] + closes[i]) / 4 for i in range(n)]
    else:
        src = closes

    prev_ts = 0.0
    buy = sell = False
    cur = 0.0
    for i in range(c - 1, n):
        nloss = UT_KEY_VALUE * atr[i]
        s, s1 = src[i], src[i - 1]
        if s > prev_ts and s1 > prev_ts:
            cur = max(prev_ts, s - nloss)
        elif s < prev_ts and s1 < prev_ts:
            cur = min(prev_ts, s + nloss)
        elif s > prev_ts:
            cur = s - nloss
        else:
            cur = s + nloss
        if i == n - 1:
            above = s > cur and s1 <= prev_ts   # crossover(ema(src,1), stop)
            below = cur > s and prev_ts <= s1   # crossover(stop, ema(src,1))
            buy = s > cur and above
            sell = s < cur and below
        prev_ts = cur
    return buy, sell, cur


def touches(candle, ema):
    tol = TOUCH_TOLERANCE_PCT / 100
    return candle[3] <= ema * (1 + tol) and candle[2] >= ema * (1 - tol)


def fmt_link(symbol):
    base = symbol.split("/")[0]
    if MARKET_TYPE == "swap":
        return f"https://bingx.com/en/perpetual/{base}-USDT/"
    return f"https://bingx.com/en/spot/{base}USDT/"


async def send_telegram(session, text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.error("Telegram %s: %s", r.status, await r.text())
    except Exception as e:
        log.error("Error Telegram: %s", e)


async def get_symbols(ex):
    await ex.load_markets(True)
    symbols = []
    for s, m in ex.markets.items():
        if not m.get("active", True) or m.get("quote") != "USDT":
            continue
        if MARKET_TYPE == "swap" and not (m.get("swap") and m.get("linear")):
            continue
        if MARKET_TYPE == "spot" and not m.get("spot"):
            continue
        symbols.append(s)
    if MIN_VOLUME_USDT > 0:
        try:
            tickers = await ex.fetch_tickers(symbols)
            symbols = [s for s in symbols
                       if (tickers.get(s, {}).get("quoteVolume") or 0) >= MIN_VOLUME_USDT]
        except Exception as e:
            log.warning("No se pudo filtrar por volumen: %s", e)
    return symbols


async def analyze(ex, session, sem, symbol, tf):
    async with sem:
        try:
            ohlcv = await ex.fetch_ohlcv(symbol, tf, limit=CANDLES)
        except Exception as e:
            log.debug("%s %s error: %s", symbol, tf, e)
            return 0
    now_ms = int(time.time() * 1000)
    tf_ms = tf_seconds(tf) * 1000
    closed = [c for c in ohlcv if c[0] + tf_ms <= now_ms]
    if len(closed) < EMA_PERIOD + 2:
        return 0
    ema = calc_ema([c[4] for c in closed], EMA_PERIOD)
    last, prev = closed[-1], closed[-2]
    e_last, e_prev = ema[-1], ema[-2]
    if e_last is None or e_prev is None:
        return 0

    hora = datetime.fromtimestamp(last[0] / 1000, tz=timezone.utc).strftime("%H:%M UTC")
    link = f'<a href="{fmt_link(symbol)}">Abrir en BingX</a>'
    sent = 0

    # ---------- Modulo 1: toque a la EMA200 (toque nuevo) ----------
    if ENABLE_EMA_TOUCH and touches(last, e_last) and not touches(prev, e_prev):
        origen = ("desde ARRIBA 🔻 (posible soporte)" if prev[4] > e_prev
                  else "desde ABAJO 🔺 (posible resistencia)")
        cierre = "arriba" if last[4] > e_last else "abajo"
        await send_telegram(session,
            f"🚨 <b>Toque EMA{EMA_PERIOD}</b> · {tf}\n<b>{symbol}</b>\n"
            f"Toque {origen}\n"
            f"EMA{EMA_PERIOD}: <code>{e_last:.8g}</code>\n"
            f"Cierre: <code>{last[4]:.8g}</code> ({cierre} de la EMA)\n"
            f"Vela: {hora}\n{link}")
        sent += 1

    # ---------- Modulo 2: UT Bot + filtro EMA200 ----------
    if ENABLE_UT_BOT:
        ut = ut_bot(closed)
        if ut:
            buy, sell, stop = ut
            key = (symbol, tf)
            pos = POSITIONS.get(key)
            price = last[4]

            # 3) Senal contraria -> salir inmediatamente
            if pos and ((pos["side"] == "long" and sell) or (pos["side"] == "short" and buy)):
                if pos["side"] == "long":
                    pnl = (price - pos["entry"]) / pos["entry"] * 100
                else:
                    pnl = (pos["entry"] - price) / pos["entry"] * 100
                pnl -= FEE_PCT_ROUNDTRIP
                lado = "LONG" if pos["side"] == "long" else "SHORT"
                icono = "✅ GANADA" if pnl > 0 else "❌ PERDIDA"
                await send_telegram(session,
                    f"⚠️ <b>SALIR {lado} AHORA</b> · {tf}\n<b>{symbol}</b>\n"
                    f"Señal contraria UT Bot ({'Sell' if sell else 'Buy'})\n"
                    f"Entrada: <code>{pos['entry']:.8g}</code>\n"
                    f"Salida: <code>{price:.8g}</code>\n"
                    f"{icono}: <b>{pnl:+.2f}%</b> (neto de comisiones)\n"
                    f"Vela: {hora}\n{link}")
                CLOSED_TRADES.append({"symbol": symbol, "tf": tf, "side": pos["side"],
                                      "entry": pos["entry"], "exit": price, "pnl": pnl})
                del POSITIONS[key]
                sent += 1

            # 2) Long solo si el Buy sale por encima de la EMA200
            above_ema = (last[3] > e_last) if UT_STRICT_ABOVE else (price > e_last)
            below_ema = (last[2] < e_last) if UT_STRICT_ABOVE else (price < e_last)
            if buy and above_ema:
                POSITIONS[key] = {"side": "long", "entry": price}
                await send_telegram(session,
                    f"🟢 <b>UT BOT · BUY (LONG)</b> · {tf}\n<b>{symbol}</b>\n"
                    f"Precio: <code>{price:.8g}</code>\n"
                    f"EMA{EMA_PERIOD}: <code>{e_last:.8g}</code> (precio por encima ✅)\n"
                    f"Trailing stop UT: <code>{stop:.8g}</code>\n"
                    f"Salida: cuando aparezca Sell\n"
                    f"Vela: {hora}\n{link}")
                sent += 1
            # Short solo si el Sell sale por debajo de la EMA200
            elif sell and below_ema:
                POSITIONS[key] = {"side": "short", "entry": price}
                await send_telegram(session,
                    f"🔴 <b>UT BOT · SELL (SHORT)</b> · {tf}\n<b>{symbol}</b>\n"
                    f"Precio: <code>{price:.8g}</code>\n"
                    f"EMA{EMA_PERIOD}: <code>{e_last:.8g}</code> (precio por debajo ✅)\n"
                    f"Trailing stop UT: <code>{stop:.8g}</code>\n"
                    f"Salida: cuando aparezca Buy\n"
                    f"Vela: {hora}\n{link}")
                sent += 1
    return sent


async def send_long(session, text):
    """Divide mensajes largos (limite Telegram 4096 caracteres)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await send_telegram(session, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await send_telegram(session, chunk)


def short_name(symbol):
    return symbol.split(":")[0]


def trade_line(t):
    icon = "✅" if t["pnl"] > 0 else "❌"
    return f"{icon} {short_name(t['symbol'])} {t['side'].upper()} {t['tf']} <b>{t['pnl']:+.2f}%</b>"


def build_report(trades, date_str):
    open_n = len(POSITIONS)
    if not trades:
        return f"📊 <b>Resumen del día {date_str}</b>\nSin operaciones cerradas hoy.\nPosiciones abiertas: {open_n}"
    wins = sorted([t for t in trades if t["pnl"] > 0], key=lambda t: -t["pnl"])
    losses = sorted([t for t in trades if t["pnl"] <= 0], key=lambda t: t["pnl"])
    n = len(trades)
    total = sum(t["pnl"] for t in trades)
    avg_w = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_l = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    lines = [
        f"📊 <b>Resumen del día {date_str}</b> (neto de comisiones)",
        f"Operaciones cerradas: <b>{n}</b>",
        f"✅ Ganadoras: <b>{len(wins)}</b> ({len(wins) / n * 100:.0f}%) · media {avg_w:+.2f}%",
        f"❌ Perdedoras: <b>{len(losses)}</b> ({len(losses) / n * 100:.0f}%) · media {avg_l:+.2f}%",
        f"Resultado total (suma de %): <b>{total:+.2f}%</b>",
        f"Posiciones aún abiertas: {open_n}",
        "",
        "<b>Por timeframe</b>",
    ]
    for tf in sorted({t["tf"] for t in trades}, key=tf_seconds):
        tt = [t for t in trades if t["tf"] == tf]
        w = sum(1 for t in tt if t["pnl"] > 0)
        lines.append(f"{tf}: {len(tt)} ops · {w}G/{len(tt) - w}P · {sum(t['pnl'] for t in tt):+.2f}%")
    lines += ["", "<b>Mejores</b>"] + [trade_line(t) for t in wins[:10]]
    lines += ["", "<b>Peores</b>"] + [trade_line(t) for t in losses[:10]]
    return "\n".join(lines)


async def daily_report_loop(session):
    try:
        tz = ZoneInfo(REPORT_TZ)
    except Exception:
        log.warning("Zona horaria invalida (%s), uso UTC", REPORT_TZ)
        tz = timezone.utc
    hh, mm = (int(x) for x in REPORT_TIME.split(":"))
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        trades = CLOSED_TRADES[:]
        CLOSED_TRADES.clear()
        try:
            await send_long(session, build_report(trades, target.strftime("%d/%m/%Y")))
        except Exception as e:
            log.error("Error resumen diario: %s", e)


async def scan(ex, session, tf, symbols):
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()
    res = await asyncio.gather(*[analyze(ex, session, sem, s, tf) for s in symbols])
    log.info("[%s] %d monedas, %d mensajes, %d posiciones virtuales abiertas, %.1fs",
             tf, len(symbols), sum(res), len(POSITIONS), time.time() - t0)


async def tf_loop(ex, session, tf, symbols_ref):
    secs = tf_seconds(tf)
    while True:
        now = time.time()
        nxt = (now // secs + 1) * secs + CLOSE_DELAY_SEC
        await asyncio.sleep(nxt - now)
        try:
            await scan(ex, session, tf, symbols_ref["list"])
        except Exception as e:
            log.error("Error en scan %s: %s", tf, e)


async def refresh_symbols(ex, symbols_ref):
    while True:
        await asyncio.sleep(3600)
        try:
            symbols_ref["list"] = await get_symbols(ex)
            log.info("Lista actualizada: %d monedas", len(symbols_ref["list"]))
        except Exception as e:
            log.error("Error actualizando lista: %s", e)


async def main():
    ex = ccxt.bingx({"enableRateLimit": True,
                     "options": {"defaultType": "swap" if MARKET_TYPE == "swap" else "spot"}})
    async with aiohttp.ClientSession() as session:
        try:
            symbols = await get_symbols(ex)
            log.info("Monedas BingX (%s): %d", MARKET_TYPE, len(symbols))
            symbols_ref = {"list": symbols}
            modulos = []
            if ENABLE_EMA_TOUCH:
                modulos.append(f"Toque EMA{EMA_PERIOD}")
            if ENABLE_UT_BOT:
                modulos.append(f"UT Bot (key={UT_KEY_VALUE}, ATR={UT_ATR_PERIOD}) + filtro EMA{EMA_PERIOD}")
            await send_telegram(session,
                f"✅ Bot iniciado\nTF: {', '.join(TIMEFRAMES)}\n"
                f"Módulos: {' | '.join(modulos)}\nResumen diario: {REPORT_TIME} ({REPORT_TZ})\nMonedas: {len(symbols)} ({MARKET_TYPE})")
            await asyncio.gather(refresh_symbols(ex, symbols_ref),
                                 daily_report_loop(session),
                                 *[tf_loop(ex, session, tf, symbols_ref) for tf in TIMEFRAMES])
        finally:
            await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
