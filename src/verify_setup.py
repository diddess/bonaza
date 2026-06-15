"""
verify_setup.py - Verification complete de l'environnement Bonaza
Usage : python src/verify_setup.py
"""
import sys
import os
from pathlib import Path

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"; B = "\033[1m"; E = "\033[0m"

def ok(m):   print(f"  {G}[OK]{E} {m}")
def warn(m): print(f"  {Y}[!!]{E} {m}")
def fail(m): print(f"  {R}[XX]{E} {m}")
def step(m): print(f"\n{C}{B}[>>]{E} {m}")

print(f"\n{B}{'='*50}")
print("   BONAZA - Verification environnement")
print(f"{'='*50}{E}\n")

# --- Python ---
step("Version Python")
v = sys.version_info
if v.major == 3 and v.minor == 11:
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
else:
    warn(f"Python {v.major}.{v.minor}.{v.micro} (attendu : 3.11)")

# --- Imports obligatoires ---
step("Librairies core")
required = [
    ("pandas",    "pandas"),
    ("numpy",     "numpy"),
    ("dotenv",    "python-dotenv"),
    ("loguru",    "loguru"),
    ("aiohttp",   "aiohttp"),
    ("websockets","websockets"),
    ("sqlalchemy","sqlalchemy"),
    ("httpx",     "httpx"),
]
missing = []
for mod, name in required:
    try:
        m = __import__(mod)
        ok(f"{name} {getattr(m,'__version__','OK')}")
    except ImportError:
        fail(f"{name} MANQUANT")
        missing.append(name)

# --- rich (import direct) ---
try:
    import rich
    ok(f"rich {rich.__version__}")
except ImportError:
    warn("rich manquant (non critique)")

# --- Indicateurs : TA-Lib en priorite, pandas-ta en fallback ---
step("Librairie indicateurs techniques")
talib_ok = False
try:
    import talib
    ok(f"TA-Lib {talib.__version__} (prioritaire)")
    talib_ok = True
except ImportError:
    warn("TA-Lib absent")

pandas_ta_ok = False
try:
    import pandas_ta
    ok("pandas-ta OK (fallback)")
    pandas_ta_ok = True
except ImportError:
    if not talib_ok:
        fail("pandas-ta ET TA-Lib absents - au moins un est necessaire !")
    else:
        warn("pandas-ta absent (TA-Lib utilise - OK)")

# --- Optionnels ---
step("Librairies optionnelles")
for mod, name in [("vectorbt","vectorbt"), ("trading_ig","trading-ig")]:
    try:
        m = __import__(mod)
        ok(f"{name} {getattr(m,'__version__','OK')}")
    except ImportError:
        warn(f"{name} absent")

# --- .env ---
step("Fichier .env")
env = Path(__file__).parent.parent / ".env"
if env.exists():
    ok(f".env trouve")
    from dotenv import load_dotenv
    load_dotenv(env)
    key = os.getenv("IG_API_KEY","")
    if key and key != "VOTRE_CLE_API_IG_MARKETS":
        ok("IG_API_KEY configuree")
    else:
        warn("IG_API_KEY non configuree (edite .env)")
    ok(f"Mode : {os.getenv('BONAZA_MODE','PAPER')}")
else:
    warn(".env absent - copie .env.example vers .env")

# --- Dossiers ---
step("Structure dossiers")
root = Path(__file__).parent.parent
for d in ["src","tests","data","logs","docs"]:
    p = root / d
    p.mkdir(parents=True, exist_ok=True)
    ok(f"/{d}/")

# --- Test calcul indicateurs ---
step("Test calcul indicateurs (donnees synthetiques DAX)")
try:
    import pandas as pd
    import numpy as np

    n = 150
    np.random.seed(42)
    px = 18000 + np.cumsum(np.random.randn(n) * 15)
    df = pd.DataFrame({
        "open":   px + np.random.randn(n),
        "high":   px + abs(np.random.randn(n) * 5),
        "low":    px - abs(np.random.randn(n) * 5),
        "close":  px,
        "volume": np.random.randint(500, 8000, n).astype(float),
    })

    if talib_ok:
        import talib as ta
        ema20 = ta.EMA(df["close"].values, timeperiod=20)
        ema50 = ta.EMA(df["close"].values, timeperiod=50)
        rsi   = ta.RSI(df["close"].values, timeperiod=14)
        atr   = ta.ATR(df["high"].values, df["low"].values, df["close"].values, timeperiod=14)
        macd, signal, hist = ta.MACD(df["close"].values)
        ok(f"EMA20  = {ema20[-1]:.2f}")
        ok(f"EMA50  = {ema50[-1]:.2f}")
        ok(f"RSI14  = {rsi[-1]:.2f}")
        ok(f"ATR14  = {atr[-1]:.2f}")
        ok(f"MACD   = {macd[-1]:.4f} | Signal = {signal[-1]:.4f}")
        ok("TA-Lib : tous les indicateurs fonctionnels")
    elif pandas_ta_ok:
        import pandas_ta as pta
        ema20 = pta.ema(df["close"], length=20)
        rsi   = pta.rsi(df["close"], length=14)
        ok(f"EMA20  = {ema20.iloc[-1]:.2f}")
        ok(f"RSI14  = {rsi.iloc[-1]:.2f}")
        ok("pandas-ta : indicateurs fonctionnels")

except Exception as e:
    fail(f"Erreur indicateurs : {e}")

# --- Resume ---
print(f"\n{B}{'='*50}")
if not missing and (talib_ok or pandas_ta_ok):
    print(f"{G}   PRET - Environnement Bonaza operationnel{E}")
else:
    print(f"{Y}   PARTIEL - Voir avertissements{E}")
    for m in missing:
        print(f"   pip install {m}")
print(f"{B}{'='*50}{E}\n")
