# UI conventions

The web interface is server-rendered Django templates styled by a single
stylesheet, `OpenBench/static/style.css`, plus `OpenBench/static/site.js` for
the theme toggle, current-page highlighting, the mobile navigation drawer,
timestamp formatting and the shared `data-*` behaviours listed in
[SECURITY.md](SECURITY.md#content-security-policy).
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
(`light` / `dark`) to `localStorage` and sets `data-theme` on `<html>`.
`static/theme-init.js`, loaded synchronously at the top of `<head>`, applies
the stored choice before the stylesheet loads, so there is no flash. Storage access is wrapped in `try/catch`; with
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
  full-width table). It is positioned, so `.visually-hidden` text inside
  a scrolled row stays clipped by it instead of widening the page. `tr.table-header` is a header row; `.stripes` and
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
  the inline failure banner. `.insights-verdict` (`-positive`, `-negative`)
  is the one-paragraph verdict with a toned left edge; `.insights-details`
  a small `<details>` note under the tiles; `.results-grid` lays
  the outcome table beside the By CPU table, `.results-wide` spans it; and
  `.results-outcome-bar-{loss,loss-soft,level,win-soft,win}` recolour a `.share-bar`
  along the loss-to-win scale, always beside its text label.
- **Server strip**: `.server-stats` is the compact tile grid at the top of the
  index. `.row-progress` (`-games` fill or `-llr` marker, `--fraction`) is the
  thin bar under an active row's stat block, rendered by the `workload_progress`
  template filter from the Test's own fields, with no extra queries. `.row-timing` is the
  line below it (`Blocks/row_timing.html`): time left and games per hour for a
  running row, time taken for a finished one; `.row-timing-left` lifts the time
  left to body colour. Templates
  pass these values as `data-fraction` and `data-share`, which `site.js` copies
  into the custom properties; a missing value draws an empty bar. That makes
  the server-rendered meters depend on JavaScript: without it, the progress and
  share bars stay empty, while the figures beside them and the meters'
  accessible labels (`aria-valuetext` and `title`, or `aria-label`) still
  carry the value. The trade-off buys a
  Content-Security-Policy with no `style-src-attr 'unsafe-inline'`; CSS
  cannot yet read a typed number from an attribute in every browser.
- **Sortable tables**: add `data-sortable` to a `<table>` with a `<thead>`
  and `<tbody>`, and put a `<button class="sort-button">` in each sortable
  header; `data-sort="number"` sorts numerically (largest first on the first
  click). A cell's `data-sort-value` overrides its text as the sort key.
  `OpenBench/static/fleet.js` wires them up and sets `aria-sort`; load it from
  the page's `scripts` block. Never put a click handler on a bare `<th>`.
- **Error triage** (`/errors/`, `/event/<id>/`; behaviour in
  [INSIGHTS.md](INSIGHTS.md#worker-errors)): `.triage` stacks the page.
  `.triage-controls` holds the filters, each a `<nav class="fleet-filter">`
  of `.fleet-filter-option` links with `aria-current` on the chosen one, so
  every filter is a plain link that works without script and can be
  bookmarked. `.triage-groups` is the grouped table; its error, workload and
  host cells wrap, and a status is a `.badge` (`badge-fail` still happening,
  `badge-warn` quiet, `badge-pass` resolved) with the reason in words on the
  `.row-meta` line beneath, so colour is never the only signal.
  `.triage-empty` is the dashed empty state. On the event page `.key-lines`
  is the extract (line number, label badge, text) and `.log` the scrolling
  log region (focusable, labelled, at most 70vh): every line is a
  `.log-line` block whose number comes from `data-line` through `::before`,
  so numbers are neither selected nor copied; `.log-fold` is the `<details>`
  holding the middle of a long log and `.log-omitted` the note for lines
  left out. `static/triage.js` only adds the copy button's behaviour (it
  announces through a hidden `role="status"` line), opens the fold when a
  key line or a `#L<n>` fragment points into it, and marks that line
  (`.log-line-target`); without it the log, the fold and the raw download
  all still work. `Blocks/event_workload.html` renders a workload as
  `#id title` with the commit pair beneath, as the listings do, for the
  events, errors and event pages. `.workload-errors` is the line of the
  workload page's Errors section that links to its worker errors.
- **Banners**: `.error-message`, `.warning-message`, `.status-message` render
  the session messages in `base.html`.
- **Diagnosis**: `.diagnosis` with `.diagnosis-{ok,info,warning}` is the
  workload page's "what is this waiting for" (or "what stopped this") banner (`Blocks/diagnosis.html`):
  a `<section>` with a hidden `<h2>`, the headline, and the evidence in a
  `<details>` that starts open only for a warning. `.row-reason` is the same
  verdict in a few words on a listing row's `.row-meta` line,
  `.row-reason-warning` in `--warn-text`; its hidden "Status:" prefix and its
  `title` carry the meaning without the colour.
- **Error pages**: `OpenBench/security/error_pages.py` serves `404.html` and
  `403.html` in the site layout with an empty Engines list, so they run no
  query and show an anonymous visitor nothing about the server. `500.html`
  stands alone (`body.standalone`, no sidebar), because the database or the
  session may be what failed; it needs no request context.
- **Small pieces**: `.flag-*` for the test-list markers, `.icon-ok`,
  `.icon-danger`, `.icon-muted` for Font Awesome icons, `.mono` for hashes,
  options and time controls, `.muted` for secondary text.

## Row navigation

Every table whose rows have one obvious destination opens it from a click
anywhere on the row, not only from the link.

| Table | Destination |
| --- | --- |
| Workload listings (index, greens, user pages, search results) | the workload |
| Machines | the machine |
| A machine's workloads | the workload |
| Users, and the progress page's contributor and author tables | the user's workloads |
| Events | the workload |
| Errors, grouped | the newest log of the group when it has one, otherwise the workload |
| Errors, every event | the error's page when it has a log, otherwise the workload |
| Progress: lineage, direct-check and detached rows that stand for exactly one workload | the workload |

Networks stay plain rows: a row there has three equal candidates (engine,
download, edit), and a stray click must not start a download. The compare
table lists workloads in columns, and the workload page's per-machine results
are built by `workload_utils.js`; neither is wired.

The markup is two attributes, and `site.js` does the rest on every page:

```html
<tr data-row-href="/test/12/">
    <td><a href="/user/admin">Admin</a></td>
    <td><a class="row-link" href="/test/12/">#12 lmr-tweak</a></td>
    ...
</tr>
```

- `data-row-href` on the `<tr>` is the destination. Exactly one
  `<a class="row-link">` inside the row carries the same `href`: it is the
  keyboard and screen-reader path (Tab, Enter, the link's own context menu),
  and the whole feature without JavaScript. `OpenBench/tests/test_listing_rows.py`
  renders every wired page and fails on a row with no primary link, two of
  them, or one that goes elsewhere.
- One delegated `click` listener on `document` handles every table, including
  rows that `fleet.js` re-sorts. It ignores a click that lands on, or inside,
  a link, button, form control, `<label>`, `<summary>` or anything marked
  `data-row-ignore`, so the author, diff, owner and log links and the sort
  buttons keep their own behaviour.
- Cmd-click, Ctrl-click and the middle button open the destination in a new
  tab (`window.open` with `noopener`); Shift- and Alt-click do nothing, since
  the browser gives them meanings a row cannot honour. A click that ends a
  text selection inside the row does not navigate, so figures can still be
  copied out of a stat block. A double-click navigates on its first click,
  like a double-click on a link does; drag to select a word instead.
- Only same-origin destinations are followed.
- `tr[data-row-href]` shows a pointer cursor and takes the `--row-hover`
  background on hover and on `:focus-within`, so the row a keyboard user is
  on is marked like the one under the mouse.

**Why a script and not a stretched link.** The pure-CSS alternative gives the
primary link an `::after` that covers the row (`position: absolute; inset: 0`
against a positioned `<tr>`). It was rejected for these tables: the overlay
sits above the cells, so text under it cannot be selected and the `title`
tooltips on the progress meters, flags and timestamps stop appearing; every
other link in the row has to be lifted above it with `position` and
`z-index`, which a new link added later silently forgets; and a positioned
table row as a containing block is the least reliable part of table layout
across engines. The script costs nothing when it fails: the primary link is
still a link.

### Workload names and numbers

A listing row starts with `#<id>`, so a test can be referred to by number, and
then a label from `OpenBench/listing_rows.py` (`workload_label`):

- A branch-named workload keeps the name `prettyDevName` gives it.
- A commit-pinned workload is titled by the first line of its info text,
  normally the commit subject, with `76f2da3c vs 8c308d43` (8-character SHAs,
  dev then base) in monospace beneath. Without info the commit pair is the
  title. The info column then shows only the lines after the subject (the lab
  agent's `avl:<sha>` tag, for instance), and is empty when there are none.
  A dev name counts as a commit when it is a full 40-digit hex SHA, as
  `prettyName` decides it, or 7 to 39 hex digits with at least one decimal
  digit, so `deadbeef` stays a branch.
- When a network or another engine names the row, that name wins as before.

The title link (`.row-title`) wraps to at most two lines, between 26ch and
44ch wide, and carries the full text in `title`, so a long subject never
forces the table to scroll. The info cell and the author link (`.row-author`,
shown in full up to 14ch) carry their full text in `title` as well. At 1100px
and narrower the stat block leaves too little room for everything, so the
listing tightens: cell padding shrinks, the title's floor drops to 11ch, the
author is cut at 7ch, the commit pair may wrap, and the info column is
hidden. The search results' date column is already hidden from 1350px down,
where it would squeeze the info column to nothing; the row's own
`finished ... ago` line still dates it. That keeps the table inside its card down to 1024px; below
that it scrolls inside `.table-wrap` as before. Machine pages use `#<id>`
and the `short_name` filter; events and errors use the same title as the
listings (`Blocks/event_workload.html`).

Below the name, `listing_moment` adds `finished 3d ago`, `started 2h ago`
(first recorded report) or `created 5m ago`, as a `<time>` whose `datetime`
and `title` hold the exact instant. It reads only the listing's existing
annotations, so it adds no query.

### Quick filter

The index, greens and user pages carry a filter box above the table.
`<input data-row-filter="<table id>" data-row-filter-count="<element id>">`
hides the `tr[data-row-href]` rows of that table whose text does not contain
every whitespace-separated term (case-insensitive; the text includes the
hidden result word, so `passed` and `failed` work), hides group headings left
with no rows, and writes `7 of 33 shown` into the count element, a
`role="status"` region with a reserved width so nothing moves. It filters the
rows on the current page only; Search is the way to look across pages.
Zebra striping is switched off while a filter is active, because hidden rows
would break the alternation. Column widths can still change as rows hide,
since the table sizes its columns to what is visible.

## Workload page

`Templates/OpenBench/workload.html` answers an operator's questions in the
order they are asked. `OpenBench/workloads/page.py` builds everything the
server renders above the fold (`WorkloadPage`: header, summary, section list)
from the `Test` row and one query.

### Before

The page had grown one feature at a time: a diagnosis banner and an errors
link, then the settings table (thirty rows, first on the page) beside a column
holding the diff link, eleven buttons in six rows, the live indicator, the
stat block and the modify form; then an Insights section (progress tiles,
strength tiles, a Results group with the verdict, charts, contributions,
games); then three raw tables. The answer to "did it pass, and by how much"
was the stat block in the right-hand column and a verdict line 900 pixels
down, drawn only after a fetch. The title was hidden. Elo was printed in the
stat block, an Elo tile, the verdict, the chart and three per-CPU tables; LLR
in the stat block, a tile and a chart; the forecast in the verdict and in two
tiles; games per CPU in "Consistency by CPU", "Contributions by CPU" and the
raw summary.

### After

1. **What is this, and what is the answer?** A visible `<h1>` (`#id` and the
   label the listings use: the subject of a commit-pinned test, else the dev
   branch) with a state badge, and one meta line: the commit pair as the diff
   link, engine, time class with the time control, mode (`SPRT [0, 3]`,
   `Fixed 4,000 games`, `SPSA tune`), author, creation. The rest of the info
   text follows as plain text, so nobody has to open the edit form to read
   it. Under it the **summary** (`.workload-summary`): the diagnosis banner
   when there is one, the verdict of
   [INSIGHTS.md](INSIGHTS.md#verdict) in one line, a meter (the LLR between
   its bounds, or games over the target) with the time left and rate of the
   index row, and the stat block, which stays the one place for the exact
   counters and the text that Copy Stat Block copies. All of it is rendered by
   the server; `insights.js` and `live.js` then keep it current.
2. **What should I do next?** One Actions card (`#actions`) beside the
   summary: Approve, Restart or Restore, Confirm at LTC or the link to the
   existing confirmation, Clone; the compare form; the copy and download
   tools and the notify toggle; the info, priority and throughput form folded
   into a `<details>` whose summary states the current priority and
   throughput; and, apart under a rule and right-aligned, Stop and Delete. A
   button that cannot apply in the current state (Approve on an approved
   workload) is not drawn; one the viewer lacks the right for is drawn
   disabled, as before.
3. **Why?** The evidence, each an `<h2>` section with an anchor, reached from
   a section nav (`nav[aria-label="Page sections"]`, styled as
   `.fleet-filter`) that sticks under the header above 1024px and wraps in
   place below it:
   `#results` (strength and pair tiles, pair outcomes), `#progress` (elapsed,
   rate, time left, the charts, the CSV download), `#workers` (one table per
   CPU, hosts that stand out, one table per machine), `#games` (uploaded
   PGNs), `#parameters` (a tune), `#errors` (the link to the worker errors,
   with the count in the nav entry).
4. **Reference.** `#configuration` (three captioned tables: Dev, Base, Match,
   side by side when there is room; SPRT bounds or the game target are rows
   of Match) and `#raw-results` (the summary by user, CPU and ISA, and the
   per-machine results fetched on demand).

A section that cannot show anything for this workload is not rendered, and
has no nav entry: Results for a tune, Games without PGN uploads, Parameters
for anything but a tune, Errors with no error. Results, Progress and Workers
need games: before the first one they are rendered `hidden`, with their nav
entries, and `insights.js` reveals section and entry together
(`show_section`) when the data arrives, as it does for Games when the first
report comes in.

### One place per number

A quantity is stated in the summary and at most once more below it.

| Quantity | Where it is now | What went |
| --- | --- | --- |
| Elo with its interval | stat block, verdict; the Elo chart | the Elo tile |
| LLR and its bounds | stat block, summary meter; the LLR chart | the LLR tile |
| Games, W/L/D | stat block; the games chart | the Games tile, the W/D/L line of the draw-ratio tile |
| Chance to pass, games to decide | verdict, with "About the forecast" under it | the two forecast tiles |
| Time left, games per hour | summary line; Progress tiles (with the date and the window) | |
| Verdict | summary | the copy inside the Results group |
| Games, Elo, share per CPU | Workers "By CPU" (share, games, pairs/h, Elo, deviation, crashes, time losses, speed) | "Contributions by CPU" beside "Consistency by CPU": now one table. A tune has no consistency, and keeps the contributions table |
| Author, creation | header | the two configuration rows |

The raw summary keeps its per-CPU penta and KNPS: it is the reference the
other tables are derived from.

### Hooks

Scripts find their targets by attribute, inside
`.workload-container[data-workload-id][data-workload-insights]`:
`data-insights-{evidence,status,error,announcer,verdict,forecast,tiles,charts,results,contributions,workers-note}`,
`data-summary-{meter,timing}`, `data-section` and `data-section-link`,
`data-live-{workload,status,games,badge,outcome,indicator,notify,notify-note}`,
`data-workload-action`, `data-workload-announcer`, `data-games-insights`,
`#long-statblock`, `#summary-container`, `#results-container`.
`OpenBench/tests/test_workload_page.py` pins them, the section order per
state, and that every state-changing control sits in `#actions`.

### Cost

The header, verdict and meter read the `Test` row only. The time left and
rate reuse the index row's annotations (`listing_tests`), one query, skipped
until a game has been played ([PERFORMANCE.md](PERFORMANCE.md)).

## Live updates

`OpenBench/static/live.js` keeps the index, a user's page and a workload page
current without a reload. It has no dependency, runs under the site's
Content-Security-Policy, and writes only through `textContent`, attributes
and `createElement`.

- **Where it runs**: only where something can change. The index loads it
  when the page lists a pending or active row (`table[data-live-listing]`),
  a workload page while the workload is unfinished
  (`.workload-container[data-live-workload]`). Later index pages, Greens,
  Search and finished workloads never poll. Neither the script nor the
  indicator is rendered for a viewer the API would refuse (`may_poll`: a
  signed-in account that is not enabled, while viewing needs a login).
- **Polling**: every 15 seconds while the tab is visible, with the token of
  [API.md](API.md#getpost-apiliveworkloads-and-apiliveworkloadid), so an
  unchanged poll is a 57-byte answer. A hidden tab does not poll, and polls
  once as soon as it is shown again. Failures double the wait up to four
  minutes, with the seconds to the next try counted down in the indicator;
  three refusals in a row (a session that ended) stop it. It also
  stops when the listing has nothing unfinished left, or the workload
  finished.
- **Listing rows**: each `tr[data-live-row]` carries its id, status and games.
  Each poll builds the row's stat-block cell from the payload (the same
  markup as `Blocks/testsummary.html`: stat block, progress bar, timing line,
  reason) and swaps in only the parts that differ from what is shown, so a
  text selection in an unchanged stat block survives the once-a-minute
  refresh of the relative times. Rows are not re-sorted.
- **A row that leaves or arrives** (finished, stopped, approved, new) is not
  moved between sections by script: the indicator says "1 workload finished
  or changed state" with a **Refresh** button, which reloads the page. The
  server renders the finished row, its timing and its place in the list.
  Until then a row that left the payload is marked `.live-stale`: its stat
  block is dimmed, its hidden result reads "Out of date" and its meta line
  "changed since this page loaded", so it no longer claims to be running.
- **Workload page**: the stat block, its hidden "Result:" text, the state
  badge beside the title (`data-live-badge`) and the diagnosis banner follow
  the payload. Each change raises
  `openbench:workload-change` on `document`; `workload_utils.js` reloads the
  results summary on it, and `insights.js` refreshes at once when the status
  moved. The index raises `openbench:listing-change`, on which `insights.js`
  refreshes the server strip at most once a minute.
- **When the workload finishes**, the title gains a marker (`✓ Passed · …`,
  `✗ Failed · …`), the indicator offers **Reload** for the buttons that only
  the server decides (Restart, Confirm at LTC), and polling stops.
- **Notification**: "Notify me when this finishes" is a toggle
  (`aria-pressed`) on an unfinished workload's page. The browser's permission
  is requested only by that click. The choice is kept per workload in
  `localStorage` (`openbench-live-watch`) and dropped once it fires, which
  also releases the toggle. Without
  it no `Notification` is ever created. While a workload is watched its tab
  keeps polling in the background, once a minute, since a notification is
  for the tab nobody is looking at; the tab still has to stay open.
- **Indicator** (`Blocks/live_status.html`, `.live-status`): a dot and
  "Live · updated 12 s ago". That text ticks every second and is **not** a
  live region. The hidden `aria-live="polite"` element beside it speaks only
  when the list changed, the workload finished or changed state, and when
  polling is interrupted or resumes.
- **Highlight**: a row (or the stat block) whose games moved gets
  `.live-changed` for 2.5 seconds, a tint that fades by `transition`. Under
  `prefers-reduced-motion` the site-wide rule removes the fade, leaving a
  tint that appears and disappears.
- **Staleness**: between changes the page keeps what it has. The token folds
  in the minute, so relative times and reasons are at most a minute old.

## Quick jump

The header of every page carries a jump box (`#quick-jump`, a `GET` form to
`/go/?q=`), hidden only from anonymous visitors of a server that requires a
login. `/` focuses it from anywhere outside a form field and Escape leaves
it. It is plain HTML: without JavaScript, Enter submits the form. Rendering
it costs no query.

`OpenBench/navigation/resolve.py` turns the text into one internal path. The
rules run in this order and the first that answers wins:

| Input | Destination |
| --- | --- |
| `#12`, `12` (ASCII digits, at most 18) | Workload 12 under its own type (`/test/`, `/tune/`, `/datagen/`). An unknown id lands on `/search/` with "No workload #12"; a bare run of seven or more digits is tried as a commit first |
| 7 to 40 hex digits | Workloads, deleted ones excluded, whose dev or base commit sha or branch name starts with it. One match opens it; several open `/search/?q=<prefix>`, newest first; none falls through to the rules below |
| `user:<name>`, or an exact username | `/user/<name>/`, in the stored spelling. `user:` with an unknown name lands on `/users/` with a notice. A name made only of dots is never a user, so no `/user/../` path is built |
| An exact engine name | `/progress/<engine>/` |
| `machine 12`, `m:12`, `m#12` | `/machines/12/`. `m12` and `M4` are text (a CPU model), and an unknown machine falls through to search |
| Anything else | `/search/?q=<text>` |

Names match case-insensitively. Input is whitespace-normalised and capped at
100 characters. Every destination is built from a fixed prefix plus an id or
a percent-encoded name that was read back from the database, so the box
cannot redirect off the site. A jump costs at most four lookups, each one
query, whatever the number of workloads.

A deleted workload still opens by `#id`, but is left out of commit matches,
text matches and suggestions. A notice is a flash message in the session, so
an anonymous viewer of a public server is redirected without one rather than
being given a session.

Search's `Text` field (`q`) is what the fallback fills in. Every
whitespace-separated term must match the info text or a dev or base branch
name (as substrings), or the start of the dev or base commit sha. A name
that is itself a commit (`workload_names.COMMIT_NAME`) is matched by its start
only: forty hex digits contain almost any short term by accident. It combines
with the other search fields and pages like them. `Keywords` still matches
only the dev branch name.

`static/jump.js` adds suggestions from
[`/api/jump/`](API.md#getpost-apijumpq) as an ARIA combobox: the input gets
`role="combobox"` and a `role="listbox"` only once the script runs. Requests
are debounced by 150 ms and the previous one is aborted. Up and Down move
through the options (wrapping through "no option") and do nothing when there
are none for the text now in the box; typing deselects the active option and
emptying the box or a failed request discards the list, so Enter never opens
an option from an earlier query. Enter opens the active
option or else submits the text, the first Escape closes the list and keeps
the text, and the second leaves the box. Option text is set with `textContent`, and an option
whose URL is not same-origin is dropped.

Below 768px the box collapses to a square magnifier in the header and, when
focused, expands across the header over the title; it is the same input in
both states, so it still works without JavaScript.

## Conventions

- No inline scripts, `on*=` handlers or `style` attributes: the
  Content-Security-Policy blocks them. See
  [SECURITY.md](SECURITY.md#content-security-policy) for what to do instead.
- Proportional type for interface text; monospace only for machine values
  (hashes, engine options, benches, time controls, stat blocks).
- Keep class names that scripts use: `table-header`, `active-highlight`,
  `summary-table`, `stripes`, `wrappable`, `anchorbutton`, `btn-preset`,
  `col-half`, `pl-half`, `pr-half`, `mt-1`, `w-100`, `engine-options`,
  `engine-options-popup`, `timestamp`, `datestamp`, `sidebar-open`,
  `row-link`, `row-filtered`, `statblock-cell`, `row-name`, `diagnosis-headline`,
  `diagnosis-details`, `diagnosis-evidence`, `live-changed`.
- Tinted fills stay at 12% (rest) and 20% (hover) of their colour; stronger
  mixes drop text contrast below 4.5:1.
- The mobile drawer moves focus to its first link and makes the page inert
  while open; Escape or a click on the scrim closes it and returns focus to
  the toggle.
- The layout breakpoints are 1024px (single-column forms and workload view,
  where the section nav also stops being sticky)
  and 767px (off-canvas sidebar). Check pages at 375px and in both themes.

## Accessibility

The target is WCAG 2.2 AA. `OpenBench/tests/test_accessibility.py` renders
every page and checks labels, accessible names, duplicate ids, landmarks and
heading order without a browser; run axe-core against a `seed_demo` server
for contrast and anything dynamic.

- **Page frame**: `base.html` supplies the skip link (first tab stop, to
  `#content`), the site `<nav>` inside `#sidebar`, the `<header>` bar and one
  `<main id="content">`. Keep `id="sidebar"` on its `<div>`.
- **Titles and headings**: every page sets `{% block title %}Name · OpenBench{% endblock %}`
  and `{% block heading %}Name{% endblock %}`, which becomes a visually hidden
  `<h1>`. A page with a visible title (progress, machine, workload) overrides
  `{% block page_heading %}{% endblock %}` and renders its own `<h1>`.
  Below it, sections are `<h2>` and their subsections `<h3>`, never skipping a
  level; style headings by class, not by level.
- **Forms**: every control has a `<label for>` matching its `id`. A control
  that shares a row with another's label, or sits under a section heading,
  takes `aria-label` or `aria-labelledby` instead. Give credential fields an
  `autocomplete` value.
- **Icons**: Font Awesome `<i>` elements carry `aria-hidden="true"`. An
  icon-only link or button needs an `aria-label` naming its target
  (`Edit r1`, `Delete r1`); a status icon needs `.visually-hidden` text beside
  it. Table headers over icon columns hold `.visually-hidden` text.
- **Actions**: anything that runs script is a `<button type="button">`, not an
  `<a>` without `href`. `.btn-disabled` anchors have no `href` and stay out of
  the tab order.
- **Colour**: text tokens meet 4.5:1 against every surface they sit on in both
  themes (`--text-subtle` included, on `--surface-3`). Result colour is
  repeated as `.visually-hidden` text (`Blocks/result_text.html`). Links
  inside a `<p>` are underlined, since colour alone does not set them apart.
- **Live regions**: session banners use `role="alert"` (errors) or
  `role="status"`. The insights error line is `role="alert"`; the visible
  "Updated ..." line is not live, so the minute refresh stays quiet, and the
  hidden `data-insights-announcer` speaks only on first load and when the
  workload's status changes. The workload page's hidden
  `data-workload-announcer` reports completed actions (copy, fetch), and a
  copy announces success only when the clipboard write succeeded. The live
  indicator's ticking text is not live either; see [Live updates](#live-updates).
- **Focus**: an action never leaves focus on `<body>`: copying restores focus
  to its button, and removing a profile repo row moves focus to the next
  row's remove button or the new-engine select.
- **Scrolling tables**: `site.js` makes a `.table-wrap` that overflows
  focusable (`tabindex="0"`, `role="region"`) so keyboard users can scroll it,
  and names it from its caption, its section heading, or `data-region-label`.
- **Charts**: a canvas has `role="img"`, an `aria-label` with the latest
  value and `aria-describedby` pointing at its subtitle; where a data table
  exists, `aria-details` points at it.
- **Motion and focus**: `prefers-reduced-motion` removes transitions and
  animations; `:focus-visible` draws `--focus-ring`, and `scroll-padding-top`
  keeps focused elements clear of the sticky header.

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
- One series per chart, so no legend; the card title names it. The
  comparison page (`compare.js`) is the exception: its charts overlay two
  workloads, `--series-1` solid and `--series-2` dashed, with a legend, and the
  summary table's column headers carry the same line keys. The Elo chart
  draws the 95% interval as a 14% wash of the series colour. Its y range is
  fitted to the interval from 10% of the games onwards, so the very wide first
  points are clipped rather than flattening the rest; the tooltip still shows
  their values.
- The game-length histogram (`games.js`) stacks two series, decisive in
  `--series-1` and drawn in `--series-2`, so it carries a legend above the
  plot and its tooltip names both; the win/draw/loss bars in the colour table
  use `--pass`, `--neutral-edge` and `--fail` with a legend and the counts
  beside them.
- Every canvas has `role="img"` and an `aria-label` stating the latest value.
- The `--series-1` to `--series-4` values were re-stepped for this feature and
  pass the categorical palette checks (lightness band, chroma, colour-vision
  separation, 3:1 contrast) against `--surface` in both themes; in light every
  slot clears 4:1 against white.
