# Bonaza VPS2 — image reproductible, versions epinglees = parite avec la prod VPS1.
# Python 3.11.13 (= prod), Debian bookworm (vs bullseye EOL), TA-Lib C 0.6.x via .deb
# officiel HTTPS (vs ancien wget HTTP sans checksum), user uid 1000 (= ubuntu VPS2).
FROM python:3.11.13-slim-bookworm AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Paris

# --- TA-Lib C library 0.6.4 via paquet .deb officiel (HTTPS + checksum) ---
# Le wrapper Python TA-Lib==0.6.8 (epingle dans le lock) s'y lie. Ligne 0.6.x
# stable numeriquement pour ATR/ADX/SMA/EMA/RSI (les fonctions utilisees).
WORKDIR /tmp
RUN wget -q https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib_0.6.4_amd64.deb \
        -O ta-lib.deb && \
    echo "Installing TA-Lib C lib" && \
    dpkg -i ta-lib.deb && \
    rm -f ta-lib.deb

WORKDIR /app

# Lockfile epingle (143 paquets, = pip freeze prod) -> reproduction exacte
COPY requirements.lock .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock

COPY src/ ./src/

# User non-root uid 1000 = ubuntu (proprietaire des bind mounts /app/data, /app/logs
# sur le VPS2). Difference vs VPS1 (uid 1005=DIDDESS) : adaptation indispensable.
RUN useradd -m -u 1000 bonaza && chown -R bonaza:bonaza /app
USER bonaza

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "src/main.py"]
