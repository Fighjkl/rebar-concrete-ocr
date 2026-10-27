"""保守空白检查：OCR无文字且图像无明显内容才列为空白候选。"""
import cv2
import numpy as np


def ink_evidence(crop):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 35, 15)
    h, w = mask.shape
    # 只消除仍留在裁剪格内的长框线，不在OCR输入上去线。
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w*.85)), 1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(h*.85)))))
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    residual = cv2.bitwise_and(mask, cv2.bitwise_not(grid))
    # 裁剪边缘6px仅在空白辅助判断中屏蔽；OCR仍读取整个裁剪图。
    # 该规则可能漏掉极靠边且OCR也失败的字迹，因此只输出可审计的空白候选。
    margin = min(6, h // 10, w // 10)
    if margin:
        residual[:margin] = 0
        residual[-margin:] = 0
        residual[:, :margin] = 0
        residual[:, -margin:] = 0
    _, _, stats, _ = cv2.connectedComponentsWithStats(residual, 8)
    # 小于20像素的孤立连通域作为扫描噪点；这是当前标准尺寸的实验阈值。
    components = [s for s in stats[1:] if s[cv2.CC_STAT_AREA] >= 20]
    return {"has_ink": bool(components), "component_count": len(components),
            "residual_pixels": int(sum(s[cv2.CC_STAT_AREA] for s in components))}


def classify_row(raw, ink):
    if any(value.strip() for value in raw.values()):
        return "content"
    if any(value["has_ink"] for value in ink.values()):
        return "unreadable"
    return "blank_candidate"
