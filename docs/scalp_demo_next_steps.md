# Scalp S10+M5 démo — état & étapes suivantes (handoff)

_Dernière mise à jour : 2026-06-15 ~10:15 UTC._

## 1. Où on en est

- **Backtest** (`src/backtest_scalp_m1.py`) : momentum 3×S10 + filtre tendance M5,
  variante **with-trend-only** = profitable sur DAX (PF 1.05, Sharpe/trade 0.70,
  +1312 pts) sur données **Dukascopy S10 denses** (nov-2025→mai-2026).
- **Démo live** (`bonaza_demo`, conteneur isolé) : tourne sur le compte **DÉMO IG
  Z5GGQ7**, volumes `data_demo/` + `logs_demo/`, entrypoint `src/main_scalp_demo.py`,
  stratégie `src/scalp_strategy.py`. Healthy, **0 trade**.

## 2. ⚠️ BLOCAGE IDENTIFIÉ : le « S10 » live n'est pas du vrai 10 s

Mesuré sur live ET démo (2026-06-15) : les bougies « S10 » sont espacées de
**300 s (5 min)**, **0 paire contiguë à 10 s**. Cause : l'abonnement CHART du feed
est en **`5MINUTE`** (`src/instruments.py` → `SUBSCRIPTIONS`, scale="5MINUTE"), et
sur ce flux IG le tick ne tombe qu'**une fois par bougie 5 min** (consolidation).
L'agrégateur S10 (`src/s10_aggregator.py`) ne reçoit donc qu'un point / 5 min.

**Conséquences :**
- La stratégie exige 3 bougies S10 **contiguës (10 s)** → jamais satisfait → 0 signal.
- Le MTF (M1…H4) et la structure reconstruits depuis ce « S10 » sont en fait
  du 5 min déguisé. Le backtest (Dukascopy 10 s dense) ne reflète donc PAS le grain
  réellement fourni par le live IG tel qu'abonné.

## 3. Correctif prévu (à faire plus tard)

Obtenir un vrai grain sub-minute via un abonnement **`CHART:{epic}:TICK`** (flux tick
réel : chaque variation BID/OFR/LTP/LTV/UTM) et router ces updates vers la tick_queue
→ l'agrégateur S10 produit alors de vraies bougies 10 s.

Fichiers concernés : `src/data_feed.py` (ajouter la souscription TICK + extraction
des ticks réels ; aujourd'hui `_subscribe_chart` n'utilise que le scale 5MINUTE).
**Gater derrière une env** (ex. `CHART_TICK_ENABLED`) off par défaut.

## 4. 🔒 CONTRAINTES DE SÉCURITÉ (impératif)

- **NE PAS TOUCHER `bonaza_main`** (moteur **copieur** LIVE, compte réel LUZQM) :
  **positions ouvertes** au 2026-06-15. Pas de rebuild-restart, pas de recreate.
- Démo et live partagent l'image `bonaza:latest` → **risque** : un `docker compose
  up -d` global recréerait `bonaza_main` sur une nouvelle image.
  **Étape 0 avant tout correctif** : donner au démo sa **propre image**
  (`bonaza-demo:latest`, bloc `build:` sur le service `bonaza_demo`) pour que
  `bonaza:latest` ne soit **jamais** reconstruite. Ensuite ne recréer **que**
  `bonaza_demo`. Ne jamais lancer `docker compose up -d` sans nom de service.

## 5. Séquence de reprise (quand positions live closes / feu vert)

1. Isolation image : `build:` + `image: bonaza-demo:latest` sur `bonaza_demo`.
2. `data_feed` : souscription `CHART:{epic}:TICK` gatée par `CHART_TICK_ENABLED`.
3. `docker compose build bonaza_demo` puis `docker compose up -d bonaza_demo` (SEUL).
4. Vérifier la densité S10 (paires contiguës 10 s > 0) avant d'attendre des trades.
5. Sessions : DAX/CAC 08–16 UTC, Or 16–21 UTC. Tendance M5 doit être bull/bear.

## 5bis. Copieur LIVE « en retard » (constat 2026-06-15) — À INVESTIGUER

**Symptôme** : `bonaza_main` (copieur Telegram) ouvre les positions « en retard ».

**Diagnostic (lecture seule)** : la latence est dans la **réception Telegram**, PAS
l'exécution. `telegram_reader` logge `[TG] LATENCE : message recu avec Xs de retard`
(56s, 170s, 61s le 15/06) ; une fois reçu, l'ordre part en ~1 s (CONFIRMS ~1 s après).
Sporadique (3 épisodes/jour), variable, **aucune déconnexion Telethon loggée**, CPU 3,6 %.

**Cause probable** : retard de livraison Telegram/Telethon (rattrapage `getDifference`
/ réseau), souvent hors contrôle. **Pas prouvé lié** aux ajouts S10/MTF (collecteur
quasi inactif car ticks ~5 min — cf §2). Non visible dans le log du 14/06 avant le
déploiement MTF (23:49), mais log partiel (→22:01) donc non concluant.

**À faire plus tard (fenêtre SANS positions ouvertes — sinon NE PAS redémarrer le copieur) :**
1. Test décisif : relancer `bonaza_main` avec `S10_COLLECTOR_ENABLED=false` (+ `MTF_ENABLED=false`)
   et voir si les LATENCE disparaissent → confirme/infirme l'implication du collecteur.
2. Inspecter `src/telegram_reader.py` : push Telethon vs polling, option `catch_up`,
   stabilité de la session.
3. **Fix robuste recommandé** : sortir le lecteur copieur Telegram dans son PROPRE
   conteneur/processus, découplé de l'event loop de trading, pour qu'aucune activité
   tick/S10 ne puisse retarder la réception des signaux.

## 6. Sujet ouvert : broker XM / MetaTrader 5

PoC connexion écrit (`mt5_poc.py`, à lancer sur Windows). XM = MT4/MT5, pas d'API
REST officielle ; route = package `MetaTrader5` (terminal MT5 requis, Windows/Wine).
Couche stratégie réutilisable ; à écrire un adaptateur Feed/Executor/Rules MT5.
En attente de la sortie du PoC (noms de symboles XM, exécution démo).
