"""Bounded recognition of Responses terminal events in identity-encoded SSE."""
import json


class ResponseCompletion:
    def __init__(self, limit=4 * 1024 * 1024):
        self.limit = limit
        self.buffer = b''
        self.data = []
        self.event_bytes = 0
        self.saw_sse = False
        self.completed = False
        self.failed = False
        self.invalid = False

    def feed(self, chunk):
        if self.invalid:
            return
        self.buffer += chunk
        if len(self.buffer) + self.event_bytes > self.limit:
            self.invalid = True
            self.buffer = b''; self.data.clear()
            return
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if not line:
                self._dispatch()
            elif line.startswith(b'data:'):
                self.saw_sse = True
                value = line[5:].lstrip(b' ')
                self.data.append(value); self.event_bytes += len(value)
            elif line.startswith(b'event:'):
                self.saw_sse = True

    def _dispatch(self):
        if self.data:
            try:
                event = json.loads(b'\n'.join(self.data))
            except (ValueError, UnicodeError):
                event = None
            if isinstance(event, dict):
                kind = event.get('type')
                if kind == 'response.completed':
                    self.completed = True
                elif kind in ('response.failed', 'response.incomplete', 'error'):
                    self.failed = True
        self.data.clear(); self.event_bytes = 0

    @property
    def successful(self):
        return self.completed and not self.failed and not self.invalid
