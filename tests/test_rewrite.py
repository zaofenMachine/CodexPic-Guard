import copy
import json
import tempfile
import unittest
from codexpic_guard.config import Policy,GuardError
from codexpic_guard.rewrite import rewrite,request_scope,refs_in
from codexpic_guard.images import decode_url
from codexpic_guard.store import Store
from tests.helpers import picture,request,image_block


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.policy=Policy(image_target_bytes=40000,media_high_bytes=250000,media_low_bytes=140000,max_images=6,keep_recent=2)
        self.store=Store(self.temp.name,self.policy)
        self.headers={'session_id':'thread-a'}
        self.images=[picture(i,(300,200)) for i in range(12)]

    def test_budget_removes_oldest_and_keeps_latest(self):
        body=request(self.images);out,scope,stats=rewrite(body,self.headers,self.store,self.policy)
        self.assertLessEqual(stats['media_after_bytes'],self.policy.media_high_bytes)
        self.assertGreater(stats['dropped'],0)
        results=[i for i in out['input'] if i.get('type')=='function_call_output']
        self.assertEqual(results[0]['output'][0]['type'],'input_text')
        self.assertEqual(results[-1]['output'][0]['type'],'input_image')
        self.assertEqual(results[-2]['output'][0]['type'],'input_image')
        self.assertIn('codexpic_get_image',results[0]['output'][0]['text'])
        self.assertEqual(len(self.store.list_images(scope)),12)

    def test_identical_images_deduplicated_at_latest_position(self):
        out,_,stats=rewrite(request([self.images[0]]*4),self.headers,self.store,self.policy)
        self.assertEqual(stats['deduplicated'],3)
        self.assertEqual(len(refs_in(out)),1)

    def test_pin_preserves_old_reference(self):
        body=request(self.images);scope,_=request_scope(body,self.headers)
        ident,_=self.store.register(decode_url(image_block(self.images[0])['image_url'],self.policy),scope)
        self.store.pin(scope,ident,True)
        out,_,_=rewrite(body,self.headers,self.store,self.policy)
        first=out['input'][2]['output'][0]
        self.assertEqual(first['type'],'input_image')

    def test_text_tool_args_and_unknown_blocks_unchanged(self):
        special=image_block(self.images[0])['image_url']
        b=request(self.images)
        b['instructions']='literal '+special
        b['input'][1]['arguments']=json.dumps({'example':special})
        b['input'].append({'type':'function_call_output','call_id':'text','output':json.dumps([image_block(self.images[1])])})
        original=copy.deepcopy(b)
        out,_,_=rewrite(b,self.headers,self.store,self.policy)
        self.assertEqual(out['instructions'],original['instructions'])
        self.assertEqual(out['input'][1]['arguments'],original['input'][1]['arguments'])
        self.assertIn(original['input'][-1],out['input'])

    def test_scope_survives_compaction_into_text_only_input(self):
        _,scope,_=rewrite(request(self.images),self.headers,self.store,self.policy)
        out,_,stats=rewrite({'input':[{'role':'user','content':[{'type':'input_text','text':'After compact, find the previous render.'}]}]},self.headers,self.store,self.policy)
        self.assertTrue(stats['changed']);self.assertIn(scope,out['input'][-1]['content'][0]['text'])

    def test_native_thread_header_precedes_shared_session_header(self):
        a,_=request_scope({}, {'Thread-Id':'child-a','Session-Id':'parent'})
        b,_=request_scope({}, {'Thread-Id':'child-b','Session-Id':'parent'})
        self.assertNotEqual(a,b)

    def test_latest_batch_over_budget_fails_explicitly(self):
        b={'input':[{'role':'user','content':[image_block(x) for x in self.images]}]}
        with self.assertRaises(GuardError) as e:rewrite(b,self.headers,self.store,self.policy)
        self.assertEqual(e.exception.code,'protected_images_exceed_budget')

    def test_new_images_stay_pending_across_retry(self):
        old=request(self.images[:3]);rewrite(copy.deepcopy(old),self.headers,self.store,self.policy)
        second=request(self.images[:6]);out,scope,stats=rewrite(copy.deepcopy(second),self.headers,self.store,self.policy)
        self.assertGreater(len(self.store.pending(scope)),0)
        _,_,again=rewrite(copy.deepcopy(second),self.headers,self.store,self.policy)
        self.assertEqual(stats['retained_ids'],again['retained_ids'])
        self.store.delivered(scope,again['retained_ids']);self.assertFalse(self.store.pending(scope))

    def test_first_image_batch_after_text_is_protected(self):
        rewrite({'input':'First text-only step'},self.headers,self.store,self.policy)
        with self.assertRaises(GuardError) as e:rewrite(request(self.images),self.headers,self.store,self.policy)
        self.assertEqual(e.exception.code,'protected_images_exceed_budget')

    def test_new_duplicate_keeps_its_protection_after_dedup(self):
        rewrite(request(self.images),self.headers,self.store,self.policy)
        new=picture(999,(300,200))
        body=request(self.images[:5]+[new,new]+self.images[5:])
        _,_,stats=rewrite(body,self.headers,self.store,self.policy)
        ident='CPIMG-'+decode_url(image_block(new)['image_url'],self.policy).sha
        self.assertIn(ident,stats['retained_ids'])

    def test_original_grant_bypasses_preview_normalization(self):
        p=Policy(media_high_bytes=3*1024*1024,media_low_bytes=2*1024*1024)
        raw=picture(size=(700,500));b=request([raw]);scope,_=request_scope(b,self.headers)
        st=Store(self.temp.name,p);ident,_=st.register(decode_url(image_block(raw)['image_url'],p),scope);st.grant_original(scope,ident)
        out,_,stats=rewrite(b,self.headers,st,p)
        self.assertEqual(out['input'][2]['output'][0]['image_url'],image_block(raw)['image_url'])
        self.assertEqual(stats['original_grants'],[ident])

    def test_does_not_guess_missing_incremental_context(self):
        with self.assertRaises(GuardError) as e:rewrite({'input':[],'previous_response_id':'resp_old'},self.headers,self.store,self.policy)
        self.assertEqual(e.exception.code,'incremental_context_unsupported')

    def test_computer_screenshot_schema_is_preserved(self):
        b={'input':[{'type':'computer_call_output','call_id':'call','output':{'type':'computer_screenshot','image_url':image_block(self.images[0])['image_url']}}]}
        out,_,_=rewrite(b,self.headers,self.store,self.policy)
        self.assertEqual(out['input'][0]['output']['type'],'computer_screenshot')
