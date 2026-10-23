"""Lands D 1:1000 map sheets (the CSDI sheet index layers) and the CSDI building footprints of one sheet.
  python3 sheets.py plan <product> <sheets|all> <sheets per job> <max jobs>   matrix JSON for the workflow (skips published sheets)
  python3 sheets.py footprints <sheet> <out.json>                               CSDI footprints over the sheet + ~100 m context
  python3 sheets.py neighbours <sheet>                                          photomesh sheets within ~150 m (self included)
Products: indiv-k4 (Individualised models index), bake3dsd (Open3Dhk photomesh index). Indexes are cached in $WORK (default .)."""
import json, os, sys, urllib.parse, urllib.request
from shapely.geometry import Polygon, Point, box

CSDI = 'https://portal.csdi.gov.hk/server/rest/services/common'
INDEX = {'indiv-k4': f'{CSDI}/landsd_rcd_1671676915450_88604/FeatureServer/0', 'bake3dsd': f'{CSDI}/landsd_rcd_1671677054006_62261/FeatureServer/0'}
BUILDINGS = f'{CSDI}/landsd_rcd_1637211194312_35158/MapServer/0'
BUCKET = 'milleleaf/hk-building-textures'
UA = {'User-Agent': 'milleleaf-hk-building-textures'}

def query(url, **q):
    """every feature of an ArcGIS query, paged (the layers return 3,000 at most per request)"""
    feats = []
    while True:
        p = {'f': 'json', 'outSR': 4326, 'orderByFields': 'OBJECTID', 'resultOffset': len(feats), 'resultRecordCount': 2000, **q}
        for i in range(4):
            try: d = json.load(urllib.request.urlopen(urllib.request.Request(f'{url}/query?{urllib.parse.urlencode(p)}', headers=UA), timeout=120)); break
            except Exception:
                if i == 3: raise
        if 'error' in d: raise RuntimeError(d['error'])
        feats += d['features']
        if not d.get('exceededTransferLimit') and len(d['features']) < 2000: return feats

def index(product):
    """{sheet: {'rev': ms, 'rings': [...]}} for a product's sheet index, cached"""
    f = os.path.join(os.environ.get('WORK', '.'), f'index-{product}.json')
    if not os.path.exists(f):
        fs = query(INDEX[product], where='1=1', outFields='SHEETNO,REVISIONDATE')
        json.dump({x['attributes']['SHEETNO']: {'rev': x['attributes']['REVISIONDATE'], 'rings': x['geometry']['rings']} for x in fs}, open(f, 'w'))
    return json.load(open(f))

def poly(rings): return Polygon(rings[0], rings[1:])

def published(product, sheets):
    """{sheet: rev} of sheets whose report.json is on the bucket (rev from the product manifest when it lists the sheet)"""
    from huggingface_hub import create_bucket, get_bucket_paths_info
    create_bucket(BUCKET, private=False, exist_ok=True)
    have = {p.path.split('/')[1] for p in get_bucket_paths_info(BUCKET, [f'{product}/{s}/report.json' for s in sheets])}
    try: man = json.load(urllib.request.urlopen(f'https://huggingface.co/buckets/{BUCKET}/resolve/{product}/manifest.json', timeout=60))['sheets']
    except Exception: man = {}
    return {s: man.get(s, {}).get('rev') for s in have}

def plan(product, which, per_job, max_jobs):
    idx = index(product); sheets = sorted(idx) if which == 'all' else [s.strip() for s in which.split(',') if s.strip()]
    bad = [s for s in sheets if s not in idx]
    if bad: sys.exit(f'not in the {product} sheet index: {bad}')
    pub = published(product, sheets)
    todo = [s for s in sheets if s not in pub or (pub[s] is not None and pub[s] != idx[s]['rev'])]
    chunks = [todo[i:i + per_job] for i in range(0, len(todo), per_job)][:min(max_jobs, 256)]
    print(f'{product}: {len(sheets)} asked, {len(sheets) - len(todo)} already published, {len(todo)} to do, '
          f'{sum(map(len, chunks))} in {len(chunks)} jobs this run', file=sys.stderr)
    return [{'id': f'{c[0]}+{len(c) - 1}' if len(c) > 1 else c[0], 'sheets': ' '.join(c)} for c in chunks]

def footprints(sheet, out, ctx=0.001):
    """CSDI footprints meeting the sheet's bbox grown by ctx degrees (~100 m). On the sheet (baked) when a representative point of
    the footprint falls in the sheet polygon; the rest are context (sheet: false: party walls, which 3DSD model stands for what).
    Same shape as the hk-ifcx spike's footprints.py output (rings in lon/lat, even-odd), bbox = the sheet's own bbox."""
    sp = poly(index('bake3dsd')[sheet]['rings']); x0, y0, x1, y1 = sp.bounds
    fs = query(BUILDINGS, where='1=1', geometry=f'{x0 - ctx},{y0 - ctx},{x1 + ctx},{y1 + ctx}', geometryType='esriGeometryEnvelope', inSR=4326,
               spatialRel='esriSpatialRelIntersects', outFields='BuildingCSUID,BuildingNameEN,BuildingBlockType,BaseHeight,TopHeight')
    res = []
    for f in fs:
        p, rings = f['attributes'], [[[round(x, 7), round(y, 7)] for x, y in r] for r in f['geometry']['rings']]
        try: rp = Polygon(rings[0]).buffer(0).representative_point()
        except Exception: rp = Point(rings[0][0])
        res.append({'id': p['BuildingCSUID'], 'name': p.get('BuildingNameEN'), 'type': p.get('BuildingBlockType'), 'base': p.get('BaseHeight'),
                    'top': p.get('TopHeight'), 'rings': rings, 'sheet': sp.contains(rp)})
    json.dump({'bbox': [x0, y0, x1, y1], 'sheet': sheet, 'buildings': res}, open(out, 'w'))
    n = sum(b['sheet'] for b in res); print(f'{sheet}: {n} footprints on the sheet, {len(res) - n} context -> {out}', file=sys.stderr)
    return n

def neighbours(sheet, pad=0.0015):
    idx = index('bake3dsd'); g = box(*poly(idx[sheet]['rings']).bounds).buffer(pad)
    return sorted(s for s, v in idx.items() if poly(v['rings']).intersects(g))

def bbox(sheet, product='bake3dsd'): return poly(index(product)[sheet]['rings']).bounds

if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    if a[0] == 'plan': print(json.dumps(plan(a[1], a[2], int(a[3]), int(a[4]))))
    elif a[0] == 'footprints': footprints(a[1], a[2])
    elif a[0] == 'neighbours': print(' '.join(neighbours(a[1])))
    else: sys.exit(__doc__)
