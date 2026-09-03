from pypaimon.multimodal import connect
conn = connect(database='morphlake', options={'warehouse':'s3://morphlake-paimon/warehouse','s3.endpoint':'http://host.docker.internal:9000','s3.access-key':'minioadmin','s3.secret-key':'minioadmin','s3.path-style-access':'true'})
t = conn.get_table('multimodal_asset_descriptor_ollama').raw_table

def app_list(limit, offset):
    # replicate service list_assets without filters
    rb = t.new_read_builder().with_projection(['file_id','filename','created_at']).with_limit(limit + offset)
    rows = rb.new_read().to_arrow(rb.new_scan().plan().splits()).to_pylist()
    rows.sort(key=lambda row: (row['created_at'], row['file_id']), reverse=True)
    return rows[offset : offset + limit]

for off in [0, 5, 10, 15, 35]:
    page = app_list(5, off)
    print(f'limit=5 offset={off}: got={len(page)}', [(r['file_id'][:8], str(r['created_at'])[:19]) for r in page][:5])
