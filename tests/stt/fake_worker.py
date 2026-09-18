"""A stdio worker double for engine tests: no model, no GPU, no network."""
import base64
import json
import sys

print(json.dumps({'kind': 'ready', 'metadata': {'fake': True}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get('quit'):
        break
    if message.get('operation') == 'release_unused_cache':
        print(json.dumps({'kind': 'result', 'request': message['request'], 'text': ''}), flush=True)
        continue
    if message.get('operation') == 'draft_timestamps':
        print(json.dumps({'kind': 'draft_result', 'request': message['request'],
                          'draft': {'text': '天気予報', 'chars': [], 'review': 'model_estimate_only'}}), flush=True)
        continue
    pcm = base64.b64decode(message['audio'], validate=True)
    print(json.dumps({'kind': 'result', 'request': message['request'], 'text': '天気予報' if len(pcm) > 100 else ''}),
          flush=True)
