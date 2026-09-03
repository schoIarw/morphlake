#!/usr/bin/env python3
"""ST2-11b: extended probe - upload idx-027..idx-110 (84 files, ~100s window, >=3 index cycles)."""
import time, json, statistics, http.client, uuid, subprocess

KEY = subprocess.run("grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2",
                    shell=True, capture_output=True, text=True).stdout.strip()

def upload(fn: str) -> tuple[float, int]:
    boundary = uuid.uuid4().hex
    body = b''
    for name, val in [('business_domain','st2idx'),('department','qa')]:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n').encode()
    data = open(fn,'rb').read()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn.split("/")[-1]}"\r\n'
             f'Content-Type: text/plain\r\n\r\n').encode() + data + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    conn = http.client.HTTPConnection('localhost', 8080, timeout=900)
    t0 = time.time()
    conn.request('POST', 'http://localhost:8080/api/v1/files/documents', body,
                 {'X-API-Key': KEY, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    resp = conn.getresponse(); resp.read()
    dt = time.time() - t0
    conn.close()
    return dt, resp.status

files = [f'/tmp/morphlake-test/st2idx/idx-{i:03d}.txt' for i in range(27, 111)]
results = []
t_start = time.time()
for i, f in enumerate(files):
    dt, code = upload(f)
    wall = time.time() - t_start
    results.append({'i': i+27, 'latency_s': round(dt,3), 'http': code, 'wall_s': round(wall,3)})
    if dt > 1.2:
        print(f">>> SPIKE idx-{i+27:03d} lat={dt:.3f}s wall={wall:.1f}s", flush=True)
    time.sleep(0.6)

json.dump(results, open('/tmp/st2idx-results-b.json','w'), indent=1)
lat = [r['latency_s'] for r in results if r['http'] in (200,201)]
codes = {}
for r in results: codes[r['http']] = codes.get(r['http'],0)+1
print('codes:', codes, f'wall={time.time()-t_start:.1f}s n={len(lat)}')
print(f'avg={statistics.mean(lat):.3f}s median={statistics.median(lat):.3f}s max={max(lat):.3f}s p95={sorted(lat)[int(0.95*len(lat))-1]:.3f}s')
med = statistics.median(lat)
spikes = [(r['i'], r['latency_s'], r['wall_s']) for r in results if r['latency_s'] > 2*med]
print(f'spikes(>2x median {med:.3f}s):')
for s in spikes: print('  idx-%03d lat=%.3fs wall=%.1fs' % s)
