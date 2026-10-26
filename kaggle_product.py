"""Kaggle archive of one product: ONE public dataset per product (Helen, 2026-10-25), id <owner>/hk-building-textures-<product>,
owner from KAGGLE_OWNER (default helenkwok, Helen's one Kaggle account, as for hk-transit). Manual only (workflow `kaggle`); needs KAGGLE_API_TOKEN, never run by CI on a timer.
Layout on Kaggle: <map>/<sheet>/... where <map> is the 1:20,000 map number (1..17), so there are 17 top-level folders plus
tileset.json and manifest.json (Kaggle allows 50 top-level files; each folder goes up as a zip that Kaggle unpacks). The root
tileset.json is rebuilt with <map>/<sheet>/tileset.json uris. The bucket's failed-<run>.json attempt records are not mirrored.
A growing product: every run uploads EVERYTHING in the bucket manifest at that moment as a NEW VERSION of the same dataset (a Kaggle
version holds exactly the files of its upload folder; there is no per-folder update), so each run costs the product's whole size;
run it at milestones, not on a timer. It refuses to run on a product with no published sheet with buildings.
usage: python3 kaggle_product.py <product> <work dir> [--dry] [--sheets a,b,...]
       --dry: stage the folder and print what would be uploaded, no Kaggle call (needs no token); --sheets: stage only these (a test)"""
import json, os, shutil, sys, urllib.request
from huggingface_hub import sync_bucket
import kaggle_mirror, publish, sheets

OWNER = os.environ.get('KAGGLE_OWNER', 'helenkwok')
WHAT = {'indiv-k4': ("Lands Department's own Individualised models (one glTF per building, photo textures), textures re-encoded as KTX2 ETC1S at 1/4 size, "
                     "geometry meshopt-compressed, in 150 m cells (desktop)"),
        'bake3dsd': ("Lands Department's 3D Spatial Data building models, each with a texture atlas baked from the Open3Dhk photomesh at 20 cm texels "
                     "(KTX2 ETC1S, meshopt), about ten times smaller than indiv-k4 (phones and tablets)")}

def slug(product): return f'{OWNER}/hk-building-textures-{product}'

def intro(product, man):
    return (f"Milleleaf mirror of the `{product}` product of [milleleaf/hk-building-textures](https://github.com/milleleaf/hk-building-textures): "
            f"{WHAT[product]}. This version holds {man['sheets_with_buildings']} map sheets with buildings and {man['buildings']} buildings "
            f"(of {man['sheets_in_index']} sheets in Lands D's index), built {man['built']}.\n\n"
            f"**Layout here:** `<map>/<sheet>/tileset.json` plus one `.glb` per building and the sheet's `report.json`; `tileset.json` at the root "
            f"is the 3D Tiles 1.1 root tileset over all sheets, `manifest.json` lists every sheet's report. (On Hugging Face the same sheets sit at "
            f"`<sheet>/` without the `<map>/` level.) The layout, defects and terms below are from the Hugging Face card.\n\n---\n\n")

def stage(product, man, d, only=None):
    """download the published sheets with buildings into d/<map>/<sheet>/, write manifest.json and the root tileset.json; returns the reports used"""
    reps = {s: r for s, r in man['sheets'].items() if r.get('buildings') and r.get('region') and (not only or s in only)}
    if not reps: sys.exit(f'{product}: no published sheet with buildings to mirror')
    for s in sorted(reps):
        sync_bucket(f'hf://buckets/{sheets.BUCKET}/{product}/{s}', os.path.join(d, s.split('-')[0], s), exclude=['failed-*.json'], quiet=True)
    json.dump({**{k: v for k, v in man.items() if k != 'sheets'}, 'sheets': reps}, open(os.path.join(d, 'manifest.json'), 'w'), indent=1)
    json.dump(publish.root_tileset(product, reps, uri=lambda s: f"{s.split('-')[0]}/{s}/tileset.json"), open(os.path.join(d, 'tileset.json'), 'w'))
    return reps

def main(product, work, dry=False, only=None):
    if product not in WHAT: sys.exit(f'unknown product {product}')
    man = json.load(urllib.request.urlopen(f'https://huggingface.co/buckets/{sheets.BUCKET}/resolve/{product}/manifest.json', timeout=120))
    if not dry:                                                         # an owner that does not exist fails the create after the whole upload: say so first
        import subprocess
        if subprocess.run(['kaggle', 'datasets', 'list', '--user', OWNER, '--page-size', '1'], capture_output=True).returncode:
            print(f'WARNING: Kaggle does not know the owner {OWNER!r} (or the token is not valid); the create will fail unless it exists', file=sys.stderr, flush=True)
    d = os.path.join(work, product); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    reps = stage(product, man, d, only)
    top = sorted(os.listdir(d)); n = sum(len(f) for _, _, f in os.walk(d)); gb = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(d) for f in fs) / 1e9
    print(f'{product}: {len(reps)} sheets, {sum(r["buildings"] for r in reps.values())} buildings, {n} files, {gb:.2f} GB; top level: {top}', flush=True)
    assert len(top) < 50, 'Kaggle allows 50 top-level files'
    if dry: return
    os.environ.setdefault('KAGGLE_MESSAGE', f"{len(reps)} sheets, {sum(r['buildings'] for r in reps.values())} buildings, built {man['built']}")
    kaggle_mirror.mirror(slug(product), f'Milleleaf HK building textures: {product}', d, top, description=intro(product, man) + kaggle_mirror.card_body())
    shutil.rmtree(d, ignore_errors=True)

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--') and x != (sys.argv[sys.argv.index('--sheets') + 1] if '--sheets' in sys.argv else None)]
    if len(a) != 2: sys.exit(__doc__)
    main(a[0], a[1], '--dry' in sys.argv, set(sys.argv[sys.argv.index('--sheets') + 1].split(',')) if '--sheets' in sys.argv else None)
