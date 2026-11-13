"""Offline monotonic-history experiment; the sticky wrapper is not production.

Counts byte payloads and changed image slots. Neither item-prefix comparison nor
zstd size measures token caching. Pin/rehydration/compaction policy is not modeled.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from codexpic_guard.config import Policy
from codexpic_guard.proxy import pack
from codexpic_guard.rewrite import rewrite
from codexpic_guard.store import Store
from tests.helpers import picture, image_block


class StickyPrototype:
    """Persist omitted occurrences for an append-only synthetic trace only."""
    def __init__(self):
        self.omitted = {}

    def apply(self, body, store, policy):
        for item in body['input']:
            if item.get('type') == 'function_call_output' and item['call_id'] in self.omitted:
                item['output'] = copy.deepcopy(self.omitted[item['call_id']])
        out, scope, stats = rewrite(body, {'thread-id': 'retention-probe'}, store, policy)
        for item in out['input']:
            if item.get('type') == 'function_call_output' and item['output'][0]['type'] == 'input_text':
                self.omitted[item['call_id']] = copy.deepcopy(item['output'])
        return out, scope, stats


def trace(count=40):
    images = [picture(i, (100, 100)) for i in range(count)]
    history = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Inspect a growing sequence of synthetic frames.'}]}]
    for i, raw in enumerate(images):
        history.extend([
            {'type': 'function_call', 'call_id': f'frame_{i}', 'name': 'view_image', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': f'frame_{i}', 'output': [image_block(raw)]},
        ])
        yield {'model': 'offline-only', 'input': copy.deepcopy(history)}


def measure(policy, count):
    results = []
    for mode in ('per-request-selection', 'sticky-selection'):
        rows = []; previous = []; previous_images = {}; sticky = StickyPrototype()
        with tempfile.TemporaryDirectory(prefix='codexpic-retention-') as d:
            store = Store(d, policy)
            for step, body in enumerate(trace(count), 1):
                if mode == 'per-request-selection':
                    out, scope, stats = rewrite(body, {'thread-id': 'retention-probe'}, store, policy)
                else:
                    out, scope, stats = sticky.apply(body, store, policy)
                # Control note placement in both arms so only retention differs.
                for i, item in enumerate(out['input']):
                    if item.get('role') == 'developer' and any(
                        c.get('text', '').startswith('CodexPic local image-cache scope:')
                        for c in item.get('content', [])
                    ):
                        out['input'].insert(0, out['input'].pop(i))
                        break
                hashes = [hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest() for x in out['input']]
                common = 0
                for before, after in zip(previous, hashes):
                    if before != after:
                        break
                    common += 1
                images = {x['call_id']: x['output'][0]['type'] for x in out['input'] if x.get('type') == 'function_call_output'}
                changed = sum(previous_images[k] != images[k] for k in previous_images)
                raw = json.dumps(out, ensure_ascii=False, separators=(',', ':')).encode()
                rows.append({'step': step, 'images_sent': stats['images_after'],
                             'image_base64_bytes': stats['media_after_bytes'],
                             'zstd_payload_bytes': len(pack(raw, 'zstd')),
                             'previous_image_slots_changed': changed,
                             'prior_input_is_prefix': bool(previous) and common == len(previous),
                             'common_input_items': common})
                store.delivered(scope, stats['retained_ids'])
                previous = hashes; previous_images = images
        results.append({'mode': mode, 'requests': len(rows),
                        'image_slot_change_events': sum(r['previous_image_slots_changed'] > 0 for r in rows),
                        'changed_image_slots': sum(r['previous_image_slots_changed'] for r in rows),
                        'prefix_preserved_transitions': sum(r['prior_input_is_prefix'] for r in rows),
                        'zstd_payload_bytes': sum(r['zstd_payload_bytes'] for r in rows),
                        'image_base64_bytes': sum(r['image_base64_bytes'] for r in rows),
                        'max_image_base64_bytes': max(r['image_base64_bytes'] for r in rows),
                        'rows': rows})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    policy = Policy(media_high_bytes=320000, media_low_bytes=160000, max_images=50, keep_recent=1)
    report = {'kind': 'offline synthetic monotonic history; identical fixed scope note in both arms', 'token_usage_measured': False,
              'policy': {'high_bytes': policy.media_high_bytes, 'low_bytes': policy.media_low_bytes,
                         'keep_recent': policy.keep_recent}, 'arms': measure(policy, 40)}
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SystemExit('Refusing to overwrite an existing report')
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({**report, 'arms': [{k: v for k, v in a.items() if k != 'rows'} for a in report['arms']]}, indent=2))
