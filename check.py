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
import zipcut, urllib.error, io                                       # Lands D 403s any Range request that crosses byte 2^31 of a zip (probed 2026-10-28)
B31, seen = 1 << 31, []
def fake_urlopen(req, timeout=0):
    a, b = (int(x) for x in req.headers['Range'].split('=')[1].split('-')); seen.append((a, b))
    if a // B31 != b // B31: raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, io.BytesIO(b''))
    return type('R', (), {'status': 206, 'headers': {}, 'read': lambda self: bytes((a + k) % 251 for k in range(b - a + 1))})()
zipcut.urllib.request.urlopen, zipcut.TRIES, zipcut.PAUSE, zipcut.backoff = fake_urlopen, 1, 0, lambda i, e: None
for a, b in ((B31 - 100, B31 + 99), (2 * B31 - 7, 2 * B31 + 8), (B31, B31 + 50), (B31 - 60, B31 - 1), (0, 99)):
    seen.clear(); got = zipcut.rng('https://example.invalid/z.zip', a, b)
    assert got == bytes(k % 251 for k in range(a, b + 1)), (a, b)          # the right bytes, in order
    assert all(x // B31 == y // B31 for x, y in seen), (a, b, seen)        # and no request crossed a multiple of 2^31
assert len(seen) == 1 and zipcut.rng('https://example.invalid/z.zip', B31 - 100, B31 + 99) and len(seen) == 3       # a straddling span is two requests
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

# adaptive pace (zipcut.Pace): ramps up on good requests, halves on a slow or retried one, never leaves 1..ceiling, and a gate holds the allowance
p = zipcut.Pace(6, 0.5)
assert p.cur == 2
for _ in range(8 * 5): p.ok(8_000_000, 1.0)                                  # steady 8 MB/s: +1 every 8 requests up to the ceiling
assert p.cur == 6 and p.pause == 0.5 and p.stats['speedups'] == 4
p.ok(8_000_000, 40.0); assert p.cur == 3 and p.pause == 1.0                  # one request at under 40% of the best speed: halve, double the pause
p.slow(); assert p.cur == 1 and p.pause == 2.0; p.slow(); assert p.cur == 1 and p.pause == 4.0
for _ in range(60): p.ok(8_000_000, 1.0)                                      # recovery: pause eases back, allowance climbs again
assert p.cur >= 5 and p.pause == 0.5, (p.cur, p.pause)
q = zipcut.Pace(4, 0, on=False); q.slow(); q.ok(1, 100); assert q.cur == 4 and q.stats['slowdowns'] == 0                # ZIP_ADAPTIVE=0: fixed pace
import threading, time as _t                                                   # the gate: never more than `cur` requests inside at once
g = zipcut.Pace(3, 0); g.cur = 2; inside, peak, lock = [0], [0], threading.Lock()
def work():
    g.enter()
    with lock: inside[0] += 1; peak[0] = max(peak[0], inside[0])
    _t.sleep(0.05)
    with lock: inside[0] -= 1
    g.leave()
ts = [threading.Thread(target=work) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
assert peak[0] == 2, peak
print('adaptive pace checks passed')

# early publish (sheet.py): only buildings whose .gltf AND every file it names are on disk count as complete; a partial sheet is listed in the root tileset
import os, tempfile, sheet as _sheet
with tempfile.TemporaryDirectory() as t:
    def bld(i, gltf, files=()):
        os.makedirs(os.path.join(t, 'BUILDING', i)); open(os.path.join(t, 'BUILDING', i, f'{i}.gltf'), 'w').write(gltf)
        for f in files: open(os.path.join(t, 'BUILDING', i, f), 'wb').write(b'x')
    bld('A', json.dumps({'images': [{'uri': 'a.png'}], 'buffers': [{'uri': 'a.bin'}]}), ['a.png', 'a.bin'])        # complete
    bld('B', json.dumps({'images': [{'uri': 'b.png'}], 'buffers': [{'uri': 'b.bin'}]}), ['b.bin'])                   # texture not fetched yet
    bld('C', json.dumps({'buffers': [{'uri': 'data:application/octet-stream;base64,AAAA'}]}))                       # embedded data only: complete
    bld('D', '{not json')                                                                                            # unreadable
    os.makedirs(os.path.join(t, 'BUILDING', 'E'))                                                                    # no .gltf yet
    assert _sheet.complete_buildings(t) == ['A', 'C'], _sheet.complete_buildings(t)
    assert _sheet.complete_buildings(os.path.join(t, 'none')) == []
ts = publish.root_tileset('indiv-k4', {'6-NE-1A': {'buildings': 3, 'region': [2.0, 0.39, 2.01, 0.4, 0, 100], 'partial': True}, '6-NE-1B': {'buildings': 0}})
assert ts and ts['root']['children'][0]['children'][0]['content']['uri'] == '6-NE-1A/tileset.json' and len(ts['root']['children'][0]['children']) == 1
print('early publish checks passed')
