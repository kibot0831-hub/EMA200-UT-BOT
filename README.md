# Bot BingX -> Telegram: toque EMA200 + UT Bot Alerts

Escanea todas las monedas USDT de BingX (futuros perpetuos por defecto) al cierre de cada vela.

## Modulos
1. **Toque EMA200**: avisa cuando una vela cerrada toca por primera vez la EMA 200.
2. **UT Bot Alerts + filtro EMA200** (port del indicador de TradingView):
   - LONG: solo si sale Buy y el precio esta por ENCIMA de la EMA200.
   - SHORT: solo si sale Sell y el precio esta por DEBAJO de la EMA200.
   - SALIDA: al aparecer la senal contraria, el bot avisa "SALIR" con el resultado en %.

3. **Resumen diario**: a la hora REPORT_TIME (por defecto 23:59, Europe/Madrid) envia ganadoras, perdedoras,
   % de acierto, resultado total, desglose por timeframe y mejores/peores operaciones. Los % son netos de
   comisiones (FEE_PCT_ROUNDTRIP, 0.1% por defecto).

## Despliegue
1. Sube estos archivos a un repo de GitHub (todos en la raiz).
2. Railway -> New Project -> Deploy from GitHub repo.
3. Variables: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID (resto opcional, ver .env.example).
4. Railway ejecuta `worker: python bot.py` del Procfile.

## Variables
- TIMEFRAMES (3m,5m; admite 15m, 1h...) · EMA_PERIOD · MIN_VOLUME_USDT · CONCURRENCY · MARKET_TYPE (swap/spot)
- ENABLE_EMA_TOUCH / TOUCH_TOLERANCE_PCT
- REPORT_TZ · REPORT_TIME · FEE_PCT_ROUNDTRIP
- ENABLE_UT_BOT · UT_KEY_VALUE (sensibilidad) · UT_ATR_PERIOD · UT_HEIKIN_ASHI · UT_STRICT_ABOVE (toda la vela fuera de la EMA)

## Nota
Las posiciones del UT Bot son virtuales y se guardan en memoria: si Railway reinicia el bot, se pierden
y no llegara el aviso de salida de las operaciones que estaban abiertas.
