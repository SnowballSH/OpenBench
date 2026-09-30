# Creating workloads

Tests, tunes and datagen sessions are created on `/test/new/`, `/tune/new/` and
`/datagen/new/`. `OpenBench/workloads/create_workload.py` builds the form,
`create_workload.js` fills it from the selected engine's presets, and
`verify_workload.py` validates every submission and resolves both branches
against GitHub before anything is saved.

## Rejected submissions

A submission that fails validation re-renders the form with the errors in the
banner and every submitted value restored, instead of redirecting to an empty
form. The values travel through the same prefill payload as a clone.

## Cloning

Every workload page has a **Clone** button, a plain link to
`/<type>/new/?clone=<id>`. The create page then fills every field from that
workload, after the engine presets have run, so the clone wins over them.

- `OpenBench/workloads/clone.py` maps a `Test` (with its `SPSARun` for a tune)
  to the form's field names. `FORM_FIELDS` lists the fields of each form, and a
  test keeps it equal to the controls the template renders.
- The payload reaches the page through `json_script` as `json-prefill`, and
  `apply_prefill` in `create_workload.js` assigns each value with `.value`.
  A `<select>` value that is not available (a deleted network, a disabled
  engine or book) is left at the preset and named in a warning banner above
  the `#workload-form` form.
- The form notes "Cloned from #id name" and carries the id in a hidden
  `clone_of` field, so the note survives a rejected submission. The create
  flow ignores `clone_of`.
- Nothing is trusted from the source workload: the submission goes through
  `verify_workload` like any other, and branches are resolved on GitHub again.
- Any workload a signed-in user can open can be cloned; creating still requires
  an enabled account, so the button is disabled for other viewers. An id
  that is malformed, unknown, or of another type is
  ignored with a warning banner.

### Mapping rules

| Form field | From the workload |
| --- | --- |
| `dev_branch`, `base_branch` | `dev.name`, `base.name`: the branch, tag or SHA as typed |
| `dev_bench`, `base_bench` | The stored bench only when the branch was a pinned commit SHA; otherwise empty, so the bench is read again from the branch's newest commit |
| `info` (tests) | Only when the dev branch was a pinned commit SHA; otherwise empty, since a test's info is usually the dev commit message and the branch may have moved. Tunes and datagen store info verbatim, so theirs is always copied |
| `dev_network`, `base_network` | The network SHA, selected by value |
| `test_bounds` | `[elolower, eloupper]`, in plain decimals |
| `test_confidence` | `[beta, alpha]`, the order the create flow parses |
| `test_max_games` / bounds | `N/A` for whichever the test mode does not use |
| `scale_nps` | Omitted when the source has none (older or seeded workloads), leaving the engine's NPS |
| `spsa_inputs` | `spsa_original_input`: each parameter's starting value, bounds, `c_end` and `r_end` |
| `spsa_pairs_per` | `SPSARun.pairs_per` (a tune's workload size) |
| `datagen_play_reverses` | `YES` / `NO` from `play_reverses` |

Everything else (engines, repos, options, time controls, book, PGN upload,
priority, throughput, workload size, Syzygy, adjudication, scale method,
tune and datagen info, SPSA and datagen settings) is copied as stored.
