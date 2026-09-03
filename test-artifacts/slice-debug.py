from pypaimon.multimodal import connect
conn = connect(database='morphlake', options={'warehouse':'s3://morphlake-paimon/warehouse','s3.endpoint':'http://host.docker.internal:9000','s3.access-key':'minioadmin','s3.secret-key':'minioadmin','s3.path-style-access':'true'})
t = conn.get_table('multimodal_asset_descriptor_ollama').raw_table

def read_n(n):
    rb = t.new_read_builder().with_projection(['file_id','created_at']).with_limit(n)
    return rb.new_read().to_arrow(rb.new_scan().plan().splits()).to_pylist()

for n in [5, 10]:
    rows = read_n(n)
    print(f'--- with_limit({n}) raw rows={len(rows)} ---')
    for r in rows: print('  raw:', r['file_id'][:8], str(r['created_at'])[:19])
    rows.sort(key=lambda row: (row['created_at'], row['file_id']), reverse=True)
    print(f'  sorted:')
    for r in rows: print('  srt:', r['file_id'][:8], str(r['created_at'])[:19])
    print(f'  slice[5:10] =', [(r['file_id'][:8], str(r['created_at'])[:19]) for r in rows[5:10]])
    print(f'  slice[0:5]  =', [(r['file_id'][:8], str(r['created_at'])[:19]) for r in rows[0:5]])
    print(f'  type rows: {type(rows)}')
