'use strict';
const $=id=>document.getElementById(id);
let token=location.hash.slice(1)||sessionStorage.getItem('sas-v2-session')||'';
if(location.hash){sessionStorage.setItem('sas-v2-session',token);history.replaceState(null,'',location.pathname);}
let project=null,dirty=false,selectedFile=null,runtime={},activeJob=null,pollTimer=null;
let viewGeneration=0,sourceGeneration=0,pollFailures=0;
const labels={missing_source:'Отсутствует исходник',unparsed_text:'Участок требует разбора',lexical_cutoff:'Разбор остановился; оригинал сохранён',effect_inventory_review:'Проверить полноту входов, выходов и макроэффектов программы',dynamic_source:'Не разрешён динамический источник',dynamic_destination:'Не разрешено динамическое назначение записи',dataset_list_options_review:'Проверить список DATA SET с опциями',macro_definition_ambiguity:'Несколько определений макроса'};
function el(tag,text,attrs={}){const n=document.createElement(tag);if(text!==null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;}
function field(value,placeholder){return el('input',null,{value:value||'',placeholder:placeholder||''});}
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function api(path,options={}){const r=await fetch('/api/'+path,{...options,headers:{'X-Local-Session':token,...(options.headers||{})}});if(!r.ok){let d;try{d=await r.json();}catch{d={error:r.status===403?'Откройте окно через запуск приложения: требуется локальная сессия.':'Ошибка '+r.status};}throw Error(d.error||JSON.stringify(d));}return r;}
async function json(path,options){return(await api(path,options)).json();}
function requireProject(){if(!project||typeof project.id!=='string')throw Error('Сначала загрузите EGP или откройте сохранённый проект.');return project;}
function viewToken(){return{id:project?.id,generation:viewGeneration};}
function currentView(view){return project?.id===view.id&&viewGeneration===view.generation;}
function checkedProject(value){if(!value||typeof value.id!=='string'||!Number.isInteger(value.revision)||!value.snapshot)throw Error('Приложение не получило проект. Обновите список и откройте его снова.');return value;}
function beginSelection(){viewGeneration++;sourceGeneration++;clearTimeout(pollTimer);pollTimer=null;activeJob=null;pollFailures=0;return viewGeneration;}
function projectActions(){const loaded=!!project?.id;$('open').disabled=!$('projects').value;$('show-source').disabled=!loaded||!$('source').value;$('apply-order').disabled=!loaded;for(const name of ['ctas','pipeline'])$('example-'+name).disabled=!loaded;}
async function command(action,payload){const p=requireProject(),view=viewToken();const next=await json('projects/'+p.id+'/commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:p.revision,action,payload})});if(!currentView(view))return false;project=checkedProject(next);dirty=false;render();await recent();return true;}
function run(fn){return async()=>{try{await fn();}catch(e){message(e.message,true);}};}
function changed(){dirty=true;message('Есть несохранённые изменения. Сохраните контракт; затем потребуется повторное согласование.');for(const id of ['approve-plan','task','candidate-template','verify','export','generate','execute','approve-release'])$(id).disabled=true;}
async function recent(){const items=await json('projects'),selected=$('projects').value;$('projects').replaceChildren(...items.map(p=>el('option',p.name+' · ревизия '+p.revision,{value:p.id})));$('projects').value=items.some(p=>p.id===project?.id)?project.id:(items.some(p=>p.id===selected)?selected:(items[0]?.id||''));projectActions();}
function newOutput(output){const row=el('div',null,{class:'output'});const name=field(output?.name.parts.join('.')||'','work.result или schema.result');name.className='out-name';const cols=field(output?.columns.join(', ')||'','Столбцы, через запятую');cols.className='out-cols';const role=el('select',null,{class:'out-role'});role.append(el('option','Конечная',{value:'final'}),el('option','Промежуточная',{value:'intermediate'}));role.value=output?.role||'final';const del=el('button','Удалить');del.onclick=()=>{row.remove();changed();renderWorkRegistry();};const effects=field((project?.plan?.allowed_effects?.[output?.name.parts.join('.')]||['create']).join(', '),'Разрешённые действия: create, insert, update, delete, drop');effects.className='out-effects';const origin=field(project?.plan?.output_sources?.[output?.name.parts.join('.')]||output?.name.parts.join('.')||'','Исходное имя выхода SAS');origin.className='out-source';const dest=field((project?.plan||project?.plan_draft||project?.intake)?.output_destinations?.[output?.name.parts.join('.')]?.parts.join('.')||'','Адрес: пусто = автоматически, либо schema.table');dest.className='out-destination';row.append(origin,name,dest,cols,role,effects,del,el('small','',{class:'out-preview'}));
 const metadata=el('details',null);metadata.append(el('summary','Типы, ключи и хранение'));
 const contract=el('textarea',null,{class:'out-contract',rows:'5',placeholder:'{"column_metadata":[{"name":"id","data_type":"INTEGER","nullable":false}],"unique_keys":[["id"]],"clickhouse_engine":"MergeTree"}'});
 const values={column_metadata:output?.column_metadata||[],unique_keys:output?.unique_keys||[],
   clickhouse_engine:output?.clickhouse_engine||null,clickhouse_nullable_key:output?.clickhouse_nullable_key||false};
 if(values.column_metadata.length||values.unique_keys.length||values.clickhouse_engine||values.clickhouse_nullable_key)contract.value=JSON.stringify(values,null,2);
 metadata.append(el('label','Дополнительный контракт JSON'),contract);row.append(metadata);
 const comparison=project?.plan?.comparisons?.find(c=>c.output.parts.join('.')===output?.name.parts.join('.'));
 const details=el('details',null);details.append(el('summary','Эталон сравнения'));
 for(const[cls,placeholder,value]of [['reference','Схема.таблица эталона',comparison?.reference.parts.join('.')],['run-id','Идентификатор независимого запуска',comparison?.reference_run_id],['input-hash','Хеш одинаковых входов SAS/SQL',comparison?.input_snapshot_hash],['source-hash','Хеш исходного SAS-проекта',comparison?.source_snapshot_hash],['params-hash','Хеш параметров SAS-запуска',comparison?.parameters_hash]]){const f=field(value,placeholder);f.className=cls;details.append(f);}
 const provenance=el('select',null,{class:'provenance'});provenance.append(el('option','Синтетический контрольный результат',{value:'synthetic_handwritten'}),el('option','Результат независимого SAS-запуска',{value:'sas_reference'}));provenance.value=comparison?.provenance||'synthetic_handwritten';details.append(provenance);row.append(details);row.oninput=()=>{changed();renderWorkRegistry();};$('outputs').append(row);}
function render(){
 projectActions();if(!project)return;
 renderPreparation();renderWholeScope();renderContextHelpers();
 $('source-text').textContent='';
 if(project.stale_profile_review)message('Профиль ревью изменился. Перед выпуском перепроверьте сохранённый SQL.');
 $('summary').textContent=project.name+' · ревизия '+project.revision+' · '+project.state+' · '+project.snapshot.sources.filter(s=>s.status==='available').length+'/'+project.snapshot.sources.length+' исходников доступны';
 $('intake-diagnostics').textContent='Восстановлены программы CodeTask и списки запуска. Другие объекты сохраняются в оригинальном EGP; их содержимое пока не интерпретируется.\n'+JSON.stringify({diagnostics:project.snapshot.diagnostics,unsupported_nodes:project.snapshot.unsupported_nodes||[],element_types:project.snapshot.element_types||{}},null,2);
 $('source').replaceChildren(...project.snapshot.sources.map(s=>el('option',s.label+(s.status==='missing'?' · отсутствует':''),{value:s.id})));
 sourceGeneration++;projectActions();
 $('scenario').replaceChildren();for(const s of project.snapshot.scenarios){const b=el('button',s.label+' · '+s.source_ids.length+' программ');b.onclick=run(async()=>{if(await command('select_scenario',{scenario_id:s.id}))message('Порядок выбран. Проверьте источники и контракт.');});$('scenario').append(b);}
 $('manual-order').value=(project.scenario?.source_ids||project.snapshot.sources.map(s=>s.id)).join('\n');
 $('workspace').hidden=!project.context;if(!project.context)return;
 const c=project.context,p=project.plan||project.plan_draft||project.intake,bindings=new Map((p?.bindings||project.binding_drafts||[]).map(b=>[b.source_id,b]));
 $('manual-source').replaceChildren(...project.snapshot.sources.filter(s=>c.source_ids.includes(s.id)).map(s=>el('option',s.label,{value:s.id})));
 $('bindings').replaceChildren();for(const input of c.inputs){
  const row=el('tr',null,{'data-id':input.id});const original=el('td',input.original);original.append(el('small',input.origin_scope+' · '+input.uses.length+' обращений'),el('small',input.dynamic?'Динамическое имя · нужно уточнение':'Обнаружено структурно; полноту проверяет человек'));
  const internal=el('input',null,{type:'checkbox',class:'internal-input'});internal.checked=(p?.intermediate_inputs||[]).includes(input.id);const ilabel=el('label',null);ilabel.append(internal,el('span','Создаётся внутри процесса'));original.append(ilabel);
  const cell=el('td',null),fields=el('div',null,{class:'names'}),binding=bindings.get(input.id),parts=binding?.target.parts||[];
  const schema=field(parts.length===2?parts[0]:'','Схема'),table=field(parts.at(-1)||'','Таблица');schema.className='schema';table.className='table';schema.disabled=table.disabled=input.dynamic;fields.append(schema,table);
  const keep=el('button','Оставить исходное имя');keep.disabled=input.dynamic;keep.onclick=()=>{const a=input.name||[];schema.value=a.length===2?a[0]:'';table.value=a.at(-1)||'';row.dataset.decision='keep_original';changed();};row.dataset.members=JSON.stringify(binding?.members||[]);row.dataset.membersComplete=String(binding?.members_complete||false);row.dataset.decision=binding?.decision||'replace';schema.oninput=table.oninput=()=>{row.dataset.decision='replace';changed();};cell.append(fields,keep);
  const meta=el('td',null),columns=el('textarea',null,{rows:'3',placeholder:'id:INTEGER\nname:VARCHAR(100)'});columns.className='columns';columns.value=(binding?.columns||[]).map(c=>c.name+':'+c.data_type+(c.nullable===false?' NOT NULL':'')).join('\n');const keys=el('textarea',null,{class:'unique-keys',rows:'2',placeholder:'Уникальные ключи: поля через запятую, каждый ключ с новой строки'});keys.value=(binding?.unique_keys||[]).map(k=>k.join(', ')).join('\n');const complete=el('input',null,{type:'checkbox',class:'schema-complete'});complete.checked=!!binding?.schema_complete;const completeLabel=el('label',null);completeLabel.append(complete,el('span','Перечислены все поля источника'));meta.append(completeLabel);const uploadMeta=el('input',null,{type:'file',accept:'.json',class:'metadata-file'});uploadMeta.onchange=run(()=>importMetadata(row,uploadMeta.files?.item(0)));meta.append(columns,keys,el('small','Метаданные JSON: только columns и unique_keys, без строк данных.'),uploadMeta,el('small','NOT NULL и уникальные ключи проверяются по тестовым данным.'));row.append(original,cell,meta);row.onchange=changed;$('bindings').append(row);
 }
 $('writes').textContent=c.writes.map(w=>w.source_id+' · строка '+w.line+' · '+w.name+' · версия '+w.version+' · '+w.storage).join('\n');
 $('target').value=p?.target||'greenplum';$('target-version').value=p?.target_version||'';$('mode').value=p?.mode||'fixed_run';$('acceptance').value=p?.acceptance||'code_review';$('target-versions').value=Object.entries(p?.target_versions||{}).map(([k,v])=>k+'='+v).join('\n');$('scenario-outputs').value=Object.entries(p?.scenario_outputs||{}).map(([k,v])=>k+'='+v.join(',')).join('\n');$('parameters').value=Object.entries(project.parameters||{}).map(([k,v])=>k+'='+v).join('\n');$('validation-scope').value=p?.validation_scope||'';$('shared-targets').value=Object.entries(p?.shared_target_rationale||{}).map(([k,v])=>k+'='+v).join('\n');
 for(const key of ['numeric_arithmetic','numeric_missing','character_missing','character_storage','input_observation_order'])$(key).value=p?.source_semantics?.[key]||'unconfirmed';
 $('test-mode').checked=!!p?.test_mode;$('outputs').replaceChildren();for(const o of p?.outputs||[null])newOutput(o);renderWorkRegistry();
 const resolutions=new Map((p?.resolutions||[]).map(r=>[r.issue_id,r.rationale]));$('issues').replaceChildren();for(const i of c.issues){const row=el('div',null,{class:'issue'+(i.hard?' hard':''),'data-id':i.id}),label=el('label',null),check=el('input',null,{type:'checkbox'});check.checked=resolutions.has(i.id);check.disabled=i.hard;check.className='review';label.append(check,el('span',(labels[i.code]||i.code)+' · '+i.source_id));const rationale=field(resolutions.get(i.id)||'','Основание решения после просмотра оригинала');rationale.className='rationale';rationale.disabled=i.hard;row.append(label,el('small',i.detail),rationale);row.oninput=changed;$('issues').append(row);}
 const scope=$('export-target'),previousScope=scope.value,targets=Object.keys(project.pipeline?.profiles||{});
 scope.replaceChildren(el('option','Весь проект',{value:''}),...targets.map(target=>el('option',target,{value:target})));
 if(targets.includes(previousScope))scope.value=previousScope;
 $('blockers').textContent=(project.blockers||[]).join('\n');$('approve-plan').disabled=project.state!=='PLAN_READY';$('task').disabled=!project.task;$('candidate-template').disabled=!project.task;$('verify').disabled=!project.gate1;$('export').disabled=!project.candidate&&!project.pipeline;
 $('advise-plan').disabled=!runtime.model_enabled||!!activeJob;
 $('evidence').textContent=project.evidence?JSON.stringify(project.evidence,null,2):'Исполнение в целевой СУБД: не выполнялось. Сравнение с SAS: недоступно.';
 $('candidate').value=project.candidate?JSON.stringify(project.candidate,null,2):'';
 $('generate').disabled=dirty||!project.gate1||!runtime.model_enabled||!!activeJob;
 $('execute').disabled=dirty||project.plan?.acceptance==='code_review'||!project.evidence?.static_passed||!runtime.db_enabled||!!activeJob;
 $('approve-release').disabled=dirty||!project.evidence?.release_allowed||!!project.gate2;
 $('sql-file').replaceChildren(...(project.candidate?.artifacts||[]).map((a,i)=>el('option',a.name,{value:String(i)})));
 showSql();showEvidence();renderPipeline();
}
function objectName(raw){let parts=raw.trim().split('.');if(parts.length===2&&parts[0].toLowerCase()==='work')parts=parts.slice(1);return{parts};}
function keyValues(id){const result={};for(const line of $(id).value.split('\n').filter(x=>x.trim())){const at=line.indexOf('=');if(at<1)throw Error('Укажите name=value');const key=line.slice(0,at).trim();if(Object.hasOwn(result,key))throw Error('Повтор имени');result[key]=line.slice(at+1).trim();}return result;}
function validatePlanFields(plan){
 if(!plan.target_version)throw Error('Укажите точную версию целевой СУБД в разделе «Контракт результата».');
 if(plan.target_version.length>64)throw Error('Версия СУБД должна содержать не больше 64 символов.');
 for(const [index,binding]of plan.bindings.entries()){
  if(binding.target.parts.some(p=>!p.trim()))throw Error('Укажите таблицу назначения для источника № '+(index+1)+'. Схема может быть пустой.');
  for(const column of binding.columns){if(!column.name.trim())throw Error('Укажите имя поля источника № '+(index+1)+' перед двоеточием.');if(!column.data_type.trim())throw Error('Укажите тип поля «'+column.name+'» после двоеточия, например INTEGER или VARCHAR(100).');}
  if(new Set(binding.columns.map(c=>c.name)).size!==binding.columns.length)throw Error('У источника № '+(index+1)+' повторяются имена полей.');
 }
 if(!plan.outputs.length)throw Error('Добавьте хотя бы одну обязательную выходную таблицу.');
 for(const [index,output]of plan.outputs.entries()){
  if(output.name.parts.some(p=>!p.trim()))throw Error('Укажите имя выходной таблицы № '+(index+1)+'.');
  if(!output.columns.length)throw Error('Укажите столбцы результата для таблицы «'+output.name.parts.join('.')+'».');
  if(new Set(output.columns).size!==output.columns.length)throw Error('В таблице «'+output.name.parts.join('.')+'» повторяются имена столбцов.');
 }
 if(!plan.outputs.some(o=>o.role==='final'))throw Error('Назначьте хотя бы одну выходную таблицу конечным результатом.');
 if(new Set(plan.outputs.map(o=>o.name.parts.join('.'))).size!==plan.outputs.length)throw Error('Выходные таблицы должны иметь разные имена.');
 const destinations=new Set();
 for(const output of plan.outputs){const logical=output.name.parts.join('.');let parts=[...(plan.output_destinations?.[logical]?.parts||output.name.parts)];if(parts.length===2&&parts[0].toLowerCase()==='work')parts=parts.slice(1);if(plan.test_mode&&parts.length===2&&!parts[1].endsWith('_test'))parts[1]+='_test';const key=parts.join('.');if(destinations.has(key))throw Error('Две выходные таблицы назначены в «'+key+'». Выберите разные адреса.');destinations.add(key);}
 if(plan.validation_scope.trim().length<12)throw Error('Опишите, что и на каких входах нужно сравнивать: не менее 12 символов.');
 for(const resolution of plan.resolutions)if(resolution.rationale.trim().length<12)throw Error('Для каждого подтверждённого вопроса укажите основание решения: не менее 12 символов.');
 for(const comparison of plan.comparisons)if(!comparison.reference_run_id.trim())throw Error('Укажите идентификатор независимого запуска для эталона «'+comparison.reference.parts.join('.')+'».');
 return plan;
}
function collectPlan(){
 requireProject();if(!project.scenario)throw Error('Сначала выберите порядок выполнения проекта.');
 const bindings=readBindings();
 const outputs=[...$('outputs').children].map(row=>{
   const name=objectName(row.querySelector('.out-name').value);
   const raw=row.querySelector('.out-contract')?.value.trim();
   const extra=raw?JSON.parse(raw):{};
   if(!extra||typeof extra!=='object'||Array.isArray(extra)||Object.keys(extra).some(k=>!['column_metadata','unique_keys','clickhouse_engine','clickhouse_nullable_key'].includes(k)))throw Error('Проверьте дополнительный контракт таблицы '+name.parts.join('.'));
   return{...extra,name,storage:name.parts.length===1?'temporary':'persistent',columns:row.querySelector('.out-cols').value.split(',').map(s=>s.trim()).filter(Boolean),role:row.querySelector('.out-role').value};
 });
 const parameters=keyValues('parameters');
 const resolutions=[...$('issues').children].filter(r=>r.querySelector('.review').checked).map(r=>({issue_id:r.dataset.id,rationale:r.querySelector('.rationale').value}));
 const comparisons=[...$('outputs').children].filter(row=>row.querySelector('.reference').value.trim()).map(row=>({output:objectName(row.querySelector('.out-name').value),reference:objectName(row.querySelector('.reference').value),provenance:row.querySelector('.provenance').value,reference_run_id:row.querySelector('.run-id').value,input_snapshot_hash:row.querySelector('.input-hash').value.trim(),source_snapshot_hash:row.querySelector('.source-hash').value.trim(),parameters_hash:row.querySelector('.params-hash').value.trim()}));
 const source_semantics=Object.fromEntries(['numeric_arithmetic','numeric_missing','character_missing','character_storage','input_observation_order'].map(key=>[key,$(key).value]));
 return validatePlanFields({scenario_id:project.scenario.id,target:$('target').value,target_version:$('target-version').value.trim()||'unknown',acceptance:$('acceptance').value,targets:$('acceptance').value==='code_review'?['greenplum','postgresql','clickhouse']:[],target_versions:keyValues('target-versions'),scenario_outputs:Object.fromEntries(Object.entries(keyValues('scenario-outputs')).map(([k,v])=>[k,v.split(',').map(s=>s.trim()).filter(Boolean)])),parameter_types:project.parameter_types||{},test_mode:$('test-mode').checked,output_destinations:destinationOverrides(),intermediate_inputs:[...$('bindings').children].filter(row=>row.querySelector('.internal-input').checked).map(row=>row.dataset.id),output_sources:Object.fromEntries([...$('outputs').children].map(row=>[objectName(row.querySelector('.out-name').value).parts.join('.'),row.querySelector('.out-source').value.trim()||objectName(row.querySelector('.out-name').value).parts.join('.')])),allowed_effects:Object.fromEntries([...$('outputs').children].map(row=>[objectName(row.querySelector('.out-name').value).parts.join('.'),row.querySelector('.out-effects').value.split(',').map(s=>s.trim()).filter(Boolean)])),mode:$('mode').value,parameters,bindings,outputs,comparisons,source_semantics,validation_scope:$('validation-scope').value,resolutions,shared_target_rationale:keyValues('shared-targets')});
}
function download(data,name,type){const a=el('a',null),url=URL.createObjectURL(data instanceof Blob?data:new Blob([data],{type:type||'application/json'}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function chooseFile(file){selectedFile=null;$('upload').disabled=true;if(!file){$('file-status').textContent='Файл не выбран';return;}if(!file.name.toLowerCase().endsWith('.egp')){message('Нужен файл .egp',true);return;}if(file.size>32*1024*1024){message('Файл больше 32 МБ',true);return;}selectedFile=file;$('file-status').textContent=file.name+' · '+Math.ceil(file.size/1024)+' КБ';$('upload').disabled=false;message('Файл выбран. Нажмите «Загрузить EGP».');}
$('file').addEventListener('change',event=>chooseFile(event.target.files?.item(0)));
$('drop-zone').ondragover=e=>{e.preventDefault();};$('drop-zone').ondrop=e=>{e.preventDefault();chooseFile(e.dataTransfer.files?.item(0));};
$('upload').onclick=run(async()=>{const f=selectedFile||$('file').files?.item(0);if(!f)throw Error('Браузер не передал файл. Выберите его снова или перетащите в область загрузки.');if(dirty&&!confirm('Отбросить несохранённые изменения?'))return;const generation=beginSelection();$('upload').disabled=true;message('Читаю '+f.name+'...');try{const bytes=await f.arrayBuffer();if(!bytes.byteLength)throw Error('Выбран пустой файл');const next=await json('projects?filename='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:bytes});if(generation!==viewGeneration)return;project=checkedProject(next);dirty=false;render();await recent();message('Оригиналы сохранены. Выберите и проверьте порядок исполнения.');}finally{$('upload').disabled=!selectedFile;}});
$('open').onclick=run(async()=>{const id=$('projects').value;if(!id)throw Error('Сохранённых проектов пока нет. Загрузите EGP.');if(dirty&&!confirm('Отбросить несохранённые изменения?'))return;const generation=beginSelection(),next=await json('projects/'+encodeURIComponent(id));if(generation!==viewGeneration)return;project=checkedProject(next);dirty=false;render();await resumeJob();});
$('projects').onchange=projectActions;
$('apply-order').onclick=run(async()=>{requireProject();const ids=$('manual-order').value.split('\n').map(x=>x.trim()).filter(Boolean);if(!ids.length)throw Error('Укажите хотя бы одну программу в порядке выполнения.');if(await command('select_scenario',{source_ids:ids}))message('Явный порядок сохранён.');});
$('show-source').onclick=run(async()=>{const p=requireProject(),source=$('source').value;if(!source)throw Error('Выберите исходную программу в списке.');const view=viewToken(),request=++sourceGeneration,d=await json('projects/'+p.id+'/source/'+encodeURIComponent(source));if(currentView(view)&&request===sourceGeneration&&$('source').value===source)$('source-text').textContent=d.text;});
$('source').onchange=()=>{sourceGeneration++;$('source-text').textContent='';projectActions();};
$('add-output').onclick=()=>{newOutput();changed();};
$('apply-parameters').onclick=run(async()=>{if(await command('set_parameters',{parameters:keyValues('parameters')}))message('Имена пересчитаны. Назначения и контракт нужно заполнить для нового контекста.');});
$('declare-input').onclick=run(async()=>{if(await command('declare_input',{origin_source_id:$('manual-source').value,name:objectName($('manual-input').value),rationale:$('manual-rationale').value}))message('Источник добавлен в реестр. Проверьте и сохраните назначения.');});
$('save-plan').onclick=run(async()=>{const p=collectPlan();if(await command('save_plan',p))message(project.blockers.length?'Контракт сохранён. Есть нерешённые вопросы.':'Контракт сохранён. Можно согласовать гейт 1.');});
$('approve-plan').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните изменения');if(await command('approve_plan',{}))message('Контракт утверждён. Можно запустить генерацию SQL.');});
$('task').onclick=run(async()=>{const task=await json('projects/'+project.id+'/task');download(JSON.stringify(task,null,2),'local-migration-task.json');});
$('candidate-template').onclick=()=>{$('candidate').value=JSON.stringify({schema_version:1,task_hash:project.task.hash,artifacts:[{name:'001.sql',sql:''}],source_claims:[...new Set(project.context.regions.filter(r=>r.kind!=='layout_or_comment').map(r=>r.id))],assumptions:[]},null,2);};
$('verify').onclick=run(async()=>{if(dirty)throw Error('Есть несохранённые изменения контракта');if(await command('submit_candidate',{candidate_json:$('candidate').value}))message(project.evidence.static_passed?'Статические проверки пройдены. Исполнение и эквивалентность ещё не проверены.':'Кандидат сохранён; найдены ошибки.');});
$('export').onclick=run(async()=>{if(dirty)throw Error('Есть несохранённые изменения');const target=$('export-target').value;const name=target?'migration-v2-'+target+'.zip':project.gate2?(project.pipeline?'migration-v2-code-reviewed.zip':'migration-v2-accepted.zip'):'migration-v2-draft.zip';download(await(await api('projects/'+project.id+'/draft'+(target?'?target='+encodeURIComponent(target):''))).blob(),name);});
for(const id of ['target','target-version','mode','parameters','validation-scope','shared-targets','numeric_arithmetic','numeric_missing','character_missing','character_storage','input_observation_order','acceptance','target-versions','scenario-outputs'])$(id).oninput=changed;
window.onbeforeunload=e=>{if(dirty){e.preventDefault();e.returnValue='';}};
run(recent)();

async function refreshRuntime(){runtime=await json('status');const cost=runtime.budget?.charged_or_reserved_usd||0;$('runtime-status').textContent=(runtime.model_enabled?'Модель: '+runtime.model.id+' · '+(runtime.model.exposure==='synthetic_only'?'только вымышленные примеры':'корпоративный профиль'):'Модель не настроена')+' | '+(runtime.db_enabled?'Тестовая PostgreSQL подключена в профиле':'Тестовая база не настроена')+(runtime.model?.tariff==='paid'?' | API: $'+cost.toFixed(5)+' / $'+runtime.model.budget_usd.toFixed(2):runtime.model?.tariff==='corporate'?' | Без денежной тарификации':'');}
function showSql(){const a=project?.candidate?.artifacts?.[Number($('sql-file').value)||0];$('sql-preview').textContent=a?.sql||'SQL ещё не получен.';}
function showEvidence(){const e=project?.evidence;$('verification-summary').replaceChildren();if(!e)return;const status=el('p',e.static_passed?'Статическая проверка пройдена.':'Статическая проверка выявила ошибки.');$('verification-summary').append(status);for(const error of e.errors||[])$('verification-summary').append(el('p',error,{class:'error'}));for(const c of e.comparison||[])$('verification-summary').append(el('p',c.output+': '+(c.passed?'совпало':'расхождение')+' · строк '+c.rows_actual+' / '+c.rows_expected+' · лишних '+c.extra_rows+' · недостающих '+c.missing_rows,{class:c.passed?'':'error'}));if(e.checks.test_data==='failed')$('verification-summary').append(el('p','На независимых тестовых данных найдено расхождение. Приёмка текущего SQL заблокирована.',{class:'error'}));if(e.checks.comparison==='passed')$('verification-summary').append(el('p',e.release_allowed?'Совпало с заявленным независимым SAS-эталоном на тех же входах.':'Синтетический эталон совпал. Эквивалентность реальному SAS пока не подтверждена.'));}
$('sql-file').onchange=showSql;
async function startJob(kind){const p=requireProject(),view=viewToken();if(dirty)throw Error('Сначала сохраните контракт');const job=await json('projects/'+p.id+'/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:p.revision,kind,...(kind!=='execute'&&$('ai-job-profile').value?{profile_id:$('ai-job-profile').value}:{})})});if(!currentView(view))return;activeJob=job;pollFailures=0;message('Задание запущено. Результат будет сохранён в текущем проекте.');render();await pollJob();}
async function pollJob(){
 if(!activeJob)return;const tracked=activeJob,view=viewToken();let job;
 const schedule=delay=>{clearTimeout(pollTimer);pollTimer=setTimeout(()=>{if(currentView(view)&&activeJob?.id===tracked.id)run(pollJob)();},delay);};
 const recover=()=>{
  if(!currentView(view)||activeJob?.id!==tracked.id)return;
  pollFailures++;const delay=Math.min(1000*2**Math.min(pollFailures,4),15000);
  $('job-status').textContent='Связь с приложением прервалась. Повторная проверка задания через '+(delay/1000)+' сек. Само задание повторно не запускается.';
  schedule(delay);
 };
 try{job=await json('jobs/'+tracked.id);}catch(error){recover();return;}
 if(!currentView(view)||activeJob?.id!==tracked.id)return;
 if(job.project_id!==project.id){activeJob=null;render();return;}
 activeJob=job;$('job-status').textContent=({generate:'Генерация и проверка кода',plan:'Подготовка и проверка контракта',execute:'Исполнение и сравнение'}[job.kind]||job.kind)+': '+({queued:'в очереди',running:'в работе',cancelling:'отмена',cancelled:'отменено',succeeded:'завершено',failed:'ошибка',stale:'сохранён ответ для старой ревизии',interrupted:'прервано при остановке приложения'}[job.state]||job.state)+(job.error?' · '+job.error:'');
 $('cancel-job').disabled=!['queued','running'].includes(job.state);
 if(['queued','running','cancelling'].includes(job.state)){pollFailures=0;schedule(1000);return;}
 try{
  await refreshRuntime();if(!currentView(view))return;
  let next=null;if(!dirty){next=await json('projects/'+view.id);if(!currentView(view))return;}
  if(next&&!dirty){project=checkedProject(next);activeJob=null;pollFailures=0;render();await recent();}
  else{activeJob=null;pollFailures=0;message('Задание завершено. В форме есть несохранённые изменения - результат сохранён в проекте.');}
 }catch(error){if(activeJob)recover();else message('Результат загружен. Не удалось обновить список проектов: '+error.message,true);}
}
$('generate').onclick=run(()=>startJob('generate'));$('execute').onclick=run(()=>startJob('execute'));
$('cancel-job').onclick=run(async()=>{if(activeJob)await json('jobs/'+activeJob.id+'/cancel',{method:'POST'});});
$('approve-release').onclick=run(()=>command('approve_release',{}));
for(const name of ['ctas','pipeline'])$('example-'+name).onclick=run(async()=>{const p=requireProject();if(dirty&&!confirm('Заменить несохранённые изменения учебным контрактом?'))return;const view=viewToken(),next=await json('projects/'+p.id+'/example/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:p.revision})});if(!currentView(view))return;project=checkedProject(next);dirty=false;render();message('Учебный контракт заполнен. Проверьте его и утвердите гейт 1.');});
projectActions();
run(refreshRuntime)();

async function resumeJob(){const p=requireProject(),view=viewToken(),jobs=await json('projects/'+p.id+'/jobs');if(!currentView(view))return;activeJob=jobs.find(j=>['queued','running','cancelling'].includes(j.state))||null;pollFailures=0;if(activeJob){render();await pollJob();}else{$('job-status').textContent=jobs.length?'Последнее задание: '+jobs[0].state+(jobs[0].error?' · '+jobs[0].error:''):'';}}

function renderPreparation(){
 const p=project.preparation||{},names={source_read:'Исходник прочитан',context_ready:'Контекст согласован',plan_complete:'План полный',sql_generated:'SQL сформирован',sql_executed:'SQL исполнился',results_matched:'Результаты совпали',code_checked:'Код проверен',code_accepted:'Код принят'},states={passed:'да',not_run:'не проверено',needs_input:'нужны сведения',unavailable:'недоступно',failed:'ошибка',stale:'устарело'};
 $('preparation-stages').textContent=Object.entries(p.stages||{}).map(([k,v])=>(names[k]||k)+': '+(states[v]||v)).join(' | ');
 const questions=$('preparation-questions');questions.replaceChildren();
 const requirements=p.requirements||[];
 for(const category of [...new Set(requirements.map(r=>r.category))]){const rows=requirements.filter(r=>r.category===category),details=el('details',null),categories={source:'Исходники',context:'Контекст',mapping:'Назначения источников',metadata:'Поля и типы',parameter:'Параметры',reference:'Эталоны',review:'Проверка структуры',implementation:'Ограничения реализации'};details.append(el('summary',(categories[category]||category)+' · '+rows.length));for(const r of rows){details.append(el('p',r.message+(r.detail?' · '+r.detail:'')),el('small',r.actions.length?'Действие: '+r.actions.map(a=>({attach_source:'прикрепить SAS ниже',select_scenario:'выбрать сценарий выше',save_plan:'заполнить и сохранить контракт ниже',set_parameters:'задать параметры ниже'}[a]||a)).join(', '):'Нужна доработка указанной конструкции.'));}questions.append(details);}
 $('attachment-source').replaceChildren(...project.snapshot.sources.filter(s=>s.status!=='available').map(s=>el('option',s.label,{value:s.id})));
 $('attach-source').disabled=!$('attachment-source').value;
 $('compile-offline').disabled=dirty||!project.gate1||project.plan?.acceptance==='code_review'||!!activeJob;
 $('offline-diagnostics').textContent=project.offline?JSON.stringify({generator:project.offline.generator,requirements:project.offline.requirements,api_calls:0},null,2):'';
}
$('attach-source').onclick=run(async()=>{requireProject();if(dirty)throw Error('Сначала сохраните изменения.');const file=$('attachment-file').files?.item(0);if(!file)throw Error('Выберите соответствующий файл SAS.');if(file.size>2000000)throw Error('Файл больше 2 МБ.');let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());}catch{throw Error('Сохраните SAS в UTF-8 перед прикреплением.');}if(await command('attach_source',{source_id:$('attachment-source').value,filename:file.name,text}))message('Исходник прикреплён. Проверьте обновлённый контекст.');});
$('compile-offline').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните изменения.');if(await command('compile_offline',{}))message(project.candidate?'SQL-черновик собран без API. Проверьте результат.':'Полный SQL не собран. Ограничения указаны в подготовке.');});

function renderWholeScope(){
 const holder=$('whole-scenarios');holder.replaceChildren();
 const scopes=project.scope_orders?.length?project.scope_orders:project.snapshot.scenarios;
 for(const order of scopes){const row=el('div',null,{class:'whole-order','data-id':order.id}),label=el('strong',order.label),ids=el('textarea',null,{rows:'3'});ids.value=order.source_ids.join('\n');row.append(label,ids);holder.append(row);}
 const included=new Set(scopes.flatMap(s=>s.source_ids));$('extra-order').value=project.snapshot.sources.filter(s=>!included.has(s.id)).map(s=>s.id).join('\n');
 holder.append(el('p','Программы: '+project.snapshot.sources.map(s=>s.id+' = '+s.label).join('; ')));
}
$('confirm-whole').onclick=run(async()=>{requireProject();if(dirty&&!confirm('Изменение объёма сбросит несохранённый контракт и согласования. Продолжить?'))return;const orders=[...document.querySelectorAll('.whole-order')].map(row=>({id:row.dataset.id,label:row.querySelector('strong').textContent,source_ids:row.querySelector('textarea').value.split('\n').map(s=>s.trim()).filter(Boolean)}));const extra=$('extra-order').value.split('\n').map(s=>s.trim()).filter(Boolean);if(extra.length)orders.push({id:'additional-explicit',label:'Дополнительный порядок, заданный пользователем',source_ids:extra});if(await command('set_project_scope',{orders}))message('Весь EGP учтён. Сценарии сохраняются раздельно. Заполните общий контракт и выходы каждого сценария.');});
function renderPipeline(){
 const p=project.pipeline,holder=$('pipeline-summary');holder.replaceChildren();
 for(const record of project.test_evidence_registry||[]){
   const row=el('div',null);row.append(el('p',record.label));
   if(record.retired)row.append(el('small','Свидетельство пересмотрено. '+record.retired.reason));
   else{
     const reason=field('','Причина пересмотра свидетельства');
     const retire=el('button','Пересмотреть свидетельство');
     retire.onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните контракт.');await command('retire_test_evidence',{id:record.id,reason:reason.value});});
     row.append(reason,retire);
   }
   holder.append(row);
 }
 if(!p){$('hitl-task').replaceChildren();$('hitl-findings').textContent='';return;}
 for(const[target,profile]of Object.entries(p.profiles||{}))holder.append(el('p',target+': '+(profile.passed?'код проверен':'есть нерешённые вопросы')+'; исполнение не проверено; сравнение с SAS не выполнялось.'));
 $('hitl-task').replaceChildren(...Object.entries(p.units||{}).map(([key,u])=>el('option',key+' · '+(u.passed?'проверено':'нужен разбор')+' · попыток '+u.attempts.length,{value:key})));
 const reason=field('','Основание повторного ревью выбранной задачи');
 const rereview=el('button','Отправить тот же SQL на повторное ревью');
 rereview.onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните контракт.');if(await command('request_unit_rereview',{unit_key:$('hitl-task').value,reason:reason.value}))message('Запустите генерацию и проверку. Задача будет проверена повторно с тем же SQL.');});
 holder.append(reason,rereview);
 showHitlTask();
 $('gate2-note').textContent=p.release_allowed&&project.evidence?.release_allowed?'Код прошёл проверки для согласованных профилей. Гейт 2 принимает код без утверждения о совпадении данных SAS.':'Есть блокирующие замечания. Их нельзя закрыть общим принятием риска.';
}
function showHitlTask(){const unit=project.pipeline?.units?.[$('hitl-task').value];if(!unit)return;const last=unit.attempts?.at(-1),candidate=unit.manual_candidate||unit.candidate||last?.candidate;$('hitl-findings').textContent=JSON.stringify(last?.findings||[],null,2);$('hitl-sql').value=(candidate?.artifacts||[]).map(a=>a.sql).join('\n;\n');}
$('hitl-task').onchange=showHitlTask;
$('save-hitl-sql').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните контракт.');if(await command('replace_task_sql',{unit_key:$('hitl-task').value,sql:$('hitl-sql').value}))message('SQL сохранён. Запустите генерацию/проверку: исправленная задача будет направлена на независимое ревью.');});
$('grant-budget').onclick=run(async()=>{if(await command('grant_model_calls',{additional_calls:Number($('grant-calls').value),rationale:$('grant-rationale').value}))message('Дополнительные вызовы разрешены. Денежный лимит профиля не изменён.');});

function readBindings(){
 const bindings=[...$('bindings').children].filter(row=>!row.querySelector('.internal-input').checked).map(row=>{const columns=row.querySelector('.columns').value.split('\n').filter(x=>x.trim()).map(line=>{const at=line.indexOf(':');if(at<1)throw Error('Для каждого поля укажите имя:тип');return{name:line.slice(0,at).trim(),data_type:line.slice(at+1).trim().replace(/\s+NOT NULL$/i,''),nullable:/\s+NOT NULL$/i.test(line.slice(at+1))?false:null};});const schema=row.querySelector('.schema').value.trim(),table=row.querySelector('.table').value.trim();return{source_id:row.dataset.id,decision:row.dataset.decision,target:{parts:schema?[schema,table]:[table]},columns,unique_keys:row.querySelector('.unique-keys').value.split('\n').filter(k=>k.trim()).map(k=>k.split(',').map(x=>x.trim())),metadata_status:columns.length?'user_supplied':'not_checked',schema_complete:!!row.querySelector('.schema-complete')?.checked,members:JSON.parse(row.dataset.members||'[]'),members_complete:row.dataset.membersComplete==='true'};});
 return bindings;
}

function renderContextHelpers(){
 const list=$('library-scenario');list.replaceChildren(...(project.scope_orders||[]).map(o=>el('option',o.label,{value:o.id})));updateLibraryBefore();
 const refs=new Map();
 for(const i of project.context?.issues||[]){if(i.code==='external_code_contract_required'){const marker=i.id.lastIndexOf(':include:');if(marker>=0){const source_id=i.source_id,offset=Number(i.id.slice(marker+9));refs.set(source_id+'@'+offset,{source_id,offset});}}}
 for(const i of project.task_plan?.requirements||[])if(i.code==='include_source_required')refs.set(i.source_id+'@'+i.offset,{source_id:i.source_id,offset:i.offset});
 $('include-reference').replaceChildren(...[...refs].map(([key,value])=>el('option',value.source_id+' · позиция '+value.offset,{value:key})));
 $('apply-advice').disabled=!project.plan_proposal?.applicable;
 $('plan-advice').textContent=project.plan_proposal?JSON.stringify({outputs:project.plan_proposal.outputs,requirements:project.plan_proposal.requirements,advisory_notes:project.plan_proposal.advisory_notes??[],applicable:project.plan_proposal.applicable},null,2):'Предложение формируется после назначения источников и ввода их полей. Оно не утверждает план автоматически.';
 $('task-preview').textContent=project.task_plan?JSON.stringify({tasks:project.task_plan.tasks.map(u=>({id:u.id,scenario:u.scenario_id,source:u.source_id,line:u.line,end_line:u.end_line,obligations:u.obligations.length,dependencies:u.dependencies})),requirements:project.task_plan.requirements,classifications:project.task_plan.classifications},null,2):'Сохраните контракт или запросите предложение ИИ.';
}
function updateLibraryBefore(){const order=project?.scope_orders?.find(o=>o.id===$('library-scenario').value);$('library-before').replaceChildren(...(order?.source_ids||[]).map(i=>el('option',i,{value:i})));}
$('library-scenario').onchange=updateLibraryBefore;
async function sasFile(input){const file=input.files?.item(0);if(!file||!file.name.toLowerCase().endsWith('.sas')||file.size>2000000)throw Error('Выберите SAS-файл UTF-8 не больше 2 МБ.');try{return{filename:file.name,text:new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer())};}catch{throw Error('Нужен SAS в UTF-8.');}}
$('save-external').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните изменения контракта.');if(await command('set_external_parameter',{name:$('external-name').value.trim(),type:$('external-type').value,value:$('external-value').value}))message('Внешний параметр сохранён. Контекст и план требуют повторного согласования.');});
$('attach-include').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните изменения.');const key=$('include-reference').value,at=key.lastIndexOf('@');if(at<1)throw Error('Выберите конкретную ссылку INCLUDE.');const file=await sasFile($('include-file'));if(await command('attach_include',{source_id:key.slice(0,at),offset:Number(key.slice(at+1)),...file}))message('Файл привязан к конкретной ссылке. Проверьте обновлённый план.');});
$('attach-library').onclick=run(async()=>{if(dirty)throw Error('Сначала сохраните изменения.');const file=await sasFile($('library-file'));if(await command('attach_library',{scenario_id:$('library-scenario').value,before_source_id:$('library-before').value,...file}))message('Источник добавлен в указанное место сценария. Подтвердите обновлённый контракт.');});
async function importMetadata(row,file){if(!file)return;if(file.size>1000000)throw Error('Метаданные больше 1 МБ.');const data=JSON.parse(await file.text());if(Object.keys(data).some(k=>!['columns','unique_keys','schema_complete','members','members_complete'].includes(k)))throw Error('Импортируйте только columns и unique_keys; строки данных не принимаются.');let schema=row.querySelector('.schema').value.trim(),table=row.querySelector('.table').value.trim();if(!table&&data.members?.length){const p=data.members[0].target.parts;schema=p.length===2?p[0]:'';table=p.at(-1);}if(!table)throw Error('Сначала назначьте таблицу.');await command('import_binding',{source_id:row.dataset.id,decision:row.dataset.decision||'replace',target:{parts:schema?[schema,table]:[table]},columns:data.columns,unique_keys:data.unique_keys||[],metadata_status:'user_supplied',schema_complete:data.schema_complete===true,members:data.members||[],members_complete:data.members_complete===true});message('Поля и типы импортированы. Повторно проверьте план.');}
$('advise-plan').onclick=run(async()=>{requireProject();if(!project.scope_orders?.length)throw Error('Сначала подтвердите весь EGP и отдельные сценарии.');const source_semantics=Object.fromEntries(['numeric_arithmetic','numeric_missing','character_missing','character_storage','input_observation_order'].map(k=>[k,$(k).value]));if(await command('save_intake',{bindings:readBindings(),parameters:keyValues('parameters'),source_semantics,target_versions:keyValues('target-versions'),test_mode:$('test-mode').checked,output_destinations:destinationOverrides(),intermediate_inputs:[...$('bindings').children].filter(row=>row.querySelector('.internal-input').checked).map(row=>row.dataset.id)}))await startJob('plan');});
$('apply-advice').onclick=run(async()=>{if(await command('apply_plan_proposal',{}))message('Предложение перенесено в контракт. Проверьте выходы, эффекты и гейт 1.');});

function destinationOverrides(){return Object.fromEntries([...$('outputs').children].filter(r=>r.querySelector('.out-destination').value.trim()).map(r=>[objectName(r.querySelector('.out-name').value).parts.join('.'),objectName(r.querySelector('.out-destination').value)]));}
function renderWorkRegistry(){
 const holder=$('work-registry');if(!holder)return;holder.replaceChildren();
 for(const row of [...$('outputs').children]){
  const raw=row.querySelector('.out-name').value.trim(),source=row.querySelector('.out-source').value.trim();if(!raw)continue;
  let parts=objectName(row.querySelector('.out-destination').value||raw).parts;
  if($('test-mode').checked&&parts.length===2&&!parts[1].endsWith('_test'))parts[1]+='_test';
  const preview=row.querySelector('.out-preview');if(preview)preview.textContent='Назначение: '+parts.join('.')+' · '+(parts.length===1?'TEMP в текущей сессии':'постоянная таблица');
  if(objectName(raw).parts.length!==1&&!source.toLowerCase().startsWith('work.')&&parts.length!==1)continue;
  const line=el('div',null,{class:'work-item'}),edit=el('button','Выбрать адрес');edit.type='button';edit.onclick=()=>row.querySelector('.out-destination').focus();
  line.append(el('strong',source||raw),el('span',' -> '+parts.join('.')+' · '+(parts.length===1?'TEMP в текущей сессии':'постоянная таблица')),edit);holder.append(line);
 }
 if(!holder.children.length)holder.append(el('p','WORK-выходы появятся после формирования контракта.'));
}
$('test-mode').onchange=()=>{changed();renderWorkRegistry();};
