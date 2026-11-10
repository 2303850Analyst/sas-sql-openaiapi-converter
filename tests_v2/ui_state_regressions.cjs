/* Focused asynchronous UI regressions. Executes app.js; no DOM library or network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
 constructor(){this.value='';this.textContent='';this.children=[];this.disabled=false;this.dataset={};}
 addEventListener(){} setAttribute(k,v){this[k]=v;} append(...v){this.children.push(...v);} replaceChildren(...v){this.children=v;if(v[0]?.value!==undefined)this.value=v[0].value;else this.value='';}
 querySelector(){return new Element();} click(){}
}
const elements=new Map(),timers=[];
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let calls=0,confirmation=true,confirmCalls=0;
const response=x=>({ok:true,status:200,json:async()=>x});
const status={model_enabled:false,db_enabled:false,budget:{}};
let fetchImpl=async url=>response(url==='/api/projects'?[]:status);
const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element()},location:{hash:'',pathname:'/'},sessionStorage:{getItem:()=>'',setItem(){}},history:{replaceState(){}},window:{},fetch:(...a)=>{calls++;return fetchImpl(...a);},console,URL,Blob,Set,Map,JSON,Error,Object,Number,String,encodeURIComponent,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},confirm(){confirmCalls++;return confirmation;}});
const run=s=>vm.runInContext(s,context),tick=()=>new Promise(r=>setImmediate(r));
const project=(id,revision=1)=>({id,revision,snapshot:{sources:[]},context:null});
const setProject=(id)=>run('project='+JSON.stringify(project(id))+';dirty=false;activeJob=null;');
vm.runInContext(fs.readFileSync(path.join(__dirname,'../migration_v2/web/app.js'),'utf8'),context);
const checks=[];
async function check(name,fn){try{await fn();checks.push({name,passed:true});}catch(error){checks.push({name,passed:false,error:error.message});}}
async function main(){
 await tick();const actualRecent=run('recent'),actualRender=run('render');run('render=function(){};recent=async function(){};');
 await check('empty_project_actions_are_guarded',async()=>{
  assert.equal(get('open').disabled,true);assert.equal(get('show-source').disabled,true);assert.equal(get('apply-order').disabled,true);
  calls=0;await run("$('open').onclick()");assert.match(get('message').textContent,/Загрузите EGP/);
  await run("$('show-source').onclick()");assert.match(get('message').textContent,/Сначала загрузите EGP/);
  await run("$('apply-order').onclick()");assert.equal(calls,0);
 });
 await check('source_response_matches_project_and_source',async()=>{
  let resolve;fetchImpl=()=>new Promise(r=>resolve=r);setProject('A');get('source').value='source-A';
  const pending=run("$('show-source').onclick()");setProject('B');get('source').value='source-B';run("$('source').onchange()");
  resolve(response({text:'A source'}));await pending;assert.equal(get('source-text').textContent,'');
  const same=run("$('show-source').onclick()");resolve(response({text:'B source'}));await same;assert.equal(get('source-text').textContent,'B source');
 });
 await check('command_cannot_replace_new_selected_project',async()=>{
  let resolve;fetchImpl=()=>new Promise(r=>resolve=r);setProject('A');const pending=run("command('save_plan',{})");
  setProject('B');resolve(response(project('A',2)));assert.equal(await pending,false);assert.equal(run('project.id'),'B');
  const valid=run("command('save_plan',{})");resolve(response(project('B',2)));assert.equal(await valid,true);assert.equal(run('project.revision'),2);
 });
 await check('latest_open_request_wins',async()=>{
  const pending={};fetchImpl=url=>url.endsWith('/jobs')?Promise.resolve(response([])):new Promise(r=>pending[url]=r);
  get('projects').value='A';const first=run("$('open').onclick()");get('projects').value='B';const second=run("$('open').onclick()");
  pending['/api/projects/B'](response(project('B')));await second;
  pending['/api/projects/A'](response(project('A')));await first;assert.equal(run('project.id'),'B');
 });
 await check('active_project_and_source_controls_are_synchronized',async()=>{
  const p={...project('A'),name:'Synthetic A',state:'IMPORTED',snapshot:{sources:[{id:'source-A',label:'Synthetic source',status:'available'}],scenarios:[]}};
  run('project='+JSON.stringify(p));get('projects').value='B';get('source').value='';get('show-source').disabled=true;
  fetchImpl=async()=>response([{id:'A',name:'Synthetic A',revision:1},{id:'B',name:'Synthetic B',revision:1}]);
  await actualRecent();assert.equal(get('projects').value,'A');actualRender();assert.equal(get('source').value,'source-A');assert.equal(get('show-source').disabled,false);assert.equal(get('apply-order').disabled,false);
 });
 await check('poll_recovers_without_resubmitting_job',async()=>{
  setProject('B');run("activeJob={id:'job-B',project_id:'B'};");timers.length=0;let attempts=0,posts=0;
  fetchImpl=async(url,options)=>{if(options?.method==='POST')posts++;if(url==='/api/jobs/job-B'){if(++attempts===1)throw Error('Synthetic temporary failure');return response({id:'job-B',project_id:'B',kind:'generate',state:'succeeded'});}if(url==='/api/status')return response(status);if(url==='/api/projects/B')return response(project('B',2));throw Error('Unexpected URL '+url);};
  await run('pollJob()');assert.equal(timers.length,1);assert.ok(run('activeJob'));assert.match(get('job-status').textContent,/Повторная проверка/);
  timers.shift().fn();await tick();await tick();assert.equal(attempts,2);assert.equal(posts,0);assert.equal(run('activeJob'),null);assert.equal(run('project.revision'),2);
  for(const failedUrl of ['/api/status','/api/projects/B']){
   setProject('B');run("activeJob={id:'job-B',project_id:'B'};");timers.length=0;let failed=false;
   fetchImpl=async(url,options)=>{if(options?.method==='POST')posts++;if(url===failedUrl&&!failed){failed=true;throw Error('Synthetic completion refresh failure');}if(url==='/api/jobs/job-B')return response({id:'job-B',project_id:'B',kind:'generate',state:'succeeded'});if(url==='/api/status')return response(status);if(url==='/api/projects/B')return response(project('B',2));throw Error('Unexpected URL '+url);};
   await run('pollJob()');assert.equal(timers.length,1);assert.ok(run('activeJob'));timers.shift().fn();await tick();await tick();assert.equal(run('activeJob'),null);assert.equal(run('project.revision'),2);assert.equal(posts,0);
  }
 });
 await check('dirty_example_can_be_declined_or_confirmed',async()=>{
  setProject('A');run('dirty=true');confirmation=false;calls=0;const before=confirmCalls;
  await run("$('example-ctas').onclick()");assert.equal(calls,0);assert.equal(run('dirty'),true);assert.equal(confirmCalls,before+1);
  confirmation=true;fetchImpl=async()=>response(project('A',2));await run("$('example-ctas').onclick()");assert.equal(calls,1);assert.equal(run('dirty'),false);assert.equal(run('project.revision'),2);
 });
 await check('empty_contract_fields_have_actionable_errors',async()=>{
  const valid={target_version:'17',bindings:[{target:{parts:['dwh','events']},columns:[{name:'id',data_type:'INTEGER'}]}],outputs:[{name:{parts:['result']},columns:['id'],role:'final'}],validation_scope:'Compare all output rows',resolutions:[],comparisons:[]};
  const verify=p=>run('validatePlanFields('+JSON.stringify(p)+')');assert.deepEqual(JSON.parse(JSON.stringify(verify(valid))),valid);
  for(const [mutate,message]of [
   [p=>p.target_version='',/версию/],
   [p=>p.bindings[0].target.parts=[''],/таблицу назначения/],
   [p=>p.bindings[0].columns[0].name='',/имя поля/],
   [p=>p.bindings[0].columns[0].data_type='',/тип поля/],
   [p=>p.outputs=[],/выходную таблицу/],
   [p=>p.outputs[0].name.parts=[''],/имя выходной/],
   [p=>p.outputs[0].columns=[],/столбцы результата/]
  ]){const copy=JSON.parse(JSON.stringify(valid));mutate(copy);assert.throws(()=>verify(copy),message);}
  const collision=JSON.parse(JSON.stringify(valid));collision.outputs.push({name:{parts:['qa','report']},columns:['id'],role:'final'});collision.test_mode=true;collision.output_destinations={result:{parts:['qa','report_test']}};assert.throws(()=>verify(collision),/разные адреса/);
 });
 console.log(JSON.stringify({synthetic_only:true,model_calls:0,network_calls:0,checks},null,2));
 if(checks.some(c=>!c.passed))process.exitCode=1;
}
main().catch(e=>{console.error(e);process.exitCode=1;});
