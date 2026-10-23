"""Build one map sheet of one product end to end and publish it to the bucket (needs HF_TOKEN unless --no-upload):
  indiv-k4   zipcut BUILDING/ of the sheet's Individualised-models zip -> indiv.mjs --tex ktx2:4 --ge 40
  bake3dsd   CSDI footprints -> zipcut the photomesh leaves near them from the sheet and its neighbours -> fetch3dsd.py over the
             sheet -> bake.mjs --all --mesh3dsd
Uploads <product>/<sheet>/ (tileset.json + one .glb per building), report.json LAST: a sheet counts as published only when its
report.json is there (sheets.py plan). Downloads are deleted after each sheet.
usage: python3 sheet.py <product> <work dir> <sheet>... [--no-upload]"""
import json, os, resource, shutil, subprocess, sys, time
import sheets

HERE = os.path.dirname(os.path.abspath(__file__))
PY = [sys.executable, '-I']

def peak_gb():   # largest resident set of any finished child so far (Linux: KB, macOS: bytes)
    r = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round(r / (1e9 if sys.platform == 'darwin' else 1e6), 2)

def step(times, name, cmd):
    t = time.time(); print(f'## {name}: {" ".join(cmd)}', flush=True); subprocess.run(cmd, check=True, cwd=HERE)
    times[name] = round(time.time() - t)

def region(product, sheet, out):
    """region for the root tileset: the sheet's bbox grown ~200 m, heights from the sheet tileset's root"""
    ts = json.load(open(os.path.join(out, 'tileset.json')))['root']['boundingVolume']
    if 'region' in ts: lo, hi = ts['region'][4], ts['region'][5]
    else: b = ts['box']; lo, hi = b[2] - abs(b[11]) - 5, b[2] + abs(b[11]) + 5     # bake.mjs: ENU box at h = 0, z = height
    x0, y0, x1, y1 = sheets.bbox(sheet, 'indiv-k4' if product == 'indiv-k4' else 'bake3dsd'); g = 0.002
    return [round(v * 3.141592653589793 / 180, 9) for v in (x0 - g, y0 - g, x1 + g, y1 + g)] + [round(lo, 1), round(hi, 1)]

def build(product, work, sheet):
    times, d = {}, os.path.join(work, sheet); out = os.path.join(d, 'out'); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    rev = sheets.index(product)[sheet]['rev']; rep = {'product': product, 'sheet': sheet, 'rev': rev}
    if product == 'indiv-k4':
        step(times, 'fetch', [*PY, 'zipcut.py', 'GLTF', os.path.join(d, 'zip'), sheet, '--prefix', 'BUILDING/'])
        rep['zipcut'] = json.load(open(os.path.join(d, 'zip', 'zipcut-GLTF.json')))[sheet]
        if not os.path.isdir(os.path.join(d, 'zip', sheet, 'BUILDING')): rep['buildings'] = 0
        else:
            step(times, 'convert', ['node', 'indiv.mjs', out, os.path.join(d, 'zip', sheet), '--tex', 'ktx2:4', '--ge', '40'])
            rep['convert'] = json.load(open(os.path.join(out, 'report.json'))); os.remove(os.path.join(out, 'report.json'))
            rep['buildings'] = rep['convert']['buildings']
    else:
        fp = os.path.join(d, 'footprints.json'); t = time.time()
        n = sheets.footprints(sheet, fp); times['footprints'] = round(time.time() - t)
        if not n: rep['buildings'] = 0
        else:
            step(times, 'fetch_photomesh', [*PY, 'zipcut.py', 'CESIUM', os.path.join(d, 'pm'), *sheets.neighbours(sheet), '--near', fp, '--pad', '25'])
            rep['zipcut'] = json.load(open(os.path.join(d, 'pm', 'zipcut-CESIUM.json')))
            x0, y0, x1, y1 = sheets.bbox(sheet); g = 0.001
            step(times, 'fetch_3dsd', [*PY, 'fetch3dsd.py', *map(str, (x0 - g, y0 - g, x1 + g, y1 + g)), os.path.join(d, '3dsd')])
            dirs = ','.join(os.path.join(d, 'pm', s) for s in rep['zipcut'])
            step(times, 'bake', ['node', 'bake.mjs', dirs, fp, out, '--all', '--mesh3dsd', os.path.join(d, '3dsd'), '--png', '0'])
            br = json.load(open(os.path.join(out, 'bake-report.json'))); br.pop('buildings_detail', None); os.remove(os.path.join(out, 'bake-report.json'))
            shutil.rmtree(os.path.join(out, 'atlas'), ignore_errors=True); rep['convert'] = br; rep['buildings'] = br['buildings']
    files = [f for f in os.listdir(out)] if os.path.isdir(out) else []
    rep.update(seconds=times, peak_rss_GB=peak_gb(), files=len(files), bytes=sum(os.path.getsize(os.path.join(out, f)) for f in files))
    if rep['buildings']: rep['region'] = region(product, sheet, out)
    return d, out, rep

def upload(product, sheet, out, rep):
    from huggingface_hub import batch_bucket_files, create_bucket, sync_bucket
    create_bucket(sheets.BUCKET, private=False, exist_ok=True); t = time.time()
    if os.path.isdir(out): sync_bucket(out, f'hf://buckets/{sheets.BUCKET}/{product}/{sheet}', delete=True, quiet=True)
    rep['seconds']['upload'] = round(time.time() - t)
    batch_bucket_files(sheets.BUCKET, add=[(json.dumps(rep, indent=1).encode(), f'{product}/{sheet}/report.json')])

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if x != '--no-upload']
    if len(a) < 3: sys.exit(__doc__)
    product, work, todo = a[0], os.path.abspath(a[1]), a[2:]; os.environ.setdefault('WORK', work); os.makedirs(work, exist_ok=True)
    failed = []
    for s in todo:
        try:
            d, out, rep = build(product, work, s)
            if '--no-upload' not in sys.argv: upload(product, s, out, rep)
            print(json.dumps({k: rep[k] for k in ('sheet', 'buildings', 'files', 'bytes', 'seconds', 'peak_rss_GB')}), flush=True)
            if '--no-upload' not in sys.argv: shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            print(f'FAILED {s}: {e!r}', flush=True); failed.append(s)
    if failed: sys.exit(f'failed: {failed}')
