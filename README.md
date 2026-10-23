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
- `kaggle_group.py` + `kaggle_mirror.py`: Kaggle archive, one dataset per product and 1:5000 sheet group (not run yet).
- Workflows: `bake` (manual: product, sheets or `all`, sheets per job, jobs per run; at most 10 jobs at once and one run at a
  time, for Lands D's 100-concurrent-user limit), `kaggle` (manual), `check` (every push, no data).

Run one sheet locally (needs Python 3.14 with `requirements.txt`, Node 24 with `npm ci --ignore-scripts`, ffmpeg, KTX-Software 4.4.2
`toktx`/`ktx` in `$KTX_BIN`): `python3 sheet.py indiv-k4 <work dir> 14-NW-17A --no-upload`.

The converters come from the hk-ifcx spikes of 2026-10-23. Data © Lands Department, HKSAR Government (data.map.gov.hk, CSDI Portal),
free for commercial and non-commercial use with attribution; sources in `sources.json`. Code: Apache-2.0.
