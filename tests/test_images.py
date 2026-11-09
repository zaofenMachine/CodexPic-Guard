import base64
import io
import tempfile
import unittest
from PIL import Image
from codexpic_guard.config import Policy,GuardError
from codexpic_guard.images import decode_url,normalize
from codexpic_guard.store import Store,scope_id
from tests.helpers import picture,image_block


class ImagesTest(unittest.TestCase):
    def decode(self,raw,policy=None):return decode_url(image_block(raw)['image_url'],policy or Policy())

    def test_small_file_is_byte_identical(self):
        raw=picture(size=(20,20));s=self.decode(raw)
        self.assertEqual(normalize(s,Policy()).data,raw)

    def test_noisy_photo_meets_byte_target(self):
        raw=picture(size=(1200,900));p=Policy()
        result=normalize(self.decode(raw),p)
        self.assertLessEqual(len(result.data),512000)
        self.assertLess(len(result.data),len(raw));self.assertEqual(result.mime,'image/jpeg')

    def test_alpha_survives_optimization_and_original_restore(self):
        raw=picture(size=(700,500),alpha=True);p=Policy(image_target_bytes=80000)
        original=self.decode(raw,p);result=normalize(original,p)
        self.assertTrue(result.alpha);self.assertLessEqual(len(result.data),80000)
        with Image.open(io.BytesIO(result.data)) as im:
            self.assertIn('A',im.getbands());self.assertEqual(im.getchannel('A').getextrema(),(0,255))
        with tempfile.TemporaryDirectory() as d:
            st=Store(d,p);scope=scope_id('test');ident,_=st.register(original,scope)
            st.get(scope,ident)
            self.assertEqual(st.get(scope,ident,original=True).data,raw)

    def test_animation_not_silently_flattened(self):
        b=io.BytesIO();Image.new('RGB',(20,20),'red').save(b,'GIF',save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100,loop=0)
        source=self.decode(b.getvalue());self.assertTrue(source.animated)
        self.assertEqual(normalize(source,Policy()).data,b.getvalue())

    def test_malformed_and_external_urls(self):
        self.assertIsNone(decode_url('https://example.invalid/image.png',Policy()))
        for url in ('data:image/png;base64,@@@@','data:image/png;base64,aGVsbG8='):
            with self.assertRaises(GuardError):decode_url(url,Policy())

    def test_exif_orientation_is_applied_on_resize(self):
        im=Image.new('RGB',(800,300),'red');exif=im.getexif();exif[274]=6;b=io.BytesIO();im.save(b,'JPEG',exif=exif)
        result=normalize(self.decode(b.getvalue()),Policy(max_edge=256))
        self.assertGreater(result.height,result.width)

    def test_cache_scope_and_integrity(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(d,Policy());scope=scope_id('a');ident,_=st.register(self.decode(picture(size=(20,20))),scope)
            with self.assertRaises(GuardError):st.get(scope_id('b'),ident)
            with self.assertRaises(GuardError):st.get(scope,'../../arbitrary-file')
            st.original_path(ident).write_bytes(b'tampered')
            with self.assertRaises(GuardError):st.get(scope,ident,original=True)

    def test_preview_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(d,Policy());scope=scope_id('test');ident,_=st.register(self.decode(picture(size=(30,30))),scope)
            st.get(scope,ident)
            preview=next(st.blobs.glob('*.preview'));preview.write_bytes(b'wrong')
            with self.assertRaises(GuardError):st.get(scope,ident)
