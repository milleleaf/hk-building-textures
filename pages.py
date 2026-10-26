"""Serve the textures to the map from Cloudflare Pages (Hugging Face stays the source of truth, Kaggle the archive).
Limits found in Cloudflare's docs (2026-10-26): Free plan 20,000 files per Pages site, 25 MiB per file, 100 projects per account,
100 rules in _headers, static asset requests and bandwidth free; "500 builds per month" is for Git builds, direct uploads are not
listed there. A deployment is a whole-site snapshot (wrangler dedupes unchanged files by hash), so a shard directory holds ALL
files of the shard at every deploy.

Shards: a committed, append-only map (areas/pages-shards.json) sheet -> shard per product, at most MAX_FILES deployed files
per shard; a sheet never moves. Project names: milleleaf-hkbt-<product>-<nn>; one small `milleleaf-hkbt-index` project serves
<product>/tileset.json (root, ABSOLUTE uris to each sheet's tileset on its shard's .pages.dev), <product>/manifest.json and
<product>/shards.json. glb files are renamed <id>-<sha8>.glb at staging, so Cache-Control immutable is safe after a rebuild.
Real subdomains can differ from the project name (Pages adds a suffix when a name is taken), so the deploy job records each
project's subdomain on the bucket (pages/projects/<project>.json) and the staging reads it.

usage: python3 pages.py plan <mode: changed|all|shard> <product|all> [<shard>]    JSON for the workflow matrix (assigns new sheets)
       python3 pages.py stage <product> <shard> <out dir>                         stage a shard (downloads from the bucket)
       python3 pages.py index <out dir> [--all-shards]                            stage the index project
       python3 pages.py mark <product> <shard> <project> <files> <signature>      record a successful deploy
       python3 pages.py project <project> <subdomain>                             record a project's real subdomain"""
import concurrent.futures as cf, hashlib, json, os, shutil, sys, time, urllib.request
from huggingface_hub import batch_bucket_files, list_bucket_tree, download_bucket_files, sync_bucket
import publish, sheets

HERE = os.path.dirname(os.path.abspath(__file__))
SHARDS = os.path.join(HERE, 'areas', 'pages-shards.json')
MAX_FILES, HARD_FILES, PREFIX, INDEX = 15000, 20000, 'milleleaf-hkbt', 'milleleaf-hkbt-index'
MIN_HOURS = 24
PRODUCTS = ['indiv-k4', 'bake3dsd']
# CORS for the map; glb files carry a content hash in their name (see stage), so immutable is safe; tilesets can change.
HEADERS_SHARD = """/*
  Access-Control-Allow-Origin: *
/*.glb
  Cache-Control: public, max-age=31536000, immutable
/tileset.json
  Cache-Control: public, max-age=300
/*/tileset.json
  Cache-Control: public, max-age=300
"""
HEADERS_INDEX = """/*
  Access-Control-Allow-Origin: *
  Cache-Control: public, max-age=300
"""

def project(product, n): return f'{PREFIX}-{product}-{n}'
def base_url(proj, subs=None):
    t = os.environ.get('PAGES_BASE_TEMPLATE')                    # local cross-origin test: e.g. http://127.0.0.1:886{n} (n = the shard number)
    if t: return t.format(n=int(proj.rsplit('-', 1)[1]))
    return 'https://' + ((subs or {}).get(proj) or f'{proj}.pages.dev')
def bucket_json(path):
    try: return json.load(urllib.request.urlopen(f'https://huggingface.co/buckets/{sheets.BUCKET}/resolve/{path}', timeout=120))
    except Exception: return None
def manifest(product): return bucket_json(f'{product}/manifest.json')
def deployed_files(report): return report['files']                    # glb + sheet tileset.json (report.json is not in `files` and not deployed)

# ---- the shard map -------------------------------------------------------------------------------------------------------------
def load_map(): return json.load(open(SHARDS)) if os.path.exists(SHARDS) else {'max_files': MAX_FILES, 'products': {}}

def assign(m, product, reps, order):
    """append sheets without a shard to the last shard with room; sheets already assigned keep their shard (their file count is refreshed)"""
    P = m['products'].setdefault(product, {}); where = {s: n for n, d in P.items() for s in d['sheets']}; new = 0
    for s in [x for x in order if x in reps] + sorted(set(reps) - set(order)):
        n = deployed_files(reps[s])
        if s in where: P[where[s]]['sheets'][s] = n; continue
        last = max(P) if P else None
        if last is None or sum(P[last]['sheets'].values()) + n > m['max_files']:
            last = f'{(int(last) + 1) if last else 1:02d}'; P[last] = {'sheets': {}}
        P[last]['sheets'][s] = n; where[s] = last; new += 1
    return new

def cbd_order(): return sheets.area('cbd') or []

# ---- staging -------------------------------------------------------------------------------------------------------------------
def sha8(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()[:8]

def rename_glbs(node, names):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == 'uri' and isinstance(v, str) and v.endswith('.glb'): node[k] = names[v]
            else: rename_glbs(v, names)
    elif isinstance(node, list):
        for v in node: rename_glbs(v, names)

def stage_sheet(product, s, out, workers_dir):
    d = os.path.join(workers_dir, s)
    sync_bucket(f'hf://buckets/{sheets.BUCKET}/{product}/{s}', d, exclude=['failed-*.json', 'report.json'], quiet=True)
    dst = os.path.join(out, s); os.makedirs(dst, exist_ok=True); names = {}
    for f in sorted(os.listdir(d)):
        if f.endswith('.glb'):
            names[f] = f'{f[:-4]}-{sha8(os.path.join(d, f))}.glb'; shutil.move(os.path.join(d, f), os.path.join(dst, names[f]))
    ts = json.load(open(os.path.join(d, 'tileset.json'))); rename_glbs(ts, names)
    json.dump(ts, open(os.path.join(dst, 'tileset.json'), 'w'))
    shutil.rmtree(d); return len(names) + 1

def stage_shard(product, n, out, workers=8):
    man = manifest(product); m = load_map(); S = m['products'][product][n]['sheets']
    reps = {s: man['sheets'][s] for s in S if s in man['sheets']}
    shutil.rmtree(out, ignore_errors=True); os.makedirs(out); tmp = out.rstrip('/') + '.dl'; os.makedirs(tmp, exist_ok=True)
    with cf.ThreadPoolExecutor(workers) as ex: counts = list(ex.map(lambda s: stage_sheet(product, s, out, tmp), sorted(reps)))
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(publish.root_tileset(product, reps, uri=lambda s: f'{s}/tileset.json'), open(os.path.join(out, 'tileset.json'), 'w'))
    open(os.path.join(out, '_headers'), 'w').write(HEADERS_SHARD)
    n_files = sum(len(f) for _, _, f in os.walk(out)); size = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(out) for f in fs)
    big = max((os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(out) for f in fs), default=0)
    assert n_files <= HARD_FILES - 1, f'{product} shard {n}: {n_files} files, the Free plan allows {HARD_FILES} per site'
    assert big < 25 * 1024 * 1024, f'{product} shard {n}: a file of {big} bytes, Pages allows 25 MiB'
    return {'product': product, 'shard': n, 'project': project(product, n), 'sheets': len(reps), 'files': n_files, 'MB': round(size / 1e6, 1), 'largest_file_KB': round(big / 1e3)}

def project_records():
    out = {}
    try: tree = list(list_bucket_tree(sheets.BUCKET, prefix='pages/projects/'))
    except Exception: return out                                  # nothing recorded yet
    for f in tree:
        p = getattr(f, 'path', '')
        if p.endswith('.json'):
            j = bucket_json(p)
            if j: out[j['project']] = j['subdomain']
    return out

def state_records():
    out = {}
    try: tree = list(list_bucket_tree(sheets.BUCKET, prefix='pages/state/'))
    except Exception: return out
    for f in tree:
        p = getattr(f, 'path', '')
        if p.endswith('.json'):
            j = bucket_json(p)
            if j: out[f"{j['product']}/{j['shard']}"] = j
    return out

def stage_index(out, all_shards=False):
    m, st, subs = load_map(), ({} if all_shards else state_records()), project_records()
    shutil.rmtree(out, ignore_errors=True); os.makedirs(out); info = {}
    for product in PRODUCTS:
        man = manifest(product)
        if not man or product not in m['products']: continue
        where = {s: n for n, d in m['products'][product].items() for s in d['sheets']}
        live = {s: man['sheets'][s] for s, n in where.items() if s in man['sheets'] and (all_shards or f'{product}/{n}' in st)}
        if not live: continue
        os.makedirs(os.path.join(out, product), exist_ok=True)
        root = publish.root_tileset(product, live, uri=lambda s: f"{base_url(project(product, where[s]), subs)}/{s}/tileset.json")
        json.dump(root, open(os.path.join(out, product, 'tileset.json'), 'w'))
        json.dump({**{k: v for k, v in man.items() if k != 'sheets'}, 'sheets': live}, open(os.path.join(out, product, 'manifest.json'), 'w'))
        shards = {n: {'project': project(product, n), 'url': base_url(project(product, n), subs), 'sheets': len(d['sheets'])} for n, d in m['products'][product].items()
                  if all_shards or f'{product}/{n}' in st}
        json.dump(shards, open(os.path.join(out, product, 'shards.json'), 'w'), indent=1); info[product] = {'sheets': len(live), 'shards': len(shards)}
    open(os.path.join(out, '_headers'), 'w').write(HEADERS_INDEX)
    json.dump({'about': 'Hong Kong building textures, Lands Department open data, built by github.com/milleleaf/hk-building-textures', 'products': info},
              open(os.path.join(out, 'index.json'), 'w'), indent=1)
    n_files = sum(len(f) for _, _, f in os.walk(out)); size = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(out) for f in fs)
    return {'project': INDEX, 'files': n_files, 'MB': round(size / 1e6, 2), 'products': info}

# ---- plan, state ---------------------------------------------------------------------------------------------------------------
def signature(reps):
    return hashlib.sha256(json.dumps(sorted((s, r.get('rev'), r.get('code'), r.get('files'), r.get('bytes')) for s, r in reps.items())).encode()).hexdigest()[:16]

def plan(mode, which, shard=None, now=None):
    m, st, now = load_map(), (state_records() if mode == 'changed' else {}), (now or time.time()); jobs, added = [], 0
    for product in (PRODUCTS if which == 'all' else [which]):
        man = manifest(product)
        if not man: continue
        reps = {s: r for s, r in man['sheets'].items() if r.get('buildings') and r.get('region')}
        added += assign(m, product, reps, cbd_order())
        for n, d in sorted(m['products'].get(product, {}).items()):
            if mode == 'shard' and n != shard: continue
            sig = signature({s: reps[s] for s in d['sheets'] if s in reps}); old = st.get(f'{product}/{n}')
            total = sum(d['sheets'].values())
            if total > HARD_FILES - 1: sys.exit(f'{product} shard {n} has {total} files (the Free plan allows {HARD_FILES}); a sheet grew after assignment')
            if mode == 'changed':
                if old and old.get('signature') == sig: continue
                if old and now - old.get('deployed_epoch', 0) < MIN_HOURS * 3600: continue      # queued: the next run after 24 h picks it up
            jobs.append({'product': product, 'shard': n, 'project': project(product, n), 'files': total, 'signature': sig})
    json.dump(m, open(SHARDS, 'w'), indent=1, sort_keys=True)
    return {'jobs': jobs, 'assigned_new_sheets': added}

def mark(product, n, proj, files, sig):
    rec = {'product': product, 'shard': n, 'project': proj, 'files': int(files), 'signature': sig, 'deployed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'deployed_epoch': int(time.time())}
    batch_bucket_files(sheets.BUCKET, add=[(json.dumps(rec).encode(), f'pages/state/{product}-{n}.json')])

if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    if a[0] == 'plan': print(json.dumps(plan(a[1], a[2], a[3] if len(a) > 3 else None)))
    elif a[0] == 'stage': print(json.dumps(stage_shard(a[1], a[2], a[3])))
    elif a[0] == 'index': print(json.dumps(stage_index(a[1], '--all-shards' in a)))
    elif a[0] == 'mark': mark(a[1], a[2], a[3], a[4], a[5])
    elif a[0] == 'project': batch_bucket_files(sheets.BUCKET, add=[(json.dumps({'project': a[1], 'subdomain': a[2]}).encode(), f'pages/projects/{a[1]}.json')])
    else: sys.exit(__doc__)
