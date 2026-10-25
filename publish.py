"""Index step (needs HF_TOKEN with write access to the milleleaf org): read every published <product>/<sheet>/report.json, then write
  bucket   <product>/tileset.json   root tileset: 1:5000-sheet groups (e.g. 11-NW) -> one external tileset per map sheet
           <product>/manifest.json  every sheet's report (revision, buildings, files, bytes, timings), totals, sources
  dataset  milleleaf/hk-building-textures: the card (hf/README.md) and both manifests (one commit)
Run after the sheet jobs, also when some failed (what is published is indexed).
usage: python3 publish.py <product>... [--no-card]"""
import json, math, os, sys, tempfile, time
from huggingface_hub import HfApi, batch_bucket_files, download_bucket_files, get_bucket_paths_info
import sheets

HERE = os.path.dirname(os.path.abspath(__file__))
UP = {'indiv-k4': 'Y', 'bake3dsd': 'Z', 'x': 'Y'}
SHEET_GE = 60      # a sheet's tileset.json loads within ~3.5 km (sse 16, 1080 px, 60 deg fov); its own cells decide the rest

def reports(product):
    idx = sheets.index(product); paths = [f'{product}/{s}/report.json' for s in sorted(idx)]
    have = [p.path for p in get_bucket_paths_info(sheets.BUCKET, paths)]
    with tempfile.TemporaryDirectory() as d:
        download_bucket_files(sheets.BUCKET, [(p, os.path.join(d, p)) for p in have])
        return {p.split('/')[1]: json.load(open(os.path.join(d, p))) for p in have}

def union(rs): return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs), min(r[4] for r in rs), max(r[5] for r in rs)]

def root_tileset(product, reps, uri=lambda s: f'{s}/tileset.json'):
    groups = {}
    for s, r in sorted(reps.items()):
        if r.get('buildings') and r.get('region'): groups.setdefault('-'.join(s.split('-')[:2]), []).append((s, r['region']))
    kids = [{'boundingVolume': {'region': union([g for _, g in v])}, 'geometricError': SHEET_GE, 'refine': 'ADD',
             'children': [{'boundingVolume': {'region': g}, 'geometricError': SHEET_GE, 'refine': 'ADD', 'content': {'uri': uri(s)}} for s, g in v]}
            for _, v in sorted(groups.items())]
    if not kids: return None
    # 3d-tiles-renderer takes gltfUpAxis from the ROOT tileset for every external one: indiv.mjs writes Y-up glTF, bake.mjs Z-up
    return {'asset': {'version': '1.1', 'gltfUpAxis': UP[product], 'extras': {'product': product, 'source': 'github.com/milleleaf/hk-building-textures'}}, 'geometricError': 100000,
            'root': {'boundingVolume': {'region': union([k['boundingVolume']['region'] for k in kids])}, 'geometricError': 100000, 'refine': 'ADD', 'children': kids}}

def main(products, card=True):
    api, built, mans = HfApi(), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), {}
    for product in products:
        reps = reports(product); ts = root_tileset(product, reps); n = len(sheets.index(product))
        tot = lambda k: sum(r.get(k) or 0 for r in reps.values())
        man = {'built': built, 'product': product, 'sheets_in_index': n, 'sheets_published': len(reps), 'sheets_with_buildings': sum(1 for r in reps.values() if r.get('buildings')),
               'buildings': tot('buildings'), 'files': tot('files'), 'bytes': tot('bytes'), 'sources': json.load(open(os.path.join(HERE, 'sources.json'))), 'sheets': reps}
        add = [(json.dumps(man, indent=1).encode(), f'{product}/manifest.json')]
        if ts: add.insert(0, (json.dumps(ts).encode(), f'{product}/tileset.json'))
        batch_bucket_files(sheets.BUCKET, add=add); mans[product] = man
        print(json.dumps({k: man[k] for k in ('product', 'sheets_in_index', 'sheets_published', 'sheets_with_buildings', 'buildings', 'files', 'bytes')}))
    if card:
        api.create_repo(sheets.BUCKET, repo_type='dataset', exist_ok=True)
        from huggingface_hub import CommitOperationAdd
        ops = [CommitOperationAdd('README.md', os.path.join(HERE, 'hf', 'README.md'))] + \
              [CommitOperationAdd(f'{p}/manifest.json', json.dumps(m, indent=1).encode()) for p, m in mans.items()]
        api.create_commit(sheets.BUCKET, ops, repo_type='dataset', commit_message=f'card and manifests, {built}')

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    if not a: sys.exit(__doc__)
    os.environ.setdefault('WORK', tempfile.mkdtemp()); main(a, '--no-card' not in sys.argv)
