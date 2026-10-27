"""页级方向分类；所有角度表示使页面归正的逆时针旋转角度。"""
import numpy as np


def rotate_page(image, angle):
    if angle not in (0,90,180,270):
        raise ValueError('rotation must be 0/90/180/270')
    h,w = image.shape[:2]
    matrices = {
        0: [[1,0,0],[0,1,0],[0,0,1]],
        90: [[0,1,0],[-1,0,w-1],[0,0,1]],
        180: [[-1,0,w-1],[0,-1,h-1],[0,0,1]],
        270: [[0,-1,h-1],[1,0,0],[0,0,1]],
    }
    return np.ascontiguousarray(np.rot90(image,angle//90)),np.array(matrices[angle],dtype=float)


def create_orientation(device):
    from paddleocr import DocImgOrientationClassification
    return DocImgOrientationClassification(model_name='PP-LCNet_x1_0_doc_ori',device=device)


def predict_orientation(model, image, json_path):
    import json
    results = list(model.predict(image,batch_size=1))
    if len(results)!=1:
        raise ValueError('方向模型返回数量异常')
    results[0].save_to_json(str(json_path))
    data = json.loads(json_path.read_text(encoding='utf-8'))
    data = data.get('res',data)
    angle,score = int(data['label_names'][0]),float(data['scores'][0])
    if angle not in (0,90,180,270):
        raise ValueError('方向模型返回非法角度')
    return angle,score
