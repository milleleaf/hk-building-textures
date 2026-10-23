---
pretty_name: Hong Kong buildings with photo textures (3D Tiles)
license: other
license_name: csdi-terms
license_link: https://portal.csdi.gov.hk/csdi-webpage/doc/TNC
tags:
  - geospatial
  - 3d-tiles
  - 3d
  - buildings
  - hong-kong
---

# Hong Kong buildings with photo textures (3D Tiles)

Every Hong Kong building with a real photo texture, in two forms, built per 1:1000 map sheet by
[milleleaf/hk-building-textures](https://github.com/milleleaf/hk-building-textures) from Lands Department open data.
The tiles are in the bucket [milleleaf/hk-building-textures](https://huggingface.co/buckets/milleleaf/hk-building-textures) and are
read in place by browsers (CORS and HTTP Range). This dataset repo holds this card and the two manifests.

| Product | For | What it is |
|---|---|---|
| `indiv-k4/` | desktop | Lands D's own Individualised models (one glTF per building, photo textures), textures re-encoded as KTX2 ETC1S at 1/4 size, geometry meshopt-compressed. Buildings are grouped in 150 m cells that load within about 2 km of the camera. |
| `bake3dsd/` | phones and tablets | Lands D's 3D Spatial Data building models (simple shapes), each given its own texture atlas baked from the Open3Dhk photomesh at 20 cm texels (KTX2 ETC1S, meshopt). About ten times smaller than `indiv-k4`. |

Layout, per product: `tileset.json` (root, 3D Tiles 1.1: 1:5000 map-sheet groups, each map sheet an external tileset),
`<sheet>/tileset.json` with one `.glb` per building, `<sheet>/report.json` (counts, sizes, timings of that sheet's build),
`manifest.json` (every sheet's report and the totals). Both need a KTX2 (Basis Universal) and a meshopt decoder, e.g. three.js
`KTX2Loader` and `MeshoptDecoder` with [3d-tiles-renderer](https://github.com/NASA-AMMOS/3DTilesRendererJS), or CesiumJS.

```js
const tiles = new TilesRenderer('https://huggingface.co/buckets/milleleaf/hk-building-textures/resolve/bake3dsd/tileset.json')
```

## Known defects

`bake3dsd` (our bake; seen on the Mong Kok pilot sheets, baker of hk-ifcx commit 6b7e5bf, which reduced but did not remove them):
- trees, signs and canopies in front of a wall can still be baked onto the wall;
- faces the photomesh does not see (narrow lanes, under podiums) take another part of the model's texture or its mean colour,
  so they look flat;
- on the pilot sheets 9 to 23 % of wall texels and 11 to 24 % of roof texels found no photomesh surface and were filled
  (per-sheet figures in each `report.json`); curved roofs come out pale;
- a CSDI footprint with no 3D Spatial Data model and no heights is not drawn.

Both products: heights are metres above Hong Kong Principal Datum, used as ellipsoidal heights without conversion, and
`indiv-k4` ignores the HK1980 grid convergence (about 0.003 degrees).
Anonymous Hugging Face downloads are limited to 3,000 files per 5 minutes per IP address. One street-level view of Mong Kok
loads about 1,400 files of `indiv-k4` or 2,350 of `bake3dsd`, so a second view within 5 minutes fails part-way (HTTP 429)
unless the files are served through a cache.

## Sources and terms

© Lands Department, HKSAR Government. Data from data.map.gov.hk and the CSDI Portal (portal.csdi.gov.hk), free for commercial and
non-commercial use with attribution, under the [CSDI terms](https://portal.csdi.gov.hk/csdi-webpage/doc/TNC). The Lands Department
owns the intellectual property rights in the data. This dataset is derived from it, provided as is with no warranty, and not
endorsed by the Government. Full source list: the GitHub repository's `sources.json`.
