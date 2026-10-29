# AIVirTeach Progress Worker

This is a background worker, not an HTTP service. It has no port and is not
included in Unified Docs.

For each trusted target returned by `aivirteach-server`, it checks only the
earliest not-yet-achieved guest checkpoint through Diagnostic Gateway port
8765. Normalized observations are committed to SQLite together with an outbox
event in one transaction. The Server acknowledges individual `event_id`
values; unacknowledged events retain the same ID and are retried.
If an atomic Server batch is permanently rejected, the Worker bisects it so a
single stale event is dead-lettered without discarding valid neighbors.

```text
aivirteach-server targets
  -> Progress Worker
     -> Diagnostic Gateway :8765
        -> QGA -> /usr/local/bin/aivirteach-check-progress
     -> SQLite WAL/outbox
     -> aivirteach-server observations
```

The Worker never receives the VM Manager token, Agent token, model API key,
libvirt socket, VM password, or an arbitrary command/path. It stores bounded,
normalized evidence only; raw stdout/stderr are rejected by the Gateway. For a
progress check, the Worker sends the Server-owned expected `vm_instance_id`.
The Gateway resolves `lab_id`, rejects a mismatch before QGA, executes through
that immutable UUID, and returns it again for the Worker to verify.

## Run locally

```bash
cp progress-worker/config/progress.env.example \
  progress-worker/config/progress.env.local
chmod 600 progress-worker/config/progress.env.local
set -a
source progress-worker/config/progress.env.local
set +a

./progress-worker/start_progress_worker.sh --once
./progress-worker/start_progress_worker.sh
```

The Server API base defaults to `http://127.0.0.1:4000/api/v1`, producing:

```text
GET  /api/v1/internal/progress/targets?worker_id=labs-host-01
POST /api/v1/internal/progress/observations
```

`--once` performs one delivery/probe/delivery cycle and is useful before
enabling systemd. Production uses
`systemd/aivirteach-progress-worker.service` and
`/etc/aivirteach-progress.env`.

The supplied unit assumes the repository is deployed at
`/opt/aivirteach-labs`. Install it with a dedicated account and environment
file (change the unit paths first if your deployment root differs):

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin \
  aivirteach-progress
sudo install -o root -g aivirteach-progress -m 0640 \
  progress-worker/config/progress.env.example /etc/aivirteach-progress.env
sudoedit /etc/aivirteach-progress.env
sudo install -o root -g root -m 0644 \
  systemd/aivirteach-progress-worker.service \
  /etc/systemd/system/aivirteach-progress-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now aivirteach-progress-worker.service
sudo systemctl status aivirteach-progress-worker.service
```

Do not add this account to `libvirt`, `kvm`, `docker`, or the VM Manager group.

The SQLite database belongs to one Worker process only. Do not share it between
replicas. `passed` is sticky completion history. A later runtime fault does not
erase completion; live troubleshooting remains the Agent's job. `unknown` is
not treated as failure and backs off to avoid repeatedly probing P07, which
requires additional browser/platform evidence. The Worker never manufactures
heartbeats for achieved checkpoints: unchanged events are emitted only after a
real fresh probe and the configured heartbeat interval, so old evidence is not
rewritten forever merely to appear fresh.
