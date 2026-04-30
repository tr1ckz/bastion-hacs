# Bastion Backup Gateway

Home Assistant custom integration that registers
[Bastion](https://github.com/McGeaverBeaver/Bastion) as a native **Backup
Agent**. When Home Assistant produces a backup (manual or scheduled), this
integration streams the resulting `.tar` to the Bastion ingress API in 50 MiB
chunks — staying under the Cloudflare 100 MB request-body cap so it works
through Cloudflare Tunnel as well as on the LAN.

## Install (HACS)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/tr1ckz/bastion-hacs` as type **Integration**
3. Install **Bastion Backup Gateway**, restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Bastion Backup Gateway**
5. Enter:
   - **Bastion API URL** — e.g. `https://bastion.example.com` or `http://192.168.1.50:8000`
   - **Ingress bearer token** — the value of `BASTION_INGRESS_TOKEN` on the Bastion server

The integration validates connectivity against `GET /api/ingress/healthz`
before saving.

## Usage

Once configured, Bastion appears as a **backup location** in
**Settings → System → Backups**. Tick it on any backup (one-shot or scheduled)
and Home Assistant will push the archive through:

```
POST {bastion_url}/api/ingress/ha-backup/chunk
Authorization: Bearer <token>
Content-Type: multipart/form-data
  upload_id     = <uuid>
  chunk_index   = 0…N-1
  total_chunks  = N
  file          = <50 MiB binary chunk>
```

Bastion reassembles the chunks into
`homeassistant_backup_<UTC-timestamp>.tar` under its staging directory and
hands the archive off to the Bastion processing pipeline (zstd, Duplicacy,
etc.).

## Folder layout

```
bastion-hacs/
├─ hacs.json
├─ README.md
└─ custom_components/
   └─ bastion_gateway/
      ├─ __init__.py
      ├─ backup.py
      ├─ config_flow.py
      ├─ const.py
      ├─ manifest.json
      ├─ strings.json
      └─ translations/
         └─ en.json
```

## Notes

* Each chunk is retried up to **3 times** with exponential backoff before the
  whole upload is aborted; auth (401) and other 4xx errors are non-transient
  and abort immediately.
* The same configuration works whether Home Assistant reaches Bastion through
  a Cloudflare Tunnel or directly on the LAN. The 50 MiB chunking is what
  makes Cloudflare possible — there is no separate "Cloudflare mode."
