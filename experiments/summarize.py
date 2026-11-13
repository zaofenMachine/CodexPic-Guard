"""Export allowlisted research numbers, never image data or raw client logs."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def summarize(mock, retention, live_roots):
    mock_report = load(mock)
    retention_report = load(retention)
    rows = []
    for root in live_roots:
        for arm in load(Path(root) / 'summary.json')['arms']:
            row = {k: arm[k] for k in (
                'arm', 'model', 'passed', 'generation_requests', 'request_payload_bytes',
                'image_base64_bytes', 'seconds', 'usage', 'global_configuration_unchanged')}
            row['visual_codes_correct'] = sum(c['correct'] for c in arm['visual_calls'])
            row['visual_tool_calls'] = len(arm['visual_calls'])
            row['request_shapes'] = arm['requests']
            row['response_usage'] = [r['usage'] for r in arm['responses'] if r.get('usage')]
            # Older raw reports included per-item attribution; never publish IDs.
            row['response_usage'] = [{k: v for k, v in u.items() if k in (
                'input_tokens', 'input_tokens_details', 'output_tokens', 'output_tokens_details', 'total_tokens')}
                for u in row['response_usage']]
            for key in ('guard_recorded_statuses', 'guard_pending_images'):
                if key in arm:
                    row[key] = arm[key]
            rows.append(row)
    return {
        'measurement_date': '2026-11-14', 'codex_cli': mock_report['codex_version'],
        'units': 'bytes (decimal MB in prose); application payload, not TLS/TCP/VPN billing',
        'mock_transport': mock_report,
        'controlled_retention': retention_report,
        'live_pilot': rows,
        'limits': [
            'One synthetic three-image workflow; sequential nonrandomized arms, not a cost benchmark.',
            'Cache availability, implicit breakpoints, tool discovery and outputs vary between runs.',
            'HTTP payloads use zstd; WebSocket messages use identity without negotiated message compression.',
            'CLI turn.completed token totals exclude WebSocket prewarm and do not establish ChatGPT quota cost.',
            'Guard-http pilot used five generation requests; other successful arms used four.',
            'Two setup attempts failed: MCP process import (zero model calls), then missing read-only tool annotation (two model calls). They are excluded from successful comparisons.',
            'Historical MV data and raw logs are not part of this export.',
        ],
    }


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mock', required=True); p.add_argument('--retention', required=True)
    p.add_argument('--live', nargs='+', required=True); p.add_argument('--out', required=True)
    a = p.parse_args()
    result = summarize(a.mock, a.retention, a.live)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'live_arms': len(result['live_pilot']), 'output_bytes': out.stat().st_size}))
