"""Offline measurement of the final active window in a Codex JSONL(.zst).

Never sends a request. Report contains sizes/counts only. Original images are
cached in the explicitly supplied --cache directory for local rehydration.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import time
import zstandard
from codexpic_guard.config import Policy
from codexpic_guard.proxy import pack
from codexpic_guard.rewrite import rewrite,refs_in
from codexpic_guard.store import Store


def main():
    p=argparse.ArgumentParser();p.add_argument('--rollout',type=Path,required=True);p.add_argument('--cache',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    history=[]
    with a.rollout.open('rb') as raw:
        reader=io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(raw)) if a.rollout.suffix=='.zst' else raw
        for b in reader:
            record=json.loads(b)
            if record.get('type')=='response_item':
                item=dict(record['payload']);item.pop('internal_chat_message_metadata_passthrough',None)
                history.append(item)
            elif record.get('type')=='compacted':history=list(record['payload'].get('replacement_history') or [])
    body={'model':'offline-replay-no-network','input':history,'stream':True}
    before=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
    before_wire=pack(before,'zstd');store=Store(a.cache,Policy());cache_was_populated=store.status()['images']>0;started=time.perf_counter()
    result,scope,stats=rewrite(body,{'thread-id':'offline-mv-replay'},store,Policy());elapsed=time.perf_counter()-started
    after=json.dumps(result,ensure_ascii=False,separators=(',',':')).encode();after_wire=pack(after,'zstd')
    result_summary={'mode':'offline replay; no HTTP request sent','source_rollout_bytes':a.rollout.stat().st_size,
                    'input_bytes':len(before),'output_bytes':len(after),'zstd_input_bytes':len(before_wire),'zstd_output_bytes':len(after_wire),
                    'zstd_reduction_percent':100*(1-len(after_wire)/len(before_wire)),
                    'image_counts':{k:stats[k] for k in ('images_before','images_after','optimized','dropped','deduplicated')},
                    'media_base64_before_bytes':stats['media_before_bytes'],'media_base64_after_bytes':stats['media_after_bytes'],
                    'rewrite_seconds':round(elapsed,3),'cache_was_populated':cache_was_populated,'cache_bytes':store.status()['blob_bytes'],
                    'limits':'Reconstructed active window differs from the historical exact API body. These are same-payload offline before/after measurements, not VPN billing or live model quality results.'}
    # Check two originals through the same retrieval path used by MCP, without exporting pixels.
    entries=store.list_images(scope,100);checked=0
    for entry in entries[:2]:
        image=store.get(scope,entry['id'],original=True)
        assert hashlib.sha256(image.data).hexdigest()==entry['id'][6:];checked+=1
    result_summary['original_hash_checks']=checked
    a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(result_summary,indent=2),encoding='utf-8')
    print(json.dumps(result_summary,indent=2))


if __name__=='__main__':main()
