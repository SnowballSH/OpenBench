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
| Accent | `--accent` (links, secondary buttons), `--accent-solid` + `--on-accent` (primary buttons), `--accent-soft`, `--focus-ring` |
| Results | `--pass`, `--fail`, `--warn`, `--info` (edges, icons, text); `--pass-bg` / `--pass-ink` and the matching `-bg` / `-ink` pairs for filled blocks; `--odds`; `--neutral-bg`, `--neutral-ink`, `--neutral-edge` |
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
- **Cards and tiles**: `.card` (optionally with `.card-header` and
  `.card-title`) is a bordered surface. `.stat-tiles` is an auto-fitting grid
  of `.stat-tile`, each with `.stat-label`, `.stat-value` and optional
  `.stat-meta`; `.stat-tile-{pass,fail,warn,info}` colours its top edge.
- **Badges**: `.badge` with `.badge-{pass,fail,warn,info,accent}`.
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
- The layout breakpoints are 1024px (single-column forms and workload view)
  and 767px (off-canvas sidebar). Check pages at 375px and in both themes.
