# Supervised workers

Two additions let an external supervisor share a machine between OpenBench and
other work: it asks the Server whether a workload is available, then runs the
Client for exactly one workload.

## `POST /api/active/`

Form fields:

| Field | Meaning |
|---|---|
| `username`, `password` | An enabled account. Required even when `require_login_to_view` is off. |
| `system_info` | JSON object, the subset of the Client's registration `system_info` below. |
| `blacklist` | Optional, repeated. Test ids (at most 18 digits) to exclude, as `clientGetWorkload` accepts them. |

`system_info` must contain `concurrency`, `physical_cores`, `logical_cores`,
`ram_total_mb`, `syzygy_max` (integers), `noisy` (boolean), `cpu_flags` (list),
`os_name` (string), `compilers` and `tokens` (objects keyed by engine name). It
may contain `focus` and `only` (lists), which filter as they do for a
registered Client.

Supervisors should not assemble `system_info` by hand: the Client derives
compilers (version-checked), CPU flags, core counts and Syzygy support itself,
and any difference makes the answer describe a different Machine. Run the
Client once with `--print-system-info` (same `-T`, `-N`, `--syzygy`, `--noisy`,
`--focus`, `--only` as the real run) and send the JSON from its
`SYSTEM_INFO <json>` line; extra keys are ignored.

The Server derives the supported engines exactly as `clientWorkerInfo` does,
then runs the same `filter_valid_workloads` that `clientGetWorkload` uses,
including thread requirements, Syzygy, `--noisy`, priority and focus. Nothing
is written: no Machine, Result, or session is created.

| Status | Body |
|---|---|
| 200 | `{"assignable": n}`: the workloads `clientGetWorkload` would choose among. `n > 0` exactly when a registered Client with this `system_info` and blacklist would be assigned work now. |
| 400 | `{"error": ...}`: malformed `system_info` or `blacklist`. |
| 401 | `{"error": "Bad Credentials"}`: unknown user, wrong password, or disabled profile. |
| 429 | `{"error": "Too many failed logins"}`: this username failed 10 times from this address, or 50 checks failed from this address, within 15 minutes. The password was not checked. Back off and retry after the window; failures from other addresses never cause this. See [SECURITY.md](SECURITY.md). |
| 405 | `{"error": "POST required"}` |

## Client flags

`--print-system-info` scans the machine as registration would (it fetches
`clientGetBuildInfo`, but registers nothing), prints one line
`SYSTEM_INFO <json>`, and exits 0.

`--blacklist 12,34` seeds the Client's blacklist, sent with every workload
request.

`--single-workload` makes one workload request, then exits. Every start
registers a new Machine; the fleet pages group them into one machine, and the
Server removes the ones that found no work
([DEPLOYMENT.md](DEPLOYMENT.md#machine-registrations)):

| Exit | Meaning |
|---|---|
| 0 | The workload completed, or the Server stopped it (e.g. the test finished). |
| 3 | The Server had no workload for this Machine. |
| 4 | The workload failed: build, bench, network, book, or match-runner errors. `FAILED_TEST <id>` is printed first. |
| 5 | The Server failed or became unreachable mid-workload. Not the test's fault; retry later. |
| 130 | Interrupted by SIGINT, or by SIGTERM, which `--single-workload` handles like SIGINT so the match runner and engines are killed first. |
| 1 | Setup failed, for example `make` or a C++ compiler is missing. |

A Server configuration or version change still raises to `client.py`, which
updates the Client, or exits non-zero under `--no-client-downloads`. Any
interrupt that reaches `client.py`, with or without `--single-workload`, now
exits 130 rather than 0. Before the
workload request, `--single-workload` retries registration and requests like
any Client, so a supervisor should bound how long it waits for the
`Workload [` line.
