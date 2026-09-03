#!/bin/bash
# ST2: small concurrency baseline - 12 docs, 4 parallel uploads via /api/v1/files/documents
KEY=$(grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2)
BASE=http://localhost:8080
D=/tmp/morphlake-test/st2-conc
OUT=/tmp/st2-conc-results.txt
> $OUT

upload_one() {
  local f=$1
  local r=$(curl -s -m 900 -o /tmp/st2-conc-resp-$$.json -w "%{time_total} %{http_code}" \
    -X POST "$BASE/api/v1/files/documents" \
    -H "X-API-Key: $KEY" \
    -F 'business_domain=st2' -F 'department=qa' \
    -F "file=@$f")
  local t=$(echo $r | cut -d' ' -f1); local code=$(echo $r | cut -d' ' -f2)
  local fid=$(python3 -c "import json;d=json.load(open('/tmp/st2-conc-resp-$$.json'));print(d.get('file_id','-'))" 2>/dev/null || echo -)
  echo "$(basename $f) $t $code $fid" >> $OUT
  rm -f /tmp/st2-conc-resp-$$.json
}
export -f upload_one
export KEY BASE OUT

START=$(date +%s.%N)
echo "start: $(date -u +%H:%M:%S.%3N)"
ls $D/st2-doc-*.txt | xargs -P 4 -I{} bash -c 'upload_one "$@"' _ {}
END=$(date +%s.%3N 2>/dev/null || date +%s)
wait
echo "end: $(date -u +%H:%M:%S)"
sort $OUT
python3 - <<'EOF'
import re
rows=[l.split() for l in open('/tmp/st2-conc-results.txt') if l.strip()]
ts=[float(r[1]) for r in rows]
codes=[r[2] for r in rows]
ok=[t for t,c in zip(ts,codes) if c=='200']
ts_sorted=sorted(ok)
n=len(ts)
print(f'total={n} ok={len(ok)} fail={n-len(ok)} codes={set(codes)}')
if ok:
    import statistics
    print(f'avg={statistics.mean(ok):.2f}s min={min(ok):.2f}s max={max(ok):.2f}s p95={ts_sorted[int(0.95*len(ts_sorted))-1 if len(ts_sorted)>1 else 0]:.2f}s')
    print(f'sum_serial_time={sum(ok):.1f}s aggregate_throughput={len(ok)/sum(ok):.3f} files/s')
EOF
