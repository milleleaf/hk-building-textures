"""One-off probe of Lands D (Helen approved it 2026-10-25): is a Range request into a zip past 2 GiB refused?
Two requests, no retries: a 1 KB control at a small offset, then 1 KB at a given offset, both on the same GLTF zip.
Prints status codes and Content-Range only, never the URL (it carries the key). On a 403 it leaves the stop marker and exits 76.
usage: LANDSD_KEY=... python3 probe.py <sheet> <control offset> <offset>"""
import json, os, sys, time, urllib.request
sheet, control, offset = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
url = f'https://download.map.gov.hk/api/3d-zip/GLTF/{sheet}.zip?key={os.environ["LANDSD_KEY"]}'

def one(off):
    req = urllib.request.Request(url, headers={'Range': f'bytes={off}-{off + 1023}', 'User-Agent': 'milleleaf-hk-building-textures'})
    try:
        r = urllib.request.urlopen(req, timeout=120); n = len(r.read()); return r.status, r.headers.get('Content-Range'), n
    except urllib.error.HTTPError as e: return e.code, None, 0
    except Exception as e: return type(e).__name__, None, 0       # not the message: it can carry the URL

for name, off in (('control', control), ('past 2 GiB', offset)):
    status, cr, n = one(off)
    print(f'{name}: offset {off} -> HTTP {status}, Content-Range {cr}, {n} bytes', flush=True)
    if status == 403:
        from huggingface_hub import batch_bucket_files
        batch_bucket_files('milleleaf/hk-building-textures', add=[(json.dumps({'url': f'https://download.map.gov.hk/api/3d-zip/GLTF/{sheet}.zip', 'probe_offset': off,
            'http403': 1, 'first_403_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'run': os.environ.get('GITHUB_RUN_ID'), 'settings': {'key': 'own', 'tool': 'probe'}}).encode(), 'stop/landsd-403.json')])
        print('403: stop marker written, no further request', flush=True); sys.exit(76)
