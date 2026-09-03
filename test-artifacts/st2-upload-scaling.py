#!/usr/bin/env python3
"""ST2-13: upload concurrency scaling - same 8 docs (4.6KB) at 1c/4c/8c, measure per-file latency & throughput."""
import time, json, statistics, http.client, uuid, subprocess, os, concurrent.futures

KEY = subprocess.run("grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2",
                    shell=True, capture_output=True, text=True).stdout.strip()

os.makedirs('/tmp/morphlake-test/st2scale', exist_ok=True)
files = []
for i in range(1, 25):
    p = f'/tmp/morphlake-test/st2scale/scale-{i:02d}.txt'
    open(p,'w').write(f'morphlake st2 scale concurrency probe {i:02d}. ' + 'Upload concurrency scaling baseline text. ' * 80)
    files.append(p)

def upload(fn):
    boundary = uuid.uuid4().hex
    body = b''
    for name, val in [('business_domain','st2scale'),('department','qa')]:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n').encode()
    data = open(fn,'rb').read()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn.split("/")[-1]}"\r\n'
             f'Content-Type: text/plain\r\n\r\n').encode() + data + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    conn = http.client.HTTPConnection('localhost', 8080, timeout=900)
    t0 = time.time()
    conn.request('POST', 'http://localhost:8080/api/v1/files/documents', body,
                 {'X-API-Key': KEY, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    r = conn.getresponse(); r.read()
    dt = time.time() - t0
    conn.close()
    return dt, r.status

def run_batch(n_conc, batch_files, tag):
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_conc) as ex:
        results = list(ex.map(upload, batch_files))
    wall = time.time() - t0
    lat = [r[0] for r in results]
    codes = {}
    for _, c in results: codes[c] = codes.get(c,0)+1
    ok = sum(1 for _,c in results if c in (200,201))
    print(f'{tag}: n={len(batch_files)} conc={n_conc} wall={wall:.1f}s '
          f'throughput={ok/wall:.2f} files/s avg_lat={statistics.mean(lat):.2f}s '
          f'max_lat={max(lat):.2f}s codes={codes}', flush=True)
    return wall

run_batch(1, files[0:8],  'batch1')
run_batch(4, files[8:16], 'batch4')
run_batch(8, files[16:24], 'batch8')
