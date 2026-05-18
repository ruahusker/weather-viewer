Model view rollback note

Timestamp: 2026-03-21T17:52:19Z

Context:
- No `.git` repository is present under `/home/main/weather-viewer`, so rollback has to rely on file-level notes and manual reversal.
- This note marks the state immediately before upper-air model view changes.

Files already changed before the upper-air work:
- `/home/main/weather-viewer/web/static/js/app.js`
- `/home/main/weather-viewer/web/static/css/style.css`
- `/home/main/weather-viewer/web/static/index.html`
- `/home/main/weather-viewer/pipeline/renderer.py`

Known pre-upper-air state:
- Model maps had frontend-only borders after removing baked renderer boundary lines.
- Model city labels were zoom-aware and roads/counties were enabled by zoom thresholds.
- Screenshot capture used a temporary rasterized boundary overlay to avoid misaligned exported lines.
- Hidden UI products already exposed:
  - `gh_250mb`
  - `wind_700mb`
  - `vort_500mb`

If rollback is needed:
1. Revert upper-air composite render additions first.
2. Revert any new pressure-level parameters next.
3. Leave the screenshot/city/roads-counties fixes unless they are explicitly part of the rollback target.

Additional changes after this note:
- Pressure-level composite entries were promoted in the sidebar so users can pick combined upper-air views directly.
- Composite display labels now flow through the map legend/title path instead of showing only the raw parameter label.
- Upper-air height contour spacing was reviewed against the emailed guidance and confirmed:
- `250/500 mb = 60 m spacing (6 dam)`
- `700/850 mb = 30 m spacing (3 dam)`
- Chase-tab additions:
- new products `llmt`, `ascent`, and `hp_risk`
- existing `citi` now includes 700mb ascent
- Height units were later switched suite-wide from decameters to meters:
- geopotential heights now stay in meters at the processor layer
- height legends/scales now use `m`
- upper-air contour intervals remain the same real spacing (`60 m` at 250/500, `30 m` at 700/850)
