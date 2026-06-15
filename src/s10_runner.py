"""
s10_runner.py - Collecteur S10 multi-instruments pour le bot Bonaza (prod).
===========================================================================
Branche le collecteur S10 sur le flux de ticks LIVE existant (IGDataFeed.iter_ticks()).

Conception (cf docs/superpowers/specs/2026-06-14-s10-collector-design.md) :
  - UNE seule tache consomme feed.iter_ticks() (flux mixte tous epics) et route
    chaque RawTick vers l'agregateur S10 de son epic.
  - Les bougies S10 finalisees sont enfilees dans une queue asyncio ; un worker
    UNIQUE les consomme et delegue le traitement lourd (ecriture disque + fsync +
    reconstitution TA-Lib) a un ThreadPoolExecutor MONO-THREAD via run_in_executor.
    => l'event loop de trading n'est JAMAIS bloque (audit : interdits les appels
       bloquants dans la boucle) ; le mono-thread serialise l'etat mutable
       (store, services par instrument) -> thread-safe sans verrou.
  - Une tache de flush periodique ferme les buckets S10 restes ouverts quand le
    marche est lent (aucun tick pour declencher la frontiere).

NE cree AUCUNE session IG : consomme le flux deja produit par le feed partage
(respecte la regle d'or "un seul bonaza_main / une seule session IG").
NE prend AUCUNE decision de trading : capture + stockage + reconstitution (observabilite).
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Optional

from loguru import logger

from s10_aggregator import S10Aggregator
from s10_store import S10Store
from mtf_service import MTFService
from market_structure import MarketState


class S10Runner:
    def __init__(self, feed, instruments: dict, base_dir: str,
                 flush_interval_s: float = 5.0, stats_interval_s: float = 600.0) -> None:
        self.feed = feed
        self.base_dir = base_dir
        self.flush_interval_s = flush_interval_s
        self.stats_interval_s = stats_interval_s

        os.makedirs(base_dir, exist_ok=True)
        self.store = S10Store(base_dir=base_dir, fsync=True)

        # epic -> name et structures par epic/instrument
        self.epic_to_name: Dict[str, str] = {inst.epic: name for name, inst in instruments.items()}
        self.aggs: Dict[str, S10Aggregator] = {
            inst.epic: S10Aggregator(inst.epic) for inst in instruments.values()
        }
        # Construction multi-timeframe (M1..H4 + indicateurs a la cloture).
        # Gate par MTF_ENABLED (defaut on) ; si off, on stocke quand meme les S10.
        mtf_on = os.getenv("MTF_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        # Etat de structure EN RAM, partage par tous les instruments et expose
        # (runner.market_state.snapshot() / trend_summary()).
        self.market_state = MarketState()
        self.services: Dict[str, MTFService] = {
            name: MTFService(inst.epic, name, store=self.store,
                             market_state=self.market_state)
            for name, inst in instruments.items()
        } if mtf_on else {}

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)
        self._SENTINEL = object()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s10")
        self.bars = 0

    async def run(self) -> None:
        logger.info(f"[S10] collecteur demarre | base_dir={self.base_dir} | "
                    f"instruments={list(self.epic_to_name.values())} | "
                    f"MTF={'on' if self.services else 'off'}")
        worker = asyncio.create_task(self._worker(), name="s10_worker")
        flusher = asyncio.create_task(self._flusher(), name="s10_flusher")
        stats = asyncio.create_task(self._stats(), name="s10_stats")
        try:
            async for tick in self.feed.iter_ticks():
                name = self.epic_to_name.get(tick.epic)
                if name is None:
                    continue
                agg = self.aggs.get(tick.epic)
                if agg is None:
                    continue
                for candle in agg.on_tick(tick):
                    await self._queue.put((name, candle))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[S10] boucle ingestion arretee : {e}")
        finally:
            flusher.cancel()
            stats.cancel()
            # flush final des buckets ouverts
            now = datetime.now(timezone.utc)
            try:
                for epic, agg in self.aggs.items():
                    name = self.epic_to_name.get(epic)
                    for candle in agg.flush(now):
                        await self._queue.put((name, candle))
            except Exception:
                pass
            await self._queue.put(self._SENTINEL)
            await worker
            await asyncio.gather(flusher, stats, return_exceptions=True)
            self._executor.shutdown(wait=True)
            try:
                self.store.close()
                for svc in self.services.values():
                    svc.close()
            except Exception:
                pass
            logger.info(f"[S10] collecteur arrete | {self.bars} bougies S10 traitees")

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            try:
                if item is self._SENTINEL:
                    return
                name, candle = item
                await loop.run_in_executor(self._executor, self._process_one, name, candle)
                self.bars += 1
            except Exception as e:
                logger.error(f"[S10] traitement barre echoue : {e}")
            finally:
                self._queue.task_done()

    def _process_one(self, name: str, candle) -> None:
        """SYNCHRONE, execute dans l'executor mono-thread (hors event loop)."""
        self.store.append(name, candle)
        svc = self.services.get(name)
        if svc is not None:
            svc.on_s10_bar(candle)

    async def _flusher(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.flush_interval_s)
                now = datetime.now(timezone.utc)
                for epic, agg in self.aggs.items():
                    name = self.epic_to_name.get(epic)
                    for candle in agg.flush(now):
                        await self._queue.put((name, candle))
        except asyncio.CancelledError:
            return

    async def _stats(self) -> None:
        try:
            last = 0
            while True:
                await asyncio.sleep(self.stats_interval_s)
                logger.info(f"[S10] {self.bars} bougies S10 cumulees "
                            f"(+{self.bars - last} depuis le dernier point)")
                last = self.bars
                if self.services:
                    summary = self.market_state.trend_summary()
                    if summary:
                        logger.info(f"[STRUCT] tendances | {summary}")
        except asyncio.CancelledError:
            return


def build_s10_runner(feed, instruments: dict, config) -> S10Runner:
    """Construit le runner ; base_dir = <dossier de la DB>/s10 (bind-mount /app/data)."""
    from pathlib import Path
    base_dir = str(Path(config.db.path).parent / "s10")
    return S10Runner(feed, instruments, base_dir=base_dir)
