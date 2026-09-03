# Protocol

Command envelope:

```json
{"schema":1,"id":"caller-unique-id","op":"observe","payload":{}}
```

Receipt:

```json
{"ok":true,"node_id":"stable-machine-resident-id","generation":42}
```

## Identity model

`node_id` is created once and persisted in `identity.json`. `boot_id` changes on each process launch and is diagnostic only. Authorization must never depend on `boot_id` continuity.

## Commit model

For each new mutating command id:

1. Apply the typed transition in memory.
2. Increment monotonic `generation`.
3. Append the full committed state record to `journal.jsonl` and `fsync` it.
4. Atomically replace `state.json`.
5. Emit a receipt.

On restart, load the snapshot and replay any committed journal generation newer than the snapshot. Duplicate command ids return the original committed generation instead of reapplying the effect.

## Failure semantics

A producer owns an inbox filename by atomic rename. The resident owns it after renaming to `archive/*.processing`. A completed command is renamed to `*.done` only after its receipt is atomically written. An interrupted `.processing` item is evidence of incomplete handling and can be recovered deliberately rather than guessed from transport state.
