"""OCR service contract consumed by the current Vue/Spring application.

The Java service owns editing/review/export. This service owns immutable PDF,
local OCR, automatic matching and source coordinates.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool
from material_ocr.workflow import write_json
from material_ocr.web import hybrid
from material_ocr.web.paddle_anchors import PIPELINE_ID, pipeline_identity, run_entrypoint
from material_ocr.web.recognition import RecognitionQueue
from material_ocr.pdf_runtime import PDF_RENDER_LOCK
from material_ocr.materials.rebar.review_policy import apply_review_policy

GPU_LOCK = threading.Lock()
COLUMNS=['供应商是否报审','文件编号','材料名称','规格品种','生产单位','供应单位',
         '供货日期与工程材料报审签认日期','使用部位','送货单编号','炉批号','验收数量','质保书编号']


def render_pdf(content,dpi=180,fmt='png'):
    # PDFium is not thread-safe within a process. Keep HTTP responsive during
    # uploads while serializing its native rendering calls.
    with PDF_RENDER_LOCK:
        return _render_pdf(content,dpi,fmt)


def _render_pdf(content,dpi=180,fmt='png'):
    import pypdfium2 as pdfium
    fmt=fmt.lower()
    if not 72<=dpi<=400 or fmt not in ('png','jpg','jpeg'):raise HTTPException(422,'DPI须在72至400，图片格式须为png或jpg')
    result=[]
    pdf = pdfium.PdfDocument(content)
    try:
        for i in range(len(pdf)):
            page=pdf[i]
            try:
                bitmap=page.render(scale=dpi/72)
                try:
                    im=bitmap.to_pil();buffer=io.BytesIO();im.save(buffer,format='PNG' if fmt=='png' else 'JPEG')
                    result.append(dict(page=i+1,width=im.width,height=im.height,format=fmt,image_base64=base64.b64encode(buffer.getvalue()).decode()))
                    im.close()
                finally:bitmap.close()
            finally:page.close()
    finally:pdf.close()
    return dict(page_count=len(result),format=fmt,pages=result)


def normalize_ledger(ledger,name):
    value=apply_review_policy(deepcopy(ledger));value['pdf_name']=name;checks=[]
    value.setdefault('schema_version','automatic-ledger-v1')
    value.setdefault('matching_requires_human',False)
    value.setdefault('review_stage','before_excel_export')
    def diagnostics(items,field='',locations=None):
        return [deepcopy(item) if isinstance(item,dict) else
            dict(code='unverified_keyword',field=field,reason=str(item),locations=deepcopy(locations or []))
            for item in items or []]
    for ri,record in enumerate(value['records']):
        record['diagnostics']=diagnostics(record.get('diagnostics'))
        fields=record['fields']
        if '供货日期' in fields:fields['供货日期与工程材料报审签认日期']=fields.pop('供货日期')
        for col in COLUMNS:
            if col not in fields:
                fields[col]=dict(value='',page=None,polygon=None,evidence=[],found_in_ocr=False,status='missing',
                    diagnostics=[dict(code='missing_keyword',field=col,reason='该字段未可靠提取',locations=[])])
            item=fields[col]
            item.setdefault('evidence',[])
            item.setdefault('polygon',next((e.get('polygon_page') for e in item['evidence'] if e.get('polygon_page')),None))
            item.setdefault('found_in_ocr',bool(item.get('value') and item['evidence']))
            item['diagnostics']=diagnostics(item.get('diagnostics'),col,item['evidence'])
            checks.append(dict(path=f'records[{ri}].{col}',**deepcopy(item)))
    value['consistency']=dict(field_count=len(checks),field_checks=checks,
        hallucination_count=sum(not c['found_in_ocr'] for c in checks),summary=f"自动生成{len(value['records'])}条记录；请在导出Excel前集中核验")
    value['summary']=value['consistency']['summary']
    return value


def create_library_router(root,model_cache,device,profile,*,artifact_policy='compact'):
    if artifact_policy not in ('compact','full'):raise ValueError('artifact_policy须为compact或full')
    router=APIRouter();root=Path(root)/'library';root.mkdir(parents=True,exist_ok=True)
    jobs=RecognitionQueue(root)
    router.add_event_handler('shutdown',jobs.shutdown)
    locks={};locks_guard=threading.Lock()
    def folder(name):
        if not name or len(name)>180 or name in ('.','..') or re.search(r'[/\\\x00-\x1f]',name):raise HTTPException(422,'无效PDF标识')
        p=(root/name).resolve()
        if not p.is_relative_to(root.resolve()):raise HTTPException(422,'无效PDF标识')
        return p
    def existing(name):
        p=folder(name)
        if not (p/'source.pdf').is_file():raise HTTPException(404,'PDF不存在')
        return p
    @contextmanager
    def exclusive(name,allow_task=False,blocking=False):
        with locks_guard: lock=locks.setdefault(name,threading.Lock())
        if not lock.acquire(blocking=blocking):raise HTTPException(409,'该PDF正在修改，请稍后重试')
        try:
            if not allow_task and jobs.active(name):raise HTTPException(409,'该PDF已在OCR队列中，完成后可修改文件或缓存')
            yield
        finally:lock.release()
    def save_pages(p,content,dpi,fmt):
        data=render_pdf(content,dpi,fmt);(p/'pages').mkdir(exist_ok=True)
        for page in data['pages']:
            # Stable PNG endpoints regardless of requested source encoding.
            im=Image.open(io.BytesIO(base64.b64decode(page.pop('image_base64'))));im.save(p/'pages'/f"{page['page']}.png");im.close()
        write_json(p/'manifest.json',dict(page_count=data['page_count'],dpi=dpi,pages=data['pages'],rotations={}))
        return data
    async def pdf_bytes(file):
        content=await file.read(256*1024*1024+1)
        if len(content)>256*1024*1024:raise HTTPException(413,'PDF过大')
        if b'%PDF-' not in content[:1024]:raise HTTPException(422,'不是PDF文件')
        return content
    @router.post('/api/pdf/to_images')
    async def to_images(file:UploadFile=File(...),dpi:int=Form(180),image_format:str=Form('png')):
        content=await pdf_bytes(file)
        return await run_in_threadpool(render_pdf,content,dpi,image_format)
    @router.post('/api/library/upload')
    async def upload(file:UploadFile=File(...),name:str=Form(''),dpi:int=Form(180),image_format:str=Form('png'),overwrite:bool=Form(False),material_type:str=Form('rebar')):
        if material_type not in ('rebar','concrete','steel_beam'):raise HTTPException(422,'无效材料类别')
        name=name or Path(file.filename or 'upload.pdf').stem;p=folder(name);content=await pdf_bytes(file)
        return await run_in_threadpool(store_upload,name,p,content,file.filename,dpi,image_format,overwrite,material_type)
    def store_upload(name,p,content,filename,dpi,image_format,overwrite,material_type):
        with exclusive(name):
            if (p/'source.pdf').exists():
                meta=json.loads((p/'manifest.json').read_text())
                # Recovery uploads use overwrite=true too. Identical bytes and
                # display DPI reuse the same pages, OCR evidence and edits.
                if (p/'source.pdf').read_bytes()==content and meta['dpi']==dpi and meta.get('material_type','rebar')==material_type:
                    return dict(name=name,pdf_name=name,page_count=meta['page_count'],material_type=material_type,reused=True)
                if not overwrite:raise HTTPException(409,'同名PDF已存在')
            # Build before replacing existing data, so bad uploads cannot destroy it.
            staging=root/('staging-'+uuid.uuid4().hex);staging.mkdir()
            try:
                data=save_pages(staging,content,dpi,image_format)
                manifest=json.loads((staging/'manifest.json').read_text());manifest['material_type']=material_type
                write_json(staging/'manifest.json',manifest);(staging/'source.pdf').write_bytes(content)
                write_json(staging/'original.json',dict(original_name=filename))
                if p.exists():
                    archive=root/('archive-'+uuid.uuid4().hex);p.rename(archive)
                staging.rename(p)
                jobs.forget(name)
            finally:
                if staging.exists():shutil.rmtree(staging)
        return dict(name=name,pdf_name=name,page_count=data['page_count'],material_type=material_type)
    @router.get('/api/library')
    def listing():
        return dict(pdfs=[dict(name=p.name,page_count=json.loads((p/'manifest.json').read_text())['page_count'],
                               material_type=json.loads((p/'manifest.json').read_text()).get('material_type','rebar'))
                         for p in sorted(root.iterdir()) if (p/'source.pdf').exists() and not p.name.startswith(('archive-','staging-'))])
    @router.get('/api/library/{name}/pages/{page}/image')
    def image(name:str,page:int):
        p=existing(name)/'pages'/f'{page}.png'
        if not p.is_file():raise HTTPException(404,'页面不存在')
        return FileResponse(p)
    @router.get('/api/library/{name}/ledger')
    def ledger(name:str):
        p=existing(name)/'ledger.json'
        if not p.is_file():raise HTTPException(404,'台账尚未生成')
        return json.loads(p.read_text())
    @router.get('/api/library/ocr-models')
    def ocr_models():
        return hybrid.catalog()
    def model_identity(model):
        try:return hybrid.cache_identity(model_cache)
        except (OSError,ValueError) as exc:raise HTTPException(503,'混合识别流程不可用：'+str(exc)) from exc
    def cached_ledger(p,identity,source_sha):
        try:
            data=json.loads((p/'ledger.json').read_text())
            return (data.get('ocr_cache_identity')==identity and data.get('source_pdf_sha256')==source_sha
                and (p/data.get('run_directory','')/'work/attachments/attachment_index.json').is_file())
        except (OSError,ValueError,TypeError):return False
    def execute_recognition(name,model,force_ocr_refresh,identity,source_sha,material_type,progress):
        p=existing(name)
        with exclusive(name,allow_task=True,blocking=True),GPU_LOCK:
            progress('starting',0,1,'准备本地OCR；首次模型加载可能需要一些时间')
            if not force_ocr_refresh and cached_ledger(p,identity,source_sha):return ledger(name)
            out=p/('run-'+uuid.uuid4().hex)
            if material_type == 'concrete':
                automatic_ledger,log_path=run_entrypoint(p/'source.pdf',out,model_cache,device,
                    force_attachments=force_ocr_refresh,progress=progress,material_type='concrete')
                if automatic_ledger.get('schema_version') != 'concrete-ledger-v1':
                    raise RuntimeError('混凝土识别未返回对应材料的业务结果')
                data=dict(automatic_ledger,pdf_name=name)
            else:
                inference_device=str(device).replace('gpu','cuda',1) if str(device).startswith('gpu') else str(device)
                result=hybrid.run(p/'source.pdf',out,model_cache=model_cache,
                    cache_dir=root.parent/'ocr-cache'/'paddle-anchors',device=inference_device,
                    progress=progress,force_ocr_refresh=force_ocr_refresh)
                data=normalize_ledger(result['automatic_ledger'],name)
            data.update(run_directory=out.name,source_pdf_sha256=source_sha,model=model,model_id=model,
                ocr_cache_identity=identity)
            if material_type == 'rebar':data['ocr_backends']=result['ocr_backends']
            data['revision']=hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            write_json(p/'ledger.json',data)
            latest=dict(run=out.name,model_id=model,ocr_cache_identity=identity,source_pdf_sha256=source_sha)
            if material_type == 'concrete':latest['log']=str(log_path)
            write_json(p/'latest.json',latest)
            return data
    def active_recognition(name,model):
        state=jobs.status(name)
        if state.get('model_id') and state['model_id']!=model:
            raise HTTPException(409,'该卷正在使用另一OCR模型识别，请等待当前任务完成后切换')
        return state
    def submit_recognition(name,model,force_ocr_refresh):
        p=existing(name)
        try:material_type=json.loads((p/'manifest.json').read_text()).get('material_type','rebar')
        except (OSError,ValueError):material_type='rebar'
        if material_type == 'steel_beam':raise HTTPException(422,'钢梁已支持 PDF 分类；其专用识别和匹配规则尚未接入')
        if material_type == 'concrete':
            if model not in ('',hybrid.MODEL_ID,PIPELINE_ID,'local-glm','local-paddle'):
                raise HTTPException(422,'无效的混凝土识别流程')
            model=PIPELINE_ID
        else:
            try:model=hybrid.validate_model(model)
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        # Submitting or retrying an active job never takes the long OCR file lock.
        if jobs.active(name):
            return active_recognition(name,model)
        if material_type == 'concrete':
            try:identity=dict(pipeline_identity(model_cache,device),material_type=material_type)
            except (OSError,ValueError) as exc:raise HTTPException(503,'混凝土 OCR 流程不可用：'+str(exc)) from exc
        else:
            identity=model_identity(model)
        source_sha=hashlib.sha256((p/'source.pdf').read_bytes()).hexdigest()
        try:
            with exclusive(name,allow_task=True):
                # Another request may have queued work since the first check.
                if jobs.active(name):return active_recognition(name,model)
                return jobs.submit(name,lambda progress:execute_recognition(name,model,force_ocr_refresh,identity,source_sha,material_type,progress),
                                   cached=cached_ledger(p,identity,source_sha),force=force_ocr_refresh,model_id=model)
        except HTTPException as exc:
            # A worker may acquire its lock between our first status check and
            # lock acquisition. It is still the same job, not an HTTP conflict.
            if exc.status_code==409 and jobs.active(name):return active_recognition(name,model)
            raise
    def public_recognition(name,state):
        # Let the Java cache compare the completed remote model and source
        # revision without downloading the full ledger on every poll.
        if state.get('status')=='completed':
            try:
                data=ledger(name)
                for key in ('model_id','ocr_cache_identity'):
                    if key in data:state[key]=data[key]
                if data.get('revision'):state['source_revision']=data['revision']
            except (HTTPException,OSError,ValueError):pass
        return state
    @router.post('/api/library/{name}/recognition',status_code=202)
    def start_recognition(name:str,prompt:str=Form(''),ocr_prompt:str=Form(''),model:str=Form(''),force_ocr_refresh:bool=Form(False)):
        return public_recognition(name,submit_recognition(name,model,force_ocr_refresh))
    @router.get('/api/library/{name}/recognition')
    def recognition_status(name:str):
        existing(name)
        return public_recognition(name,jobs.status(name))
    @router.post('/api/library/{name}/recognize')
    def recognize(name:str,prompt:str=Form(''),ocr_prompt:str=Form(''),model:str=Form(''),force_ocr_refresh:bool=Form(False)):
        # Backward-compatible blocking endpoint shares exactly the same job.
        submit_recognition(name,model,force_ocr_refresh)
        jobs.wait(name)
        state=jobs.status(name)
        if state['status']!='completed':raise HTTPException(500,state.get('error') or '识别未完成')
        return ledger(name)
    @router.get('/api/library/{name}/pages/{page}/ocr')
    def page_ocr(name:str,page:int):
        p=existing(name);meta=json.loads((p/'manifest.json').read_text())
        if not 1<=page<=meta['page_count']:raise HTTPException(404,'页面不存在')
        if not (p/'latest.json').is_file():raise HTTPException(404,'OCR尚未生成')
        run=json.loads((p/'latest.json').read_text())['run']
        # Concrete runs publish one merged page index containing the upstream
        # submission/inspection forms and attachment pages.  The old endpoint
        # only read attachment_index.json, so a six-page concrete volume
        # appeared to have OCR cached for its two attachment pages.
        material_type=meta.get('material_type','rebar')
        index_path=p/run/'work/attachments/attachment_index.json'
        if material_type == 'concrete':
            merged_path=p/run/'work/concrete/page_index.json'
            if merged_path.is_file():
                index_path=merged_path
        if not index_path.is_file():
            raise HTTPException(404,'OCR页面索引不存在')
        index=json.loads(index_path.read_text())
        source=next((x for x in index.get('pages',[]) if x.get('page_no')==page),None)
        if source is None:
            raise HTTPException(404,'该页没有 OCR 索引')
        with Image.open(p/'pages'/f'{page}.png') as im:w,h=im.size
        sw,sh=source.get('pixel_size') or [w,h]
        # Rendered original pixel axes; account for requested display DPI/rotation.
        rotation=int(meta.get('rotations',{}).get(str(page),0))%360
        ow,oh=(h,w) if rotation in (90,270) else (w,h)
        def mapped(poly,size=None,dpi=None):
            if not poly:return None
            bw,bh=size or (sw,sh)
            output=[]
            for x,y in poly:
                x,y=x*ow/bw,y*oh/bh
                if rotation==90:x,y=oh-y,x
                elif rotation==180:x,y=ow-x,oh-y
                elif rotation==270:x,y=y,ow-x
                output.extend([x,y])
            return output
        items=[];seen=set();checkpoint_sizes={}
        def evidence_size(ev):
            if ev.get('source_pixel_size'):return ev['source_pixel_size']
            reference=ev.get('checkpoint_reference')
            if reference:
                if reference not in checkpoint_sizes:
                    try:
                        checkpoint=json.loads(Path(reference).read_text())
                        checkpoint_sizes[reference]=checkpoint.get('original_pixel_size') or checkpoint.get('pixel_size')
                    except (OSError,ValueError):checkpoint_sizes[reference]=None
                if checkpoint_sizes[reference]:return checkpoint_sizes[reference]
            if ev.get('render_dpi') and source.get('dpi'):
                ratio=ev['render_dpi']/source['dpi'];return [sw*ratio,sh*ratio]
            return None
        for region in source.get('regions',[]):
            for b in region.get('blocks',[]):
                key=(b['text'],str(b.get('polygon_page')))
                if key in seen:continue
                seen.add(key);items.append(dict(text=b['text'],confidence=b.get('score'),
                    location=mapped(b.get('polygon_page'),evidence_size(b) or evidence_size(region))))
        data=ledger(name)
        for ri,rec in enumerate(data['records']):
            for label,f in rec['fields'].items():
                if not isinstance(f,dict):continue
                for ev in f.get('evidence',[]):
                    if ev.get('page_no')!=page or not ev.get('polygon_page'):continue
                    size=evidence_size(ev)
                    items.append(dict(text=f['value'],location=mapped(ev['polygon_page'],size),
                        path=f'records[{ri}].{label}',field=label,diagnostics=f.get('diagnostics',[]),kind='ledger'))
        return dict(schema_version="automatic-ledger-v1",page=page,image_width=w,image_height=h,original_width=sw,original_height=sh,rotation_cw=rotation,items=items,text=source.get('raw_text') or '\n'.join(b['text'] for b in items if b.get('kind')!='ledger'),ocr_status=source.get('ocr_status','completed'))
    @router.post('/api/library/{name}/pages/{page}/ocr')
    def run_page(name:str,page:int,prompt:str=Form('')):
        # Association requires cross-page context, so a first request recognizes the volume.
        if not (existing(name)/'latest.json').exists():recognize(name,prompt,'','',False)
        return page_ocr(name,page)
    @router.post('/api/library/{name}/cache')
    def clear_cache(name:str,scope:str=Form('ledger')):
        if scope not in ('all','ledger'):raise HTTPException(422,'未知缓存范围')
        p=existing(name)
        with exclusive(name):
            (p/'ledger.json').unlink(missing_ok=True)
            if scope=='all':(p/'latest.json').unlink(missing_ok=True)
            jobs.forget(name)
        return dict(scope=scope,cleared=True)
    @router.post('/api/library/{name}/resplit')
    def resplit(name:str,dpi:int=Form(180),image_format:str=Form('png')):
        p=existing(name)
        with exclusive(name):
            material_type=json.loads((p/'manifest.json').read_text()).get('material_type','rebar')
            result=save_pages(p,(p/'source.pdf').read_bytes(),dpi,image_format)
            meta=json.loads((p/'manifest.json').read_text());meta['material_type']=material_type
            write_json(p/'manifest.json',meta)
            for filename in ('ledger.json','latest.json'):(p/filename).unlink(missing_ok=True)
            jobs.forget(name)
        return dict(page_count=result['page_count'])
    @router.post('/api/library/{name}/pages/{page}/rotate')
    def rotate(name:str,page:int,angle:int=Form(...)):
        if angle not in (90,180,270,-90):raise HTTPException(422,'旋转角度须为90度的倍数')
        p=existing(name)
        with exclusive(name):
            path=p/'pages'/f'{page}.png'
            if not path.exists():raise HTTPException(404,'页面不存在')
            with Image.open(path) as im:im.rotate(angle,expand=True).save(path)
            meta=json.loads((p/'manifest.json').read_text());meta['rotations'][str(page)]=(meta['rotations'].get(str(page),0)-angle)%360
            write_json(p/'manifest.json',meta)
        return dict(page=page,angle=angle)
    return router
