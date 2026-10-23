// (from the hk-ifcx spike P3 of 2026-10-23) our own texture baker. Each CSDI building becomes a simple block (footprint extruded BaseHeight ->
// TopHeight); its walls (one strip per footprint edge) and roof (planar) get texels at --texel metres, and every texel takes
// its colour from the Open3Dhk photomesh's finest LOD:
//   walls: a ray per texel along the wall normal, both ways within --search m; the hit nearest the wall plane wins, and
//          only triangles facing outward (normal . wall normal > 0.25) count;
//   roof:  a ray per texel straight down from TopHeight + 15 m; the highest hit at or above BaseHeight wins (facing up).
// The rays are cast as an orthographic rasterisation of the candidate triangles onto the face (one sample per texel
// centre, 2x2 per texel, averaged), which is the same set of rays. Candidates come from a uniform 2 m xy grid over the
// leaf triangles. A hit's UV is sampled (bilinear) from the leaf tile's own texture. Texels with no hit are filled from
// their neighbours (and counted). Per building: one atlas (shelf-packed, 2 px edge padding), KTX2 ETC1S + mips (toktx),
// one GLB (KHR_materials_unlit, KHR_texture_basisu) in a 3D Tiles 1.1 tileset whose root transform is ENU -> ECEF
// (gltfUpAxis Z: positions are east, north, up = mPD), as the photomesh's own frame.
// usage: node bake.mjs <sheet dir>[,<dir>...] <footprints.json> <out dir> [--near lon,lat --n 50 | --all]
//        [--texel 0.2] [--search 3] [--png 1] [--diag] [--mesh3dsd <fetch3dsd.py dir> [--chart-deg 20] [--dz 0]]
// Several sheet dirs (comma-separated) bake across sheet borders; a tile listed in a tileset.json but not on disk is skipped
// (zipcut.py fetches only the tiles near the buildings). A footprint with "sheet": false is context only, never baked.
// --mesh3dsd: see the section of that name below.
import fs from 'node:fs'; import path from 'node:path'; import os from 'node:os'; import { execFileSync } from 'node:child_process'
import * as THREE from 'three'
import { Document } from '@gltf-transform/core'; import { KHRTextureBasisu, KHRMaterialsUnlit } from '@gltf-transform/extensions'
import { meshopt } from '@gltf-transform/functions'; import { MeshoptEncoder } from 'meshoptimizer'
import { C, enuFrame, mul, M4, readB3dm, listTilesets, tilesOf, decodeImage, encodeKtx2, io, QUIET } from './lib.mjs'

const a = process.argv.slice(2), opt = (f, d) => (a.includes(f) ? a[a.indexOf(f) + 1] : d)
const [inDir, fpFile, outDir] = a
const TEXEL = +opt('--texel', 0.2), SEARCH = +opt('--search', 3), N = +opt('--n', 50), ALL = a.includes('--all'), PNG = opt('--png', '1') === '1'
const SS = 2, ROOF_UP = 15, MAXPX = 4096, FACING = +opt('--facing', 0.25)
// --diag: no output; every texel with no hit is sorted by cause (see diagWall), with rays out to DR m
const DIAG = a.includes('--diag'), DR = 15, COV = 5, covered = new Set()
const fp = JSON.parse(fs.readFileSync(fpFile, 'utf8')), [lo0, la0, lo1, la1] = fp.bbox
const ORIGIN = [(lo0 + lo1) / 2, (la0 + la1) / 2], E = enuFrame(...ORIGIN)

// --- buildings: own rings in ENU, heights present and > 2 m
const inRings = (x, y, rings) => { let c = false; for (const r of rings) for (let i = 0, j = r.length - 1; i < r.length; j = i++)
  if ((r[i][1] > y) !== (r[j][1] > y) && x < (r[j][0] - r[i][0]) * (y - r[i][1]) / (r[j][1] - r[i][1]) + r[i][0]) c = !c; return c }
// a footprint with sheet: false is context only (party walls, which 3DSD model stands for what), never baked
let blds = fp.buildings.filter((b) => b.sheet !== false && b.base != null && b.top != null && b.top - b.base > 2).map((b) => {
  const rings = b.rings.map((r) => { const q = r.map(([lo, la]) => E.ll(lo, la).slice(0, 2)); if (q.length > 1 && q[0][0] === q.at(-1)[0] && q[0][1] === q.at(-1)[1]) q.pop(); return q })
  const xs = rings.flat().map((p) => p[0]), ys = rings.flat().map((p) => p[1])
  return { id: b.id, name: b.name, base: b.base, top: b.top, rings, bb: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] }
})
const skipped = fp.buildings.filter((b) => b.sheet !== false).length - blds.length
// every footprint (with or without heights) on a 20 m grid, to tell party walls (another building right outside) from street walls
const NB = fp.buildings.map((b) => b.rings.map((r) => r.map(([lo, la]) => E.ll(lo, la).slice(0, 2)))), nbGrid = new Map()
NB.forEach((rings, k) => { const xs = rings.flat().map((p) => p[0]), ys = rings.flat().map((p) => p[1])
  for (let i = Math.floor(Math.min(...xs) / 20); i <= Math.floor(Math.max(...xs) / 20); i++) for (let j = Math.floor(Math.min(...ys) / 20); j <= Math.floor(Math.max(...ys) / 20); j++) { const key = i * 100003 + j; (nbGrid.get(key) || nbGrid.set(key, []).get(key)).push(k) } })
/** inside a footprint other than self (an id, or a Set of ids) */
const inOther = (x, y, self) => (nbGrid.get(Math.floor(x / 20) * 100003 + Math.floor(y / 20)) || []).some((k) => (self.has ? !self.has(fp.buildings[k].id) : fp.buildings[k].id !== self) && inRings(x, y, NB[k]))
if (!ALL) { const [x0, y0] = E.ll(...opt('--near', '114.1694,22.3193').split(',').map(Number)); const d = (b) => Math.hypot((b.bb[0] + b.bb[2]) / 2 - x0, (b.bb[1] + b.bb[3]) / 2 - y0)
  blds = blds.sort((p, q) => d(p) - d(q)).slice(0, N) }

// --- leaf tiles (geometricError 0) with their ENU bounding spheres
const leaves = []; let leavesMissing = 0
for (const f of inDir.split(',').flatMap(listTilesets)) { const { tiles } = tilesOf(f)
  for (const t of tiles) { if (t.node.geometricError !== 0 && (t.node.children || []).length) continue
    if (!fs.existsSync(t.file)) { leavesMissing++; continue }                              // zipcut.py fetches only the tiles near the buildings
    const v = t.node.boundingVolume, b = v.sphere || v.box; if (!b) continue
    const c = C.Matrix4.multiplyByPoint(mul(E.Finv, t.M), new C.Cartesian3(b[0], b[1], b[2]), new C.Cartesian3())
    leaves.push({ file: t.file, M: t.M, c: [c.x, c.y, c.z], r: v.sphere ? b[3] : Math.hypot(...b.slice(3, 12)) }) } }

// --- tile geometry (LRU) and decoded textures (LRU): never the whole sheet in memory at once
const geoCache = new Map(), decoded = new Map(), GEO_MAX = +opt('--geo-max', 400), TEX_MAX = 96   // --geo-max 150 kept a 9-sheet run under ~5 GB
const lru = (m, k, v, max) => { m.delete(k); m.set(k, v); while (m.size > max) m.delete(m.keys().next().value); return v }
async function loadLeaf(L) {
  if (geoCache.has(L.file)) return lru(geoCache, L.file, geoCache.get(L.file), GEO_MAX)
  const r = await readB3dm(L.file); const g = { xyz: [], uv: [], tex: [] }
  if (r) { const W0 = r.rtc ? mul(L.M, C.Matrix4.fromTranslation(new C.Cartesian3(...r.rtc))) : L.M, texs = r.doc.getRoot().listTextures()
    const T0 = texs.map((t, i) => ({ key: `${L.file}#${i}`, bytes: t.getImage(), mime: t.getMimeType() }))
    for (const node of r.doc.getRoot().listNodes()) { const mesh = node.getMesh(); if (!mesh) continue; const T = mul(mul(E.Finv, W0), M4(node.getWorldMatrix()))
      for (const p of mesh.listPrimitives()) { const P = p.getAttribute('POSITION'), UV = p.getAttribute('TEXCOORD_0'), tex = p.getMaterial()?.getBaseColorTexture(); if (!UV || !tex) continue
        const to = T0[texs.indexOf(tex)], n = P.getCount(), xyz = new Float64Array(n * 3), uv = new Float32Array(n * 2), q = [], v = new C.Cartesian3()
        for (let i = 0; i < n; i++) { P.getElement(i, q); C.Matrix4.multiplyByPoint(T, C.Cartesian3.fromArray(q, 0, v), v); xyz[3 * i] = v.x; xyz[3 * i + 1] = v.y; xyz[3 * i + 2] = v.z; UV.getElement(i, q); uv[2 * i] = q[0]; uv[2 * i + 1] = q[1] }
        const idx = p.getIndices()?.getArray() ?? Uint32Array.from({ length: n }, (_, i) => i)
        for (let i = 0; i < idx.length; i += 3) { for (let k = 0; k < 3; k++) { const j = idx[i + k]; g.xyz.push(xyz[3 * j], xyz[3 * j + 1], xyz[3 * j + 2]); g.uv.push(uv[2 * j], uv[2 * j + 1]) } g.tex.push(to) } } } }
  g.xyz = Float64Array.from(g.xyz); g.uv = Float32Array.from(g.uv)
  if (DIAG) for (let i = 0; i < g.xyz.length; i += 3) covered.add(Math.floor(g.xyz[i] / COV) * 100003 + Math.floor(g.xyz[i + 1] / COV))   // where the mesh exists at all
  return lru(geoCache, L.file, g, GEO_MAX)
}
/** decode every texture the hits use (before the synchronous shading loop) */
async function decodeFor(tris, hitT) { const need = new Map(); for (const t of hitT) if (t >= 0) need.set(tris.tex[t].key, tris.tex[t])
  const m = new Map(); for (const [k, o] of need) m.set(k, decoded.has(k) ? lru(decoded, k, decoded.get(k), TEX_MAX) : lru(decoded, k, await decodeImage(o.bytes, o.mime, 'b'), TEX_MAX)); return m }

// --- per building: candidate triangles from the leaves around it, on a 2 m xy grid
const CELL = 2
async function candidates(b) {
  const pad = (DIAG ? DR : SEARCH) + 2, bb = [b.bb[0] - pad, b.bb[1] - pad, b.bb[2] + pad, b.bb[3] + pad], sel = []
  for (const L of leaves) { if (L.c[0] + L.r < bb[0] || L.c[0] - L.r > bb[2] || L.c[1] + L.r < bb[1] || L.c[1] - L.r > bb[3]) continue
    const g = await loadLeaf(L), ts = []
    for (let t = 0; t < g.tex.length; t++) { const o = 9 * t, X = g.xyz
      const zmin = Math.min(X[o + 2], X[o + 5], X[o + 8]), zmax = Math.max(X[o + 2], X[o + 5], X[o + 8])
      if (zmax < b.base - 1 || zmin > b.top + ROOF_UP) continue
      if (Math.max(X[o], X[o + 3], X[o + 6]) < bb[0] || Math.min(X[o], X[o + 3], X[o + 6]) > bb[2] || Math.max(X[o + 1], X[o + 4], X[o + 7]) < bb[1] || Math.min(X[o + 1], X[o + 4], X[o + 7]) > bb[3]) continue
      ts.push(t) }
    if (ts.length) sel.push([g, Int32Array.from(ts)]) }
  // typed arrays sized up front (growing JS arrays for a 250 m podium pushed the process past 8 GB)
  const n = sel.reduce((x, [, ts]) => x + ts.length, 0), tri = { xyz: new Float64Array(9 * n), uv: new Float32Array(6 * n), tex: new Array(n) }; let i = 0
  for (const [g, ts] of sel) for (const t of ts) { tri.xyz.set(g.xyz.subarray(9 * t, 9 * t + 9), 9 * i); tri.uv.set(g.uv.subarray(6 * t, 6 * t + 6), 6 * i); tri.tex[i++] = g.tex[t] }
  tri.query = indexXY(tri.xyz, tri.tex.length); return tri
}
/** xy grid over n triangles (9 coords each in X): query(x0, y0, x1, y1) -> triangles whose xy bbox touches those cells */
function indexXY(X, n) {
  const grid = new Map()
  for (let t = 0; t < n; t++) { const o = 9 * t
    const i0 = Math.floor(Math.min(X[o], X[o + 3], X[o + 6]) / CELL), i1 = Math.floor(Math.max(X[o], X[o + 3], X[o + 6]) / CELL)
    const j0 = Math.floor(Math.min(X[o + 1], X[o + 4], X[o + 7]) / CELL), j1 = Math.floor(Math.max(X[o + 1], X[o + 4], X[o + 7]) / CELL)
    for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) { const k = i * 100003 + j; (grid.get(k) || grid.set(k, []).get(k)).push(t) } }
  const stamp = new Int32Array(n); let gen = 0
  return (x0, y0, x1, y1) => { gen++; const out = []
    for (let i = Math.floor(x0 / CELL); i <= Math.floor(x1 / CELL); i++) for (let j = Math.floor(y0 / CELL); j <= Math.floor(y1 / CELL); j++)
      for (const t of grid.get(i * 100003 + j) || []) if (stamp[t] !== gen) { stamp[t] = gen; out.push(t) }
    return out }
}

/** Rasterise triangles onto a face: proj(t) -> [[px, py, depth] x3] (px units), score(depth) -> lower wins or null.
 *  Returns per-sample { uv, tex } of the winning hit. */
function raster(W, H, tris, ids, proj, accept) {
  const best = new Float32Array(W * H).fill(Infinity), hitT = new Int32Array(W * H).fill(-1), hitU = new Float32Array(W * H * 2), dep = new Float32Array(W * H)
  for (const t of ids) { const P = proj(t); if (!P) continue
    const x0 = Math.max(0, Math.ceil(Math.min(P[0][0], P[1][0], P[2][0]) - 0.5)), x1 = Math.min(W - 1, Math.floor(Math.max(P[0][0], P[1][0], P[2][0]) - 0.5))
    const y0 = Math.max(0, Math.ceil(Math.min(P[0][1], P[1][1], P[2][1]) - 0.5)), y1 = Math.min(H - 1, Math.floor(Math.max(P[0][1], P[1][1], P[2][1]) - 0.5))
    if (x0 > x1 || y0 > y1) continue
    const e = (a, b, x, y) => (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]), A = e(P[0], P[1], P[2][0], P[2][1]); if (Math.abs(A) < 1e-12) continue
    const U = tris.uv, o = 6 * t
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) { const cx = x + 0.5, cy = y + 0.5
      const w0 = e(P[1], P[2], cx, cy) / A, w1 = e(P[2], P[0], cx, cy) / A, w2 = 1 - w0 - w1; if (w0 < 0 || w1 < 0 || w2 < 0) continue
      const z = w0 * P[0][2] + w1 * P[1][2] + w2 * P[2][2], s = accept(z, y * W + x); if (s == null || s >= best[y * W + x]) continue
      best[y * W + x] = s; dep[y * W + x] = z; hitT[y * W + x] = t; hitU[2 * (y * W + x)] = w0 * U[o] + w1 * U[o + 2] + w2 * U[o + 4]; hitU[2 * (y * W + x) + 1] = w0 * U[o + 1] + w1 * U[o + 3] + w2 * U[o + 5] } }
  return { hitT, hitU, best, dep }
}
/** samples -> texel RGBA (SSxSS average of hits) and a hit mask; inside(i, j) limits what counts as a texel (roof outside the footprint does not) */
async function shade(W, H, tris, { hitT, hitU }, inside = () => true) {
  const ims = await decodeFor(tris, hitT), w = W / SS, h = H / SS, px = new Uint8Array(w * h * 4), hit = new Uint8Array(w * h); let texels = 0, miss = 0
  for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) { let r = 0, g = 0, bl = 0, n = 0
    for (let dy = 0; dy < SS; dy++) for (let dx = 0; dx < SS; dx++) { const s = (j * SS + dy) * W + i * SS + dx, t = hitT[s]; if (t < 0) continue
      const im = ims.get(tris.tex[t].key), u = hitU[2 * s] * im.w - 0.5, v = hitU[2 * s + 1] * im.h - 0.5
      const x0 = Math.max(0, Math.min(im.w - 1, Math.floor(u))), y0 = Math.max(0, Math.min(im.h - 1, Math.floor(v))), x1 = Math.min(im.w - 1, x0 + 1), y1 = Math.min(im.h - 1, y0 + 1)
      const fx = Math.min(1, Math.max(0, u - x0)), fy = Math.min(1, Math.max(0, v - y0)), q = (x, y, c) => im.px[4 * (y * im.w + x) + c]
      const bil = (c) => (q(x0, y0, c) * (1 - fx) + q(x1, y0, c) * fx) * (1 - fy) + (q(x0, y1, c) * (1 - fx) + q(x1, y1, c) * fx) * fy
      r += bil(0); g += bil(1); bl += bil(2); n++ }
    const k = j * w + i; if (!inside(i, j)) continue; texels++
    if (n) { px.set([r / n, g / n, bl / n, 255], 4 * k); hit[k] = 1 } else miss++ }
  fill(w, h, px, hit); return { w, h, px, texels, miss }
}
/** no-hit texels take the mean of hit neighbours, growing outward; anything left (no hit at all) mid grey */
function fill(w, h, px, hit) {
  let todo = true; for (let it = 0; todo && it < 64; it++) { todo = false; const nh = hit.slice()
    for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) { const k = j * w + i; if (hit[k]) continue; let r = 0, g = 0, b = 0, n = 0
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) { const x = i + dx, y = j + dy; if (x < 0 || y < 0 || x >= w || y >= h || !hit[y * w + x]) continue; const q = 4 * (y * w + x); r += px[q]; g += px[q + 1]; b += px[q + 2]; n++ }
      if (n) { px.set([r / n, g / n, b / n, 255], 4 * k); nh[k] = 1 } else todo = true }
    hit.set(nh) }
  for (let k = 0; k < w * h; k++) if (!hit[k]) px.set([128, 128, 128, 255], 4 * k)
}

/** atlas: shelf pack of faces[].im ({w, h}, px optional), 2 px padding (edge pixels repeated) */
const P = 2
function pack(faces, dimsOnly = false) {
  const rects = faces.map((f) => ({ f, w: f.im.w + 2 * P, h: f.im.h + 2 * P })).sort((p, q) => q.h - p.h)
  const total = rects.reduce((x, r) => x + r.w * r.h, 0), AW = Math.min(MAXPX, Math.max(Math.max(...rects.map((r) => r.w)), Math.ceil(Math.sqrt(total * 1.2))))
  let x = 0, y = 0, rowH = 0; for (const r of rects) { if (x + r.w > AW) { x = 0; y += rowH; rowH = 0 } r.x = x; r.y = y; x += r.w; rowH = Math.max(rowH, r.h) }
  const aw = Math.ceil(AW / 4) * 4, ah = Math.ceil((y + rowH) / 4) * 4; if (dimsOnly) return { aw, ah }
  const atlas = new Uint8Array(aw * ah * 4).fill(128)
  for (const r of rects) { const { w, h, px } = r.f.im
    for (let j = -P; j < h + P; j++) for (let i = -P; i < w + P; i++) { const si = Math.min(w - 1, Math.max(0, i)), sj = Math.min(h - 1, Math.max(0, j)); atlas.set(px.subarray(4 * (sj * w + si), 4 * (sj * w + si) + 4), 4 * ((r.y + P + j) * aw + r.x + P + i)) } }
  return { rects, aw, ah, atlas }
}
const add = (st, k, n = 1) => { st.d = st.d || {}; st.d[k] = (st.d[k] || 0) + n }
const anyHit = (R, W, i, j) => { for (let dy = 0; dy < SS; dy++) for (let dx = 0; dx < SS; dx++) if (R.hitT[(j * SS + dy) * W + i * SS + dx] >= 0) return true; return false }
const minBest = (R, W, i, j) => { let m = Infinity; for (let dy = 0; dy < SS; dy++) for (let dx = 0; dx < SS; dx++) m = Math.min(m, R.best[(j * SS + dy) * W + i * SS + dx]); return m }
/** any mesh vertex within 10 m (xy) of this point: false = the sheet's mesh does not reach here (edge of the sheet) */
const meshNear = (x, y) => { for (let i = -2; i <= 2; i++) for (let j = -2; j <= 2; j++) if (covered.has((Math.floor(x / COV) + i) * 100003 + Math.floor(y / COV) + j)) return true; return false }
/** each wall texel with no hit (R: facing outward, within SEARCH) gets one cause:
 *  no_mesh_here (sheet edge) > far_<8 / far_<15 (an outward-facing surface, but SEARCH..DR m off the wall) > wrong_facing
 *  (a surface within SEARCH, but facing away or sideways) > above_mesh_top (texel over 1 m above the highest mesh within DR m)
 *  > nothing_15m (no surface within DR m);
 *  split by kind (party/street) and by height above the block's base (low < 10 m) */
function diagWall(st, kind, W, H, su, sv, A, d, n, b, R, Rfar, Rany, tris, ids) {
  const top = new Float64Array(W / SS).fill(-Infinity), X = tris.xyz          // per texel column: highest mesh vertex within DR m of the wall
  for (const t of ids) for (let k = 0; k < 3; k++) { const o = 9 * t + 3 * k, x = X[o] - A[0], y = X[o + 1] - A[1], i = Math.floor((x * d[0] + y * d[1]) / (su * SS))
    if (i >= 0 && i < top.length && Math.abs(x * n[0] + y * n[1]) <= DR) top[i] = Math.max(top[i], X[o + 2]) }
  for (let j = 0; j < H / SS; j++) for (let i = 0; i < W / SS; i++) {
    const z = b.top - (j + 0.5) * sv * SS, band = z - b.base < 10 ? 'low' : 'high'
    add(st, `${kind}.${band}.all`); if (anyHit(R, W, i, j)) continue
    const u = (i + 0.5) * su * SS, x = A[0] + u * d[0] + 1 * n[0], y = A[1] + u * d[1] + 1 * n[1], f = minBest(Rfar, W, i, j)
    const cause = !meshNear(x, y) ? 'no_mesh_here' : f <= 8 ? 'far_<8' : f <= DR ? 'far_<15' : anyHit(Rany, W, i, j) ? 'wrong_facing' : z > top[i] + 1 ? 'above_mesh_top' : 'nothing_15m'
    add(st, `${kind}.${band}.${cause}`) } }

async function bake(b) {
  const t0 = Date.now(), tris = await candidates(b), X = tris.xyz, H = b.top - b.base, faces = []
  if (!tris.tex.length) return null                                                       // outside the sheet's photomesh
  const holes = b.rings.map((r, i) => i > 0 && b.rings.some((o, k) => k !== i && inRings(r[0][0], r[0][1], [o])))
  // texel size: TEXEL, coarser if the atlas would exceed MAXPX^2
  let perim = 0; for (const r of b.rings) for (let i = 0; i < r.length; i++) { const p = r[i], q = r[(i + 1) % r.length]; perim += Math.hypot(q[0] - p[0], q[1] - p[1]) }
  const area0 = (perim * H + (b.bb[2] - b.bb[0]) * (b.bb[3] - b.bb[1])) / TEXEL ** 2 * 1.3, s = Math.max(TEXEL, TEXEL * Math.sqrt(area0) / (MAXPX * 0.95))
  const st = { wallTexels: 0, wallMiss: 0, partyTexels: 0, partyMiss: 0, roofTexels: 0, roofMiss: 0 }
  for (const r of b.rings) for (let i = 0; i < r.length; i++) {
    const A = r[i], B = r[(i + 1) % r.length], L = Math.hypot(B[0] - A[0], B[1] - A[1]); if (L < 0.05) continue
    const d = [(B[0] - A[0]) / L, (B[1] - A[1]) / L]; let n = [d[1], -d[0]]
    if (inRings((A[0] + B[0]) / 2 + 0.3 * n[0], (A[1] + B[1]) / 2 + 0.3 * n[1], b.rings)) n = [-n[0], -n[1]]           // outward
    const w = Math.max(1, Math.round(L / s)), h = Math.max(1, Math.round(H / s)), W = w * SS, Hh = h * SS, su = L / W, sv = H / Hh
    const ids = tris.query(Math.min(A[0], B[0]) - SEARCH, Math.min(A[1], B[1]) - SEARCH, Math.max(A[0], B[0]) + SEARCH, Math.max(A[1], B[1]) + SEARCH)
    const proj = (fac) => (t) => { const o = 9 * t
      const e1 = [X[o + 3] - X[o], X[o + 4] - X[o + 1], X[o + 5] - X[o + 2]], e2 = [X[o + 6] - X[o], X[o + 7] - X[o + 1], X[o + 8] - X[o + 2]]
      const nx = e1[1] * e2[2] - e1[2] * e2[1], ny = e1[2] * e2[0] - e1[0] * e2[2], nz = e1[0] * e2[1] - e1[1] * e2[0], nl = Math.hypot(nx, ny, nz)
      if (!nl || (nx * n[0] + ny * n[1]) / nl < fac) return null
      return [0, 1, 2].map((k) => { const x = X[o + 3 * k] - A[0], y = X[o + 3 * k + 1] - A[1]; return [(x * d[0] + y * d[1]) / su, (b.top - X[o + 3 * k + 2]) / sv, x * n[0] + y * n[1]] }) }
    const R = raster(W, Hh, tris, ids, proj(FACING), (wd) => (Math.abs(wd) <= SEARCH ? Math.abs(wd) : null))
    const im = await shade(W, Hh, tris, R); st.wallTexels += im.texels; st.wallMiss += im.miss
    const party = [0.25, 0.5, 0.75].filter((f) => inOther(A[0] + f * (B[0] - A[0]) + 0.8 * n[0], A[1] + f * (B[1] - A[1]) + 0.8 * n[1], b.id)).length >= 2
    if (party) { st.partyTexels += im.texels; st.partyMiss += im.miss }
    if (DIAG) { const far = tris.query(Math.min(A[0], B[0]) - DR, Math.min(A[1], B[1]) - DR, Math.max(A[0], B[0]) + DR, Math.max(A[1], B[1]) + DR)
      diagWall(st, party ? 'party' : 'street', W, Hh, su, sv, A, d, n, b, R, raster(W, Hh, tris, far, proj(FACING), (wd) => (Math.abs(wd) <= DR ? Math.abs(wd) : null)),
        raster(W, Hh, tris, ids, proj(-2), (wd) => (Math.abs(wd) <= SEARCH ? Math.abs(wd) : null)), tris, far) }
    faces.push({ kind: 'wall', im, A, B })
  }
  { const w = Math.max(1, Math.round((b.bb[2] - b.bb[0]) / s)), h = Math.max(1, Math.round((b.bb[3] - b.bb[1]) / s)), W = w * SS, Hh = h * SS
    const su = (b.bb[2] - b.bb[0]) / W, sv = (b.bb[3] - b.bb[1]) / Hh
    const R = raster(W, Hh, tris, tris.query(...b.bb), (t) => { const o = 9 * t
      const e1 = [X[o + 3] - X[o], X[o + 4] - X[o + 1], X[o + 5] - X[o + 2]], e2 = [X[o + 6] - X[o], X[o + 7] - X[o + 1], X[o + 8] - X[o + 2]]
      const nz = e1[0] * e2[1] - e1[1] * e2[0], nl = Math.hypot(e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], nz)
      if (!nl || nz / nl < 0.2) return null
      return [0, 1, 2].map((k) => [(X[o + 3 * k] - b.bb[0]) / su, (b.bb[3] - X[o + 3 * k + 1]) / sv, X[o + 3 * k + 2]])
    }, (z) => (z >= b.base && z <= b.top + ROOF_UP ? -z : null))
    const im = await shade(W, Hh, tris, R, (i, j) => inRings(b.bb[0] + (i + 0.5) * (b.bb[2] - b.bb[0]) / w, b.bb[3] - (j + 0.5) * (b.bb[3] - b.bb[1]) / h, b.rings)); st.roofTexels += im.texels; st.roofMiss += im.miss
    if (DIAG) { const w2 = W / SS; for (let j = 0; j < Hh / SS; j++) for (let i = 0; i < w2; i++) { const x = b.bb[0] + (i + 0.5) * su * SS, y = b.bb[3] - (j + 0.5) * sv * SS
        if (!inRings(x, y, b.rings)) continue
        if (anyHit(R, W, i, j)) { add(st, 'roofdz.' + Math.max(-20, Math.min(20, Math.round(-minBest(R, W, i, j) - b.top)))); continue }   // mesh roof - TopHeight, 1 m bins
        add(st, 'roof.' + (meshNear(x, y) ? 'other' : 'no_mesh_here')) } }
    faces.push({ kind: 'roof', im }) }
  if (DIAG && !MESH) return { id: b.id, ...st, ms: Date.now() - t0, box: [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1] }   // with --mesh3dsd the run also writes its output
  const { rects, aw, ah, atlas } = pack(faces)
  // geometry: walls as quads, roof triangulated (three's earcut), UVs into the atlas
  const pos = [], uv = [], idx = [], U = (r, u, v) => [(r.x + P + u * r.f.im.w) / aw, (r.y + P + v * r.f.im.h) / ah]
  for (const r of rects) { const f = r.f
    if (f.kind === 'wall') { const o = pos.length / 3
      pos.push(f.A[0], f.A[1], b.base, f.B[0], f.B[1], b.base, f.B[0], f.B[1], b.top, f.A[0], f.A[1], b.top); uv.push(...U(r, 0, 1), ...U(r, 1, 1), ...U(r, 1, 0), ...U(r, 0, 0))
      idx.push(o, o + 1, o + 2, o, o + 2, o + 3) }
    else for (let k = 0; k < b.rings.length; k++) { if (holes[k]) continue
      const outer = b.rings[k].map(([px, py]) => new THREE.Vector2(px, py)), hs = b.rings.filter((h, i) => holes[i] && inRings(h[0][0], h[0][1], [b.rings[k]])).map((h) => h.map(([px, py]) => new THREE.Vector2(px, py)))
      const all = [...outer, ...hs.flat()], o = pos.length / 3
      for (const p of all) { pos.push(p.x, p.y, b.top); uv.push(...U(r, (p.x - b.bb[0]) / (b.bb[2] - b.bb[0]), (b.bb[3] - p.y) / (b.bb[3] - b.bb[1]))) }
      for (const t of THREE.ShapeUtils.triangulateShape(outer, hs)) idx.push(o + t[0], o + t[1], o + t[2]) } }
  const kt = await encodeKtx2({ w: aw, h: ah, px: atlas }, 1, 'atlas')
  const doc = new Document(), buf = doc.createBuffer(), unlit = doc.createExtension(KHRMaterialsUnlit); doc.createExtension(KHRTextureBasisu).setRequired(true)
  const tex = doc.createTexture(b.id).setImage(new Uint8Array(kt)).setMimeType('image/ktx2')
  const mat = doc.createMaterial(b.id).setBaseColorTexture(tex).setDoubleSided(true).setExtension('KHR_materials_unlit', unlit.createUnlit())
  const prim = doc.createPrimitive().setMaterial(mat).setIndices(doc.createAccessor().setType('SCALAR').setArray(Uint32Array.from(idx)).setBuffer(buf))
    .setAttribute('POSITION', doc.createAccessor().setType('VEC3').setArray(Float32Array.from(pos)).setBuffer(buf))
    .setAttribute('TEXCOORD_0', doc.createAccessor().setType('VEC2').setArray(Float32Array.from(uv)).setBuffer(buf))
  doc.createScene().addChild(doc.createNode(b.id).setMesh(doc.createMesh().addPrimitive(prim)))
  const glb = Buffer.from(await io.writeBinary(doc)); fs.writeFileSync(path.join(outDir, `${b.id}.glb`), glb)
  if (PNG) execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', `${aw}x${ah}`, '-i', '-', path.join(outDir, 'atlas', `${b.id}.png`)], { input: Buffer.from(atlas) })
  return { id: b.id, name: b.name, base: b.base, top: b.top, texel_m: +s.toFixed(3), atlas: [aw, ah], bytes: glb.length, ktx2: kt.length, tris_candidate: tris.tex.length, faces: faces.length, ...st, ms: Date.now() - t0,
    box: [(b.bb[0] + b.bb[2]) / 2, (b.bb[1] + b.bb[3]) / 2, (b.base + b.top) / 2, (b.bb[2] - b.bb[0]) / 2 + 0.1, 0, 0, 0, (b.bb[3] - b.bb[1]) / 2 + 0.1, 0, 0, 0, H / 2 + 0.1] }
}

// ---- --mesh3dsd <dir> (fetch3dsd.py's cache + leaves.json): bake onto Lands D 3D Spatial Data (3DSD) building models instead
// of CSDI blocks. Each model (one batch-table component name) keeps its geometry and gets a new texture: its triangles are
// split into charts (connected through shared edges, normals within --chart-deg of the chart's largest triangle), each chart
// is projected onto its own plane at --texel m, and every texel takes the photomesh hit nearest the chart's surface along the
// chart normal, within --search m (outward-facing photomesh only, as for the blocks). A chart texel is "hidden" when another
// 3DSD surface lies just outside it (0.05..0.3 m, or a coincident one facing the other way, -0.05..0.3 m): party walls and
// faces buried in a neighbour. A chart that is >= 80 % hidden keeps its geometry but gets one texel (the model's mean colour).
// Which CSDI buildings a model stands for: the model's roof (upward triangles, 1 m cells) overlaps the footprints; a model is
// baked when >= 50 % of its roof falls in the footprints, and a footprint with < 50 % of its cells under a baked model's roof
// falls back to the CSDI block bake above (counted).
const MESH = opt('--mesh3dsd', null), DZ = +opt('--dz', 0), CHART = Math.cos(+opt('--chart-deg', 20) * Math.PI / 180), HIDE = 0.3
const OFFW = new Float64Array(81), OFFR = new Float64Array(81)          // signed photomesh - 3DSD offset of hits, -10..10 m, 0.25 m bins
let models = [], meshInfo = {}, ALLX = null, queryAll = null, ZALL = null, DRAWN = null
const cellKey = (i, j) => i * 100003 + j
function triCells(X, o, out) {                                        // 1 m cells whose centre lies in triangle o (xy)
  const [ax, ay, bx, by, cx, cy] = [X[o], X[o + 1], X[o + 3], X[o + 4], X[o + 6], X[o + 7]], A = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax); if (!A) return
  for (let i = Math.floor(Math.min(ax, bx, cx)); i <= Math.floor(Math.max(ax, bx, cx)); i++) for (let j = Math.floor(Math.min(ay, by, cy)); j <= Math.floor(Math.max(ay, by, cy)); j++) {
    const x = i + 0.5, y = j + 0.5, w0 = ((bx - x) * (cy - y) - (by - y) * (cx - x)) / A, w1 = ((cx - x) * (ay - y) - (cy - y) * (ax - x)) / A
    if (w0 >= 0 && w1 >= 0 && w0 + w1 <= 1) out.add(cellKey(i, j)) } }
if (MESH) {
  const byName = new Map(), Y2Z = C.Axis.Y_UP_TO_Z_UP; let tiles = 0
  for (const l of JSON.parse(fs.readFileSync(path.join(MESH, 'leaves.json'), 'utf8'))) {
    const file = path.join(MESH, l.file), buf = fs.readFileSync(file); if (buf.length < 28) continue; tiles++
    const ftj = buf.readUInt32LE(12), ftb = buf.readUInt32LE(16), btj = buf.readUInt32LE(20)
    const inst = JSON.parse(buf.toString('utf8', 28 + ftj + ftb, 28 + ftj + ftb + btj)).extensions?.['3DTILES_batch_table_hierarchy']?.classes?.[0]?.instances || { names: [], component: [] }
    const r = await readB3dm(file), T0 = mul(mul(E.Finv, M4(l.transform)), Y2Z)                 // tile frame (glTF Y-up) -> ENU
    for (const node of r.doc.getRoot().listNodes()) { const mesh = node.getMesh(); if (!mesh) continue; const T = mul(T0, M4(node.getWorldMatrix()))
      for (const p of mesh.listPrimitives()) { const Pa = p.getAttribute('POSITION'), Ba = p.getAttribute('_BATCHID'), n = Pa.getCount(), q = [], v = new C.Cartesian3(), xyz = new Float64Array(3 * n)
        for (let i = 0; i < n; i++) { Pa.getElement(i, q); C.Matrix4.multiplyByPoint(T, C.Cartesian3.fromArray(q, 0, v), v); xyz[3 * i] = v.x; xyz[3 * i + 1] = v.y; xyz[3 * i + 2] = v.z + DZ }
        const idx = p.getIndices()?.getArray() ?? Uint32Array.from({ length: n }, (_, i) => i)
        for (let i = 0; i < idx.length; i += 3) { const bid = Ba ? Ba.getScalar(idx[i]) : 0, key = inst.names[bid] || inst.component[bid] || `${l.file}#${bid}`
          let m = byName.get(key); if (!m) byName.set(key, m = { name: key, xyz: [], tiles: new Set() }); m.tiles.add(l.file)
          for (let k = 0; k < 3; k++) { const j = idx[i + k]; m.xyz.push(xyz[3 * j], xyz[3 * j + 1], xyz[3 * j + 2]) } } } } }
  const all = [...byName.values()]; let nAll = 0
  for (const m of all) { m.xyz = Float64Array.from(m.xyz); m.t0 = nAll; nAll += m.xyz.length / 9; const X = m.xyz; m.roof = new Set()
    let x0 = Infinity, y0 = Infinity, z0 = Infinity, x1 = -Infinity, y1 = -Infinity, z1 = -Infinity
    for (let i = 0; i < X.length; i += 3) { x0 = Math.min(x0, X[i]); x1 = Math.max(x1, X[i]); y0 = Math.min(y0, X[i + 1]); y1 = Math.max(y1, X[i + 1]); z0 = Math.min(z0, X[i + 2]); z1 = Math.max(z1, X[i + 2]) }
    m.bb = [x0, y0, x1, y1]; m.base = z0; m.top = z1
    for (let o = 0; o < X.length; o += 9) { const e1 = [X[o + 3] - X[o], X[o + 4] - X[o + 1], X[o + 5] - X[o + 2]], e2 = [X[o + 6] - X[o], X[o + 7] - X[o + 1], X[o + 8] - X[o + 2]]
      const nz = e1[0] * e2[1] - e1[1] * e2[0], nl = Math.hypot(e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], nz); if (nl && nz / nl > 0.3) triCells(X, o, m.roof) } }
  ALLX = new Float64Array(nAll * 9); for (const m of all) ALLX.set(m.xyz, m.t0 * 9); queryAll = indexXY(ALLX, nAll); ZALL = new Float32Array(nAll * 6)
  // CSDI footprints on the same 1 m cells (every footprint in the file, with or without heights)
  const cellOf = new Map(), cells = NB.map((rings, k) => { const xs = rings.flat().map((p) => p[0]), ys = rings.flat().map((p) => p[1]); let n = 0
    for (let i = Math.floor(Math.min(...xs)); i <= Math.floor(Math.max(...xs)); i++) for (let j = Math.floor(Math.min(...ys)); j <= Math.floor(Math.max(...ys)); j++) if (inRings(i + 0.5, j + 0.5, rings)) { const c = cellKey(i, j); (cellOf.get(c) || cellOf.set(c, []).get(c)).push(k); n++ }
    if (!n) { const c = cellKey(Math.floor(xs[0]), Math.floor(ys[0])); (cellOf.get(c) || cellOf.set(c, []).get(c)).push(k); n = 1 } return n })   // footprints overlap (tower on podium): a cell can hold several
  const cov = new Float64Array(NB.length)
  for (const m of all) { const per = new Map(); let over = 0; for (const c of m.roof) { const ks = cellOf.get(c); if (ks) { over++; for (const k of ks) per.set(k, (per.get(k) || 0) + 1) } }
    m.sel = m.roof.size > 0 && over / m.roof.size >= 0.5
    m.own = new Set([...per].filter(([k, n]) => n / cells[k] >= 0.25).map(([k]) => fp.buildings[k].id))
    m.sel = m.sel && fp.buildings.some((b) => b.sheet !== false && m.own.has(b.id))                    // stands for a footprint on the sheet
    if (m.sel) { for (const [k, n] of per) cov[k] += n; m.id = '3dsd_' + m.name.replace(/[^\w-]/g, '_') } }
  const onSheet = fp.buildings.filter((b) => b.sheet !== false), coveredIds = new Set(fp.buildings.filter((b, k) => b.sheet !== false && cov[k] / cells[k] >= 0.5).map((b) => b.id))
  models = all.filter((m) => m.sel)
  DRAWN = new Uint8Array(nAll); for (const m of models) DRAWN.fill(1, m.t0, m.t0 + m.xyz.length / 9)   // only surfaces that are drawn can hide one
  const nameOk = models.filter((m) => [...m.own].some((id) => id.slice(0, 10) === m.name.slice(1, 11))).length
  const fallback = blds.filter((b) => !coveredIds.has(b.id))
  meshInfo = { tiles, models_loaded: all.length, models_selected: models.length, models_unselected_overlapping: all.filter((m) => !m.sel && m.own.size).length,
    models_without_roof: all.filter((m) => !m.roof.size).length, name_prefix_agrees: nameOk, footprints: onSheet.length, footprints_covered: coveredIds.size,
    fallback_csdi_blocks: fallback.length, not_drawn_no_model_no_heights: onSheet.filter((b) => !coveredIds.has(b.id) && !blds.some((x) => x.id === b.id)).length, dz: DZ, chart_deg: +opt('--chart-deg', 20) }
  meshInfo.uncovered = fp.buildings.flatMap((b, k) => (b.sheet !== false && !coveredIds.has(b.id) ? [{ id: b.id, type: b.type, top: b.top, cells: cells[k], cov: +(cov[k] / cells[k]).toFixed(2),
    any: +(Math.max(0, ...all.map((m) => [...m.roof].filter((c) => cellOf.get(c)?.includes(k)).length)) / cells[k]).toFixed(2) }] : []))
  console.error('3DSD', JSON.stringify(meshInfo))
  blds = fallback
  if (!ALL) { const [x0, y0] = E.ll(...opt('--near', '114.1694,22.3193').split(',').map(Number)); const d = (b) => Math.hypot((b.bb[0] + b.bb[2]) / 2 - x0, (b.bb[1] + b.bb[3]) / 2 - y0)
    models = models.sort((p, q) => d(p) - d(q)).slice(0, N) }
}

async function bakeMesh(m) {
  const t0 = Date.now(), X = m.xyz, nt = X.length / 9, tris = await candidates(m); if (!tris.tex.length) return null
  const PX = tris.xyz, nrm = new Float64Array(3 * nt), area = new Float64Array(nt), ZUV = new Float32Array(6 * nt)
  for (let t = 0; t < nt; t++) { const o = 9 * t, e1 = [X[o + 3] - X[o], X[o + 4] - X[o + 1], X[o + 5] - X[o + 2]], e2 = [X[o + 6] - X[o], X[o + 7] - X[o + 1], X[o + 8] - X[o + 2]]
    const n = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]], l = Math.hypot(...n); area[t] = l / 2; if (l) nrm.set(n.map((c) => c / l), 3 * t) }
  // weld (1 cm) and edge adjacency
  const vid = new Map(), tv = new Int32Array(3 * nt), edges = new Map()
  for (let t = 0; t < nt; t++) for (let k = 0; k < 3; k++) { const o = 9 * t + 3 * k, key = `${Math.round(X[o] * 100)},${Math.round(X[o + 1] * 100)},${Math.round(X[o + 2] * 100)}`
    let i = vid.get(key); if (i === undefined) vid.set(key, i = vid.size); tv[3 * t + k] = i }
  const ek = (a, b) => (a < b ? a * 4194304 + b : b * 4194304 + a)
  for (let t = 0; t < nt; t++) for (let k = 0; k < 3; k++) { const e = ek(tv[3 * t + k], tv[3 * t + (k + 1) % 3]); (edges.get(e) || edges.set(e, []).get(e)).push(t) }
  // twins: 3DSD stores many faces twice, the same three vertices with opposite winding (a back-face copy); both get baked,
  // the copy with more photomesh hits is kept and the other dropped (an outward face sees outward-facing photomesh)
  const twin = new Int32Array(nt).fill(-1), tkey = new Map()
  for (let t = 0; t < nt; t++) { const k = [tv[3 * t], tv[3 * t + 1], tv[3 * t + 2]].sort((p, q) => p - q).join(','), u = tkey.get(k); if (u === undefined) tkey.set(k, t); else if (twin[u] < 0) { twin[u] = t; twin[t] = u } }
  // charts: region growing from the largest unassigned triangle
  const chartOf = new Int32Array(nt).fill(-1), charts = [], ok = (t) => area[t] > 1e-6, dot = (t, N) => nrm[3 * t] * N[0] + nrm[3 * t + 1] * N[1] + nrm[3 * t + 2] * N[2]
  for (const s0 of [...Array(nt).keys()].filter(ok).sort((p, q) => area[q] - area[p])) { if (chartOf[s0] >= 0) continue
    const c = charts.length, list = [s0], N0 = [...nrm.subarray(3 * s0, 3 * s0 + 3)]; chartOf[s0] = c
    for (let qi = 0; qi < list.length; qi++) { const t = list[qi]
      for (let k = 0; k < 3; k++) for (const u of edges.get(ek(tv[3 * t + k], tv[3 * t + (k + 1) % 3]))) if (chartOf[u] < 0 && ok(u) && dot(u, N0) >= CHART) { chartOf[u] = c; list.push(u) } }
    const N = [0, 1, 2].map((i) => list.reduce((x, t) => x + nrm[3 * t + i] * area[t], 0)), l = Math.hypot(...N); for (let i = 0; i < 3; i++) N[i] /= l
    const up = Math.abs(N[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0], U = [up[1] * N[2] - up[2] * N[1], up[2] * N[0] - up[0] * N[2], up[0] * N[1] - up[1] * N[0]], ul = Math.hypot(...U); for (let i = 0; i < 3; i++) U[i] /= ul
    const V = [N[1] * U[2] - N[2] * U[1], N[2] * U[0] - N[0] * U[2], N[0] * U[1] - N[1] * U[0]], O = [X[9 * s0], X[9 * s0 + 1], X[9 * s0 + 2]]
    const uvd = (x, y, z) => { const p = [x - O[0], y - O[1], z - O[2]]; return [p[0] * U[0] + p[1] * U[1] + p[2] * U[2], p[0] * V[0] + p[1] * V[1] + p[2] * V[2], p[0] * N[0] + p[1] * N[1] + p[2] * N[2]] }
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity, xb = [Infinity, Infinity, -Infinity, -Infinity], ar = 0
    for (const t of list) { ar += area[t]; for (let k = 0; k < 3; k++) { const o = 9 * t + 3 * k, [u, v] = uvd(X[o], X[o + 1], X[o + 2]); u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v)
      xb = [Math.min(xb[0], X[o]), Math.min(xb[1], X[o + 1]), Math.max(xb[2], X[o]), Math.max(xb[3], X[o + 1])] } }
    charts.push({ list, N, U, V, O, uvd, u0, u1, v0, v1, xb, area: ar }) }
  // texel size: TEXEL, coarser until the shelf-packed atlas fits MAXPX
  let s = TEXEL; const dims = () => { for (const c of charts) c.im = { w: Math.max(1, Math.ceil((c.u1 - c.u0) / s - 1e-6)), h: Math.max(1, Math.ceil((c.v1 - c.v0) / s - 1e-6)) } }
  for (dims(); pack(charts, true).ah > MAXPX; dims()) s *= 1.1
  const st = { wallTexels: 0, wallMiss: 0, partyTexels: 0, partyMiss: 0, roofTexels: 0, roofMiss: 0, downTexels: 0, downMiss: 0, hiddenTexels: 0, hiddenCharts: 0 }
  let topCells = null
  const topNear = (x, y) => { if (!topCells) { topCells = new Map(); for (let o = 0; o < PX.length; o += 3) { const k = cellKey(Math.floor(PX[o] / COV), Math.floor(PX[o + 1] / COV)); topCells.set(k, Math.max(topCells.get(k) ?? -Infinity, PX[o + 2])) } }
    let z = -Infinity; for (let i = -3; i <= 3; i++) for (let j = -3; j <= 3; j++) z = Math.max(z, topCells.get(cellKey(Math.floor(x / COV) + i, Math.floor(y / COV) + j)) ?? -Infinity); return z }
  let rs = 0, gs = 0, bs = 0, ns = 0
  for (const [c, ch] of charts.entries()) {
    const { N, uvd, u0, v1, im } = ch, W = im.w * SS, H = im.h * SS, sp = s / SS
    const projOn = (XX, filt) => (t) => { const o = 9 * t; if (filt && !filt(t)) return null
      return [0, 1, 2].map((k) => { const [u, v, d] = uvd(XX[o + 3 * k], XX[o + 3 * k + 1], XX[o + 3 * k + 2]); return [(u - u0) / sp, (v1 - v) / sp, d] }) }
    const own = raster(W, H, { uv: ZUV }, ch.list, projOn(X), (d) => -d), mask = own.hitT.map((t) => t >= 0), surf = own.dep   // outermost own surface per sample
    const facing = (fac) => (t) => { const o = 9 * t, e1 = [PX[o + 3] - PX[o], PX[o + 4] - PX[o + 1], PX[o + 5] - PX[o + 2]], e2 = [PX[o + 6] - PX[o], PX[o + 7] - PX[o + 1], PX[o + 8] - PX[o + 2]]
      const n = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]], l = Math.hypot(...n); return l && (n[0] * N[0] + n[1] * N[1] + n[2] * N[2]) / l >= fac }
    const near = (r) => tris.query(ch.xb[0] - r, ch.xb[1] - r, ch.xb[2] + r, ch.xb[3] + r), within = (r) => (d, k) => (mask[k] && Math.abs(d - surf[k]) <= r ? Math.abs(d - surf[k]) : null)
    const ids = near(SEARCH), R = raster(W, H, tris, ids, projOn(PX, facing(FACING)), within(SEARCH))
    // hidden: another 3DSD surface just outside
    const mine = (g) => g >= m.t0 && g < m.t0 + nt && (chartOf[g - m.t0] === c || (twin[g - m.t0] >= 0 && chartOf[twin[g - m.t0]] === c))   // itself and its twins
    const oth = queryAll(ch.xb[0] - 0.5, ch.xb[1] - 0.5, ch.xb[2] + 0.5, ch.xb[3] + 0.5).filter((g) => DRAWN[g] && !mine(g))
    const opp = (g) => { const o = 9 * g, e1 = [ALLX[o + 3] - ALLX[o], ALLX[o + 4] - ALLX[o + 1], ALLX[o + 5] - ALLX[o + 2]], e2 = [ALLX[o + 6] - ALLX[o], ALLX[o + 7] - ALLX[o + 1], ALLX[o + 8] - ALLX[o + 2]]
      const n = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]], l = Math.hypot(...n); return l && (n[0] * N[0] + n[1] * N[1] + n[2] * N[2]) / l < -0.5 }
    const H1 = raster(W, H, { uv: ZALL }, oth, projOn(ALLX, opp), (d, k) => (mask[k] && d - surf[k] > -0.05 && d - surf[k] <= HIDE ? 0 : null))
    const H2 = raster(W, H, { uv: ZALL }, oth, projOn(ALLX, (g) => !opp(g)), (d, k) => (mask[k] && d - surf[k] > 0.05 && d - surf[k] <= HIDE ? 0 : null))
    const kind = N[2] >= 0.5 ? 'roof' : N[2] <= -0.5 ? 'down' : 'wall', w = im.w, h = im.h
    let party = false
    if (kind === 'wall') { const big = [...ch.list].sort((p, q) => area[q] - area[p]).slice(0, 3)
      party = big.filter((t) => { const o = 9 * t; return inOther((X[o] + X[o + 3] + X[o + 6]) / 3 + 0.8 * N[0], (X[o + 1] + X[o + 4] + X[o + 7]) / 3 + 0.8 * N[1], m.own) }).length >= Math.ceil(big.length * 2 / 3) }
    const texel = [], cs = ch.st = { wallTexels: 0, wallMiss: 0, partyTexels: 0, partyMiss: 0, roofTexels: 0, roofMiss: 0, downTexels: 0, downMiss: 0, hiddenTexels: 0, hiddenCharts: 0 }; let inside = 0, hid = 0
    for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) { let mk = 0, hd = 0, ks = -1
      for (let dy = 0; dy < SS; dy++) for (let dx = 0; dx < SS; dx++) { const k = (j * SS + dy) * W + i * SS + dx; if (!mask[k]) continue; mk++; ks = k; if (H1.hitT[k] >= 0 || H2.hitT[k] >= 0) hd++
        if (R.hitT[k] >= 0) { const b = Math.max(0, Math.min(80, Math.floor((R.dep[k] - surf[k] + 10) / 0.25))); if (Math.abs(N[2]) < 0.1) OFFW[b]++; else if (N[2] > 0.95) OFFR[b]++ } }
      if (!mk) continue; inside++; const hidden = hd * 2 >= mk; hid += hidden; texel.push([i, j, ks, hidden]) }
    const chartHidden = inside > 0 && hid >= 0.8 * inside
    ch.im = await shade(W, H, tris, R, (i, j) => { for (let dy = 0; dy < SS; dy++) for (let dx = 0; dx < SS; dx++) if (mask[(j * SS + dy) * W + i * SS + dx]) return true; return false })
    ch.kind = kind; if (chartHidden) { cs.hiddenCharts++; cs.hiddenTexels += inside; ch.hidden = true; continue }
    let Rfar = null, Rany = null
    if (DIAG) { Rfar = raster(W, H, tris, near(DR), projOn(PX, facing(FACING)), within(DR)); Rany = raster(W, H, tris, ids, projOn(PX, facing(-2)), within(SEARCH)) }
    for (const [i, j, ks, hidden] of texel) { if (hidden) { cs.hiddenTexels++; continue }
      const u = u0 + (i + 0.5) * s, v = v1 - (j + 0.5) * s, d = surf[ks] + 1, x = ch.O[0] + u * ch.U[0] + v * ch.V[0] + d * N[0], y = ch.O[1] + u * ch.U[1] + v * ch.V[1] + d * N[1], z = ch.O[2] + u * ch.U[2] + v * ch.V[2] + (d - 1) * N[2]
      const hit = anyHit(R, W, i, j), key = 'm.' + (kind === 'wall' ? `${party ? 'party' : 'street'}.${z - m.base < 10 ? 'low' : 'high'}` : kind)   // m. = 3DSD models
      cs[kind + 'Texels']++; if (!hit) cs[kind + 'Miss']++; if (party) { cs.partyTexels++; if (!hit) cs.partyMiss++ }
      if (hit) { const k = 4 * (j * w + i); rs += ch.im.px[k]; gs += ch.im.px[k + 1]; bs += ch.im.px[k + 2]; ns++ }
      if (!DIAG) continue
      add(cs, `${key}.all`); if (hit) continue
      const f = minBest(Rfar, W, i, j)
      add(cs, `${key}.${!meshNear(x, y) ? 'no_mesh_here' : f <= 8 ? 'far_<8' : f <= DR ? 'far_<15' : anyHit(Rany, W, i, j) ? 'wrong_facing' : z > topNear(x, y) + 1 ? 'above_mesh_top' : 'nothing_15m'}`) } }
  const mean = ns ? [rs / ns, gs / ns, bs / ns, 255] : [128, 128, 128, 255]
  for (const ch of charts) if (ch.hidden) ch.im = { w: 1, h: 1, px: Uint8Array.from(mean) }
  const frac = (ch) => (ch.hidden || !ch.st[ch.kind + 'Texels'] ? 0 : 1 - ch.st[ch.kind + 'Miss'] / ch.st[ch.kind + 'Texels']), drop = new Uint8Array(nt)
  for (let t = 0; t < nt; t++) { const u = twin[t]; if (u < 0 || chartOf[t] < 0 || chartOf[u] < 0) continue
    const ft = frac(charts[chartOf[t]]), fu = frac(charts[chartOf[u]]); if (ft < fu || (ft === fu && t > u)) drop[t] = 1 }
  const kept = charts.filter((ch) => ch.list.some((t) => !drop[t]))
  for (const ch of kept) for (const [k, v] of Object.entries(ch.st)) if (k === 'd') for (const [q, n] of Object.entries(v)) add(st, q, n); else st[k] += v
  const { rects, aw, ah, atlas } = pack(kept)
  // geometry: each chart's triangles, vertices welded within the chart, UVs = chart-plane coordinates into the atlas
  const pos = [], uv = [], idx = []
  for (const r of rects) { const ch = r.f, local = new Map()
    for (const t of ch.list) for (let k = 0; k < 3; k++) { if (drop[t]) break; const g = tv[3 * t + k]; let i = local.get(g)
      if (i === undefined) { const o = 9 * t + 3 * k, [u, v] = ch.uvd(X[o], X[o + 1], X[o + 2]); local.set(g, i = pos.length / 3); pos.push(X[o], X[o + 1], X[o + 2])
        uv.push(ch.hidden ? (r.x + P + 0.5) / aw : (r.x + P + (u - ch.u0) / s) / aw, ch.hidden ? (r.y + P + 0.5) / ah : (r.y + P + (ch.v1 - v) / s) / ah) }
      idx.push(i) } }
  const kt = await encodeKtx2({ w: aw, h: ah, px: atlas }, 1, 'atlas')
  const doc = new Document(), buf = doc.createBuffer(), unlit = doc.createExtension(KHRMaterialsUnlit); doc.createExtension(KHRTextureBasisu).setRequired(true)
  const tex = doc.createTexture(m.id).setImage(new Uint8Array(kt)).setMimeType('image/ktx2')
  const mat = doc.createMaterial(m.id).setBaseColorTexture(tex).setDoubleSided(true).setExtension('KHR_materials_unlit', unlit.createUnlit())
  const prim = doc.createPrimitive().setMaterial(mat).setIndices(doc.createAccessor().setType('SCALAR').setArray(Uint32Array.from(idx)).setBuffer(buf))
    .setAttribute('POSITION', doc.createAccessor().setType('VEC3').setArray(Float32Array.from(pos)).setBuffer(buf))
    .setAttribute('TEXCOORD_0', doc.createAccessor().setType('VEC2').setArray(Float32Array.from(uv)).setBuffer(buf))
  doc.createScene().addChild(doc.createNode(m.id).setMesh(doc.createMesh().addPrimitive(prim)))
  const raw = (await io.writeBinary(doc)).length; doc.setLogger(QUIET); await doc.transform(meshopt({ encoder: MeshoptEncoder, level: 'medium' }))
  const glb = Buffer.from(await io.writeBinary(doc)); fs.writeFileSync(path.join(outDir, `${m.id}.glb`), glb)
  if (PNG) execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', `${aw}x${ah}`, '-i', '-', path.join(outDir, 'atlas', `${m.id}.png`)], { input: Buffer.from(atlas) })
  return { mesh: true, id: m.id, name: m.name, csdi: [...m.own], texel_m: +s.toFixed(3), atlas: [aw, ah], bytes: glb.length, ktx2: kt.length, geomRaw: raw - kt.length, geom: glb.length - kt.length,
    tris: idx.length / 3, trisDropped: nt - idx.length / 3 - drop.reduce((x, y) => x + y, 0), twinsDropped: drop.reduce((x, y) => x + y, 0), charts: kept.length, chartsDropped: charts.length - kept.length, tinyCharts: kept.filter((c) => c.area < 0.25).length, chartTexels: kept.reduce((x, c) => x + c.im.w * c.im.h, 0), atlasTexels: aw * ah,
    tris_candidate: tris.tex.length, ...st, ms: Date.now() - t0,
    box: [(m.bb[0] + m.bb[2]) / 2, (m.bb[1] + m.bb[3]) / 2, (m.base + m.top) / 2, (m.bb[2] - m.bb[0]) / 2 + 0.1, 0, 0, 0, (m.bb[3] - m.bb[1]) / 2 + 0.1, 0, 0, 0, (m.top - m.base) / 2 + 0.1] }
}

fs.rmSync(outDir, { recursive: true, force: true }); fs.mkdirSync(path.join(outDir, 'atlas'), { recursive: true })
// spatial order (150 m cells), so the tile caches are reused building to building
const jobs = [...blds.map((b) => ({ b, run: bake })), ...models.map((m) => ({ b: m, run: bakeMesh }))]
jobs.sort((p, q) => { const k = (b) => [Math.floor(b.bb[0] / 150), Math.floor(b.bb[1] / 150)]; const [a1, b1] = k(p.b), [a2, b2] = k(q.b); return a1 - a2 || b1 - b2 })
const res = [], t0 = Date.now(); let outsideMesh = 0, peakRss = 0
for (const [k, { b, run }] of jobs.entries()) {
  try { const r = await run(b); if (r) res.push(r); else outsideMesh++ } catch (e) { console.error(`FAILED ${b.id}: ${e.stack}`) }
  peakRss = Math.max(peakRss, process.memoryUsage().rss)
  if (k % 25 === 0) console.error(`  ${k}/${jobs.length} (+${((Date.now() - t0) / 1000).toFixed(0)} s, rss ${(process.memoryUsage().rss / 1e9).toFixed(1)} GB)`)
}
const lo = [0, 1, 2].map((i) => Math.min(...res.map((r) => r.box[i] - r.box[3 + 4 * i]))), hi = [0, 1, 2].map((i) => Math.max(...res.map((r) => r.box[i] + r.box[3 + 4 * i])))
fs.writeFileSync(path.join(outDir, 'tileset.json'), JSON.stringify({ asset: { version: '1.1', gltfUpAxis: 'Z', extras: { source: 'Baked from Lands D Open3Dhk photomesh onto CSDI building blocks (github.com/milleleaf/hk-building-textures bake.mjs)', origin: ORIGIN } },
  geometricError: 400, root: { transform: E.toEcef, boundingVolume: { box: [...lo.map((v, i) => (v + hi[i]) / 2), (hi[0] - lo[0]) / 2, 0, 0, 0, (hi[1] - lo[1]) / 2, 0, 0, 0, (hi[2] - lo[2]) / 2] },
    geometricError: 100, refine: 'ADD', children: res.map((r) => ({ boundingVolume: { box: r.box }, geometricError: 0, content: { uri: r.uri || `${r.id}.glb` } })) } }))
const blocks = MESH ? res.filter((r) => !r.mesh) : res, sum = (k, rs = blocks) => rs.reduce((x, r) => x + (r[k] || 0), 0)
const D = {}; for (const r of res) for (const [k, v] of Object.entries(r.d || {})) D[k] = (D[k] || 0) + v
if (DIAG) { fs.writeFileSync(path.join(outDir, 'diag.json'), JSON.stringify(D, null, 1)); console.log(JSON.stringify(D)) }
const rep = { buildings: res.length, outside_mesh: outsideMesh, failed: jobs.length - res.length - outsideMesh, skipped_no_height_or_low: skipped, texel_m: TEXEL, search_m: SEARCH, seconds: Math.round((Date.now() - t0) / 1000),
  MB: +(sum('bytes') / 1e6).toFixed(2), ms_per_building_median: res.map((r) => r.ms).sort((p, q) => p - q)[res.length >> 1], kB_per_building_median: Math.round(res.map((r) => r.bytes).sort((p, q) => p - q)[res.length >> 1] / 1e3),
  wall_nohit: +(sum('wallMiss') / sum('wallTexels')).toFixed(3), party_wall_share: +(sum('partyTexels') / sum('wallTexels')).toFixed(3), party_nohit: +(sum('partyMiss') / sum('partyTexels')).toFixed(3), street_wall_nohit: +((sum('wallMiss') - sum('partyMiss')) / (sum('wallTexels') - sum('partyTexels'))).toFixed(3), roof_nohit: +(sum('roofMiss') / sum('roofTexels')).toFixed(3), texels_M: +((sum('wallTexels') + sum('roofTexels')) / 1e6).toFixed(1), coarser_texel: res.filter((r) => r.texel_m > TEXEL).length }
rep.leaves = leaves.length; rep.leaves_missing = leavesMissing; rep.peak_rss_GB = +(peakRss / 1e9).toFixed(2)
if (MESH) { const ms = res.filter((r) => r.mesh), med = (k) => ms.map((r) => r[k]).sort((p, q) => p - q)[ms.length >> 1], pct = (h, q) => { const n = h.reduce((x, y) => x + y, 0); let c = 0; for (let i = 0; i < h.length; i++) if ((c += h[i]) >= q * n) return -10 + (i + 0.5) * 0.25 }
  Object.assign(rep, { mesh: { ...meshInfo, baked: ms.length, outside_mesh: models.length - ms.length, MB: +(sum('bytes', ms) / 1e6).toFixed(2), MB_geometry_raw: +(sum('geomRaw', ms) / 1e6).toFixed(2), MB_geometry_meshopt: +(sum('geom', ms) / 1e6).toFixed(2), MB_texture: +(sum('ktx2', ms) / 1e6).toFixed(2),
    tris: sum('tris', ms), tris_degenerate_dropped: sum('trisDropped', ms), twin_tris_dropped: sum('twinsDropped', ms), charts: sum('charts', ms), charts_twin_dropped: sum('chartsDropped', ms), charts_tiny_lt_0_25m2: sum('tinyCharts', ms), charts_hidden: sum('hiddenCharts', ms), atlas_fill: +(sum('chartTexels', ms) / sum('atlasTexels', ms)).toFixed(3),
    kB_per_model_median: Math.round(med('bytes') / 1e3), ms_per_model_median: med('ms'), coarser_texel: ms.filter((r) => r.texel_m > TEXEL).length,
    texels_M: +((sum('wallTexels', ms) + sum('roofTexels', ms) + sum('downTexels', ms)) / 1e6).toFixed(1), hidden_texels_M: +(sum('hiddenTexels', ms) / 1e6).toFixed(2),
    wall_nohit: +(sum('wallMiss', ms) / sum('wallTexels', ms)).toFixed(3), party_wall_share: +(sum('partyTexels', ms) / sum('wallTexels', ms)).toFixed(3), party_nohit: +(sum('partyMiss', ms) / sum('partyTexels', ms)).toFixed(3),
    street_wall_nohit: +((sum('wallMiss', ms) - sum('partyMiss', ms)) / (sum('wallTexels', ms) - sum('partyTexels', ms))).toFixed(3), roof_nohit: +(sum('roofMiss', ms) / sum('roofTexels', ms)).toFixed(3), down_nohit: +(sum('downMiss', ms) / sum('downTexels', ms)).toFixed(3),
    offset_wall_m_p10_p50_p90: [0.1, 0.5, 0.9].map((q) => pct(OFFW, q)), offset_roof_m_p10_p50_p90: [0.1, 0.5, 0.9].map((q) => pct(OFFR, q)) } }) }
fs.writeFileSync(path.join(outDir, 'bake-report.json'), JSON.stringify({ ...rep, buildings_detail: res }, null, 1)); console.log(JSON.stringify(rep))
