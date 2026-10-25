"""Fetch only the members a sheet needs from a Lands D 3D sheet zip, by HTTP Range (download.map.gov.hk sends Accept-Ranges):
read the zip's central directory over ranged reads, pick members, and fetch them in coalesced spans (neighbouring members in one
request, up to SPAN bytes), never the whole zip. At most 4 requests at a time (Lands D terms: 100 concurrent users).
  --prefix P   (GLTF, Individualised models) every member under P, e.g. BUILDING/
  --near F     (CESIUM, Open3Dhk photomesh) every tileset.json, plus the leaf tiles (geometricError 0 or no children: what bake.mjs
               reads) whose bounding sphere comes within --pad m (xy) of a building in footprints file F
Output: <out dir>/<sheet>/<member path>; with --near the tileset.json files are complete (they list tiles that were not fetched;
bake.mjs skips a tile whose file is missing). Prints and writes <out dir>/zipcut-<kind>.json (per sheet: zip size, members, bytes).
usage: python3 -I zipcut.py GLTF <out dir> <sheet>... --prefix BUILDING/
       python3 -I zipcut.py CESIUM <out dir> <sheet>... --near <footprints.json> [--pad 25]
(from the hk-ifcx spike of 2026-10-23; coalesced spans and --prefix added here)"""
import io, json, math, os, struct, sys, time, zipfile, zlib, urllib.request, concurrent.futures as cf

KEY = os.environ.get('LANDSD_KEY') or 'ad5940a63bd344c48b0351ef1c7a905e'   # our own key (repo secret) if set, else Lands D's public key from the CSDI dataset records
UA = {'User-Agent': 'milleleaf-hk-building-textures'}
# Fetch settings (environment, recorded in the report): requests at once, MB per Range request (members bridged across gaps
# under 1 MB), seconds of pause after each request per thread; STOP_ON_403=1 gives up at the first 403 and leaves a stop marker
# on the bucket (stop/landsd-403.json) so no job starts another sheet (the bake workflow checks it).
THREADS = int(os.environ.get('ZIP_THREADS', 4)); SPAN = int(float(os.environ.get('ZIP_SPAN_MB', 32)) * (1 << 20)); GAP, TRIES = 1 << 20, 16
PAUSE = float(os.environ.get('ZIP_PAUSE_S', 0)); STOP = os.environ.get('STOP_ON_403') == '1'
fetched = {'requests': 0, 'bytes': 0, 'http403': 0, 'first_403_utc': None, 'MB_before_403': None}
SETTINGS = {'threads': THREADS, 'span_MB': SPAN / (1 << 20), 'pause_s': PAUSE, 'stop_on_403': STOP, 'key': 'own' if os.environ.get('LANDSD_KEY') else 'public'}

class Blocked(Exception): pass

# Download budget (BUDGET_MB, counted in BUDGET_FILE across every Lands D fetch of a job): no new request once it is spent; the
# process then exits 75 and leaves what it fetched on disk, so a later job continues from there.
BUDGET = float(os.environ.get('BUDGET_MB') or 0) * 1e6; BUDGET_FILE = os.environ.get('BUDGET_FILE')
_spent0 = (json.load(open(BUDGET_FILE)).get('bytes', 0) if BUDGET_FILE and os.path.exists(BUDGET_FILE) else 0); _spent = [0]
import threading; _block = threading.Lock()
class OverBudget(Exception): pass
def budget_check():
    if BUDGET and _spent0 + _spent[0] >= BUDGET: raise OverBudget(f'download budget of {BUDGET / 1e6:.0f} MB spent')
def budget_add(n):
    with _block:
        _spent[0] += n
        if BUDGET_FILE: json.dump({'bytes': _spent0 + _spent[0]}, open(BUDGET_FILE, 'w'))

def saw403(url):
    fetched['http403'] += 1
    if fetched['first_403_utc'] is None:
        fetched['first_403_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()); fetched['MB_before_403'] = round(fetched['bytes'] / 1e6, 1)
    if STOP:
        try:
            from huggingface_hub import batch_bucket_files
            batch_bucket_files('milleleaf/hk-building-textures', add=[(json.dumps({'url': url.split('?')[0], **fetched, 'settings': SETTINGS,
                               'job': os.environ.get('GITHUB_JOB'), 'run': os.environ.get('GITHUB_RUN_ID')}).encode(), 'stop/landsd-403.json')])
        except Exception as e: print(f'  could not write the stop marker: {e!r}', file=sys.stderr)
        raise Blocked(f'403 from Lands D at {fetched["first_403_utc"]} after {fetched["MB_before_403"]} MB: stopping (STOP_ON_403=1)')

def rng(url, start, end):   # inclusive
    req = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}', **UA})
    for i in range(TRIES):
        budget_check()
        try:
            r = urllib.request.urlopen(req, timeout=300)
            if r.status != 206: raise RuntimeError(f'no range support: HTTP {r.status}')
            b = r.read()
            if len(b) != end - start + 1: raise IOError(f'short read {len(b)} of {end - start + 1}')
            fetched['requests'] += 1; fetched['bytes'] += len(b); budget_add(len(b))
            if PAUSE: time.sleep(PAUSE)
            return b
        except (RuntimeError, Blocked, OverBudget): raise
        except Exception as e:
            if getattr(e, 'code', None) == 403: saw403(url)
            if i == TRIES - 1: raise
            backoff(i, e)

def backoff(i, e):
    """Lands D answers 403 to a runner for 20 min or more at times (2026-10-23, ~40 connections from 10 jobs): wait 10 s, 30 s,
    90 s, 270 s, then 5 min a time, about an hour in all, before giving up"""
    t = min(300, 10 * 3 ** i); why = repr(e)
    if i == 0 and hasattr(e, 'headers'):                                  # once: what answered (WAF, CDN, origin)
        try: why += f" server={e.headers.get('Server')} via={e.headers.get('Via')} body={e.read(300)!r}"
        except Exception: pass
    print(f'  retry in {t} s after {why}', file=sys.stderr, flush=True); time.sleep(t)

def head(url):
    for i in range(TRIES):
        budget_check()
        try: return int(urllib.request.urlopen(urllib.request.Request(url, method='HEAD', headers=UA), timeout=60).headers['Content-Length'])
        except (Blocked, OverBudget): raise
        except Exception as e:
            if getattr(e, 'code', None) == 403: saw403(url)
            if i == TRIES - 1: raise
            backoff(i, e)

class RangeFile(io.RawIOBase):   # seekable read-only file over HTTP Range, 256 KB blocks cached (central directory reads)
    B = 1 << 18
    def __init__(s, url):
        s.url, s.pos, s.cache = url, 0, {}
        s.size = head(url)
    def readable(s): return True
    def seekable(s): return True
    def tell(s): return s.pos
    def seek(s, o, w=0): s.pos = o if w == 0 else s.pos + o if w == 1 else s.size + o; return s.pos
    def read(s, n=-1):
        n = s.size - s.pos if n is None or n < 0 else min(n, s.size - s.pos); out = bytearray()
        while n > 0:
            k = s.pos // s.B
            if k not in s.cache: s.cache[k] = rng(s.url, k * s.B, min(s.size, (k + 1) * s.B) - 1)
            blk = s.cache[k]; o = s.pos - k * s.B; c = blk[o:o + n]; out += c; s.pos += len(c); n -= len(c)
        return bytes(out)

def member(buf, at, zi):   # one member's bytes from a buffer holding its local header at offset `at`
    nl, xl = struct.unpack('<HH', buf[at + 26:at + 30]); d = buf[at + 30 + nl + xl:at + 30 + nl + xl + zi.compress_size]
    if len(d) != zi.compress_size: raise RuntimeError(f'span too short for {zi.filename}')
    data = d if zi.compress_type == 0 else zlib.decompressobj(-15).decompress(d) if zi.compress_type == 8 else None
    if data is None: raise RuntimeError(f'compression {zi.compress_type}')
    if zlib.crc32(data) != zi.CRC: raise RuntimeError(f'CRC mismatch {zi.filename}')
    return data

def fetch(url, z, size, names, out):
    """write the named members under out/, fetched in coalesced spans; returns uncompressed bytes written"""
    allz = sorted(z.infolist(), key=lambda zi: zi.header_offset)
    end = {zi.filename: (allz[i + 1].header_offset if i + 1 < len(allz) else z.start_dir) for i, zi in enumerate(allz)}
    want = sorted((z.getinfo(n) for n in names if not os.path.exists(os.path.join(out, n))), key=lambda zi: zi.header_offset)
    spans = []
    for zi in want:
        s = spans[-1] if spans else None
        if s and zi.header_offset - s['end'] < GAP and end[zi.filename] - s['start'] <= SPAN: s['zis'].append(zi); s['end'] = end[zi.filename]
        else: spans.append({'start': zi.header_offset, 'end': end[zi.filename], 'zis': [zi]})
    def get(s):
        buf, n = rng(url, s['start'], s['end'] - 1), 0
        for zi in s['zis']:
            f = os.path.join(out, zi.filename); os.makedirs(os.path.dirname(f), exist_ok=True)
            data = member(buf, zi.header_offset - s['start'], zi); open(f + '.part', 'wb').write(data); os.replace(f + '.part', f); n += len(data)
        return n
    with cf.ThreadPoolExecutor(THREADS) as ex: return sum(ex.map(get, spans))

def near_filter(fp_file, pad):
    """wanted(centre ECEF, radius): the leaf sphere comes within pad m (xy) of a building's bbox"""
    fp = json.load(open(fp_file)); lon_c = sum(fp['bbox'][::2]) / 2; lat_c = sum(fp['bbox'][1::2]) / 2
    A, F = 6378137.0, 1 / 298.257223563; E2 = F * (2 - F)
    def ecef(lon, lat, h=0.0):
        lo, la = math.radians(lon), math.radians(lat); n = A / math.sqrt(1 - E2 * math.sin(la) ** 2)
        return [(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - E2) + h) * math.sin(la)]
    o = ecef(lon_c, lat_c); lo, la = math.radians(lon_c), math.radians(lat_c)
    EAST = [-math.sin(lo), math.cos(lo), 0]; NORTH = [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)]
    def enu(p): d = [p[i] - o[i] for i in range(3)]; return (sum(d[i] * EAST[i] for i in range(3)), sum(d[i] * NORTH[i] for i in range(3)))
    boxes, G = {}, 100                                          # building bboxes on a 100 m grid, for speed
    for b in fp['buildings']:
        pts = [enu(ecef(x, y)) for r in b['rings'] for x, y in r]
        bb = (min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad, max(p[0] for p in pts) + pad, max(p[1] for p in pts) + pad)
        for i in range(math.floor(bb[0] / G), math.floor(bb[2] / G) + 1):
            for j in range(math.floor(bb[1] / G), math.floor(bb[3] / G) + 1): boxes.setdefault((i, j), []).append(bb)
    def wanted(c, r):
        x, y = enu(c); cand = {id(b): b for i in range(math.floor((x - r) / G), math.floor((x + r) / G) + 1)
                                for j in range(math.floor((y - r) / G), math.floor((y + r) / G) + 1) for b in boxes.get((i, j), [])}
        return any(x + r >= b[0] and x - r <= b[2] and y + r >= b[1] and y - r <= b[3] for b in cand.values())
    return wanted

def ecef(lon, lat, h=0.0):
    A, F = 6378137.0, 1 / 298.257223563; E2 = F * (2 - F); lo, la = math.radians(lon), math.radians(lat); n = A / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return [(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - E2) + h) * math.sin(la)]

def mat(m, n): return [sum(m[r + 4 * k] * n[k + 4 * c] for k in range(4)) for c in range(4) for r in range(4)]
def pt(m, p): return [sum(m[r + 4 * k] * [*p, 1][k] for k in range(4)) for r in range(3)]

def leaves_near(tss, wanted):
    want, n = [], 0
    for name, data in tss.items():
        def walk(node, M):
            nonlocal n
            M = mat(M, node['transform']) if 'transform' in node else M; u = (node.get('content') or {}).get('uri') or (node.get('content') or {}).get('url')
            kids = node.get('children') or []
            if u and not u.endswith('.json') and (node['geometricError'] == 0 or not kids):
                n += 1; bv = node['boundingVolume']
                if 'sphere' in bv: c, r = pt(M, bv['sphere'][:3]), bv['sphere'][3]
                elif 'box' in bv: c, r = pt(M, bv['box'][:3]), math.hypot(*bv['box'][3:12])          # 11-NW-25B has box leaves
                else:                                                                                  # region: radians, metres, no transform
                    w, s_, e, nn, lo, hi = bv['region']; c = ecef(math.degrees((w + e) / 2), math.degrees((s_ + nn) / 2), (lo + hi) / 2)
                    r = math.hypot((e - w) * 6378137 * math.cos((s_ + nn) / 2), (nn - s_) * 6378137, hi - lo) / 2
                if wanted(c, r): want.append(os.path.normpath(os.path.join(os.path.dirname(name), u)))
            for c in kids: walk(c, M)
        walk(json.loads(data.decode('utf-8-sig'))['root'], [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    return want, n

def main(a):
    opt = lambda f, d=None: a[a.index(f) + 1] if f in a else d
    flags = {'--prefix', '--near', '--pad'}; pos = [x for i, x in enumerate(a) if x not in flags and (i == 0 or a[i - 1] not in flags)]
    kind, out, sheets = pos[0], pos[1], pos[2:]
    wanted = near_filter(opt('--near'), float(opt('--pad', 25))) if opt('--near') else None
    report = {}; os.makedirs(out, exist_ok=True)
    for sheet in sheets:
        url = f'https://download.map.gov.hk/api/3d-zip/{kind}/{sheet}.zip?key={KEY}'; rf = RangeFile(url); z = zipfile.ZipFile(rf)
        names = [zi.filename for zi in z.infolist() if not zi.is_dir()]; n0 = dict(fetched); d = os.path.join(out, sheet); r = {}
        if wanted:
            tsn = [n for n in names if n.endswith('tileset.json')]; fetch(url, z, rf.size, tsn, d)
            tss = {n: open(os.path.join(d, n), 'rb').read() for n in tsn}
            want, nleaf = leaves_near(tss, wanted); have = set(names); pick = [w for w in want if w in have]
            r = {'tilesets': len(tsn), 'leaf_tiles': nleaf, 'missing_in_zip': len(want) - len(pick)}
        else:
            pick = [n for n in names if n.startswith(opt('--prefix'))]
        got = fetch(url, z, rf.size, pick, d)
        report[sheet] = {'zip_MB': round(rf.size / 1e6, 1), 'members': len(names), 'fetched_members': len(pick), 'MB': round(got / 1e6, 1),
                         'downloaded_MB': round((fetched['bytes'] - n0['bytes']) / 1e6, 1), 'requests': fetched['requests'] - n0['requests'], **r}
        print(sheet, report[sheet], flush=True)
    return report

def run(a):
    """main(), writing <out>/zipcut-<kind>.json {fetch: settings + totals + 403s, sheets: {...}} also when it fails"""
    flags = {'--prefix', '--near', '--pad'}; pos = [x for i, x in enumerate(a) if x not in flags and (i == 0 or a[i - 1] not in flags)]
    os.makedirs(pos[1], exist_ok=True); report, err, over, blocked = {}, None, False, False
    try: report = main(a)
    except OverBudget as e: err = repr(e); over = True
    except Blocked as e: err = repr(e); blocked = True
    except Exception as e: err = repr(e); raise
    finally:
        f = {**SETTINGS, **fetched, 'MB': round(fetched['bytes'] / 1e6, 1), 'error': err}; f.pop('bytes')
        json.dump({'fetch': f, 'sheets': report}, open(os.path.join(pos[1], f'zipcut-{pos[0]}.json'), 'w'), indent=1)
    if over: print(f'  {err}: stopping, fetched members stay on disk', file=sys.stderr); sys.exit(75)
    if blocked: print(f'  {err}', file=sys.stderr); sys.exit(76)          # a Lands D 403 with STOP_ON_403: the caller stops everything

if __name__ == '__main__':
    if len(sys.argv) < 4: sys.exit(__doc__)
    run(sys.argv[1:])
