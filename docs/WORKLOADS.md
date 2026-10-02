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

## Confirming at LTC

The progress page's **Measure against the release** links open the same form
through `/test/new/?release=<engine>&preset=<name>`
(`OpenBench/workloads/release_measurement.py`): dev is the engine's default
branch, base its latest release tag, the run settings are the named preset's,
and the test is fixed games rather than an SPRT; see
[INSIGHTS.md](INSIGHTS.md#since-the-latest-release). An unknown engine, preset
or release fills nothing in and says so.

A finished, passed STC SPRT shows a **Confirm at LTC** button on its page. It
is a link to `/test/new/?clone=<id>&preset=<name>`: the create form opens
filled in, and nothing exists until the operator submits it.

- `OpenBench/workloads/presets.py` reads the engine's `test_presets`. A named
  preset is the `default` preset overlaid by its own keys, with each `both_`
  key spread to the dev and base side and a side's own key outranking it.
- The preset is chosen by what it runs, not by a hard-coded value: the first
  preset whose time control and options class as LTC under
  `OpenBench/progress/conditions.py`, preferring one named `LTC`.
- `OpenBench/workloads/confirmation.py` decides who sees the button: the test
  is an SPRT, finished, passed, not deleted, dev and base are the same engine,
  it classes as STC (so not LTC, VLTC, SMP or time odds), the engine is
  enabled and has such a preset, and the viewer's account may create tests.
- A test whose same two commits (same engine and networks) already ran, or
  are running, as an LTC-class SPRT gets no button. The page links to that
  run instead ("LTC confirmation: #id"), for every viewer.
- With `preset=`, `load_clone_source` overlays the preset on the clone. What
  the test *is* stays the clone's: both engines, repositories, branches,
  benches and networks, and the info (`IDENTITY_FIELDS`). How it *runs* comes from the preset: time
  control, options, bounds and confidence, book, priority, throughput,
  workload size and adjudication. The clone keeps its test mode, so an SPRT
  ignores the preset's `test_max_games` and a fixed-games test its bounds.
- Preset values become form text; a JSON `true` or `false` becomes the
  form's `TRUE` or `FALSE`. A `test_presets` entry that is not an object is
  passed over rather than failing the page.
- The form's note names what the preset actually replaced: "Cloned from #id
  name, with the LTC preset in place of its time control, options, SPRT
  bounds". Values are compared as numbers where they are numbers, so
  `[0.0, 3.0]` and `[0.00, 3.00]` are the same bounds. An unknown preset name, or a preset on a tune or datagen, clones nothing and
  says so in the warning banner.
