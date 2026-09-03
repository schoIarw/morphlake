#!/bin/bash
# ST2: pagination correctness on main service (37 assets, static data)
KEY=$(grep '^MORPHLAKE_API_KEY=' /Users/samuel/Documents/bench/trae/morphlake/.env.ollama | cut -d= -f2)
BASE=http://localhost:8080/api/v1/files

echo "== full set (limit=200) =="
curl -s -m 30 -H "X-API-Key: $KEY" "$BASE?limit=200&offset=0" > /tmp/st2-pg-full.json

collect_pages() {
  local L=$1; local N=$2; local OUT=$3
  > "$OUT"
  local off=0
  while : ; do
    curl -s -m 30 -H "X-API-Key: $KEY" "$BASE?limit=$L&offset=$off" > /tmp/st2-pg-page.json
    local cnt=$(python3 -c "import json;d=json.load(open('/tmp/st2-pg-page.json'));print(d.get('returned','-'))")
    [ "$cnt" = "-" ] && echo "PAGE ERROR at offset=$off" && break
    [ "$cnt" = "0" ] && break
    python3 -c "import json;d=json.load(open('/tmp/st2-pg-page.json'));[print(i['file_id']) for i in d['items']]" >> "$OUT"
    off=$((off+L))
    [ $off -ge $N ] && break
  done
  echo "pages walked until offset=$off"
}

echo "== walk limit=5 =="
collect_pages 5 60 /tmp/st2-pg-ids-5.txt
echo "== walk limit=10 =="
collect_pages 10 60 /tmp/st2-pg-ids-10.txt
echo "== walk limit=16 (non-divisor) =="
collect_pages 16 60 /tmp/st2-pg-ids-16.txt

python3 - <<'EOF'
import json
full=json.load(open('/tmp/st2-pg-full.json'))['items']
full_ids=[i['file_id'] for i in full]
print('full set:', len(full_ids), 'unique:', len(set(full_ids)))
for tag in ['5','10','16']:
    ids=[l.strip() for l in open(f'/tmp/st2-pg-ids-{tag}.txt') if l.strip()]
    s=set(ids)
    print(f'limit={tag}: fetched={len(ids)} unique={len(s)} dup={len(ids)-len(s)} missing_vs_full={len(set(full_ids)-s)} extra_vs_full={len(s-set(full_ids))}')
EOF
