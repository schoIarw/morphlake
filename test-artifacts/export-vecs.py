import json
from pypaimon.multimodal import connect
conn = connect(database='morphlake', options={'warehouse':'s3://morphlake-paimon/warehouse','s3.endpoint':'http://host.docker.internal:9000','s3.access-key':'minioadmin','s3.secret-key':'minioadmin','s3.path-style-access':'true'})
img = conn.get_table('multimodal_image_feature_ollama').scan().to_arrow().to_pylist()
aud = conn.get_table('multimodal_audio_feature_ollama').scan().to_arrow().to_pylist()
st1_img = [r for r in img if r['business_domain']=='st1']
st1_aud = [r for r in aud if r['business_domain']=='st1']
print('st1 image features:', len(st1_img), 'st1 audio features:', len(st1_aud))
r = st1_img[0]
print('sample img:', r['file_id'][:8], r['filename'], 'model:', r['embedding_model'], 'dim:', len(r['image_embedding']))
open('/tmp/img-vec.json','w').write(json.dumps({'vector': r['image_embedding'], 'file_id': r['file_id'], 'filename': r['filename']}))
r = st1_aud[0]
print('sample aud:', r['file_id'][:8], r['filename'], 'dim:', len(r['audio_embedding']))
open('/tmp/aud-vec.json','w').write(json.dumps({'vector': r['audio_embedding'], 'file_id': r['file_id'], 'filename': r['filename']}))
