const form=document.querySelector('#release-form');
const result=document.querySelector('#release-result');
function collect(){
  const data=Object.fromEntries(new FormData(form).entries());
  const selected=(data.selected||'').split(',').map(s=>s.trim()).filter(Boolean);
  return {run_id:data.run_id,baseline_run_id:data.baseline_run_id,
          voyage:data.voyage,request_id:data.request_id,selected};
}
function render(data){
  if(data.status==='放行'){
    const rows=data.lines.map(l=>`<tr><td>${l.node_id}</td><td>${l.root}</td><td>${l.pieces}</td><td>${l.net_kg}</td><td>${l.gross_kg}</td></tr>`).join('');
    result.innerHTML=`<div class="ok">放行 · 合计 件数 ${data.totals.pieces} / 净重 ${data.totals.net_kg} / 毛重 ${data.totals.gross_kg}${data.replayed?' · 申请重放回显':''}</div><table><thead><tr><th>节点</th><th>主单</th><th>件数</th><th>净重</th><th>毛重</th></tr></thead><tbody>${rows}</tbody></table>`;
  }else{
    const rows=(data.blocks||[]).map(b=>`<tr><td>${b.id||''}</td><td class="fail">${b.code}</td><td>${b.detail}</td></tr>`).join('');
    result.innerHTML=`<div class="fail">阻断 · 停止交接${data.replayed?' · 申请重放回显':''}</div><table><thead><tr><th>节点</th><th>原因码</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
}
async function submit(endpoint){
  const payload=collect();
  if(!payload.voyage){result.innerHTML='<div class="fail">请填写航次编号</div>';return;}
  try{
    const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail||'请求失败');
    render(data);
    if(endpoint.endsWith('/ship'))setTimeout(()=>location.reload(),1200);
  }catch(e){result.innerHTML=`<div class="fail">${e.message}</div>`;}
}
document.querySelector('#preview-btn').addEventListener('click',()=>submit('/api/release/preview'));
document.querySelector('#ship-btn').addEventListener('click',()=>submit('/api/release/ship'));
