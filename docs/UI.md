# UI conventions

The web interface is server-rendered Django templates styled by a single
stylesheet, `OpenBench/static/style.css`, plus `OpenBench/static/site.js` for
the theme toggle, current-page highlighting and the mobile navigation drawer.
There is no build step and no CSS framework. The standalone `/Ethereal/` page
keeps its own `ethereal.css`.

Bump `OPENBENCH_STATIC_VERSION` in `OpenBench/config.py` whenever a static file
changes, so browsers refetch it.

## Stylesheet layout

`style.css` is ordered by layer, each introduced by a banner comment:
tokens, base, layout, components (banners, buttons, forms, tables, stat blocks,
cards / stat tiles / badges, pagination and small widgets), pages, utilities.
Put new rules in the matching layer. Prefer classes over ids, and keep
specificity low (`:where()` for element defaults) so components compose.

## Themes

Dark is the default. Light applies when the system prefers it, or when the
viewer picks it with the header toggle, which writes `openbench-theme`
(`light` / `dark`) to `localStorage` and sets `data-theme` on `<html>`. A tiny
inline script in `base.html` applies the stored choice before the stylesheet
loads, so there is no flash. Storage access is wrapped in `try/catch`; with
storage blocked the toggle still works for the current page.

Light values are declared twice, under `:root[data-theme="light"]` and under
`@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) }`.
Edit both blocks together. Never hard-code a colour in a template or rule:
use a token.

## Tokens

| Group | Tokens |
| --- | --- |
| Type | `--font-sans` (IBM Plex Sans), `--font-mono` (IBM Plex Mono); `--text-xs` 12px, `--text-sm` 13px, `--text-md` 14px (body), `--text-lg` 16px, `--text-xl` 20px, `--text-2xl` 24px; `--leading`, `--leading-tight` |
| Space | `--space-1` 4px, `--space-2` 8px, `--space-3` 12px, `--space-4` 16px, `--space-5` 24px, `--space-6` 32px, `--space-7` 48px |
| Shape | `--radius-sm` 4px (chips), `--radius` 6px (controls), `--radius-lg` 10px (cards, tables); `--control-height` |
| Surfaces | `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--input-bg`, `--border`, `--border-subtle`, `--stripe`, `--row-hover`, `--shadow-pop` |
| Text | `--text`, `--text-muted`, `--text-subtle` |
| Accent | `--accent` (links), `--accent-solid` + `--on-accent` (primary buttons), `--accent-soft` (tinted fills), `--focus-ring` |
| Results | `--pass`, `--fail`, `--warn`, `--info` (edges, icons, text); `--pass-bg` / `--pass-ink` and the matching `-bg` / `-ink` pairs for filled blocks; `--odds`; `--neutral-bg`, `--neutral-ink`, `--neutral-edge` |
| Text on tints | `--accent-text`, `--pass-text`, `--warn-text`, `--fail-text`: ink for tinted buttons, at least 4.5:1 on their 12% resting and 20% hover fills in both themes |
| Inputs | `--input-bg`, `--input-border` (at least 3:1 against cards and page) |
| Charts | `--series-1` to `--series-4`, `--chart-grid`, `--chart-axis` |

Result semantics follow `testResultColour` in `OpenBench/templatetags/mytags.py`:
green passed, blue passed with a negative-bound (non-regression) hypothesis,
yellow failed with wins >= losses, red failed, empty (neutral) still running
or stopped. Filled blocks pair a pastel `-bg` with a dark `-ink` for contrast
above 7:1 in both themes, and carry a saturated left edge so the state reads
without relying on fill alone.

## Components

- **Buttons**: `.anchorbutton` plus a variant. `.btn-blue` is the default
  (tinted accent), `.btn-start` the primary solid action, `.btn-preset` green,
  `.btn-yellow` warning, `.btn-red` destructive, `.btn-disabled` inert. Works on
  `<a>`, `<button>` and `<input type="submit">`. `.anchor-container` lays out a
  row of equal-width buttons. `.icon-button` is a square icon-only button.
- **Forms**: `.form` is a two-column grid (one column under 1024px) of
  `.col` cards; `.col-full` spans both columns without a card. Each field is a
  `.row` holding a `<label>` and a control; `.row.workflow-edit` stacks the
  label above. `h3` inside a column is a section heading. Under 480px labels
  stack above their fields. Controls are styled globally, including focus
  rings and read-only state.
- **Tables**: wrap every table in `<div class="table-wrap">`, which provides
  the card border and horizontal scrolling on narrow screens (add `w-100` for a
  full-width table). `tr.table-header` is a header row; `.stripes` and
  `.hoverable` add zebra striping and row hover; `.numeric` right-aligns
  tabular figures. Cells do not wrap by default. In `.test-list`,
  `tr.table-header` is a group heading and `tr.table-small-header` a subgroup.
- **Stat blocks**: `.statblock .statblock-{green,red,yellow,blue}` inside
  `td.statblock-cell` for list rows; `pre.long-statblock` with
  `.long-statblock-{colour}` for the detail view.
- **Cards and tiles** (provided for the insights and charts pages, pair them
  with the `--series-*` and `--chart-*` tokens): `.card` (optionally with `.card-header` and
  `.card-title`) is a bordered surface. `.stat-tiles` is an auto-fitting grid
  of `.stat-tile`, each with `.stat-label`, `.stat-value` and optional
  `.stat-meta`; `.stat-tile-{pass,fail,warn,info}` colours its top edge.
- **Badges**: `.badge` with `.badge-{pass,fail,warn,info,accent}`.
- **Insights** (built by `insights.js`, see below): `.insights` is the workload
  page section; `.insights-group` titles a `.stat-tiles` row; `.insight-meter`
  sits at the foot of a tile, `-fill` for progress towards a target and
  `-position` for a marker between two bounds (the LLR), both driven by a
  `--fraction` custom property from 0 to 1. `.chart-card` is a `<figure>` card
  holding `.chart-caption`, a fixed-height `.chart-box` for the canvas and a
  `.chart-empty` fallback; `.insights-empty` is a full-width dashed notice.
  `.contribution-table` is a full-width `.table-wrap` with a caption, and
  `.share-bar` a small horizontal bar set by `--share`. `.insights-error` is
  the inline failure banner.
- **Server strip**: `.server-stats` is the compact tile grid at the top of the
  index. `.row-progress` (`-games` fill or `-llr` marker, `--fraction`) is the
  thin bar under an active row's stat block, rendered by the `workload_progress`
  template filter from the Test's own fields, with no extra queries.
- **Sortable tables**: add `data-sortable` to a `<table>` with a `<thead>`
  and `<tbody>`, and put a `<button class="sort-button">` in each sortable
  header; `data-sort="number"` sorts numerically (largest first on the first
  click). A cell's `data-sort-value` overrides its text as the sort key.
  `OpenBench/static/fleet.js` wires them up; load it from the page's
  `scripts` block.
- **Banners**: `.error-message`, `.warning-message`, `.status-message` render
  the session messages in `base.html`.
- **Small pieces**: `.flag-*` for the test-list markers, `.icon-ok`,
  `.icon-danger`, `.icon-muted` for Font Awesome icons, `.mono` for hashes,
  options and time controls, `.muted` for secondary text.

## Conventions

- Proportional type for interface text; monospace only for machine values
  (hashes, engine options, benches, time controls, stat blocks).
- Keep class names that scripts use: `table-header`, `active-highlight`,
  `summary-table`, `stripes`, `wrappable`, `anchorbutton`, `btn-preset`,
  `col-half`, `pl-half`, `pr-half`, `mt-1`, `w-100`, `engine-options`,
  `engine-options-popup`, `timestamp`, `datestamp`, `sidebar-open`.
- Tinted fills stay at 12% (rest) and 20% (hover) of their colour; stronger
  mixes drop text contrast below 4.5:1.
- The mobile drawer moves focus to its first link and makes the page inert
  while open; Escape or a click on the scrim closes it and returns focus to
  the toggle.
- The layout breakpoints are 1024px (single-column forms and workload view)
  and 767px (off-canvas sidebar). Check pages at 375px and in both themes.

## Charts

Charts use Chart.js, vendored so the site loads no new third-party origin:

| Item | Value |
| --- | --- |
| Library | Chart.js 4.5.1, UMD build, MIT licence |
| File | `OpenBench/static/vendor/chartjs-4.5.1/chart.umd.min.js`, with `LICENSE.md` beside it |
| Source | `https://registry.npmjs.org/chart.js/-/chart.js-4.5.1.tgz`, `package/dist/chart.umd.min.js` |
| Tarball integrity | `sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==` (as published by npm) |
| File sha256 | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| File sha512 | matches the cdnjs SRI for `Chart.js/4.5.1/chart.umd.min.js` |

The file is byte-identical to the release; its trailing `sourceMappingURL`
names a map that is not vendored, which only matters with developer tools open.
The versioned directory is the cache key, so it needs no `static_version`
query. To upgrade, vendor the new release into a new `chartjs-<version>`
directory, re-verify both hashes, update this table and the `<script>` in
`workload.html`, and delete the old directory.

No date adapter or plugin is used. Strength and LLR charts plot against games
on a linear axis; the throughput chart plots cumulative games against elapsed
seconds, with duration ticks on round steps. Timestamps appear in tooltips.
`insights.js` adds two small inline plugins: labelled horizontal reference
lines (SPRT bounds, zero, a games target) and a vertical crosshair.

Rules the charts follow:

- Colours come from the tokens, read with `getComputedStyle` at render time:
  `--series-1` for the data, `--chart-grid`, `--chart-axis`, `--text-muted`
  for chrome, `--pass` / `--fail` for the SPRT bound lines (always labelled).
  A `MutationObserver` on `data-theme` and a `prefers-color-scheme` listener
  re-render every chart when the theme changes.
- `prefers-reduced-motion: reduce` turns animation off; refreshes never
  animate.
- One series per chart, so no legend; the card title names it. The Elo chart
  draws the 95% interval as a 14% wash of the series colour. Its y range is
  fitted to the interval from 10% of the games onwards, so the very wide first
  points are clipped rather than flattening the rest; the tooltip still shows
  their values.
- Every canvas has `role="img"` and an `aria-label` stating the latest value.
- The `--series-1` to `--series-4` values were re-stepped for this feature and
  pass the categorical palette checks (lightness band, chroma, colour-vision
  separation, 3:1 contrast) against `--surface` in both themes; in light every
  slot clears 4:1 against white.
