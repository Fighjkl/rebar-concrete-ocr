"""Local FastAPI app; uploaded filenames never become arbitrary server paths."""
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from datetime import datetime, timezone
import uuid
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from material_ocr.web.tasks import TaskService

STATIC = Path(__file__).parent / 'static'
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_BATCH_BYTES = 1024 * 1024 * 1024


def upload_name(name):
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or not path.name or any(ord(x)<32 for x in name):
        raise ValueError('文件名包含无效路径')
    if path.suffix.lower() != '.pdf' or len(path.name) > 180:
        raise ValueError('当前仅支持PDF文件')
    return path.name


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(404, '文件不存在')
    return path


def create_app(data_dir=Path('runs/web'), *, model_cache=Path('models/paddlex_cache'),
               device='gpu:0', profile='batched_gpu', review_library=None, task_service=None,
               artifact_policy='compact'):
    service = task_service or TaskService(data_dir, model_cache, device, profile,
                                        artifact_policy=artifact_policy)
    library = {}
    library_root = Path(review_library).resolve() if review_library else None

    def refresh_library():
        if not library_root:
            return
        indexes = set()
        for pattern in ('*/index.html', '*/review/index.html', 'web/*/documents/*/review/index.html'):
            indexes.update(library_root.glob(pattern))
        for index in sorted(indexes):
            result = index.parent / 'match_results.json'
            if result.is_file():
                relative = index.parent.relative_to(library_root)
                key = (index.parent.name if len(relative.parts) == 1 else
                       'review-' + hashlib.sha256(str(relative).encode()).hexdigest()[:16])
                stamp = result.stat().st_mtime_ns
                if key in library and library[key].get('stamp') == stamp:
                    continue
                try:
                    data = json.loads(result.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                library[key] = dict(directory=index.parent, name=data.get('pdf_name', index.parent.name),
                    pdf_sha256=data.get('pdf_sha256', key), metrics=data.get('metrics', {}),
                    stamp=stamp, version=1 if data.get('schema_version') == 'same-page-constrained-match-v5' else 0)

    def latest_reviews():
        refresh_library()
        latest = {}
        for key, item in list(library.items()):
            previous = latest.get(item['pdf_sha256'])
            order = item['version'], (item['directory'] / 'match_results.json').stat().st_mtime
            if previous is None or order > previous[2]:
                latest[item['pdf_sha256']] = key, item, order
        return latest

    def with_latest_review(job, latest=None):
        try:
            job['filling_available'] = service.registry.get(job.get('material_id', 'rebar')).form_writer() is not None
        except ValueError:
            job['filling_available'] = False
        latest = latest_reviews() if latest is None else latest
        for document in job['documents']:
            match = latest.get(document.get('sha256'))
            if document['status'] == 'completed' and match:
                key, item, _ = match
                original = (service.root / job['id'] / 'documents' / document['id'] / 'review').resolve()
                if item['directory'] != original:
                    document.update(source_review_url=document.get('review_url'), review_updated=True,
                        review_url=f'/reviews/{key}/index.html', summary_url=f'/reviews/{key}/match_results.json',
                        metrics=item['metrics'])
        return job

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(title='材料表单OCR', lifespan=lifespan)
    app.state.tasks = service
    from .library import create_library_router
    app.include_router(create_library_router(data_dir, model_cache, device, profile,
                                             artifact_policy=artifact_policy))
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/')
    def home():
        return {'service': 'material-ocr', 'frontend': '前端/vue', 'docs': '/docs'}

    @app.get('/api/materials')
    def materials():
        return service.registry.list()

    @app.get('/api/capabilities')
    def capabilities():
        return dict(device=service.device, profile=service.profile, supported_files=['pdf'],
                    folder_upload=True, filling_available=any(a.form_writer() is not None for a in service.registry.adapters.values()),
                    max_file_bytes=MAX_FILE_BYTES, max_batch_bytes=MAX_BATCH_BYTES)

    @app.get('/api/jobs')
    def jobs():
        latest = latest_reviews()
        return [with_latest_review(job, latest) for job in service.list()]

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str):
        try:
            return with_latest_review(service.get(job_id))
        except KeyError:
            raise HTTPException(404, '任务不存在')

    @app.post('/api/jobs', status_code=202)
    async def upload(files: list[UploadFile] = File(...), material_id: str = Form('rebar'), layout: str = Form('auto')):
        try:
            adapter = service.registry.get(material_id)
            if layout not in adapter.info.layouts:
                raise ValueError('该材料不支持此版式')
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not files or len(files) > 100:
            raise HTTPException(422, '每批选择1至100个PDF')
        job_id = service.new_id()
        folder = service.root / job_id
        folder.mkdir()
        documents, total_bytes = [], 0
        try:
            for i, upload in enumerate(files, 1):
                try:
                    name = upload_name(upload.filename or '')
                except ValueError as exc:
                    raise HTTPException(422, str(exc))
                document_id = f'file-{i:03d}'
                target = folder / 'inputs' / document_id / name
                target.parent.mkdir(parents=True)
                count, checksum, first = 0, hashlib.sha256(), b''
                with target.open('wb') as stream:
                    while chunk := await upload.read(1024 * 1024):
                        if not first:
                            first = chunk[:1024]
                        count += len(chunk)
                        total_bytes += len(chunk)
                        if count > MAX_FILE_BYTES or total_bytes > MAX_BATCH_BYTES:
                            raise HTTPException(413, '文件过大：单文件最多256MB，每批最多1GB')
                        stream.write(chunk)
                        checksum.update(chunk)
                if b'%PDF-' not in first:
                    raise HTTPException(422, f'{name}不是有效PDF')
                documents.append(dict(id=document_id, name=name, relative_path=upload.filename,
                    size_bytes=count, sha256=checksum.hexdigest(), input_path=str(target), status='queued'))
            return service.create(job_id, material_id, layout, documents)
        except BaseException:
            shutil.rmtree(folder)
            raise
        finally:
            for file in files:
                await file.close()

    @app.get('/artifacts/{job_id}/{relative:path}')
    def artifact(job_id: str, relative: str):
        if job_id not in service.jobs:
            raise HTTPException(404, '任务不存在')
        target = contained(service.root / job_id, relative)
        if not target.exists() and target.suffix.lower() == '.png' and Path(str(target)+'.artifact.json').is_file():
            from material_ocr.storage_artifacts import materialize_artifact
            try:
                materialize_artifact(target, allowed_root=service.root / job_id)
            except (OSError, ValueError, KeyError) as exc:
                raise HTTPException(409, '诊断图片暂时无法恢复，请保留日志检查') from exc
        normalized = target.relative_to(service.root / job_id).parts
        if not target.is_file() or not normalized or normalized[0] not in ('documents', 'logs'):
            raise HTTPException(404, '文件不存在')
        if len(normalized) >= 4 and relative == 'documents/' + normalized[1] + '/review/index.html':
            original = json.loads((target.parent / 'match_results.json').read_text()) if (target.parent / 'match_results.json').is_file() else {}
            match = latest_reviews().get(original.get('pdf_sha256'))
            if match and match[1]['directory'] != target.parent:
                return RedirectResponse(f'/reviews/{match[0]}/index.html')
        return FileResponse(target)

    @app.get('/api/reviews')
    def reviews():
        return [dict(id=key, name=item['name'], metrics=item['metrics'], url=f'/reviews/{key}/index.html')
                for key, item, _ in latest_reviews().values()]

    @app.get('/reviews/{review_id}/{relative:path}')
    def review(review_id: str, relative: str):
        refresh_library()
        if review_id not in library:
            raise HTTPException(404, '审核结果不存在')
        target = contained(library[review_id]['directory'], relative)
        if not target.exists() and target.suffix.lower() == '.png' and Path(str(target)+'.artifact.json').is_file():
            from material_ocr.storage_artifacts import materialize_artifact
            try:
                materialize_artifact(target, allowed_root=library_root)
            except (OSError, ValueError, KeyError) as exc:
                raise HTTPException(409, '诊断图片暂时无法恢复，请保留日志检查') from exc
        if not target.is_file():
            raise HTTPException(404, '文件不存在')
        if relative == 'index.html':
            match = latest_reviews().get(library[review_id]['pdf_sha256'])
            if match and match[0] != review_id:
                return RedirectResponse(f'/reviews/{match[0]}/index.html')
        return FileResponse(target)

    def prepare_fill(directory, body):
        result_file = directory / 'match_results.json'
        if not result_file.is_file():
            raise HTTPException(422, '尚无可填表的完整匹配结果')
        result = json.loads(result_file.read_text())
        material_id = result.get('filling_profile', {}).get('material_id', 'rebar')
        try:
            writer = service.registry.get(material_id).form_writer()
            if writer is None:
                raise HTTPException(422, '该材料尚未接入自动填表')
            state = body.get('review_state', {})
            if not isinstance(state, dict):
                raise ValueError('审核状态格式不正确')
            preview = writer.preview(result, state)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc))
        return result, state, writer, preview

    def export_fill(directory, body, url_prefix):
        result, state, writer, preview = prepare_fill(directory, body)
        requested = body.get('group_ids')
        if requested is not None:
            if not isinstance(requested, list) or not requested or len(set(requested)) != len(requested):
                raise HTTPException(422, '请选择至少一行，且不能重复选择')
            selected = [r for r in preview['rows'] if r['group_id'] in requested]
            if len(selected) != len(requested):
                raise HTTPException(422, '所选台账行未完成四表捆绑')
        else:
            selected = [r for r in preview['rows'] if r['approved']]
        if not selected:
            raise HTTPException(422, '没有已确认的台账行，请先核对字段并确认')
        invalid = [r for r in selected if not r['approved']]
        if invalid:
            raise HTTPException(422, '所选台账行未确认或字段不完整：'+'；'.join(
                f"{r['group_id']}（{'、'.join(r['errors']) or '尚未确认'}）" for r in invalid))
        export_id = 'export-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        folder = directory / 'exports' / export_id
        folder.mkdir(parents=True)
        try:
            target = writer.write(selected, None, folder / '材料报审台账.xlsx')
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        snapshot = dict(schema_version='material-ledger-export-v1', export_id=export_id,
            exported_at=datetime.now(timezone.utc).isoformat(), pdf_sha256=result['pdf_sha256'],
            pdf_name=result.get('pdf_name'), result_sha256=hashlib.sha256((directory/'match_results.json').read_bytes()).hexdigest(),
            row_count=len(selected), excluded_bundled_rows=len(preview['rows'])-len(selected),
            pending_matching_groups=preview['counts']['pending_matching_groups'], rows=selected, review_state=state)
        (folder / 'audit.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return dict(export_id=export_id, row_count=len(selected), excluded_bundled_rows=snapshot['excluded_bundled_rows'],
            pending_matching_groups=snapshot['pending_matching_groups'],
            download_url=f'{url_prefix}/exports/{export_id}/{target.name}',
            audit_url=f'{url_prefix}/exports/{export_id}/audit.json')

    def review_directory(review_id):
        refresh_library()
        if review_id not in library:
            raise HTTPException(404, '审核结果不存在')
        return library[review_id]['directory']

    def save_review_state(directory, body):
        result, state, _, preview = prepare_fill(directory, body)
        if not state:
            raise HTTPException(422, '缺少浏览器审核状态')
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
        key = hashlib.sha256(encoded).hexdigest()
        folder = directory / 'review_states'
        folder.mkdir(exist_ok=True)
        target = folder / f'state-{key}.json'
        if not target.exists():
            # Immutable backups preserve user confirmations without changing OCR.
            from material_ocr.workflow import write_json
            write_json(target, dict(schema_version='saved-browser-review-v1',
                saved_at=datetime.now(timezone.utc).isoformat(), pdf_sha256=result['pdf_sha256'],
                pdf_name=result.get('pdf_name'), counts=preview['counts'], review_state=state))
        return dict(saved=True, counts=preview['counts'])

    @app.post('/api/reviews/{review_id}/review-state')
    def review_state_save(review_id: str, body: dict = Body(default={})):
        return save_review_state(review_directory(review_id), body)

    @app.post('/api/jobs/{job_id}/review-state')
    def job_state_save(job_id: str, body: dict = Body(default={})):
        directory, _ = job_review_directory(job_id, body.get('document_id'))
        return save_review_state(directory, body)

    def job_review_directory(job_id, document_id):
        try:
            job = service.get(job_id)
        except KeyError:
            raise HTTPException(404, '任务不存在')
        docs = job['documents']
        if not document_id:
            if len(docs) != 1:
                raise HTTPException(422, '多文件任务须指定 document_id')
            document_id = docs[0]['id']
        doc = next((d for d in docs if d['id'] == document_id), None)
        if not doc or doc['status'] != 'completed':
            raise HTTPException(422, '所选文件尚未完成识别匹配')
        latest = latest_reviews().get(doc.get('sha256'))
        if latest:
            key, item, _ = latest
            return item['directory'], f'/reviews/{key}'
        return (service.root/job_id/'documents'/document_id/'review',
                f'/artifacts/{job_id}/documents/{document_id}/review')

    @app.post('/api/reviews/{review_id}/fill/preview')
    def review_fill_preview(review_id: str, body: dict = Body(default={})):
        return prepare_fill(review_directory(review_id), body)[-1]

    @app.post('/api/reviews/{review_id}/fill')
    def review_fill(review_id: str, body: dict = Body(default={})):
        return export_fill(review_directory(review_id), body, f'/reviews/{review_id}')

    @app.post('/api/jobs/{job_id}/fill/preview')
    def job_fill_preview(job_id: str, body: dict = Body(default={})):
        directory, _ = job_review_directory(job_id, body.get('document_id'))
        return prepare_fill(directory, body)[-1]

    @app.post('/api/jobs/{job_id}/fill')
    def fill(job_id: str, body: dict = Body(default={})):
        directory, prefix = job_review_directory(job_id, body.get('document_id'))
        return export_fill(directory, body, prefix)

    return app
