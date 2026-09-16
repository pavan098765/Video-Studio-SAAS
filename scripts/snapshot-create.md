# Create a Vultr snapshot after bake-image.sh succeeds

Prereqs: `VULTR_API_KEY`, instance id of the baked VM.

```bash
# 1. Stop leftover jobs, leave the disk clean of /tmp smokes if desired.
# 2. Snapshot (do NOT halt-to-save-money as a substitute; snapshot from a running or stopped instance per Vultr docs).

curl -sS -X POST "https://api.vultr.com/v2/snapshots" \
  -H "Authorization: Bearer $VULTR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"instance_id\": \"$VULTR_INSTANCE_ID\", \"description\": \"kala-studio-engine\"}"
```

Record `id` as `VULTR_SNAPSHOT_ID` in genedit env (`SNAPSHOT_ID` / `VULTR_SNAPSHOT_ID`).

Plan: Cloud Compute 6 vCPU / 16 GB / 320 GB. Region: same as create calls in `vultr_pool.py`.

Never bake `GOOGLE_API_KEY`, R2 secrets, or Firebase into the snapshot. Inject at boot.
