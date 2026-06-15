"""
s10_aggregator.py - Agregation des ticks bruts en bougies 10 secondes.
======================================================================
Reutilise OHLCVCandle et RawTick du code live (vps_snapshot/src/data_feed.py).

Principe (cf spec section 2.1) :
  - bucket = floor(epoch_s / 10) * 10  (frontiere alignee sur l'epoch UTC)
  - sur chaque RawTick : mid = (bid+ask)/2
      * meme bucket  -> MAJ OHLC (open = 1er tick, high/low, close = dernier),
        bid/ask close = dernier tick, volume cumule, tick_count++
      * bucket posterieur -> FINALISE la bougie precedente (is_complete=True),
        l'emet, ouvre la nouvelle
  - flush(now) : ferme un bucket reste ouvert quand aucun tick n'arrive (marche lent)
    SI le bucket courant est perime (now appartient a un bucket posterieur).

ZERO LOOK-AHEAD : une bougie n'est JAMAIS emise tant qu'un tick d'un bucket
posterieur n'est pas arrive (ou flush temporise). Les buckets sans tick restent
des TROUS : aucune barre n'est fabriquee.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import List, Optional

# Reutilisation du code live (chemin absolu injecte par l'appelant ; fallback ici).

from data_feed import OHLCVCandle, RawTick  # noqa: E402

S10_SECONDS = 10
S10_SCALE = "S10"


def bucket_floor(epoch_s: float) -> int:
    """Retourne le debut du bucket 10s contenant epoch_s : floor(epoch/10)*10.

    On floore en MILLISECONDES entieres pour eviter qu'un tick sub-microseconde
    (ex: 1750000009.9999999 issu d'erreurs flottantes) ne bascule a tort dans le
    bucket suivant. Les ts IG sont en ms : round(epoch*1000) est exact pour eux.
    """
    epoch_ms = round(epoch_s * 1000)
    return (epoch_ms // (S10_SECONDS * 1000)) * S10_SECONDS


class S10Aggregator:
    """
    Agrege les RawTick d'un instrument en bougies S10 (OHLCVCandle scale='S10').

    Usage :
        agg = S10Aggregator(epic)
        for tick in source:
            for candle in agg.on_tick(tick):   # 0 ou 1 bougie finalisee
                ...
        # en fin de session / marche lent :
        for candle in agg.flush(now):
            ...
    """

    def __init__(self, epic: str) -> None:
        self.epic = epic
        self._bucket: Optional[int] = None        # debut du bucket courant (epoch s)
        self._candle: Optional[OHLCVCandle] = None  # bougie en cours de formation
        # Dernier bucket DEJA EMIS (finalise via frontiere OU via flush). Garde
        # anti-duplicat / anti-look-ahead persistante : un tick en retard appartenant
        # a un bucket <= a celui-ci ne doit JAMAIS rouvrir une bougie deja close.
        self._last_emitted_bucket: Optional[int] = None

    # -- lecture --------------------------------------------------------------

    def current(self) -> Optional[OHLCVCandle]:
        """Bougie en cours de formation (is_complete=False) ou None."""
        return self._candle

    # -- ingestion ------------------------------------------------------------

    def on_tick(self, tick: RawTick) -> List[OHLCVCandle]:
        """
        Ingere un tick. Retourne la liste (0 ou 1) des bougies S10 FINALISEES
        par ce tick (frontiere de bucket franchie).
        """
        epoch = tick.timestamp.timestamp()
        b = bucket_floor(epoch)
        mid = tick.mid

        finalized: List[OHLCVCandle] = []

        # Garde anti-duplicat PERSISTANTE : un tick dont le bucket a deja ete emis
        # (frontiere OU flush) est ignore, meme si _bucket vaut None (post-flush).
        # Sans ca, un tick en retard du bucket deja clos rouvrirait une 2e bougie
        # pour le MEME bucket (double barre S10).
        if self._last_emitted_bucket is not None and b <= self._last_emitted_bucket:
            return finalized

        if self._candle is None:
            # premiere bougie (ou reprise apres flush) : on ouvre un bucket
            # strictement posterieur au dernier emis (garantie par le check ci-dessus).
            self._open_bucket(b, tick, mid)
            return finalized

        if b == self._bucket:
            # meme bucket : MAJ de la bougie courante
            self._update(tick, mid)
            return finalized

        if b > self._bucket:
            # bucket posterieur : on finalise le precedent (anti-look-ahead),
            # puis on ouvre le nouveau directement sur ce tick.
            # Les buckets intermediaires sans tick restent des TROUS (pas de barre).
            finalized.append(self._finalize())
            self._open_bucket(b, tick, mid)
            return finalized

        # b < self._bucket : tick en retard (hors d'ordre). On l'ignore pour ne
        # pas reecrire une bougie deja close (ZERO look-ahead / immutabilite).
        return finalized

    def flush(self, now: datetime) -> List[OHLCVCandle]:
        """
        Ferme la bougie courante SI son bucket est perime (now appartient a un
        bucket strictement posterieur). Utilise quand le marche est lent et
        qu'aucun tick n'arrive pour declencher la frontiere.
        Retourne 0 ou 1 bougie finalisee.
        """
        if self._candle is None or self._bucket is None:
            return []
        now_bucket = bucket_floor(now.timestamp())
        if now_bucket > self._bucket:
            return [self._finalize()]
        return []

    # -- interne --------------------------------------------------------------

    def _open_bucket(self, bucket: int, tick: RawTick, mid: float) -> None:
        self._bucket = bucket
        self._candle = OHLCVCandle(
            epic=self.epic,
            scale=S10_SCALE,
            timestamp=datetime.fromtimestamp(bucket, tz=timezone.utc),
            open=mid, high=mid, low=mid, close=mid,
            volume=float(tick.volume or 0.0),
            tick_count=1,
            bid_close=tick.bid,
            ask_close=tick.ask,
            is_complete=False,
        )

    def _update(self, tick: RawTick, mid: float) -> None:
        c = self._candle
        if mid > c.high:
            c.high = mid
        if mid < c.low:
            c.low = mid
        c.close = mid
        c.volume += float(tick.volume or 0.0)
        c.tick_count += 1
        c.bid_close = tick.bid
        c.ask_close = tick.ask

    def _finalize(self) -> OHLCVCandle:
        c = self._candle
        c.is_complete = True
        # On memorise le bucket emis AVANT de le remettre a None : la garde
        # anti-duplicat de on_tick s'appuie sur _last_emitted_bucket, pas sur _bucket.
        self._last_emitted_bucket = self._bucket
        self._candle = None
        self._bucket = None
        return c
