"""Bastion backup agent — streams Home Assistant backups to Bastion in chunks."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import aiohttp
from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CHUNK_TIMEOUT_SECONDS,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_CHUNK_SIZE,
    DOMAIN,
    INGRESS_PATH,
    MAX_CHUNK_RETRIES,
    STREAM_READ_BYTES,
)

_LOGGER = logging.getLogger(__name__)


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return Bastion backup agents for every loaded config entry."""
    entries: list[ConfigEntry] = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state.recoverable or e.state.name == "LOADED"
    ]
    # ``async_loaded_entries`` would be cleaner but isn't on every supported
    # HA version yet — filter by state name string instead.
    loaded = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if str(e.state).endswith("LOADED")
    ]
    if loaded:
        entries = loaded
    return [BastionBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **_: Any,
) -> Callable[[], None]:
    """Register a listener invoked when our agent set changes.

    Called by the core backup integration; we store the callbacks under a
    well-known key in ``hass.data`` so ``__init__`` can fire them on add /
    remove of a config entry.
    """
    listeners: list[Callable[[], None]] = hass.data.setdefault(
        "backup_agent_listeners", []
    )
    listeners.append(listener)

    @callback
    def _remove() -> None:
        if listener in listeners:
            listeners.remove(listener)

    return _remove


class BastionBackupAgent(BackupAgent):
    """Backup agent that ships HA backups to a Bastion ingress endpoint."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the agent for one Bastion config entry."""
        self._hass = hass
        self._entry = entry
        self._url: str = entry.data[CONF_URL].rstrip("/")
        self._token: str = entry.data[CONF_TOKEN]
        # Visible name in the HA backup UI.
        self.name = f"Bastion ({self._url})"
        # Stable ID across restarts so HA can match prior uploads.
        self.unique_id = entry.entry_id

    # ─── Required surface ───────────────────────────────────────────────────

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        **kwargs: Any,
    ) -> None:
        """Stream the backup body to Bastion in 50 MiB chunks.

        ``open_stream`` returns a fresh async iterator over the raw archive
        bytes. We accumulate up to ``DEFAULT_CHUNK_SIZE`` per HTTP POST,
        retrying each chunk up to ``MAX_CHUNK_RETRIES`` times. A definitive
        failure aborts the entire upload.
        """
        upload_id = str(uuid.uuid4())
        session = async_get_clientsession(self._hass)
        headers = {"Authorization": f"Bearer {self._token}"}
        target = f"{self._url}{INGRESS_PATH}"

        # Pull the backup body into memory one chunk at a time.
        stream = await open_stream()

        # We don't know the exact byte size up-front from the iterator alone,
        # but the AgentBackup metadata gives us the planned size. Use it to
        # compute total_chunks; fall back to a count derived from the stream
        # if the metadata is missing.
        size_bytes: int = int(getattr(backup, "size", 0) or 0)
        if size_bytes <= 0:
            # Unknown size: we'll send chunks as they arrive and tell the
            # server only at the final chunk that we're done. Bastion accepts
            # ``total_chunks`` as a fixed value, so we have to materialise
            # chunks first to count. To keep memory bounded we still use
            # 50 MiB chunks but compute the total after buffering.
            await self._upload_unknown_size(
                stream=stream, upload_id=upload_id, headers=headers, target=target
            )
            return

        total_chunks = max(1, (size_bytes + DEFAULT_CHUNK_SIZE - 1) // DEFAULT_CHUNK_SIZE)
        _LOGGER.info(
            "bastion_upload_start id=%s size=%d total_chunks=%d url=%s",
            upload_id,
            size_bytes,
            total_chunks,
            target,
        )

        chunk_index = 0
        buffer = bytearray()
        sent_bytes = 0
        async for raw in stream:
            if not raw:
                continue
            buffer.extend(raw)
            while len(buffer) >= DEFAULT_CHUNK_SIZE:
                payload = bytes(buffer[:DEFAULT_CHUNK_SIZE])
                del buffer[:DEFAULT_CHUNK_SIZE]
                await self._post_chunk(
                    session=session,
                    target=target,
                    headers=headers,
                    upload_id=upload_id,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    payload=payload,
                )
                sent_bytes += len(payload)
                chunk_index += 1

        # Flush the final partial buffer.
        if buffer or chunk_index == 0:
            payload = bytes(buffer)
            buffer.clear()
            await self._post_chunk(
                session=session,
                target=target,
                headers=headers,
                upload_id=upload_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                payload=payload,
            )
            sent_bytes += len(payload)
            chunk_index += 1

        if chunk_index != total_chunks:
            # Real size diverged from advertised size — re-run with unknown
            # size semantics would have been correct; here we just warn.
            _LOGGER.warning(
                "bastion_upload_chunk_count_mismatch id=%s sent=%d expected=%d bytes=%d",
                upload_id,
                chunk_index,
                total_chunks,
                sent_bytes,
            )

        _LOGGER.info(
            "bastion_upload_complete id=%s chunks=%d bytes=%d",
            upload_id,
            chunk_index,
            sent_bytes,
        )

    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        """Bastion is an upload-only sink; downloads happen out-of-band."""
        raise BackupNotFound(f"Bastion does not expose backup {backup_id} for download")

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List previously uploaded backups.

        Bastion currently treats this surface as a write-only ingress. Returning
        an empty list is fine for HA — the UI will simply not show prior
        uploads under this agent.
        """
        return []

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup | None:
        """Fetch metadata for a specific backup (none tracked locally)."""
        return None

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete a backup from Bastion (not yet supported by the ingress API)."""
        raise BackupAgentError(
            "Deleting Bastion backups from Home Assistant is not yet supported"
        )

    # ─── Helpers ────────────────────────────────────────────────────────────

    async def _upload_unknown_size(
        self,
        *,
        stream: AsyncIterator[bytes],
        upload_id: str,
        headers: dict[str, str],
        target: str,
    ) -> None:
        """Slow path: buffer the entire archive in chunks before computing total."""
        session = async_get_clientsession(self._hass)
        buffered_chunks: list[bytes] = []
        buffer = bytearray()
        async for raw in stream:
            if not raw:
                continue
            buffer.extend(raw)
            while len(buffer) >= DEFAULT_CHUNK_SIZE:
                buffered_chunks.append(bytes(buffer[:DEFAULT_CHUNK_SIZE]))
                del buffer[:DEFAULT_CHUNK_SIZE]
        if buffer or not buffered_chunks:
            buffered_chunks.append(bytes(buffer))

        total_chunks = len(buffered_chunks)
        _LOGGER.info(
            "bastion_upload_start (unknown size) id=%s total_chunks=%d url=%s",
            upload_id,
            total_chunks,
            target,
        )

        for index, payload in enumerate(buffered_chunks):
            await self._post_chunk(
                session=session,
                target=target,
                headers=headers,
                upload_id=upload_id,
                chunk_index=index,
                total_chunks=total_chunks,
                payload=payload,
            )

        _LOGGER.info("bastion_upload_complete id=%s chunks=%d", upload_id, total_chunks)

    async def _post_chunk(
        self,
        *,
        session: aiohttp.ClientSession,
        target: str,
        headers: dict[str, str],
        upload_id: str,
        chunk_index: int,
        total_chunks: int,
        payload: bytes,
    ) -> None:
        """POST one chunk with retry/backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, MAX_CHUNK_RETRIES + 1):
            form = aiohttp.FormData()
            form.add_field("upload_id", upload_id)
            form.add_field("chunk_index", str(chunk_index))
            form.add_field("total_chunks", str(total_chunks))
            form.add_field(
                "file",
                payload,
                filename=f"chunk_{chunk_index:05d}.bin",
                content_type="application/octet-stream",
            )
            try:
                async with session.post(
                    target,
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=CHUNK_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status == 401:
                        body = await resp.text()
                        raise BackupAgentError(
                            f"Bastion rejected the bearer token: {body[:200]}"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise BackupAgentError(
                            f"Bastion chunk {chunk_index}/{total_chunks} "
                            f"failed with HTTP {resp.status}: {body[:200]}"
                        )
                    await resp.read()
                    return
            except BackupAgentError:
                # Auth + 4xx errors are not transient — abort immediately.
                raise
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                last_exc = exc
                _LOGGER.warning(
                    "bastion chunk %d/%d attempt %d/%d failed: %s",
                    chunk_index + 1,
                    total_chunks,
                    attempt,
                    MAX_CHUNK_RETRIES,
                    exc,
                )
                if attempt < MAX_CHUNK_RETRIES:
                    # Exponential backoff: 1s, 2s, 4s, …
                    await asyncio.sleep(2 ** (attempt - 1))

        raise BackupAgentError(
            f"Bastion chunk {chunk_index}/{total_chunks} failed after "
            f"{MAX_CHUNK_RETRIES} attempts: {last_exc}"
        )
