#!/usr/bin/env python3
"""ST2-12: retrieval concurrency baseline - 8 parallel, mixed full-text + vector, 5 rounds each."""
import time, json, statistics, http.client, subprocess

KEY = subprocess.run("grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2",
                    shell=True, capture_output=True, text=True).stdout.strip()

def post(path, payload):
    conn = http.client.HTTPConnection('localhost', 8080, timeout=120)
    t0 = time.time()
    conn.request('POST', path, json.dumps(payload),
                 {'X-API-Key': KEY, 'Content-Type': 'application/json'})
    r = conn.getresponse(); body = r.read()
    dt = time.time() - t0
    conn.close()
    return dt, r.status, body

# warm vector
_, _, body = post('/api/v1/search/vector', {'business_domain':'st1','vector':[0.1]*768,'vector_field':'text','limit':10})
vec = json.loads(open('/tmp/st2-vec.json').read()) if __import__('os').path.exists('/tmp/st2-vec.json') else None

kw_list = ['morphlake','alice','readme','airtravel','test','probe','baseline','dummy']
vec_fields = ['text','image','audio']

ft_lat, vs_lat = [], []
fails = []

for rnd in range(5):
    # full-text x8
    conns_results = []
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(post, '/api/v1/search/full-text',
                          {'business_domain':'st1','keyword':kw_list[i%8],'limit':10}) for i in range(8)]
        for f in futs:
            dt, code, body = f.result()
            ft_lat.append(dt)
            if code != 200: fails.append(('full-text', code))
    # vector x8
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(post, '/api/v1/search/vector',
                          {'business_domain':'st1','vector':[0.05*(i+1)]*768,'vector_field':'text','limit':10}) for i in range(8)]
        for f in futs:
            dt, code, body = f.result()
            vs_lat.append(dt)
            if code != 200: fails.append(('vector', code))
    print(f'round {rnd}: ft_last={[round(x,3) for x in ft_lat[-8:]]} vs_last={[round(x,3) for x in vs_lat[-8:]]}', flush=True)

def stats(name, lat):
    lat_s = sorted(lat)
    print(f'{name}: n={len(lat)} avg={statistics.mean(lat):.3f}s p50={lat_s[len(lat)//2]:.3f}s p95={lat_s[int(0.95*len(lat))-1]:.3f}s max={max(lat):.3f}s')
stats('full-text 8c', ft_lat)
stats('vector   8c', vs_lat)
print('failures:', fails if fails else 'none')
