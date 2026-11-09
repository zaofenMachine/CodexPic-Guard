import base64
from dataclasses import dataclass
import hashlib
import io
import re
import warnings
from PIL import Image, ImageOps
from .config import GuardError, Policy

DATA = re.compile(r'^data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)$')
MIMES={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','GIF':'image/gif'}


@dataclass(frozen=True)
class ImageBytes:
    data: bytes
    mime: str
    width: int
    height: int
    alpha: bool
    animated: bool = False

    @property
    def sha(self): return hashlib.sha256(self.data).hexdigest()
    @property
    def url(self): return 'data:'+self.mime+';base64,'+base64.b64encode(self.data).decode('ascii')
    @property
    def encoded_bytes(self): return 4*((len(self.data)+2)//3)


def decode_url(url, policy: Policy):
    if not isinstance(url,str) or not url.startswith('data:'): return None
    m=DATA.fullmatch(url)
    if not m: raise GuardError('invalid_image_data','An inline image data URL is malformed.')
    try: raw=base64.b64decode(m[2].replace('\r','').replace('\n',''),validate=True)
    except ValueError: raise GuardError('invalid_image_data','An inline image has invalid base64.') from None
    if len(raw)>64*1024*1024: raise GuardError('image_too_large','One image exceeds the 64 MiB decode limit.',413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height>policy.max_pixels:
                    raise GuardError('image_pixels_exceeded','Image pixel count exceeds the configured decode limit. Crop or resize it first.',413)
                if im.format not in MIMES: raise GuardError('unsupported_image','Inline image format is not supported.')
                return ImageBytes(raw,MIMES[im.format],im.width,im.height,'A' in im.getbands() or 'transparency' in im.info,getattr(im,'n_frames',1)>1)
    except GuardError: raise
    except Exception: raise GuardError('invalid_image_data','Inline image bytes cannot be decoded safely.') from None


def _save(im,fmt,**opts):
    b=io.BytesIO();im.save(b,format=fmt,**opts);return b.getvalue()


def normalize(source: ImageBytes, policy: Policy):
    # Preserve animation. The aggregate budget still applies; never silently flatten it.
    if source.animated: return source
    if len(source.data)<=policy.image_target_bytes and max(source.width,source.height)<=policy.max_edge:
        return source
    try:
        with Image.open(io.BytesIO(source.data)) as original:
            im=ImageOps.exif_transpose(original).convert('RGBA' if source.alpha else 'RGB')
        im.thumbnail((policy.max_edge,policy.max_edge),Image.Resampling.LANCZOS)
        for _ in range(16):
            if source.alpha:
                candidates=[('image/png',_save(im,'PNG',optimize=True))]
                try: candidates.append(('image/webp',_save(im,'WEBP',lossless=True,method=4)))
                except (OSError,ValueError): pass
                mime,raw=min(candidates,key=lambda x:len(x[1]))
                if len(raw)<=policy.image_target_bytes:
                    return ImageBytes(raw,mime,im.width,im.height,True)
            else:
                smallest=None
                for quality in (92,85,78,70):
                    # The Windows Pillow/libjpeg build can exhaust its optimized
                    # 4:4:4 output buffer on noisy captures. Byte targeting remains
                    # explicit, so use the streaming-safe non-optimized encoder.
                    raw=_save(im,'JPEG',quality=quality,optimize=False,subsampling=0 if quality==92 else 2)
                    smallest=raw
                    if len(raw)<=policy.image_target_bytes:
                        return ImageBytes(raw,'image/jpeg',im.width,im.height,False)
                raw=smallest
            if min(im.size)<=128: break
            ratio=min(0.85,max(0.35,(policy.image_target_bytes/len(raw))**0.5*0.94))
            im=im.resize((max(1,int(im.width*ratio)),max(1,int(im.height*ratio))),Image.Resampling.LANCZOS)
        raise GuardError('image_budget_unreachable','Image cannot meet the preview byte target while preserving its alpha channel.',413)
    except GuardError: raise
    except Exception: raise GuardError('image_optimization_failed','Image optimization failed. No modified request was sent.',422) from None
