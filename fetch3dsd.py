"""fetch the finest Lands D 3D Spatial Data building tiles over a lon/lat box.
Walks the streaming tileset from its root, descends only into tiles whose ECEF bounding box meets the box (heights -50..700 m),
follows external tileset.json files, and downloads the content of every leaf tile (no children = finest LOD under REPLACE).
Every request carries ?key= (Lands D needs it on each URL). At most 4 requests at a time (Lands D terms: 100 concurrent users).
Files are cached under <out dir> by URL path; a cached file is not fetched again. Writes <out dir>/leaves.json:
[{uri, file, transform (16, column-major, tile frame -> ECEF, includes every ancestor transform), box (12)}].
usage: python3 -I fetch3dsd.py <lon0> <lat0> <lon1> <lat1> <out dir>"""
import json, math, os, sys, time, urllib.parse, urllib.request, concurrent.futures as cf

KEY = 'ad5940a63bd344c48b0351ef1c7a905e'   # Lands D's public key, published on the CSDI dataset records
ROOT = 'https://data.map.gov.hk/api/3d-data/3dsd/WGS84/building/tileset.json'
lon0, lat0, lon1, lat1 = map(float, sys.argv[1:5]); OUT = sys.argv[5]

def ecef(lon, lat, h):
    a, f = 6378137.0, 1 / 298.257223563; e2 = f * (2 - f); lo, la = math.radians(lon), math.radians(lat)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return [(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - e2) + h) * math.sin(la)]
corners = [ecef(lo, la, h) for lo in (lon0, lon1) for la in (lat0, lat1) for h in (-50, 700)]
AMIN, AMAX = [min(c[i] for c in corners) for i in range(3)], [max(c[i] for c in corners) for i in range(3)]

def mat(a, b):   # column-major 4x4 product a*b
    return [sum(a[r + 4 * k] * b[k + 4 * c] for k in range(4)) for c in range(4) for r in range(4)]
I = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
def meets(bv, M):
    b = bv.get('box')
    if not b: return True                                   # region/sphere: keep (not seen in 3DSD)
    c = [sum(M[r + 4 * k] * ([*b[:3], 1][k]) for k in range(4)) for r in range(3)]
    half = [sum(abs(sum(M[r + 4 * k] * b[3 + 3 * j + k] for k in range(3))) for j in range(3)) for r in range(3)]
    return all(c[i] + half[i] >= AMIN[i] and c[i] - half[i] <= AMAX[i] for i in range(3))

def get(url):
    u = urllib.parse.urlsplit(url); q = dict(urllib.parse.parse_qsl(u.query)); q['key'] = KEY
    f = os.path.join(OUT, 'cache', u.path.lstrip('/'))
    if not os.path.exists(f):
        os.makedirs(os.path.dirname(f), exist_ok=True)
        req = urllib.request.Request(urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), '')), headers={'User-Agent': 'milleleaf-hk-building-textures'})
        for i in range(16):
            try: data = urllib.request.urlopen(req, timeout=120).read(); break
            except Exception as e:                          # Lands D answers 403 for a while under load: back off, about an hour in all
                if i == 15: raise
                time.sleep(min(300, 10 * 3 ** i))
        open(f + '.part', 'wb').write(data); os.replace(f + '.part', f)
    return f

leaves, stats = [], {'json': 0, 'leaf': 0, 'pruned': 0}
def walk(node, base, M):
    M = mat(M, node['transform']) if 'transform' in node else M
    if not meets(node['boundingVolume'], M): stats['pruned'] += 1; return []
    uri = (node.get('content') or {}).get('uri') or (node.get('content') or {}).get('url'); kids = node.get('children') or []
    jobs = []
    if uri:
        url = urllib.parse.urljoin(base, uri)
        if urllib.parse.urlsplit(url).path.endswith('.json'): jobs.append(('json', url, M))
        elif not kids: jobs.append(('leaf', url, M, node['boundingVolume'].get('box')))
    for c in kids: jobs += walk(c, base, M)
    return jobs

with cf.ThreadPoolExecutor(int(os.environ.get('ZIP_THREADS', 4))) as ex:
    todo = [('json', ROOT, I)]
    while todo:
        js = [j for j in todo if j[0] == 'json']; lf = [j for j in todo if j[0] == 'leaf']; todo = []
        for j, f in zip(lf, ex.map(lambda j: get(j[1]), lf)):
            leaves.append({'uri': j[1], 'file': os.path.relpath(f, OUT), 'transform': j[2], 'box': j[3]}); stats['leaf'] += 1
        for j, f in zip(js, ex.map(lambda j: get(j[1]), js)):
            stats['json'] += 1; ts = json.load(open(f, encoding='utf-8-sig'))
            todo += walk(ts['root'], j[1], j[2])
        print(stats, flush=True)
json.dump(leaves, open(os.path.join(OUT, 'leaves.json'), 'w'))
print(f"{len(leaves)} leaf tiles, {sum(os.path.getsize(os.path.join(OUT, l['file'])) for l in leaves) / 1e6:.1f} MB -> {OUT}/leaves.json")
