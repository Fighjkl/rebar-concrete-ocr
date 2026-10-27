"""已归正固定表格的OCR适配器：保留透视裁剪，禁用细长文字90度旋转。

不修改site-packages。临时替换仅包围同步单元格predict调用，结束/异常均恢复。
当前batch顺序执行；不要在同一进程同时跑其他OCR线程。
"""
import inspect
import json
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np


def crop_without_quarter_turn(image, points):
    points=np.asarray(points,dtype=np.float32)
    if points.shape!=(4,2):
        raise ValueError('Expected four ordered text-box corners')
    width=int(max(np.linalg.norm(points[0]-points[1]),np.linalg.norm(points[2]-points[3])))
    height=int(max(np.linalg.norm(points[0]-points[3]),np.linalg.norm(points[1]-points[2])))
    if min(width,height)<1:
        raise ValueError('Degenerate text crop')
    destination=np.float32([[0,0],[width,0],[width,height],[0,height]])
    matrix=cv2.getPerspectiveTransform(points,destination)
    return cv2.warpPerspective(image,matrix,(width,height),
        borderMode=cv2.BORDER_REPLICATE,flags=cv2.INTER_CUBIC)


def checked_cropper_class():
    from paddlex.inference.pipelines.components.common.crop_image_regions import CropByPolys
    # 私有接口发生变化时明确停止，不默默运行一个无法确认的补丁。
    source=inspect.getsource(CropByPolys.get_rotate_crop_image)
    if 'np.rot90' not in source or '1.5' not in source:
        raise RuntimeError('Unsupported PaddleX cropper: expected tall-text rotation rule; inspect installed source')
    return CropByPolys


def save_image(path,image):
    ok,data=cv2.imencode('.png',image)
    if not ok:
        raise ValueError('Cannot encode recognition input')
    data.tofile(str(path))


class FixedCellOCR:
    def __init__(self,engine,device='gpu:0'):
        self.engine=engine
        self.device=device
        self.cropper_class=None

    def direct_ocr(self,image,path):
        # 单独识别模块不经过检测器和CropByPolys；按需初始化并跨页复用。
        model=getattr(self.engine,'_rebar_direct_recognizer',None)
        if model is None:
            from paddleocr import TextRecognition
            model=TextRecognition(model_name='PP-OCRv5_server_rec',device=self.device)
            self.engine._rebar_direct_recognizer=model
        save_image(path,image)
        results=list(model.predict(input=str(path),batch_size=1))
        if len(results)!=1:
            raise ValueError('Unexpected direct recognition result count')
        results[0].save_to_json(str(path.with_suffix('.json')))
        data=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        data=data.get('res',data)
        text,score=data['rec_text'],float(data['rec_score'])
        return text,[{'text':text,'score':score}] if text.strip() else []

    def predict(self, input, **kwargs):
        if self.cropper_class is None:
            self.cropper_class=checked_cropper_class()
        path=Path(input)
        audit=[]
        def upright_crop(cropper,image,points):
            result=crop_without_quarter_turn(image,points)
            event={'quad_in_cell':np.asarray(points).tolist(),
                   'output_size':[result.shape[1],result.shape[0]],
                   'quarter_turn_applied':False}
            # 保存序号及序号重试的小图，供确认“1”仍竖直。
            if '_row_no' in path.stem:
                name=f'{path.stem}_rec_input_{len(audit)+1:03d}.png'
                save_image(path.parent/name,result)
                event['recognizer_image']=name
            audit.append(event)
            return result
        # 消耗完predict生成器后再恢复，避免只包住生成器创建而未包住推理。
        try:
            with patch.object(self.cropper_class,'get_rotate_crop_image',upright_crop):
                results=list(self.engine.predict(input=input,**kwargs))
        finally:
            path.with_name(path.stem+'_crop_audit.json').write_text(json.dumps({
                'policy':'fixed_cell_preserve_upright_v6','captures':audit,
                'stage':'recognizer input before resize/normalization'},ensure_ascii=False,indent=2),encoding='utf-8')
        return results
