"""Constants for the Bastion Backup Gateway integration."""
from __future__ import annotations

DOMAIN = "bastion_gateway"

CONF_URL = "url"
CONF_TOKEN = "token"

# 50 MiB — keeps each request well under the Cloudflare 100 MB body cap.
DEFAULT_CHUNK_SIZE = 50 * 1024 * 1024

# Read buffer when streaming the backup body into memory for one chunk. We
# accumulate up to DEFAULT_CHUNK_SIZE before flushing one HTTP POST.
STREAM_READ_BYTES = 1024 * 1024

# Per-chunk retry budget before the upload is aborted.
MAX_CHUNK_RETRIES = 3

# Per-chunk HTTP timeout (seconds). 50 MiB over a slow link can take a while.
CHUNK_TIMEOUT_SECONDS = 600

INGRESS_PATH = "/api/ingress/ha-backup/chunk"
