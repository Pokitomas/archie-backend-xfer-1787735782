# ARCHIE Zero

The old remote-control mesh is retired. This repository now contains a local-first computer substrate built around one authority rather than mutually resurrecting relays.

## Primary kernel: `computer.py`

`computer.py` is a small software computer, not a remote shell wrapper. It defines its own:

- stack language + compiler
- bytecode VM
- process model
- cooperative quantum scheduler
- mailbox IPC
- event log
- content-addressed object store + mutable names
- typed capability boundary
- persistent process snapshots/recovery
- opcode profiler
- breakpoint state

The language supports arithmetic/comparison, labels/jumps, calls/returns, process spawn, send/receive, sleep/yield, objects, names, events, assertions, checkpoints, and explicitly named capabilities.

There is deliberately no arbitrary shell opcode and no cloud ingress. Filesystem capabilities are confined to the configured workspace.

Run its full court:

```powershell
python computer.py self-test
```

The court covers compiler/VM execution, scheduler behavior, parent-child IPC, durable objects, filesystem capability confinement, stable node identity, checkpoint recovery, event sequencing, and a mid-flight restart where a runnable process must resume instead of becoming orphaned by stale scheduler state.

Run a program:

```powershell
python computer.py run program.sol
python computer.py status
python computer.py checkpoint
```

## Durable resident: `resident.py`

`resident.py` is the smaller state/command substrate underneath experiments that do not need the VM. It has a stable persisted `node_id`, ephemeral `boot_id`, monotonic generations, journal-before-snapshot commits, durable command deduplication, atomic local inbox/receipts, and typed operations only (`observe`, `checkpoint`, `status`, `shutdown`).

```powershell
python resident.py self-test
python resident.py serve
```

## Identity rule

A restart may change a process `boot_id`; it never changes durable machine identity or invalidates committed state. Transport/session identity is not machine identity.

## No old cloud control plane

GitHub Actions, Vercel relays, Cloudflare tunnels, self-hosted runners, rotating remote session secrets, hidden PowerShell workers, and arbitrary remote `exec` are not part of the replacement protocol.

`legacy_cleanup.ps1` enumerates the observed old Windows/WSL persistence roots. It is a dry run by default; `-Apply` performs the exact-scope removal on the machine where it is run.
