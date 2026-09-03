import json
from pypaimon.multimodal import connect
conn = connect(database='morphlake', options={'warehouse':'s3://morphlake-paimon/warehouse','s3.endpoint':'http://host.docker.internal:9000','s3.access-key':'minioadmin','s3.secret-key':'minioadmin','s3.path-style-access':'true'})
t = conn.get_table('multimodal_asset_descriptor_ollama').raw_table

def read_n(n):
    rb = t.new_read_builder().with_projection(['file_id','created_at']).with_limit(n)
    rows = rb.new_read().to_arrow(rb.new_scan().plan().splits()).to_pylist()
    return [(r['file_id'][:8], str(r['created_at'])[:19]) for r in rows]

for n in [5, 10, 40, 100, 400]:
    rows = read_n(n)
    print(f'with_limit({n}): got={len(rows)}')
    if n <= 10:
        for r in rows: print('   ', r)
    else:
        dates = sorted({r[1][:10] for r in rows})
        print('    date range:', dates)
        top = sorted(rows, key=lambda r: r[1], reverse=True)[:3]
        print('    newest 3 after sort:', top)
