# ARCHIE Zero

A replacement control/state substrate with one authority and no cloud command mesh.

## Invariants

- One resident process owns state.
- Stable `node_id` survives restarts; ephemeral `boot_id` only identifies the current process.
- Every mutating command has a caller-chosen id and is exactly-once by durable deduplication.
- A commit is journaled + fsynced before the atomic snapshot is replaced.
- Recovery replays committed journal records newer than the snapshot.
- Ingress and receipts are local atomic files. No GitHub Actions, Vercel relay, Cloudflare tunnel, self-hosted runner, arbitrary shell endpoint, or rotating remote session secret is part of the protocol.
- Supported operations are typed (`observe`, `checkpoint`, `status`, `shutdown`). There is deliberately no `exec` operation.

## Run

```powershell
python resident.py self-test
python resident.py serve
```

From another local terminal:

```powershell
python resident.py submit observe --payload '{"source":"keyboard","data":[1,2,3]}'
python resident.py submit status
```

Commands arrive at `%USERPROFILE%\.archie-zero\inbox`; durable receipts appear in `receipts`.

## Why this replaces the old mesh

The old topology allowed transport identity, service lifetime, and machine identity to drift independently. ARCHIE Zero has one durable identity and one monotonic state generation. A restart may change `boot_id`; it never invalidates the durable node identity or committed command history, so clients do not become orphaned merely because the process restarted.
