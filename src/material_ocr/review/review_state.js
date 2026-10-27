/* Pure state transitions shared by the review page and regression checks. */
(function (root) {
  'use strict';
  const roles = ['certificate', 'nameplate', 'delivery'];
  const names = {certificate:'certificate_page_inventory',nameplate:'nameplate_inventory',delivery:'delivery_inventory'};
  const copy = x => JSON.parse(JSON.stringify(x));
  function inventory(data, role) { return data[names[role]] || []; }
  function initialize(data, saved = {}) {
    return {schema_version:'four-form-review-state-v1',pdf_sha256:data.pdf_sha256,
      manual_bundles:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.manual_bundles || {} : {}),
      released_auto_groups:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.released_auto_groups || [] : []),
      drafts:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.drafts || {} : {}),
      comments:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.comments || {} : {}),
      field_reviews:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.field_reviews || {} : {}),
      history:copy(saved.pdf_sha256 === data.pdf_sha256 ? saved.history || [] : [])};
  }
  function bundles(data, state) {
    const auto = (data.four_form_bundles || []).filter(b => !state.released_auto_groups.includes(b.group_id) && !state.manual_bundles[b.group_id]);
    const manual = Object.values(state.manual_bundles).filter(b => roles.every(r => inventory(data,r).some(c => c.candidate_id === b.forms[r]?.candidate_id)));
    return [...auto,...manual];
  }
  function bundleFor(data,state,entry) { return bundles(data,state).find(b => b.group_id === entry.association.group_id); }
  function selection(data,state,entry) {
    const b = bundleFor(data,state,entry);
    return b ? Object.fromEntries(roles.map(r => [r,b.forms[r].candidate_id])) : {...entry.association.selection,...(state.drafts[entry.association.group_id] || {})};
  }
  function remaining(data,state,entry,role,all = false) {
    const group = entry.association.group_id;
    const used = new Set(bundles(data,state).filter(b => b.group_id !== group).map(b => b.forms[role].candidate_id));
    const current = selection(data,state,entry)[role];
    const ranked = entry['manual_'+role+'_candidates'] || inventory(data,role);
    return ranked.filter(c => all || !used.has(c.candidate_id) || c.candidate_id === current)
      .map(c => ({...c,occupied:used.has(c.candidate_id)}));
  }
  function select(data,state,entry,role,id) {
    if (!roles.includes(role) || !inventory(data,role).some(c => c.candidate_id === id)) throw Error('候选不存在，请刷新页面');
    if (bundleFor(data,state,entry)) throw Error('请先解除本组捆绑再修改');
    const group = entry.association.group_id;
    state.drafts[group] = {...selection(data,state,entry),[role]:id};
  }
  function confirm(data,state,entry,comment,shareCertificate = false) {
    const group = entry.association.group_id, selected = selection(data,state,entry), forms = {};
    if (bundleFor(data,state,entry)) throw Error('本组已完成捆绑');
    for (const role of roles) {
      const c = inventory(data,role).find(c => c.candidate_id === selected[role]);
      if (!c) throw Error('请先选择质量证明书、铭牌和送货单三类表单');
      const used = bundles(data,state).filter(b => b.group_id !== group && b.forms[role].candidate_id === c.candidate_id);
      if (used.length && !(role === 'certificate' && shareCertificate)) throw Error(role === 'certificate' ? '该证书已捆绑；复用证书时请勾选确认共用' : '该铭牌或送货行已被其他物料捆绑，请先解除对应捆绑');
      forms[role] = copy(c);
    }
    const members = data.entries.filter(e => e.association.group_id === group);
    forms.inspection = members.map(e => ({record_id:e.record_id,page_no:e.record.page_no,row_no:e.record.row_no}));
    const b = {bundle_id:'bundle-'+group.replace(/^group-/,''),group_id:group,record_ids:members.map(e=>e.record_id),
      state:'human_confirmed',origin:'human_review',confirmed_at:new Date().toISOString(),comment:comment || '',
      shared_certificate_confirmed:shareCertificate,forms,original_records:members.map(e=>copy(e.record)),
      reviewed_conflicts:members.flatMap(e => copy(e.conflicts)),reviewed_reasons:members.flatMap(e => e.review_reasons)};
    state.manual_bundles[group] = b;
    state.history.push({action:'confirm_bundle',group_id:group,at:b.confirmed_at,selection:copy(selected)});
    return b;
  }
  function release(data,state,entry) {
    const group=entry.association.group_id,b=bundleFor(data,state,entry);
    if (!b) return;
    state.drafts[group] = Object.fromEntries(roles.map(r=>[r,b.forms[r].candidate_id]));
    delete state.manual_bundles[group];
    delete state.field_reviews[group];
    if (!state.released_auto_groups.includes(group)) state.released_auto_groups.push(group);
    state.history.push({action:'release_bundle',group_id:group,at:new Date().toISOString()});
  }
  function exportResult(data,state) {
    const current=bundles(data,state), used=Object.fromEntries(roles.map(r=>[r,new Set(current.map(b=>b.forms[r].candidate_id))]));
    return {schema_version:'four-form-association-export-v1',pdf_sha256:data.pdf_sha256,pdf_name:data.pdf_name,
      exported_at:new Date().toISOString(),four_form_bundles:copy(current),
      remaining_inventories:Object.fromEntries(roles.map(r=>[r,inventory(data,r).filter(c=>!used[r].has(c.candidate_id))])),
      pending_record_ids:data.entries.filter(e=>!bundleFor(data,state,e)).map(e=>e.record_id),
      review_state:copy(state),formal_fill_executed:false};
  }
  const api={roles,inventory,initialize,bundles,bundleFor,selection,remaining,select,confirm,release,exportResult};
  if (typeof module !== 'undefined' && module.exports) module.exports=api;
  else root.FourFormReview=api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
