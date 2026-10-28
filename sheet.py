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

class Blocked403(Exception): pass           # a Lands D 403 with STOP_ON_403: stop the whole run, not just this sheet
class Unfinished(Exception): pass            # the job's download budget is spent; what was fetched stays in the sheet's folder

def step(times, name, cmd):
    t = time.time(); print(f'## {name}: {" ".join(cmd)}', flush=True); r = subprocess.run(cmd, cwd=HERE)
    times[name] = round(time.time() - t)
    if r.returncode == 75: raise Unfinished(name)
    if r.returncode == 76: raise Blocked403(name)
    if r.returncode: raise subprocess.CalledProcessError(r.returncode, cmd)

def budget_spent():
    f, b = os.environ.get('BUDGET_FILE'), float(os.environ.get('BUDGET_MB') or 0) * 1e6
    return bool(b and f and os.path.exists(f) and json.load(open(f)).get('bytes', 0) >= b)

EARLY = os.environ.get('EARLY_PUBLISH') == '1'      # indiv-k4: when the download budget ends mid-sheet, convert and publish the buildings already complete on disk

def complete_buildings(zipdir):
    """ids under <zipdir>/BUILDING/ whose .gltf and every file it names (images, buffers) are on disk: zipcut writes each member whole or not at all"""
    root, ok = os.path.join(zipdir, 'BUILDING'), []
    for i in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        f = os.path.join(root, i, f'{i}.gltf')
        if not os.path.exists(f): continue
        try: j = json.load(open(f, encoding='utf-8'))
        except ValueError: continue
        uris = [x['uri'] for x in (j.get('images') or []) + (j.get('buffers') or []) if x.get('uri') and not x['uri'].startswith('data:')]
        if all(os.path.exists(os.path.join(root, i, u)) for u in uris): ok.append(i)
    return ok

def early_publish(product, d, sheet, times):
    """the sheet is unfinished (budget spent): publish its complete buildings as a partial sheet (<product>/<sheet>/ files + partial.json, never report.json, so the
    sheet stays unpublished in the planner and the next job continues it). Skipped when the sheet is already published or fewer than 20 buildings are complete."""
    if sheets.published(product, [sheet]): return
    ids = complete_buildings(os.path.join(d, 'zip', sheet))
    if len(ids) < 20: return
    stage, out = os.path.join(d, 'stage'), os.path.join(d, 'out-partial'); shutil.rmtree(stage, ignore_errors=True); shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(stage, 'BUILDING'))
    for i in ids: os.symlink(os.path.abspath(os.path.join(d, 'zip', sheet, 'BUILDING', i)), os.path.join(stage, 'BUILDING', i))
    step(times, 'convert_partial', ['node', 'indiv.mjs', out, stage, '--tex', 'ktx2:4', '--ge', '40'])
    r = json.load(open(os.path.join(out, 'report.json'))); os.remove(os.path.join(out, 'report.json'))
    if not r['buildings']: return
    files = os.listdir(out); rep = {'product': product, 'sheet': sheet, 'partial': True, 'complete_buildings': len(ids), 'buildings': r['buildings'], 'files': len(files),
                                    'bytes': sum(os.path.getsize(os.path.join(out, f)) for f in files), 'convert': r, 'code': sheets.code(),
                                    'region': region(product, sheet, out), 'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    from huggingface_hub import batch_bucket_files, create_bucket, sync_bucket
    create_bucket(sheets.BUCKET, private=False, exist_ok=True); sync_bucket(out, f'hf://buckets/{sheets.BUCKET}/{product}/{sheet}', delete=True, quiet=True)
    batch_bucket_files(sheets.BUCKET, add=[(json.dumps(rep, indent=1).encode(), f'{product}/{sheet}/partial.json')])
    print(f'PARTIAL {sheet}: {rep["buildings"]} of the sheet\'s buildings published, the rest is still being fetched', flush=True)

def region(product, sheet, out):
    """region for the root tileset: the sheet's bbox grown ~200 m, heights from the sheet tileset's root"""
    ts = json.load(open(os.path.join(out, 'tileset.json')))['root']['boundingVolume']
    if 'region' in ts: lo, hi = ts['region'][4], ts['region'][5]
    else: b = ts['box']; lo, hi = b[2] - abs(b[11]) - 5, b[2] + abs(b[11]) + 5     # bake.mjs: ENU box at h = 0, z = height
    x0, y0, x1, y1 = sheets.bbox(sheet, 'indiv-k4' if product == 'indiv-k4' else 'bake3dsd'); g = 0.002
    return [round(v * 3.141592653589793 / 180, 9) for v in (x0 - g, y0 - g, x1 + g, y1 + g)] + [round(lo, 1), round(hi, 1)]

def build(product, work, sheet):
    times, d = {}, os.path.join(work, sheet); out = os.path.join(d, 'out'); shutil.rmtree(out, ignore_errors=True); os.makedirs(d, exist_ok=True)
    if budget_spent(): raise Unfinished('budget spent before the sheet started')   # keeps any carried-over files
    rev = sheets.index(product)[sheet]['rev']; rep = {'product': product, 'sheet': sheet, 'rev': rev, 'code': sheets.code()}
    if product == 'indiv-k4':
        try: step(times, 'fetch', [*PY, 'zipcut.py', 'GLTF', os.path.join(d, 'zip'), sheet, '--prefix', 'BUILDING/'])
        except Unfinished:
            if EARLY and product == 'indiv-k4':
                try: early_publish(product, d, sheet, times)
                except Exception as e: print(f'early publish of {sheet} failed (the sheet carries on): {e!r}', flush=True)
            raise
        z = json.load(open(os.path.join(d, 'zip', 'zipcut-GLTF.json'))); rep['fetch'] = z['fetch']; rep['zipcut'] = z['sheets'][sheet]
        if not os.path.isdir(os.path.join(d, 'zip', sheet, 'BUILDING')): rep['buildings'] = 0
        else:
            step(times, 'convert', ['node', 'indiv.mjs', out, os.path.join(d, 'zip', sheet), '--tex', 'ktx2:4', '--ge', '40'])
            rep['convert'] = json.load(open(os.path.join(out, 'report.json'))); os.remove(os.path.join(out, 'report.json'))
            rep['buildings'] = rep['convert']['buildings']
            if not rep['buildings']: shutil.rmtree(out)                       # every building skipped: publish nothing but the report
    else:
        fp = os.path.join(d, 'footprints.json'); t = time.time()
        n = sheets.footprints(sheet, fp); times['footprints'] = round(time.time() - t)
        if not n: rep['buildings'] = 0
        else:
            step(times, 'fetch_photomesh', [*PY, 'zipcut.py', 'CESIUM', os.path.join(d, 'pm'), *sheets.neighbours(sheet), '--near', fp, '--pad', '25'])
            z = json.load(open(os.path.join(d, 'pm', 'zipcut-CESIUM.json'))); rep['fetch'] = z['fetch']; rep['zipcut'] = z['sheets']
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

def failed_attempt(product, work, sheet, e):
    """<product>/<sheet>/failed-<run>.json: the fetch settings and 403s of an attempt that did not publish"""
    z = {}
    for k in ('zip/zipcut-GLTF.json', 'pm/zipcut-CESIUM.json'):
        try: z = json.load(open(os.path.join(work, sheet, k)))
        except Exception: pass
    try:
        from huggingface_hub import batch_bucket_files
        batch_bucket_files(sheets.BUCKET, add=[(json.dumps({'product': product, 'sheet': sheet, 'code': sheets.code(), 'error': repr(e),
                           'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **z}, indent=1).encode(), f'{product}/{sheet}/failed-{os.environ.get("GITHUB_RUN_ID", "local")}.json')])
    except Exception as x: print(f'could not record the failed attempt: {x!r}', flush=True)

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if x != '--no-upload']
    if len(a) < 3: sys.exit(__doc__)
    product, work, todo = a[0], os.path.abspath(a[1]), a[2:]; os.environ.setdefault('WORK', work); os.makedirs(work, exist_ok=True)
    failed, blocked = [], False
    for s in todo:
        if budget_spent(): print(f'download budget spent: not starting {s}', flush=True); break
        try:
            d, out, rep = build(product, work, s)
            if '--no-upload' not in sys.argv: upload(product, s, out, rep)
            print(json.dumps({k: rep[k] for k in ('sheet', 'buildings', 'files', 'bytes', 'seconds', 'peak_rss_GB')}), flush=True)
            if '--no-upload' not in sys.argv: shutil.rmtree(d, ignore_errors=True)
        except Blocked403 as e:                      # 403: nothing else is fetched; no failed record (the sheet is fine); its folder is carried over
            print(f'BLOCKED {s}: Lands D answered 403 in {e}; stopping this run', flush=True)
            json.dump({'product': product, 'sheet': s}, open(os.path.join(work, 'unfinished.json'), 'w')); blocked = True; break
        except Unfinished as e:                      # carry the sheet's folder over to the next job (the workflow caches it)
            print(f'UNFINISHED {s}: download budget spent in {e}', flush=True)
            json.dump({'product': product, 'sheet': s}, open(os.path.join(work, 'unfinished.json'), 'w')); break
        except Exception as e:
            print(f'FAILED {s}: {e!r}', flush=True); failed.append(s)
            if '--no-upload' not in sys.argv: failed_attempt(product, work, s, e)
    if blocked: sys.exit(76)
    if failed: sys.exit(f'failed: {failed}')
