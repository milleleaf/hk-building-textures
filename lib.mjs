// (from the hk-ifcx spike of 2026-10-23) shared helpers for indiv.mjs and bake.mjs. Open3Dhk sheet tiles -> a local ENU frame
// (east, north, up; metres; up = ellipsoidal height, which hk-ifcx takes as mPD), images in and out.
// Images: decoded with ffmpeg (JPEG/PNG) or KTX-Software's `ktx extract` (KTX2), encoded with toktx (ETC1S, mips), as hk-ifcx lean_tiles.mjs.
import fs from 'node:fs'; import path from 'node:path'; import os from 'node:os'; import { execFile } from 'node:child_process'; import { promisify } from 'node:util'
import * as C from 'cesium'
import { NodeIO, Logger, ImageUtils } from '@gltf-transform/core'; import { ALL_EXTENSIONS } from '@gltf-transform/extensions'
import { MeshoptEncoder, MeshoptDecoder } from 'meshoptimizer'
import draco3d from 'draco3dgltf'
const run = promisify(execFile)
export const KTX_BIN = process.env.KTX_BIN || path.join(os.homedir(), '.local/opt/ktx-4.4.2/bin')
export const ETC1S = ['--encode', 'etc1s', '--clevel', '1', '--qlevel', '128']          // hk-ifcx lean_tiles.mjs settings
export const UASTC = ['--encode', 'uastc', '--uastc_quality', '2', '--uastc_rdo_l', '1.0', '--zcmp', '18']   // RDO 1.0: image-format test 2026-10-23

await MeshoptEncoder.ready; await MeshoptDecoder.ready
export const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'draco3d.decoder': await draco3d.createDecoderModule(), 'meshopt.encoder': MeshoptEncoder, 'meshopt.decoder': MeshoptDecoder })
export const QUIET = new Logger(Logger.Verbosity.ERROR); io.setLogger(QUIET)
export const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pm-spike-'))
process.on('exit', () => fs.rmSync(tmp, { recursive: true, force: true }))

/** ENU frame at lon/lat (h = 0): toEnu(ECEF matrix) and the 16-array ENU -> ECEF for a tileset root transform. */
export function enuFrame(lon, lat) {
  const F = C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(lon, lat, 0)), Finv = C.Matrix4.inverse(F, new C.Matrix4())
  const ll = (lo, la, h = 0) => { const v = C.Matrix4.multiplyByPoint(Finv, C.Cartesian3.fromDegrees(lo, la, h), new C.Cartesian3()); return [v.x, v.y, v.z] }
  return { F, Finv, toEcef: C.Matrix4.toArray(F), ll }
}
export const mul = (A, B) => C.Matrix4.multiply(A, B, new C.Matrix4())
export const M4 = (a) => C.Matrix4.fromArray(a)
export { C }

/** b3dm -> { head (feature + batch tables, kept byte-exact), doc, rtc } */
export async function readB3dm(file) {
  const b = fs.readFileSync(file); if (b.length < 28) return null                       // Lands D ships some 0-byte b3dm (DATA-ISSUES DI-10)
  const ftj = b.readUInt32LE(12), ftb = b.readUInt32LE(16), btj = b.readUInt32LE(20), btb = b.readUInt32LE(24), h = 28 + ftj + ftb + btj + btb
  const ft = ftj ? JSON.parse(b.toString('utf8', 28, 28 + ftj)) : {}
  const doc = await io.readBinary(new Uint8Array(b.subarray(h))); doc.setLogger(QUIET)
  return { bytes: b.length, head: Buffer.from(b.subarray(0, h)), doc, rtc: ft.RTC_CENTER }
}
export async function writeB3dm(file, head, doc) {
  const glb = Buffer.from(await io.writeBinary(doc)), out = Buffer.concat([head, glb, Buffer.alloc((8 - ((head.length + glb.length) % 8)) % 8, 0x20)])
  out.writeUInt32LE(out.length, 8); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, out); return out.length
}

/** Every tile of every tileset.json under dir: { node, file, M = tile frame -> ECEF } (the sheets say gltfUpAxis Z: glTF positions are already Z-up). */
export function listTilesets(dir) {
  const out = []; (function walk(d) { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name)
    if (e.isDirectory()) walk(p); else if (e.name === 'tileset.json') out.push(p) } })(dir)
  return out.sort()
}
export const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8').replace(/^﻿/, ''))
export function tilesOf(tsFile) {
  const ts = readJson(tsFile), res = []
  const walk = (n, M) => { const T = n.transform ? mul(M, M4(n.transform)) : M; const uri = n.content?.uri || n.content?.url
    if (uri && !uri.endsWith('.json')) res.push({ node: n, file: path.join(path.dirname(tsFile), uri), M: T })
    for (const c of n.children || []) walk(c, T) }
  walk(ts.root, C.Matrix4.IDENTITY); return { ts, tiles: res }
}

/** Decode an image (glTF bytes + mime) to RGBA8. */
export async function decodeImage(bytes, mime, tag = 'img') {
  const [w, h] = ImageUtils.getSize(bytes, mime) || []
  let src = path.join(tmp, `${tag}-${process.hrtime.bigint()}`)
  if (mime === 'image/ktx2') { fs.writeFileSync(src + '.ktx2', bytes); await run(path.join(KTX_BIN, 'ktx'), ['extract', '--transcode', 'rgba8', src + '.ktx2', src + '.png']); fs.rmSync(src + '.ktx2'); src += '.png' }
  else { const e = mime === 'image/png' ? '.png' : '.jpg'; fs.writeFileSync(src + e, bytes); src += e }
  const { stdout } = await run('ffmpeg', ['-v', 'error', '-i', src, '-f', 'rawvideo', '-pix_fmt', 'rgba', '-'], { encoding: 'buffer', maxBuffer: 1 << 30 })
  fs.rmSync(src); if (stdout.length !== w * h * 4) throw new Error(`decode ${mime}: ${stdout.length} bytes for ${w}x${h}`)
  return { w, h, px: stdout }
}
/** RGBA8 -> KTX2 ETC1S with mips (toktx; --scale s < 1 downsizes as it reads), returns bytes. */
export async function encodeKtx2({ w, h, px }, scale = 1, tag = 'enc', enc = ETC1S) {
  const f = path.join(tmp, `${tag}-${process.hrtime.bigint()}`), rgb = Buffer.alloc(w * h * 3)
  for (let i = 0, j = 0; i < w * h * 4; i += 4, j += 3) { rgb[j] = px[i]; rgb[j + 1] = px[i + 1]; rgb[j + 2] = px[i + 2] }
  fs.writeFileSync(f + '.ppm', Buffer.concat([Buffer.from(`P6\n${w} ${h}\n255\n`), rgb]))
  const s = Math.min(1, Math.max(scale, 1 / Math.min(w, h)))                             // never below 1 px (Lands D ships 38x1 images)
  const sc = s !== 1 ? ['--scale', String(s)] : []
  await run(path.join(KTX_BIN, 'toktx'), ['--t2', ...enc, ...sc, '--genmipmap', '--assign_oetf', 'srgb', '--threads', '2', f + '.ktx2', f + '.ppm'])
  const out = fs.readFileSync(f + '.ktx2'); fs.rmSync(f + '.ppm'); fs.rmSync(f + '.ktx2'); return out
}
/** Run fn over items with n workers. */
export async function pool(items, n, fn) { let next = 0
  await Promise.all(Array.from({ length: n }, async () => { for (let k; (k = next++) < items.length;) await fn(items[k], k) })) }
