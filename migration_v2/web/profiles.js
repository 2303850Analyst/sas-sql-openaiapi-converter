
const aiDefaults={name:'',url:'',url_kind:'base',model:'',reviewer:'',auth:'bearer',tariff:'unknown',
 approve_source:false,budget_usd:4,input_rate:0,output_rate:0,timeout:90,max_tokens:8000,
 allow_http:false,ca_file:'',proxy:'',token_parameter:'max_tokens',response_format:'none',thinking:'default',reviewer_reasoning:'default'};
const aiField=key=>$('ai-'+key.replaceAll('_','-'));
let aiState={active:null,profiles:[]};
function aiForm(id){
 const found=aiState.profiles.find(row=>row.id===id);
 for(const [key,initial] of Object.entries(aiDefaults)){
  const input=aiField(key),value=found?.settings[key]??initial;
  if(typeof initial==='boolean')input.checked=value;else input.value=value;
 }
 $('ai-key').value='';$('ai-key-action').value='keep';$('ai-models').replaceChildren();
 $('ai-key-status').textContent=({protected:'Ключ сохранён в DPAPI',session:'Ключ действует только на эту сессию',none:'Ключ не сохранён'})[found?.key_storage||'none'];
 $('ai-endpoint').textContent=found?'Конечный адрес: '+found.endpoint:'';
 for(const action of ['activate','discover','probe'])$('ai-'+action).disabled=!found;
}
function aiRender(state,selected=state.active||''){
 aiState=state;
 const option=p=>el('option',p.settings.name+(p.id===state.active?' · активный':''),{value:p.id});
 $('ai-profile').replaceChildren(el('option','Новый профиль',{value:''}),...state.profiles.map(option));
 $('ai-profile').value=selected;aiForm(selected);
 const previous=$('ai-job-profile').value;
 $('ai-job-profile').replaceChildren(el('option','Активный профиль',{value:''}),...state.profiles.map(option));
 if(state.profiles.some(row=>row.id===previous))$('ai-job-profile').value=previous;
}
function aiAction(fn){
 return async()=>{
  for(const action of ['save','activate','discover','probe'])$('ai-'+action).disabled=true;
  $('ai-message').textContent='Выполняется…';
  try{$('ai-message').textContent=await fn();}catch(error){$('ai-message').textContent=error.message;}
  finally{$('ai-save').disabled=false;for(const action of ['activate','discover','probe'])$('ai-'+action).disabled=!$('ai-profile').value;}
 };
}
const aiId=()=>$('ai-profile').value;
const aiRequest=(path,method='POST',body)=>json('model-profiles'+path,{method,
 ...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
$('ai-profile').onchange=()=>aiForm(aiId());
$('ai-save').onclick=aiAction(async()=>{
 const settings={};
 for(const [key,value] of Object.entries(aiDefaults)){
  const field=aiField(key);
  settings[key]=typeof value==='boolean'?field.checked:typeof value==='number'?Number(field.value):field.value.trim();
 }
 let body={settings,key:$('ai-key').value,key_action:$('ai-key-action').value};
 if(body.key&&body.key_action==='keep')throw Error('Выберите защищённое или сессионное сохранение ключа.');
 $('ai-key').value='';
 const id=aiId();
 aiRender(await aiRequest(id?'/'+id:'',id?'PUT':'POST',body));
 await refreshRuntime();render();return 'Профиль сохранён и активен.';
});
$('ai-activate').onclick=aiAction(async()=>{
 aiRender(await aiRequest('/'+aiId()+'/activate'));await refreshRuntime();render();return 'Профиль активирован.';
});
$('ai-discover').onclick=aiAction(async()=>{
 const data=await aiRequest('/'+aiId()+'/models');
 $('ai-models').replaceChildren(...data.models.map(value=>el('option',null,{value})));
 return 'Получено моделей: '+data.models.length+'. Можно ввести ID вручную.';
});
$('ai-probe').onclick=aiAction(async()=>{
 const data=await aiRequest('/'+aiId()+'/probe');await refreshRuntime();return 'Синтетический запрос выполнен моделью '+data.model+'.';
});
$('ai-settings').ontoggle=()=>{if($('ai-settings').open)json('model-profiles').then(state=>aiRender(state)).catch(e=>{$('ai-message').textContent=e.message;});};

