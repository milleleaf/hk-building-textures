// (from the hk-ifcx spike of 2026-10-23) Lands D "Individualised models" (per-building glTF with photo textures, from the CSDI record
// landsd_rcd_1671676915450_88604, zip https://download.map.gov.hk/api/3d-zip/GLTF/<sheet>.zip) -> one 3D Tiles 1.1 tileset.
// Each BUILDING/<id>/<id>.gltf has a root node whose matrix is Z-up -> Y-up plus a translation (E, h, -N) in HK1980 grid
// metres (h = mPD, taken as ellipsoidal height as elsewhere in this repo: docs/PROCESS.md). The translation moves into the
// tile's transform (ENU at that point -> ECEF; HK1980 grid convergence here is ~0.003 deg and is ignored), the rotation stays
// and the tileset says gltfUpAxis Y. Textures are kept as Lands D ships them (JPEG) unless --tex is given:
//   --tex ktx2[:scale]   KTX2 ETC1S + mips (toktx, as hk-ifcx lean_tiles.mjs), images scaled by 1/scale (toktx --scale)
//   --tex uastc[:scale]  KTX2 UASTC (quality 2, RDO 1.0) + Zstandard + mips, same scaling
// Buildings are grouped into 150 m cells; a cell (no content) refines to its buildings when its geometric error (--ge, m)
// exceeds the screen-space error, so buildings load within roughly ge * 100 m of the camera at sse 16 (no lower LOD exists).
// Geometry is meshopt-compressed (EXT_meshopt_compression, required): viewers need a meshopt decoder.
// usage: node indiv.mjs <out dir> <unzipped sheet dir>... [--tex ktx2:2] [--ge 8]
import fs from 'node:fs'; import path from 'node:path'; import { execFileSync } from 'node:child_process'
import { C, io, decodeImage, encodeKtx2, pool, ETC1S, UASTC } from './lib.mjs'
import { KHRTextureBasisu } from '@gltf-transform/extensions'; import { meshopt } from '@gltf-transform/functions'; import { MeshoptEncoder } from 'meshoptimizer'

const a = process.argv.slice(2), opt = (f, d) => (a.includes(f) ? a[a.indexOf(f) + 1] : d)
const flags = new Set(['--tex', '--ge']), pos = a.filter((x, i) => !flags.has(x) && !flags.has(a[i - 1]))
const [outDir, ...sheets] = pos, TEX = opt('--tex', 'jpeg'), GE = +opt('--ge', 8), CELL = 150
const [kind, sc] = TEX.split(':'), SCALE = +(sc || 1)

const files = sheets.flatMap((s) => fs.readdirSync(path.join(s, 'BUILDING')).map((id) => path.join(s, 'BUILDING', id, `${id}.gltf`)).filter((f) => fs.existsSync(f)))
// HK1980 grid -> lon/lat for every building origin, in one pyproj call
// the scene's root node carries the grid offset as a matrix (most) or as a translation only (e.g. 14-NW-2B, no rotation, Y-up)
const origins = files.map((f) => { const j = JSON.parse(fs.readFileSync(f, 'utf8')), n = j.nodes[j.scenes[j.scene ?? 0].nodes[0]]
  const t = n.matrix ? n.matrix.slice(12, 15) : n.translation || [0, 0, 0]; return [t[0], -t[2], t[1]] })
const ll = JSON.parse(execFileSync('python3', ['-c', `import json,sys
from pyproj import Transformer
t=Transformer.from_crs(2326,4326,always_xy=True)
print(json.dumps([t.transform(e,n) for e,n,h in json.load(sys.stdin)]))`], { input: JSON.stringify(origins), maxBuffer: 1 << 26 }).toString())

fs.rmSync(outDir, { recursive: true, force: true }); fs.mkdirSync(outDir, { recursive: true })
const cells = new Map(), st = { buildings: 0, triangles: 0, images: 0, imageBytesIn: 0, imageBytesOut: 0, glbBytes: 0 }
const skipped = []                                       // buildings Lands D ships broken (e.g. a texture missing from the zip)
for (const [k, f] of files.entries()) { try {
  const id = path.basename(f, '.gltf'), doc = await io.read(f), root = doc.getRoot(), node = root.listScenes()[0].listChildren()[0]
  const M0 = node.getMatrix(); node.setMatrix([...M0.slice(0, 12), 0, 0, 0, 1])               // keep Z-up -> Y-up (and any unit scale: some are in inches, 0.0254), drop the grid offset
  await pool(root.listTextures(), 4, async (t) => { const img = t.getImage(); st.images++; st.imageBytesIn += img.length
    if (kind !== 'jpeg') t.setImage(new Uint8Array(await encodeKtx2(await decodeImage(img, t.getMimeType(), id), 1 / SCALE, id, kind === 'uastc' ? UASTC : ETC1S))).setMimeType('image/ktx2')
    st.imageBytesOut += t.getImage().length })
  if (kind !== 'jpeg') doc.createExtension(KHRTextureBasisu).setRequired(true)
  for (const m of root.listMeshes()) for (const p of m.listPrimitives()) st.triangles += (p.getIndices()?.getCount() ?? p.getAttribute('POSITION').getCount()) / 3
  // bounds in the tile frame (ENU, Z-up): POSITION min/max corners through each mesh node's world matrix (Y-up), then Y-up -> Z-up
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity]
  for (const n of root.listNodes()) { const m = n.getMesh(); if (!m) continue; const W = C.Matrix4.fromArray(n.getWorldMatrix())
    for (const p of m.listPrimitives()) { const P = p.getAttribute('POSITION'), a = P.getMin([]), b = P.getMax([])
      for (let c = 0; c < 8; c++) { const v = C.Matrix4.multiplyByPoint(W, new C.Cartesian3(c & 1 ? b[0] : a[0], c & 2 ? b[1] : a[1], c & 4 ? b[2] : a[2]), new C.Cartesian3())
        ;[v.x, -v.z, v.y].forEach((x, i) => { lo[i] = Math.min(lo[i], x); hi[i] = Math.max(hi[i], x) }) } } }
  await doc.transform(meshopt({ encoder: MeshoptEncoder, level: 'medium' }))                  // geometry: EXT_meshopt_compression (after the bounds: it quantizes POSITION)
  const glb = Buffer.from(await io.writeBinary(doc)); fs.writeFileSync(path.join(outDir, `${id}.glb`), glb); st.glbBytes += glb.length; st.buildings++
  const [lon, lat] = ll[k], h = origins[k][2], E = C.Matrix4.toArray(C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(lon, lat, h)))
  const c = lo.map((v, i) => (v + hi[i]) / 2), r = lo.map((v, i) => (hi[i] - v) / 2 + 0.1)
  const key = `${Math.floor(origins[k][0] / CELL)},${Math.floor(origins[k][1] / CELL)}`
  const cell = cells.get(key) || cells.set(key, { kids: [], w: Infinity, s: Infinity, e: -Infinity, n: -Infinity, lo: Infinity, hi: -Infinity }).get(key)
  const dLon = Math.max(r[0], r[1]) * 1.5 / (111320 * Math.cos(lat * Math.PI / 180)), dLat = Math.max(r[0], r[1]) * 1.5 / 110540
  Object.assign(cell, { w: Math.min(cell.w, lon - dLon), e: Math.max(cell.e, lon + dLon), s: Math.min(cell.s, lat - dLat), n: Math.max(cell.n, lat + dLat), lo: Math.min(cell.lo, h + lo[2] - 1), hi: Math.max(cell.hi, h + hi[2] + 1) })
  cell.kids.push({ transform: E, boundingVolume: { box: [...c, r[0], 0, 0, 0, r[1], 0, 0, 0, r[2]] }, geometricError: 0, content: { uri: `${id}.glb` } })
  if (k % 100 === 0) console.error(`  ${k}/${files.length}`)
} catch (e) { skipped.push({ id: path.basename(f, '.gltf'), error: String(e.message || e).slice(0, 200) }); console.error(`  SKIPPED ${path.basename(f)}: ${e.message}`) } }
const R = (d) => d * Math.PI / 180, region = (c) => [R(c.w), R(c.s), R(c.e), R(c.n), c.lo, c.hi], all = [...cells.values()]
const top = { w: Math.min(...all.map((c) => c.w)), s: Math.min(...all.map((c) => c.s)), e: Math.max(...all.map((c) => c.e)), n: Math.max(...all.map((c) => c.n)), lo: Math.min(...all.map((c) => c.lo)), hi: Math.max(...all.map((c) => c.hi)) }
fs.writeFileSync(path.join(outDir, 'tileset.json'), JSON.stringify({ asset: { version: '1.1', gltfUpAxis: 'Y', extras: { source: 'Lands D Individualised models (CSDI landsd_rcd_1671676915450_88604), converted by github.com/milleleaf/hk-building-textures indiv.mjs', tex: TEX } },
  geometricError: 1000, root: { boundingVolume: { region: region(top) }, geometricError: 1000, refine: 'ADD',
    children: all.map((c) => ({ boundingVolume: { region: region(c) }, geometricError: GE, refine: 'ADD', children: c.kids })) } }))
const rep = { ...st, skipped: skipped.length, skipped_detail: skipped, cells: all.length, tex: TEX, ge: GE, MB: +(st.glbBytes / 1e6).toFixed(1), imageMB_in: +(st.imageBytesIn / 1e6).toFixed(1), imageMB_out: +(st.imageBytesOut / 1e6).toFixed(1) }
fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(rep, null, 1)); console.log(JSON.stringify(rep))
