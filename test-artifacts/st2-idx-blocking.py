#!/usr/bin/env python3
"""ST2-11: upload latency vs index maintenance cycle (30s period).
Serially upload 26 small docs over >60s window covering >=2 index cycles,
timestamping each upload precisely, then look for periodic latency spikes."""
import time, json, subprocess, sys, statistics

KEY = subprocess.run("grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2",
                    shell=True, capture_output=True, text=True).stdout.strip()
BASE = "http://localhost:8080/api/v1/files/documents"
import urllib.request, multipart  # noqa

import http.client, uuid

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
    conn.request('POST', BASE, body,
                 {'X-API-Key': KEY, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    resp = conn.getresponse(); resp.read()
    dt = time.time() - t0
    conn.close()
    return dt, resp.status

import os
os.makedirs('/tmp/morphlake-test/st2idx', exist_ok=True)
files = []
for i in range(1, 27):
    p = f'/tmp/morphlake-test/st2idx/idx-{i:02d}.txt'
    open(p,'w').write(f'morphlake st2idx index maintenance blocking probe {i:02d}. ' + 'Index maintenance interplay measurement text. ' * 60)
    files.append(p)

results = []
t_start = time.time()
for i, f in enumerate(files):
    dt, code = upload(f)
    wall = time.time() - t_start
    results.append({'i': i, 'file': f.split('/')[-1], 'latency_s': round(dt,3), 'http': code, 'wall_s': round(wall,3)})
    print(f"{i:02d} {f.split('/')[-1]} lat={dt:.3f}s http={code} wall={wall:.1f}s", flush=True)
    time.sleep(0.6)

json.dump(results, open('/tmp/st2idx-results.json','w'), indent=1)
lat = [r['latency_s'] for r in results if r['http'] in (200,201)]
codes = {}
for r in results: codes[r['http']] = codes.get(r['http'],0)+1
print('codes:', codes)
print(f'total files={len(results)} ok={len(lat)} wall={time.time()-t_start:.1f}s')
if lat:
    print(f'avg={statistics.mean(lat):.3f}s min={min(lat):.3f}s max={max(lat):.3f}s p95={sorted(lat)[int(0.95*len(lat))-1]:.3f}s')
    # spike detection: latency > 2x median
    med = statistics.median(lat)
    spikes = [(r['i'], r['latency_s'], r['wall_s']) for r in results if r['latency_s'] > 2*med]
    print(f'median={med:.3f}s, spikes(>2x median): {spikes}')
