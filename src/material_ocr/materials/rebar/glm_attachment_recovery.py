"""Bounded GLM retries for mixed attachment pages; source images stay in memory.

Overlapping viewports are OCR input regions, not inferred physical card boxes.
Page orientation comes from the cached local orientation model, never from a
neighbouring page. Each new text observation retains its source polygon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

POLICY = 'oriented-attachment-views-v1'


def build_plan(pdf, checkpoints, *, model_root, pages=None, panels=False, certificates=False):
    import numpy as np
    import pypdfium2
    from paddlex import create_model
    from paddlex.inference.utils.pp_option import PaddlePredictorOption
    from .glm_production import _render_page, _unrotate_polygon

    selected = set(pages or [p['page_no'] for p in checkpoints])
    model = create_model('PP-LCNet_x1_0_doc_ori',
        model_dir=str(Path(model_root) / 'PP-LCNet_x1_0_doc_ori'), device='cpu',
        pp_option=PaddlePredictorOption(run_mode='paddle', device_type='cpu', cpu_threads=2))
    document = pypdfium2.PdfDocument(str(pdf))
    regions, orientations = [], []
    try:
        for checkpoint in checkpoints:
            n = checkpoint['page_no']
            if n not in selected:
                continue
            image = _render_page(document, n, 180)
            try:
                prediction = list(model.predict(np.asarray(image)))[0]
                rotation = int(prediction['label_names'][0])
                size = list(image.size)
                width, height = (size[::-1] if rotation in (90, 270) else size)
                orientations.append(dict(page_no=n, rotation_ccw=rotation,
                                         score=float(prediction['scores'][0])))
                if certificates:
                    # Small upright inputs retain the printed labels beside
                    # the values. These are viewports, not inferred cell boxes.
                    for index, (name, bounds) in enumerate((
                            ('header', (0,0,1,.43)),
                            ('lot_rows', (0,.34,.48,.72)))):
                        x1,y1,x2,y2=[round(v*s) for v,s in zip(bounds,(width,height,width,height))]
                        regions.append(dict(region_id=f'p{n:03d}-certificate-{name}',page_no=n,
                            field_name='certificate_view',polygon_page=_unrotate_polygon(
                                [[x1,y1],[x2,y1],[x2,y2],[x1,y2]],size,rotation),
                            source_pixel_size=size,render_dpi=180,rotation_ccw=rotation,
                            coordinate_system='original_rendered_page_pixels_top_left',
                            geometry_sequence=index+1,geometry_sequence_count=2,
                            kind='certificate_recovery_view',boundary_method='oriented_certificate_label_viewport',
                            physical_card_boundary_verified=False))
                    continue
                if panels:
                    from material_ocr.ocr.glm_geometry import detect_panel_viewports
                    upright = image.rotate(rotation, expand=True)
                    try:
                        detected = detect_panel_viewports(upright, page_no=n)
                    finally:
                        upright.close()
                    for index, region in enumerate(detected):
                        region.update(page_no=n, field_name='attachment_panel',
                            polygon_page=_unrotate_polygon(region['polygon_page'], size, rotation),
                            source_pixel_size=size, render_dpi=180, rotation_ccw=rotation,
                            coordinate_system='original_rendered_page_pixels_top_left',
                            geometry_sequence=index+1, geometry_sequence_count=len(detected))
                        regions.append(region)
                    continue
                # Split viewports deliberately overlap so a card/table at the
                # cut is still available complete in at least one view. These
                # are input viewports, never a claim of physical card edges.
                for index, (top, bottom) in enumerate(((0, .65), (.40, 1))):
                    y1, y2 = round(height*top), round(height*bottom)
                    polygon = _unrotate_polygon([[0, y1], [width, y1], [width, y2], [0, y2]], size, rotation)
                    regions.append(dict(region_id=f'p{n:03d}-attachment-view-{index}', page_no=n,
                        field_name='attachment_view', polygon_page=polygon, source_pixel_size=size,
                        render_dpi=180, rotation_ccw=rotation,
                        coordinate_system='original_rendered_page_pixels_top_left',
                        geometry_sequence=index+1, geometry_sequence_count=2,
                        kind='attachment_recovery_view', boundary_method='overlapping_input_viewport',
                        physical_card_boundary_verified=False))
            finally:
                image.close()
    finally:
        document.close()
    body = dict(schema_version='glm-cell-supplement-v1', policy=(
                'certificate-label-views-v1' if certificates else 'independent-panels-v2' if panels else POLICY),
                pdf_sha256=checkpoints[0]['pdf_sha256'], regions=regions, orientations=orientations)
    body['plan_sha256'] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest()
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--checkpoints', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pages', type=int, nargs='+')
    parser.add_argument('--panels', action='store_true')
    parser.add_argument('--certificates', action='store_true')
    parser.add_argument('--model-root', type=Path, default=Path('models/paddlex_cache/official_models'))
    args = parser.parse_args()
    checkpoints = json.loads(args.checkpoints.read_text())
    plan = build_plan(args.pdf, checkpoints, model_root=args.model_root, pages=args.pages,
                      panels=args.panels, certificates=args.certificates)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print(json.dumps(dict(plan=str(args.output), pages=len(plan['orientations']), regions=len(plan['regions']))))


if __name__ == '__main__':
    main()
