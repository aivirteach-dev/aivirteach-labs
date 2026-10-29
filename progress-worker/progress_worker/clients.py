from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .models import (
    GatewayResponse,
    ObservationAck,
    ObservationBatch,
    ProgressEvent,
    ProgressTarget,
    TargetsResponse,
)


MAX_SERVER_RESPONSE_BYTES = 1_048_576
MAX_DIAGNOSTIC_RESPONSE_BYTES = 131_072


class ProgressClientError(RuntimeError):
    pass


class TransientProgressError(ProgressClientError):
    pass


class PermanentProgressError(ProgressClientError):
    pass


class AuthenticationProgressError(ProgressClientError):
    pass


class ServerClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_targets(self, worker_id: str) -> list[ProgressTarget]:
        try:
            response = await self._client.get(
                "internal/progress/targets", params={"worker_id": worker_id}
            )
        except httpx.HTTPError as exc:
            raise TransientProgressError("progress target request failed") from exc
        self._raise_status(response)
        if len(response.content) > MAX_SERVER_RESPONSE_BYTES:
            raise PermanentProgressError("progress target response is too large")
        try:
            return TargetsResponse.model_validate_json(response.content).targets
        except ValidationError as exc:
            raise PermanentProgressError("progress target response is invalid") from exc

    async def send_events(
        self, worker_id: str, events: Sequence[ProgressEvent]
    ) -> set[str]:
        payload = ObservationBatch(
            schema_version=1, worker_id=worker_id, events=list(events)
        )
        try:
            response = await self._client.post(
                "internal/progress/observations",
                json=payload.model_dump(mode="json"),
            )
        except httpx.HTTPError as exc:
            raise TransientProgressError("progress observation request failed") from exc
        self._raise_status(response)
        if len(response.content) > MAX_SERVER_RESPONSE_BYTES:
            raise PermanentProgressError("progress observation response is too large")
        try:
            ack = ObservationAck.model_validate_json(response.content)
        except ValidationError as exc:
            raise PermanentProgressError("progress observation response is invalid") from exc
        sent_ids = {event.event_id for event in events}
        accepted = set(ack.accepted_event_ids)
        if len(accepted) != len(ack.accepted_event_ids) or not accepted <= sent_ids:
            raise PermanentProgressError("server acknowledged unknown or duplicate events")
        return accepted

    @staticmethod
    def _raise_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            raise AuthenticationProgressError("progress server authentication failed")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise TransientProgressError(
                f"progress server temporarily failed with HTTP {response.status_code}"
            )
        raise PermanentProgressError(
            f"progress server rejected the request with HTTP {response.status_code}"
        )


class DiagnosticClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def check(
        self, target: ProgressTarget, checkpoint_ids: list[str]
    ) -> GatewayResponse:
        path = (
            f"v1/diagnostics/{quote(target.lab_id, safe='')}/tools/"
            "check_course_progress"
        )
        try:
            response = await self._client.post(
                path,
                json={
                    "vm_instance_id": target.vm_instance_id,
                    "parameters": {"checkpoint_ids": checkpoint_ids},
                },
            )
        except httpx.HTTPError as exc:
            raise TransientProgressError("diagnostic request failed") from exc
        if response.status_code in {401, 403}:
            raise AuthenticationProgressError("diagnostic authentication failed")
        if not 200 <= response.status_code < 300:
            raise TransientProgressError(
                f"diagnostic gateway returned HTTP {response.status_code}"
            )
        if len(response.content) > MAX_DIAGNOSTIC_RESPONSE_BYTES:
            raise PermanentProgressError("diagnostic response is too large")
        try:
            result = GatewayResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise PermanentProgressError("diagnostic response is invalid") from exc

        if (
            result.lab_id != target.lab_id
            or result.vm_instance_id != target.vm_instance_id
            or result.data.course_id != target.runtime_course_id
        ):
            raise PermanentProgressError("diagnostic response does not match the target")
        observed_ids = [item.checkpoint_id for item in result.data.observations]
        if observed_ids != checkpoint_ids:
            raise PermanentProgressError("diagnostic response checkpoint order is invalid")
        if any(item.course_id != target.runtime_course_id for item in result.data.observations):
            raise PermanentProgressError("diagnostic observation course ID is invalid")
        skew = abs((datetime.now(UTC) - result.observed_at).total_seconds())
        if skew > 300:
            raise PermanentProgressError("diagnostic observation clock skew is too large")
        return result
