from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
from collections.abc import Sequence

from .clients import (
    AuthenticationProgressError,
    DiagnosticClient,
    PermanentProgressError,
    ProgressClientError,
    ServerClient,
    TransientProgressError,
)
from .config import ConfigurationError, Settings
from .models import ProgressTarget
from .store import PendingEvent, ProgressStore


LOG = logging.getLogger("aivirteach.progress")


class ProgressWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ProgressStore,
        server: ServerClient,
        diagnostic: DiagnosticClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.server = server
        self.diagnostic = diagnostic

    async def run_once(self) -> None:
        # Flush persisted work before probing. A temporary target-list failure
        # must not prevent delivery of observations already in the outbox.
        await self.dispatch_once()
        targets = await self.server.fetch_targets(self.settings.worker_id)
        for target in targets:
            try:
                await self._probe_target(target)
            except AuthenticationProgressError:
                raise
            except ProgressClientError as exc:
                LOG.warning("target %s could not be observed: %s", target.target_id, exc)
            except Exception:
                LOG.exception("target %s failed unexpectedly", target.target_id)
        await self.dispatch_once()

    async def _probe_target(self, target: ProgressTarget) -> None:
        for _ in range(self.settings.max_probes_per_target):
            frontier = self.store.frontier(target)
            if frontier.checkpoint_id is None or not frontier.due:
                return
            result = await self.diagnostic.check(target, [frontier.checkpoint_id])
            observation = result.data.observations[0]
            self.store.record_observation(
                worker_id=self.settings.worker_id,
                target=target,
                observation=observation,
                observed_at=result.observed_at,
                poll_seconds=self.settings.poll_seconds,
                unknown_backoff_max_seconds=self.settings.unknown_backoff_max_seconds,
                heartbeat_seconds=self.settings.heartbeat_seconds,
            )
            if observation.state != "passed":
                return

    async def dispatch_once(self) -> None:
        pending = self.store.due_events(limit=self.settings.batch_size)
        if not pending:
            return
        await self._dispatch_group(pending)

    async def _dispatch_group(self, pending: list[PendingEvent]) -> None:
        try:
            accepted = await self.server.send_events(
                self.settings.worker_id, [item.event for item in pending]
            )
        except AuthenticationProgressError:
            # Keep all rows pending. Token rotation should recover them with the
            # same event IDs; turning auth errors into dead letters loses data.
            raise
        except PermanentProgressError as exc:
            if len(pending) == 1:
                self.store.mark_dead(pending, error=str(exc))
                LOG.error(
                    "server permanently rejected event %s: %s",
                    pending[0].event.event_id,
                    exc,
                )
                return
            # The Server validates a batch atomically. Split permanent 4xx
            # failures so one stale/colliding event cannot dead-letter valid
            # observations that happened to share its delivery batch.
            midpoint = len(pending) // 2
            LOG.warning(
                "isolating a permanent rejection in a batch of %d events",
                len(pending),
            )
            await self._dispatch_group(pending[:midpoint])
            await self._dispatch_group(pending[midpoint:])
            return
        except TransientProgressError as exc:
            self._retry(pending, str(exc))
            return

        sent_ids = {item.event.event_id for item in pending}
        self.store.mark_delivered(accepted)
        unacknowledged = [item for item in pending if item.event.event_id not in accepted]
        if unacknowledged:
            self._retry(unacknowledged, "server did not acknowledge the event")
        LOG.info("delivered %d/%d progress events", len(accepted & sent_ids), len(pending))

    def _retry(self, events: Sequence[PendingEvent], error: str) -> None:
        if not events:
            return
        attempts = max(item.attempts for item in events) + 1
        base = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** min(attempts - 1, 12)),
        )
        delay = base * random.uniform(0.8, 1.2)
        self.store.mark_retry(list(events), error=error, delay=delay)
        LOG.warning("retained %d events for retry", len(events))


async def _run(settings: Settings, *, once: bool) -> int:
    store = ProgressStore(settings.database_path)
    server = ServerClient(
        base_url=settings.server_url,
        token=settings.server_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    diagnostic = DiagnosticClient(
        base_url=settings.diagnostic_url,
        token=settings.diagnostic_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    worker = ProgressWorker(
        settings=settings, store=store, server=server, diagnostic=diagnostic
    )
    try:
        if once:
            await worker.run_once()
            return 0

        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopping.set)
            except NotImplementedError:
                pass
        while not stopping.is_set():
            try:
                await worker.run_once()
            except AuthenticationProgressError as exc:
                LOG.error("worker authentication configuration is invalid: %s", exc)
            except ProgressClientError as exc:
                LOG.warning("worker cycle failed: %s", exc)
            except Exception:
                LOG.exception("worker cycle failed unexpectedly")
            try:
                await asyncio.wait_for(stopping.wait(), settings.poll_seconds)
            except TimeoutError:
                pass
        return 0
    finally:
        await server.close()
        await diagnostic.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIVirTeach progress observation worker")
    parser.add_argument("--once", action="store_true", help="run one poll and delivery cycle")
    parser.add_argument("--log-level", default="INFO")
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
        return asyncio.run(_run(settings, once=arguments.once))
    except ConfigurationError as exc:
        LOG.error("invalid progress worker configuration: %s", exc)
        return 2
    except AuthenticationProgressError as exc:
        LOG.error("progress worker authentication failed: %s", exc)
        return 3
    except ProgressClientError as exc:
        LOG.error("progress worker request failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
