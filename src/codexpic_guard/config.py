from dataclasses import dataclass, asdict, field
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

MIB = 1024 * 1024


class GuardError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def loopback(host):
    if host == 'localhost': return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


@dataclass(frozen=True)
class Policy:
    image_target_bytes: int = 512_000
    max_edge: int = 2000
    media_high_bytes: int = 12 * MIB
    media_low_bytes: int = 8 * MIB
    max_images: int = 24
    keep_recent: int = 4
    max_pixels: int = 64_000_000
    cache_limit_bytes: int = 8 * 1024 * MIB

    def validate(self):
        if not 16_384 <= self.image_target_bytes <= 16*MIB:
            raise ValueError('image_target_bytes must be between 16 KiB and 16 MiB')
        if not 256 <= self.max_edge <= 8192: raise ValueError('max_edge must be 256..8192')
        if not 0 < self.media_low_bytes <= self.media_high_bytes <= 128*MIB:
            raise ValueError('require 0 < media_low_bytes <= media_high_bytes <= 128 MiB')
        if not 1 <= self.keep_recent <= self.max_images <= 600:
            raise ValueError('require 1 <= keep_recent <= max_images <= 600')
        if not 1_000_000 <= self.max_pixels <= 100_000_000: raise ValueError('invalid max_pixels')
        if self.cache_limit_bytes < 2*self.media_high_bytes: raise ValueError('cache quota too small')

    @property
    def cache_key(self):
        import hashlib
        encoding={'algorithm':'0.1','image_target_bytes':self.image_target_bytes,'max_edge':self.max_edge}
        return hashlib.sha256(json.dumps(encoding, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    data_dir: str
    upstream_base: str = 'https://chatgpt.com/backend-api/codex'
    upstream_proxy: str | None = None
    allow_direct: bool = False
    bind: str = '127.0.0.1'
    port: int = 17993
    local_prefix: str = '/backend-api/codex'
    max_request_bytes: int = 256*MIB
    concurrency: int = 2
    upstream_timeout_seconds: int = 600
    policy: Policy = field(default_factory=Policy)

    def validate(self):
        self.policy.validate()
        if not loopback(self.bind): raise ValueError('listener must be loopback-only')
        if not 1 <= self.port <= 65535: raise ValueError('invalid port')
        if not self.local_prefix.startswith('/') or '?' in self.local_prefix or '..' in self.local_prefix:
            raise ValueError('invalid local_prefix')
        if not 1 <= self.concurrency <= 4: raise ValueError('concurrency must be 1..4')
        if not self.policy.media_high_bytes < self.max_request_bytes <= 512*MIB:
            raise ValueError('invalid max_request_bytes')
        u=urlsplit(self.upstream_base)
        if not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('upstream base must have no credentials, query or fragment')
        if u.scheme!='https' and not (u.scheme=='http' and loopback(u.hostname)):
            raise ValueError('upstream requires HTTPS, except local test/gateway endpoints')
        if loopback(u.hostname) and u.port==self.port: raise ValueError('upstream cannot point to this guard')
        if self.upstream_proxy:
            p=urlsplit(self.upstream_proxy)
            if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
                raise ValueError('use an HTTP(S) proxy without embedded credentials')
            if loopback(p.hostname) and p.port==self.port: raise ValueError('proxy cannot point to this guard')
        elif not self.allow_direct and not loopback(u.hostname):
            raise ValueError('an upstream proxy is required; direct access is disabled')

    @property
    def local_base(self): return f'http://{self.bind}:{self.port}{self.local_prefix.rstrip("/")}'

    @property
    def fingerprint(self):
        import hashlib
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()

    @classmethod
    def load(cls, path):
        d=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        d['policy']=Policy(**d.get('policy',{}))
        c=cls(**d);c.validate();return c

    def save(self,path):
        self.validate()
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(asdict(self),ensure_ascii=False,indent=2),encoding='utf-8')
