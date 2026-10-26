"""Data-free checks for CI on every push (no downloads, no token): every script compiles, sources.json declares each source with
its licence and owner, the dataset card carries a licence link and names every owner, every workflow parses as YAML with its
triggers (an unquoted ': ' in a run line makes GitHub drop them silently), and the root-tileset builder's self-test passes.
Usage: python3 check.py"""
import glob, json, py_compile, re, subprocess, sys
import yaml

for p in glob.glob('*.py'): py_compile.compile(p, doraise=True)
src = json.load(open('sources.json')); card = open('hf/README.md', encoding='utf-8').read()
assert src, 'sources.json is empty'
for s in src:
    missing = {'name', 'owner', 'url', 'licence'} - s.keys()
    assert not missing, f'{s.get("name")}: missing {missing}'
    assert s['url'].startswith('https://') and s['licence'].startswith('https://'), f'{s["name"]}: url and licence must be https links'
    assert s['owner'] in card, f'hf/README.md does not credit {s["owner"]}'
assert re.search(r'^license_link: https://', card, re.M), 'hf/README.md has no license_link'
for w in glob.glob('.github/workflows/*.yml'):
    y = yaml.safe_load(open(w))                                        # YAML reads the key `on` as True
    assert isinstance(y, dict) and (y.get('on') or y.get(True)) and y.get('jobs'), f'{w}: no triggers or jobs (invalid workflow)'
for sh in glob.glob('*.sh') + glob.glob('deploy/*.sh') + glob.glob('.github/scripts/*.sh'): subprocess.run(['bash', '-n', sh], check=True)
subprocess.run(['node', '--check', 'bake.mjs'], check=True); subprocess.run(['node', '--check', 'indiv.mjs'], check=True)
import publish
t = publish.root_tileset('x', {'11-NW-19B': {'buildings': 2, 'region': [0, 0, 1, 1, 0, 9]}, '11-NW-19A': {'buildings': 0}, '12-SE-1A': {'buildings': 1, 'region': [2, 2, 3, 3, -1, 5]}})
assert [c['children'][0]['content']['uri'] for c in t['root']['children']] == ['11-NW-19B/tileset.json', '12-SE-1A/tileset.json'] and t['root']['boundingVolume']['region'] == [0, 0, 3, 3, -1, 9]
assert publish.root_tileset('x', {}) is None
import pages                                                          # shard map: stable, capacity-bound, headers within Pages limits
m = {'max_files': 10, 'products': {}}
r = lambda n: {'files': n}
assert pages.assign(m, 'p', {'a': r(6), 'b': r(3), 'c': r(5)}, ['c', 'a']) == 3
assert {n: list(d['sheets']) for n, d in m['products']['p'].items()} == {'01': ['c'], '02': ['a', 'b']}
assert pages.assign(m, 'p', {'a': r(6), 'b': r(3), 'c': r(5), 'd': r(2)}, ['c', 'a']) == 1
assert {n: list(d['sheets']) for n, d in m['products']['p'].items()} == {'01': ['c'], '02': ['a', 'b'], '03': ['d']}   # nothing moved; the full last shard got a new one
assert pages.HEADERS_SHARD.count('\n/') + 1 <= 100 and len(pages.HEADERS_SHARD) < 2000
n = {'uri': 'x.glb', 'children': [{'content': {'uri': 'sub/y.glb'}}]}; pages.rename_glbs(n, {'x.glb': 'x-1.glb', 'sub/y.glb': 'sub/y-2.glb'})
assert n['uri'] == 'x-1.glb' and n['children'][0]['content']['uri'] == 'sub/y-2.glb'
assert pages.project('indiv-k4', '01') == 'milleleaf-hkbt-indiv-k4-01' and len(pages.project('bake3dsd', '24')) < 58
print(f'check: ok ({len(src)} sources)')
