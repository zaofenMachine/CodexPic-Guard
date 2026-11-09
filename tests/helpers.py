import base64
import io
import random
from PIL import Image


def picture(seed=1,size=(500,350),alpha=False,fmt='PNG'):
    rng=random.Random(seed)
    channels=4 if alpha else 3
    im=Image.frombytes('RGBA' if alpha else 'RGB',size,rng.randbytes(size[0]*size[1]*channels))
    if alpha:
        mask=Image.new('L',size,255)
        for y in range(size[1]//3):
            for x in range(size[0]//3):mask.putpixel((x,y),0)
        im.putalpha(mask)
    b=io.BytesIO();im.save(b,fmt);return b.getvalue()


def image_block(raw,mime='image/png'):
    return {'type':'input_image','image_url':f'data:{mime};base64,'+base64.b64encode(raw).decode()}


def request(images):
    # Separate tool result groups, as in long screenshot workflows.
    items=[{'type':'message','role':'user','content':[{'type':'input_text','text':'Review the renders.'}]}]
    for i,img in enumerate(images):
        items.extend([{'type':'function_call','call_id':f'call_{i}','name':'read_image','arguments':'{}'},
                      {'type':'function_call_output','call_id':f'call_{i}','output':[image_block(img)]}])
    items.append({'type':'message','role':'user','content':[{'type':'input_text','text':'Continue with the next code step.'}]})
    return {'model':'test-model','input':items,'stream':True}
