"""Bind repeated OCR observations to physical cards by original-page geometry."""
from copy import deepcopy
import json
import math
import re

from .matching import heat_valid, norm


HEAT_LABEL = re.compile(r'^(?:炉批号|炉号|轧制批号|轧制号|批号|HEAT(?:NO)?|BATCH(?:NO)?)[.:：]*$', re.I)


def page_polygon(evidence, page):
    """Use original pixel axes only; upright/crop coordinates are not substitutes."""
    polygon = evidence.get('polygon_page')
    if not polygon or len(polygon) < 3:
        return None
    size = evidence.get('source_pixel_size') or page.get('pixel_size')
    target = page.get('pixel_size')
    try:
        sx, sy = (target[0] / size[0], target[1] / size[1]) if size and target else (1, 1)
        points = [(float(x) * sx, float(y) * sy) for x, y in polygon]
        if not all(math.isfinite(v) for p in points for v in p):
            return None
        return points
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def contains_polygon(boundary, polygon):
    """Require the entire value box inside a convex physical card, including edges."""
    if not boundary or not polygon:
        return False
    area = sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(boundary, boundary[1:] + boundary[:1]))
    if abs(area) < 1e-6:
        return False
    direction = 1 if area > 0 else -1
    return all(all(direction * ((b[0] - a[0]) * (p[1] - a[1]) -
                               (b[1] - a[1]) * (p[0] - a[0])) >= -1e-6
                   for a, b in zip(boundary, boundary[1:] + boundary[:1])) for p in polygon)


def _empty_card(page, region):
    entity = region['entity_key']
    return dict(candidate_id=f'nameplate:{page["page_no"]}:{entity}', page_no=page['page_no'],
        entity_key=entity, entity_keys=[entity], row_id=None, certificate_numbers=[],
        heat_lots=[], quantities_t=[], diameters_mm=[], grades=[], associated_record_ids=[],
        status='manual_candidate', write_allowed=False, field_observations=[],
        physical_plate_id=region.get('physical_plate_id'),
        polygon_page=deepcopy(region['polygon_page']), spatial_binding='physical_card')


def _append_observation(candidate, observation, *, source_candidate=None):
    item = deepcopy(observation)
    item['physical_entity_key'] = candidate['entity_key']
    item['spatial_binding'] = 'unique_physical_card_containment'
    if source_candidate:
        item['source_candidate_id'] = source_candidate['candidate_id']
        candidate['associated_record_ids'] = sorted(set(candidate['associated_record_ids'] +
                                                       source_candidate.get('associated_record_ids', [])))
        for entity in source_candidate.get('entity_keys', [source_candidate.get('entity_key')]):
            if entity not in candidate['entity_keys']:
                candidate['entity_keys'].append(entity)
    if item not in candidate['field_observations']:
        candidate['field_observations'].append(item)


def _summarize(candidate):
    for field, destination in [('heat_lot_no', 'heat_lots'), ('diameter_mm', 'diameters_mm'),
                               ('grade', 'grades'), ('quantity_t', 'quantities_t')]:
        values = {str(o.get('value_t') if field == 'quantity_t' and o.get('value_t') is not None
                      else o.get('normalized_value') or o.get('raw_value') or '')
                  for o in candidate['field_observations'] if o.get('field') == field and o.get('accepted')}
        candidate[destination] = sorted(v for v in values if v and (field != 'heat_lot_no' or heat_valid(v)))
    invalid = sorted({norm(o.get('normalized_value') or o.get('raw_value'))
        for o in candidate['field_observations'] if o.get('field') == 'heat_lot_no' and
        not heat_valid(o.get('normalized_value') or o.get('raw_value'))})
    if invalid:
        candidate['rejected_heat_observations'] = invalid


def bind_nameplate_resources(inventory, index):
    """Return cards and unresolved observations without merging by heat value.

    Full-page observations may cover several cards and are assigned separately.
    Unlocated, crossing or multiply-contained boxes remain explicit candidates.
    Pages without saved physical card boundaries retain their existing inventory.
    """
    result, diagnostics = [], []
    for page in index['pages']:
        original = [deepcopy(c) for c in inventory if c['page_no'] == page['page_no']]
        # Card contours on test-report tables are not a page classification.
        # Only refine pages already admitted to the nameplate inventory.
        if not original:
            continue
        regions = {r['entity_key']: r for r in page.get('regions', [])
                   if r.get('entity_key') and (str(r['entity_key']).startswith('card-') or
                       r.get('layout_role') == 'physical_nameplate') and r.get('polygon_page')}
        if not regions:
            result.extend(original)
            continue
        cards = {key: _empty_card(page, region) for key, region in regions.items()}
        boundaries = {key: page_polygon(region, page) for key, region in regions.items()}

        def owners(observation):
            polygon = page_polygon(observation.get('value_evidence') or {}, page)
            return [key for key, boundary in boundaries.items() if contains_polygon(boundary, polygon)]

        for candidate in original:
            entity = candidate.get('entity_key')
            if entity in cards:
                for observation in candidate.get('field_observations', []):
                    _append_observation(cards[entity], observation, source_candidate=candidate)
                continue
            unresolved = []
            for observation in candidate.get('field_observations', []):
                targets = owners(observation)
                if len(targets) == 1:
                    _append_observation(cards[targets[0]], observation, source_candidate=candidate)
                else:
                    unresolved.append(observation)
            if not unresolved and candidate.get('field_observations'):
                continue
            # An empty page-only placeholder carries no independent observation
            # once every actual physical card on that page is represented.
            if (not candidate.get('field_observations') and entity is None and
                    not any(candidate.get(key) for key in ('heat_lots', 'diameters_mm', 'grades', 'quantities_t'))):
                continue
            candidate['field_observations'] = unresolved
            candidate['spatial_unresolved'] = True
            _summarize(candidate)
            locations = [dict(deepcopy(o.get('value_evidence') or {}), page_no=page['page_no'],
                              source_role='nameplate') for o in unresolved]
            issue = dict(code='nameplate_observation_unassigned', field='铭牌',
                reason='全页铭牌观测缺少原图位置、跨越物理卡或位置归属不唯一，未作为独立匹配资源',
                candidate_id=candidate['candidate_id'], locations=locations)
            candidate['spatial_diagnostics'] = [issue]
            diagnostics.append(issue)
            result.append(candidate)

        # A damaged crop can lose its label while the saved full-page OCR has
        # accepted a labelled value. Recall that exact observation only inside
        # one real card; the surrounding delivery/crop role does not override it.
        seen = set()
        for region in page.get('regions', []):
            values = list(region.get('extracted_fields', {}).get('heat_lot_no', []))
            values += [o for o in region.get('field_observations', []) if o.get('field') == 'heat_lot_no']
            for value in values:
                ev = deepcopy(value.get('evidence') or {})
                heat = norm(value.get('normalized_value') or value.get('value'))
                if value.get('validation_status') != 'accepted' or not heat_valid(heat) or not HEAT_LABEL.fullmatch(norm(ev.get('label'))):
                    continue
                observation = dict(field='heat_lot_no', raw_value=value.get('value'),
                    ocr_raw_value=value.get('raw_value'), normalized_value=heat, accepted=True,
                    status='accepted_labelled_spatial_observation', region_id=region['region_id'],
                    entity_key=region.get('entity_key'), row_id=None, value_evidence=ev)
                key = json.dumps(observation, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                targets = owners(observation)
                if len(targets) == 1:
                    _append_observation(cards[targets[0]], observation)
        for card in cards.values():
            _summarize(card)
            result.append(card)
    return result, diagnostics
