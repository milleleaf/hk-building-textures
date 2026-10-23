"""Kaggle archive of one product, one dataset per 1:5000 map-sheet group (e.g. 11-NW, up to 100 map sheets): Kaggle allows 200 GB
per dataset and a runner has ~70 GB of disk, and a group is a small fraction of either (sizes: the product manifest).
Downloads <product>/<sheet>/ for the group's published sheets from the public bucket, adds the group's manifest entries, and
uploads them with kaggle_mirror.py (the same file as every Milleleaf pipeline) to helenkwok/hk-building-textures-<product>-<group>,
as one top-level folder <product>/ (Kaggle unzips it; it allows 50 top-level files). Needs KAGGLE_API_TOKEN (Helen's account).
usage: python3 kaggle_group.py <product> <group|all> <work dir>"""
import json, os, shutil, sys, urllib.request
from huggingface_hub import sync_bucket
import kaggle_mirror, sheets

def groups(man): return sorted({'-'.join(s.split('-')[:2]) for s, r in man['sheets'].items() if r.get('buildings')})

def mirror(product, group, man, work):
    d = os.path.join(work, group); shutil.rmtree(d, ignore_errors=True)
    sub = {s: r for s, r in man['sheets'].items() if r.get('buildings') and s.startswith(group + '-')}
    for s in sorted(sub): sync_bucket(f'hf://buckets/{sheets.BUCKET}/{product}/{s}', os.path.join(d, product, s), quiet=True)
    json.dump({**{k: v for k, v in man.items() if k != 'sheets'}, 'group': group, 'sheets': sub}, open(os.path.join(d, product, 'manifest.json'), 'w'), indent=1)
    kaggle_mirror.mirror(f'helenkwok/hk-building-textures-{product}-{group.lower()}', f'Milleleaf HK building textures {product} {group}', d, [product])
    shutil.rmtree(d, ignore_errors=True)

if __name__ == '__main__':
    if len(sys.argv) != 4: sys.exit(__doc__)
    product, which, work = sys.argv[1:]
    man = json.load(urllib.request.urlopen(f'https://huggingface.co/buckets/{sheets.BUCKET}/resolve/{product}/manifest.json', timeout=120))
    for g in (groups(man) if which == 'all' else which.split(',')): mirror(product, g, man, work)
