/* Field review is separate from matching; the server rechecks trusted sources. */
(function(){
  'use strict';
  const el=id=>document.getElementById(id), escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const path=location.pathname;
  const library=path.match(/^\/reviews\/([^/]+)\//), job=path.match(/^\/artifacts\/([^/]+)\/documents\/([^/]+)\/review\//);
  const endpoint=library?'/api/reviews/'+library[1]+'/fill':job?'/api/jobs/'+job[1]+'/fill':null;
  let preview=null,activeGroup=null,timer=null,busy=false,requestGeneration=0;
  function notice(text,error=false){el('fill-message').textContent=text;el('fill-message').className='ledger-message '+(error?'warn':'good')}
  async function request(url){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review_state:state,...(job?{document_id:job[2]}:{})})});const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'填表请求失败');return data}
  async function backup(){if(!endpoint||!D.filling_profile)return;try{await request(endpoint.replace(/\/fill$/,'/review-state'))}catch(err){notice('审核记录备份未成功：'+err.message,true)}}
  function current(){return preview?.rows.find(r=>r.group_id===activeGroup)}
  function sourceLink(page,label){return page?`<a class="button" href="page_images/page_${String(page).padStart(3,'0')}_upright.png" target="_blank" rel="noopener">${escape(label)} · 第${page}页</a>`:''}
  function saveDraft(confirmed=false){const row=current();if(!row)return;const values=Object.fromEntries(preview.columns.map(c=>[c.key,el('fill-field-'+c.key).value]));state.field_reviews[row.group_id]={signature:row.signature,context_id:row.context_id,values,confirmed,confirmed_at:confirmed?new Date().toISOString():null,note:el('fill-note').value};persist('fields');row.values=values;row.approved=confirmed;if(!confirmed)el('fill-export').disabled=true;renderRows()}
  function renderRows(){
    if(!preview)return;
    const approved=preview.rows.filter(r=>r.approved).length;
    el('fill-counts').innerHTML=[`已捆绑 ${preview.rows.length} 行`,`字段已确认 ${approved} 行`,`待匹配 ${preview.counts.pending_matching_groups} 组`].map(x=>'<span class="chip">'+x+'</span>').join('');
    el('fill-export').disabled=busy||approved===0;
    el('fill-rows').innerHTML=preview.rows.length?`<table><thead><tr><th>铭牌页</th><th>规格品种</th><th>炉批号</th><th>验收数量(t)</th><th>字段审核</th></tr></thead><tbody>${preview.rows.map(r=>`<tr class="${activeGroup===r.group_id?'active':''}"><td><button data-fill-group="${escape(r.group_id)}">第${r.sources.nameplate.page_no}页</button></td><td>${escape(r.values.specification)||'待补正'}</td><td>${escape(r.values.heat_lot_no)||'待补正'}</td><td>${escape(r.values.quantity_t)||'待补正'}</td><td class="${r.approved?'good':'warn'}">${r.approved?'已确认':Object.values(r.fields).some(f=>f.status==='ambiguous')?'字段候选冲突':r.errors.length?'字段待补正':'待核对'}</td></tr>`).join('')}</tbody></table>`:'<p>先完成四表匹配与捆绑，台账行会出现在这里。</p>';
    document.querySelectorAll('[data-fill-group]').forEach(button=>button.onclick=()=>{activeGroup=button.dataset.fillGroup;renderRows();renderEditor()});
  }
  function renderEditor(){const row=current();if(!row){el('fill-editor').textContent='';return}
    el('fill-editor').innerHTML=`<b>铭牌第${row.sources.nameplate.page_no}页 · ${row.approved?'字段已确认':'核对并补正字段'}</b><div class="sub">检验来源：${row.record_ids.map(escape).join('、')}</div><label>报审信息来源<select id="fill-context" class="ledger-context"><option value="">请选择报审表（或按原图填写）</option>${preview.approval_contexts.map(c=>`<option value="${escape(c.context_id)}" ${row.context_id===c.context_id?'selected':''}>第${c.page_no}页 · ${escape(c.fields.material_name.value)} · ${escape(c.fields.supply_date.value)} · ${escape(c.fields.usage_location.value)}</option>`).join('')}</select></label><div class="ledger-source-links">${sourceLink(preview.approval_contexts.find(c=>c.context_id===row.context_id)?.page_no,'报审表')}${sourceLink(row.sources.nameplate.page_no,'铭牌')}${sourceLink(row.sources.delivery.page_no,'送货单')}${sourceLink(row.sources.certificate.page_no,'质证书')}${row.inspection_pages.map(page=>sourceLink(page,'检验记录')).join('')}</div><div class="ledger-fields">${preview.columns.map(c=>{const f=row.fields[c.key];return `<div class="ledger-field"><label for="fill-field-${c.key}">${escape(c.label)}${c.required?' *':'（可空）'}${c.unit?' · 吨':''}</label><input id="fill-field-${c.key}" data-fill-key="${c.key}" type="${c.type==='date'?'date':'text'}" ${c.type==='number'?'inputmode="decimal"':''} value="${escape(row.values[c.key])}"><small>${({inspection:'检验进场数量',nameplate:'铭牌',delivery:'送货行',mixed:'铭牌＋送货行'})[f.source_role]||'OCR来源'} · ${f.status==='missing'?'OCR未提取，请核对原图填写':f.status==='ambiguous'?'字段候选冲突：有多个 OCR 值，请核对原图后选择或填写':'OCR：'+escape(f.value)}</small>${f.warnings.length?`<small class="warn">${f.warnings.map(escape).join('；')}</small>`:''}${f.candidates.length>1?`<select data-fill-choice="${c.key}"><option value="">选择原OCR值</option>${f.candidates.map(v=>`<option value="${escape(v.value)}">${escape(v.value)}</option>`).join('')}</select>`:''}<details><summary>查看字段来源与坐标</summary><div>${f.warnings.map(escape).join('；')}</div><pre>${escape(JSON.stringify({采用候选:f.candidates,对照候选:f.alternatives||[]},null,2))}</pre></details></div>`}).join('')}</div><label class="sub">字段补正说明<textarea id="fill-note" style="width:100%;height:55px" placeholder="可填写补正依据">${escape(row.review_note)}</textarea></label><div class="toolbar"><button id="fill-confirm-row" class="primary">确认本行字段</button><span id="fill-row-message" class="warn">${row.errors.map(escape).join('；')}</span></div>`;
    el('fill-context').onchange=async()=>{saveDraft(false);state.field_reviews[row.group_id].context_id=el('fill-context').value;const ctx=preview.approval_contexts.find(c=>c.context_id===el('fill-context').value);for(const c of preview.columns.filter(c=>c.role==='approval'))state.field_reviews[row.group_id].values[c.key]=ctx?.fields[c.key]?.value||'';persist('fields');await refresh()};
    document.querySelectorAll('[data-fill-key]').forEach(input=>input.oninput=()=>saveDraft(false));
    document.querySelectorAll('[data-fill-choice]').forEach(select=>select.onchange=()=>{if(select.value){el('fill-field-'+select.dataset.fillChoice).value=select.value;saveDraft(false)}});
    el('fill-note').oninput=()=>saveDraft(false);
    el('fill-confirm-row').onclick=async()=>{saveDraft(true);await refresh();const updated=current();notice(updated?.approved?'本行字段已确认，可导出 Excel。':updated?.errors.join('；')||'字段尚未确认',!updated?.approved)};
  }
  async function refresh(){
    if(!endpoint||!D.filling_profile){notice(!endpoint?'请从材料工作台打开审核页，以使用 Excel 导出。':'这份旧结果尚无填表字段，请重新生成匹配页。',true);el('fill-refresh').disabled=true;return}
    const generation=++requestGeneration;busy=true;el('fill-refresh').disabled=true;el('fill-export').disabled=true;
    try{const next=await request(endpoint+'/preview');if(generation!==requestGeneration)return;preview=next;if(!preview.rows.some(r=>r.group_id===activeGroup))activeGroup=preview.rows[0]?.group_id||null;renderRows();renderEditor();notice('台账已更新：规格优先铭牌、缺失参考送货行；验收数量优先检验进场数量。核对字段后，点击“确认本行字段”，再导出已确认行。');await backup()}
    catch(err){if(generation===requestGeneration){preview=null;el('fill-rows').textContent='';el('fill-editor').textContent='';notice(err.message,true)}}
    finally{if(generation===requestGeneration){busy=false;el('fill-refresh').disabled=false;renderRows()}}
  }
  document.addEventListener('four-form-state-changed',event=>{if(event.detail?.kind==='fields')return;clearTimeout(timer);timer=setTimeout(refresh,250)});
  el('fill-refresh').onclick=refresh;
  el('fill-export').onclick=async()=>{busy=true;el('fill-export').disabled=true;try{const data=await request(endpoint);const link=document.createElement('a');link.href=data.download_url;link.download=D.pdf_name.replace(/\.pdf$/i,'')+'_材料报审台账.xlsx';link.click();notice(`已导出 ${data.row_count} 行；其余已捆绑未确认 ${data.excluded_bundled_rows} 行，待匹配 ${data.pending_matching_groups} 组。`)}catch(err){notice(err.message,true)}finally{busy=false;renderRows()}};
  refresh();
})();
