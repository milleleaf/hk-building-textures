"""One-off probe of Lands D (Helen approved it): which 1 KB Range requests does it refuse on one GLTF zip?
No retries. Prints status codes and Content-Range only, never the URL (it carries the key). Each offset is one request for bytes
off..off+1023, in the order given. With PROBE_MARKER=1 (default) a 403 leaves the stop marker and ends the run (exit 76); with
PROBE_MARKER=0 every offset is tried, the marker is left alone, and the run exits 0.
usage: LANDSD_KEY=... python3 probe.py <sheet> <offset>[,<offset>...]"""
import json, os, sys, time, urllib.request
sheet, offsets = sys.argv[1], [int(x) for x in sys.argv[2].split(',')]
url = f'https://download.map.gov.hk/api/3d-zip/GLTF/{sheet}.zip?key={os.environ["LANDSD_KEY"]}'
marker = os.environ.get('PROBE_MARKER', '1') == '1'

def one(off):
    req = urllib.request.Request(url, headers={'Range': f'bytes={off}-{off + 1023}', 'User-Agent': 'milleleaf-hk-building-textures'})
    try:
        r = urllib.request.urlopen(req, timeout=120); n = len(r.read()); return r.status, r.headers.get('Content-Range'), n
    except urllib.error.HTTPError as e: return e.code, None, 0
    except Exception as e: return type(e).__name__, None, 0       # not the message: it can carry the URL

for off in offsets:
    status, cr, n = one(off)
    print(f'offset {off} (bytes {off}-{off + 1023}) -> HTTP {status}, Content-Range {cr}, {n} bytes', flush=True)
    if status == 403 and marker:
        from huggingface_hub import batch_bucket_files
        batch_bucket_files('milleleaf/hk-building-textures', add=[(json.dumps({'url': f'https://download.map.gov.hk/api/3d-zip/GLTF/{sheet}.zip', 'probe_offset': off,
            'http403': 1, 'first_403_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'run': os.environ.get('GITHUB_RUN_ID'), 'settings': {'key': 'own', 'tool': 'probe'}}).encode(), 'stop/landsd-403.json')])
        print('403: stop marker written, no further request', flush=True); sys.exit(76)
