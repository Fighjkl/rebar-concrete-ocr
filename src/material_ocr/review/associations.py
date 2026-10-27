"""Four-form bundles over immutable OCR observations.

Only unique associations without unresolved checks finish automatically.
Inventories remain complete; their availability is derived from bundles.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json

ROLES = ('certificate', 'nameplate', 'delivery')
INVENTORIES = dict(certificate='certificate_page_inventory', nameplate='nameplate_inventory',
                  delivery='delivery_inventory')


def content_group(record):
    fields = ('row_no', 'material_name', 'diameter_mm', 'grade', 'weight_t', 'manufacturer',
              'certificate_no', 'inspection_date', 'entry_date')
    text = json.dumps({k: record.get(k) for k in fields}, ensure_ascii=False, sort_keys=True)
    return 'group-' + hashlib.sha256(text.encode()).hexdigest()[:16]


def default_selection(entry, inventories):
    """Resolve strict physical identities; never use rank scores to confirm."""
    selected = {}
    certificates = {c['page_no'] for c in entry['quality_certificate_candidates']}
    plates = {(c['nameplate_page'], c.get('heat_lot_no')) for c in entry['nameplate_candidates']}
    deliveries = {(c['page_no'], c.get('entity_key'), c.get('row_id')) for c in entry['delivery_note_candidates']}
    predicates = dict(
        certificate=lambda c: c['page_no'] in certificates if len(certificates) == 1 else False,
        nameplate=lambda c: len(plates) == 1 and any(p == c['page_no'] and
            (h in [str(v).upper().rstrip('.。') for v in c['heat_lots']] if h else not c['heat_lots']) for p, h in plates),
        delivery=lambda c: len(deliveries) == 1 and (c['page_no'], c.get('entity_key'), c.get('row_id')) in deliveries)
    for role in ROLES:
        matches = [c for c in inventories[role] if predicates[role](c)]
        selected[role] = matches[0]['candidate_id'] if len(matches) == 1 else None
    return selected


def make_bundle(group_id, entries, selection, inventories):
    forms = {role: deepcopy(next(c for c in inventories[role] if c['candidate_id'] == selection[role]))
             for role in ROLES}
    forms['inspection'] = [dict(record_id=e['record_id'], page_no=e['record'].get('page_no'),
                                row_no=e['record'].get('row_no')) for e in entries]
    return dict(bundle_id='bundle-' + group_id.removeprefix('group-'), group_id=group_id,
                record_ids=[e['record_id'] for e in entries], state='auto_matched',
                origin='unique_four_form_match', forms=forms,
                original_records=[deepcopy(e['record']) for e in entries])


def prepare_associations(result):
    inventories = {role: result[INVENTORIES[role]] for role in ROLES}
    groups = defaultdict(list)
    for e in result['entries']:
        group = content_group(e['record'])
        groups[group].append(e)
        selection = default_selection(e, inventories)
        e['association'] = dict(group_id=group, state='needs_review', selection=selection,
                                bundle_id=None, review_required=True)
    proposed = []
    for group, entries in groups.items():
        first = entries[0]['association']['selection']
        if all(all(e['association']['selection'].values()) and not e['review_reasons'] and
               e['status'] == 'candidate_complete' and e['association']['selection'] == first for e in entries):
            proposed.append(make_bundle(group, entries, first, inventories))
    # Certificates may cover multiple materials. Plates and delivery rows cannot
    # be consumed twice by independent inspection groups without human review.
    owners = defaultdict(set)
    for b in proposed:
        for role in ('nameplate', 'delivery'):
            owners[b['forms'][role]['candidate_id']].add(b['group_id'])
    bundles = []
    for b in proposed:
        if any(len(owners[b['forms'][role]['candidate_id']]) > 1 for role in ('nameplate', 'delivery')):
            for e in groups[b['group_id']]:
                e['review_reasons'].append('attachment_used_by_multiple_inspection_groups')
            continue
        bundles.append(b)
        for e in groups[b['group_id']]:
            e['association'].update(state='auto_matched', bundle_id=b['bundle_id'], review_required=False)
    result['four_form_bundles'] = bundles
    result['association_groups'] = [dict(group_id=g, record_ids=[e['record_id'] for e in entries])
                                    for g, entries in groups.items()]
    result['review_queue'] = [dict(record_id=e['record_id'], group_id=e['association']['group_id'],
        status='pending_human_review', reasons=e['review_reasons'] or ['four_form_identity_not_unique'],
        options=e['manual_review_options'],
        manual_certificate_candidates=e['manual_certificate_candidates'],
        manual_nameplate_candidates=e['manual_nameplate_candidates'],
        manual_delivery_candidates=e['manual_delivery_candidates'], write_allowed=False)
        for e in result['entries'] if e['association']['review_required']]
    result['metrics'].update(auto_matched_records=sum(not e['association']['review_required'] for e in result['entries']),
        automatic_bundle_count=len(bundles), review_task_count=len(result['review_queue']),
        association_package_generated=bool(bundles))
    for role in ROLES:
        for c in inventories[role]:
            c['bundled_group_ids'] = [b['group_id'] for b in bundles if b['forms'][role]['candidate_id'] == c['candidate_id']]
            c['availability'] = 'bundled' if c['bundled_group_ids'] else 'remaining'
    return result
