# hk-building-textures

Hong Kong buildings with photo textures, as 3D Tiles 1.1, built per 1:1000 map sheet in GitHub Actions from Lands Department
open data and published to the Hugging Face bucket [milleleaf/hk-building-textures](https://huggingface.co/buckets/milleleaf/hk-building-textures)
(card: [dataset milleleaf/hk-building-textures](https://huggingface.co/datasets/milleleaf/hk-building-textures)). Code only: no data in this repository.

A work in progress, sheet by sheet: on 2026-10-23, 12 of the 3,456 sheets were published as `indiv-k4` (6,152 buildings) and 8 of 3,455 as `bake3dsd` (4,491 buildings). Each product's `manifest.json` has the current count (`sheets_published` of `sheets_in_index`).

| Product | Built by | From |
|---|---|---|
| `indiv-k4` (desktop) | `indiv.mjs --tex ktx2:4 --ge 40` | Lands D Individualised models: only the `BUILDING/` members of each sheet zip, read by HTTP Range (`zipcut.py GLTF`) |
| `bake3dsd` (phones) | `bake.mjs --all --mesh3dsd` | Open3Dhk photomesh leaf tiles near the sheet's buildings, from the sheet and its neighbours, by HTTP Range (`zipcut.py CESIUM`); 3D Spatial Data building models (`fetch3dsd.py`); CSDI footprints (`sheets.py footprints`) |

- `sheets.py`: the CSDI sheet indexes (3,456 / 3,455 sheets), the plan (which sheets are not yet published), footprints, neighbours.
- `sheet.py`: one sheet end to end, then `<product>/<sheet>/` to the bucket, `report.json` last (it marks the sheet as published).
- `publish.py`: root `tileset.json` and `manifest.json` per product, and the dataset card.
- `kaggle_product.py` + `kaggle_mirror.py`: the Kaggle archive, **one public dataset per product** (`helenkwok/hk-building-textures-indiv-k4` and `-bake3dsd`), run by hand with the `kaggle` workflow. Not run yet: it needs the `KAGGLE_API_TOKEN` secret.
  - **What the first run uploads:** everything the product's bucket manifest lists at that moment, as `<map>/<sheet>/` folders (17 top-level folders, the Kaggle limit is 50) plus `tileset.json` (root, with the `<map>/` level) and `manifest.json`. Today's size, for scale: indiv-k4 174 sheets with buildings, 24,033 buildings, 24,207 files, 1.78 GB; bake3dsd 8 sheets, 4,491 buildings, 89 MB. Both grow as the CBD ticks go on. Failed-attempt records are left out.
  - **A growing product:** a Kaggle dataset version is exactly the files of its upload folder, so each later run re-uploads the whole product as a new version of the same dataset (no per-folder update exists). Run it at milestones (CBDs done, then each larger batch), not on a timer. The `dry` input stages and lists without any Kaggle call.
  - **Owner:** `helenkwok`, Helen's one Kaggle account (the `owner` input; the hk-transit mirror uses it too). Titles and descriptions keep the Milleleaf brand and the Lands Department attribution.
- Workflows: `bake` (manual: product, sheets or `all`, sheets per job, jobs per run; at most 10 jobs at once and one run at a
  time, for Lands D's 100-concurrent-user limit), `kaggle` (manual, one dataset per product), `check` (every push, no data).

Run one sheet locally (needs Python 3.14 with `requirements.txt`, Node 24 with `npm ci --ignore-scripts`, ffmpeg, KTX-Software 4.4.2
`toktx`/`ktx` in `$KTX_BIN`): `python3 sheet.py indiv-k4 <work dir> 14-NW-17A --no-upload`.

The converters come from the hk-ifcx spikes of 2026-10-23. Data © Lands Department, HKSAR Government (data.map.gov.hk, CSDI Portal),
free for commercial and non-commercial use with attribution; sources in `sources.json`. Code: Apache-2.0.

## Serving the textures to the map: Cloudflare Pages

Hugging Face stays the source of truth and Kaggle the archive; the map reads from Cloudflare Pages (`pages.py` has the design, `pages.yml` runs it).
- **Limits** (Cloudflare docs, 2026-10-26, Free plan): 20,000 files per Pages site, 25 MiB per file, 100 projects per account (not routinely raised), 100 rules in `_headers`; static requests and bandwidth are free. "500 builds a month" is for Git builds; direct uploads are not listed there. Cloudflare now says to start new projects on Workers (static assets); a Pages deploy is one script (`deploy/deploy.sh`), so moving later is a small change.
- **Shards:** `areas/pages-shards.json` maps every sheet to a shard per product, at most 15,000 deployed files per shard, append-only (a sheet never moves; new sheets go to the last shard with room). Projects are `milleleaf-hkbt-<product>-<nn>`, plus one `milleleaf-hkbt-index`. A deploy is a whole-site snapshot, so every deploy stages the whole shard.
- **What a shard holds:** `<sheet>/tileset.json` and the sheet's glb files renamed `<id>-<sha8>.glb` (the name carries the content, so `Cache-Control: immutable` stays safe after a rebuild), a shard `tileset.json`, and `_headers` (CORS `*`, 1-year immutable for glb, 5 minutes for tilesets).
- **The index project** serves `<product>/tileset.json` (the root tileset, with ABSOLUTE uris to each sheet's tileset on its shard's `.pages.dev`, so one root spans origins), `<product>/manifest.json`, `<product>/shards.json` and `index.json`. It is separate so the root can change without touching a full shard.
- **Staging** syncs whole sheet folders from the bucket with the authenticated batch API (it does not spend the 5,000-per-5-minutes resolve quota that a per-file download would), 8 sheets at a time. An Actions cache of staged shards does not fit (10 GB per repo).
- **Triggers:** `pages.yml` by hand (`verify`, `changed`, `all`, `shard`; `dry` stages and counts without any Cloudflare call), a daily run, and a step at the end of a cbd tick. The last two only act when the repo variable `PAGES_ENABLED` is `true`. `changed` redeploys a shard only when its sheets changed and it was not deployed in the last 24 h; the rest wait for the next run.
