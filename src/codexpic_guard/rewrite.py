from dataclasses import dataclass
import hashlib
import json
from .config import GuardError, Policy
from .images import decode_url
from .store import Store, scope_id


@dataclass
class Ref:
    block: dict
    group: int
    field: str
    image_id: str = ''
    existed: bool = False
    before_bytes: int = 0
    after_bytes: int = 0
    protected: bool = False
    original: bool = False
    upper_bytes: int = 0


def request_scope(body,headers):
    headers={k.lower():v for k,v in headers.items()}
    for key in ('thread-id','x-codex-thread-id','thread_id','conversation_id','x-openai-conversation-id','session-id','session_id'):
        if headers.get(key):return scope_id(headers[key]),key
    if body.get('prompt_cache_key'):return scope_id(body['prompt_cache_key']),'prompt_cache_key'
    try:
        meta=json.loads(headers.get('x-codex-turn-metadata','{}'))
        for key in ('thread_id','conversation_id','session_id'):
            if meta.get(key):return scope_id(meta[key]),'turn_metadata'
    except (ValueError,TypeError):pass
    # Stable only while the first user item is unchanged. Never merge all anonymous clients.
    items=body.get('input')
    seed=next((x for x in items if isinstance(x,dict) and x.get('role')=='user'),items[:1]) if isinstance(items,list) else items
    return scope_id(json.dumps(seed,sort_keys=True,ensure_ascii=False)),'input_fingerprint'


def refs_in(body):
    refs=[]
    items=body.get('input')
    if not isinstance(items,list):return refs
    def visit(blocks,group):
        if not isinstance(blocks,list):return
        for block in blocks:
            if not isinstance(block,dict):continue
            if block.get('type')=='input_image' and isinstance(block.get('image_url'),str):
                if block['image_url'].startswith('data:'):refs.append(Ref(block,group,'image_url'))
    for i,item in enumerate(items):
        if not isinstance(item,dict):continue
        typ=item.get('type','message')
        if typ=='message':visit(item.get('content'),i)
        elif typ in ('function_call_output','custom_tool_call_output'):
            output=item.get('output')
            visit(output,i)
            if isinstance(output,dict):visit(output.get('content'),i)
        elif typ=='computer_call_output':
            output=item.get('output')
            if isinstance(output,dict) and output.get('type')=='computer_screenshot' and isinstance(output.get('image_url'),str) and output['image_url'].startswith('data:'):
                # This block cannot be replaced with input_text without changing its schema.
                refs.append(Ref(output,i,'image_url',protected=True))
    return refs


def rewrite(body,headers,store:Store,policy:Policy):
    """Mutate only typed image blocks in a newly parsed request object."""
    scope,scope_source=request_scope(body,headers)
    if body.get('previous_response_id'):
        raise GuardError('incremental_context_unsupported','CodexPic requires complete HTTP input to enforce image budgets. A previous_response_id continuation cannot be safely rewritten. Start a fresh full-context HTTP session.',409)
    refs=refs_in(body)
    stats=dict(scope_source=scope_source,images_before=len(refs),images_after=0,media_before_bytes=0,media_after_bytes=0,optimized=0,dropped=0,deduplicated=0,original_grants=[],changed=False)
    def scope_note():
        if isinstance(body.get('input'),list):
            body['input'].append({'type':'message','role':'developer','content':[{'type':'input_text','text':
                f'CodexPic local image-cache scope: {scope}. Originals are available through codexpic_list_images and codexpic_get_image. Retrieve an omitted image before judging its visual details; use original=true when preview detail is insufficient. Image IDs remain valid after context compaction.'}]})
            stats['changed']=True
    if not refs:
        if store.has_images(scope):scope_note()
        store.advance(scope)
        return body,scope,stats
    if len(refs)>2000:raise GuardError('too_many_images','Request contains more than 2000 inline images.',413)
    known_scope=store.has_scope(scope);pinned=store.pinned(scope);grants=store.original_grants(scope);pending=store.pending(scope)
    group=max(r.group for r in refs)
    last_occurrence={}
    # Validate and cache all originals before replacing anything with a placeholder.
    for index,ref in enumerate(refs):
        image=decode_url(ref.block[ref.field],policy)
        ref.before_bytes=image.encoded_bytes
        ref.image_id,ref.existed=store.register(image,scope,mark_pending=known_scope)
        ref.original=ref.image_id in grants
        ref.protected=ref.protected or ref.group==group or ref.image_id in pinned or ref.image_id in pending or ref.original or (known_scope and not ref.existed)
        if ref.original or image.animated:
            ref.upper_bytes=image.encoded_bytes
        elif len(image.data)<=policy.image_target_bytes and max(image.width,image.height)<=policy.max_edge:
            ref.upper_bytes=image.encoded_bytes
        else:ref.upper_bytes=4*((policy.image_target_bytes+2)//3)
        last_occurrence[ref.image_id]=index
        stats['media_before_bytes']+=ref.before_bytes
    for ref in refs[-policy.keep_recent:]:ref.protected=True
    protected_ids={ref.image_id for ref in refs if ref.protected}
    for ref in refs:
        if ref.image_id in protected_ids:ref.protected=True
    retained=set(range(len(refs)))
    def omit(i,reason):
        ref=refs[i]
        if ref.block.get('type')=='computer_screenshot':
            raise GuardError('protected_image_schema','A computer screenshot cannot be omitted without changing its response schema.',413)
        text=(f'[CodexPic image offloaded: {ref.image_id}; scope={scope}; {reason}. '
              'The original remains local. If visual detail is needed, call codexpic_get_image with this image_id and scope; original=true requests full detail. Do not infer unseen details.]')
        ref.block.clear();ref.block.update(type='input_text',text=text)
        retained.remove(i);stats['dropped']+=1
    # Keep the latest occurrence, where it is most relevant to the current tool result.
    for i,ref in enumerate(refs):
        if last_occurrence[ref.image_id]!=i and not ref.block.get('type')=='computer_screenshot':
            omit(i,'identical image is retained later in this request');stats['deduplicated']+=1
    def materialize(i):
        ref=refs[i];output=store.get(scope,ref.image_id,original=ref.original)
        ref.block[ref.field]=output.url;ref.after_bytes=output.encoded_bytes
        if output.sha!=ref.image_id[6:]:stats['optimized']+=1
        return ref.after_bytes
    estimated=sum(refs[i].upper_bytes for i in retained)
    if estimated>policy.media_high_bytes or len(retained)>policy.max_images:
        selected={i for i in retained if refs[i].protected}
        total=sum(materialize(i) for i in selected)
        for i in sorted(retained-selected,reverse=True):
            if total>=policy.media_low_bytes or len(selected)>=policy.max_images:break
            size=materialize(i)
            if total+size>policy.media_low_bytes:break
            selected.add(i);total+=size
        for i in sorted(retained-selected):omit(i,'older image removed to meet the request image budget')
    else:total=sum(materialize(i) for i in retained)
    if total>policy.media_high_bytes or len(retained)>policy.max_images:
        raise GuardError('protected_images_exceed_budget',
                         f'The latest/new/pinned images require {total} base64 bytes in {len(retained)} image blocks. Budget is {policy.media_high_bytes} bytes and {policy.max_images} images. Send a smaller image batch, unpin references, or explicitly raise the local budget. No request was sent.',413)
    stats.update(images_after=len(retained),media_after_bytes=total,retained_ids=sorted({refs[i].image_id for i in retained}),original_grants=sorted({refs[i].image_id for i in retained if refs[i].original}))
    store.advance(scope)
    scope_note()
    return body,scope,stats
