"""Seed a disposable local Superset dashboard from independent synthetic evidence."""
from decimal import Decimal
import json
from pathlib import Path
import httpx
from psycopg import sql
from tools.run_testifier_lab import ROOT,OUT,FROZEN,owner
from fixtures.testifier_lab.compare import value,family


def records():
    result={}
    for phase in ('initial','expanded','replayed','improved','strong'):
        for path in (OUT/phase).glob('*.json'):
            if path.name.endswith('-pipeline.json'):continue
            r=json.loads(path.read_text(encoding='utf-8'))
            if 'profiles' in r or 'preparation' in r and 'state' not in r:result[r['id']]=r
    return result


def seed():
    corpus=json.loads((FROZEN/'corpus.json').read_text(encoding='utf-8'));flow=next(c for c in corpus if c['id']=='flow')
    engine=json.loads((OUT/'opensas.json').read_text(encoding='utf-8'));found=records();monthly=[];quality=[]
    def add_monthly(side,variant,rows):
        columns=flow['outputs']['monthly']
        for row in rows:
            m,segment,n,total=[value(v,family(c['data_type'])) for c,v in zip(columns,row)]
            monthly.append((side,variant,m,m or '(пропуск)',segment or '(пропуск)',n,total))
    for variant in flow['variants']:add_monthly('Контрольный расчёт',variant['name'],variant['expected']['monthly'])
    for r in engine['results']:
        if r['id']=='flow' and r.get('rows',{}).get('monthly') is not None:add_monthly('OpenSAS',r['variant'],r['rows']['monthly'])
    for target,p in found.get('flow',{}).get('profiles',{}).items():
        for r in p.get('variants',[]):
            if r.get('rows',{}).get('monthly') is not None:add_monthly('SQL '+target,r['name'],r['rows']['monthly'])
    manual=json.loads((OUT/'manual-bi-control.json').read_text(encoding='utf-8')) if (OUT/'manual-bi-control.json').exists() else {}
    for target,p in manual.get('profiles',{}).items():
        for r in p['variants']:
            if r.get('rows',{}).get('monthly') is not None:add_monthly('Ручной SQL '+('PostgreSQL' if target=='postgresql' else 'ClickHouse'),r['name'],r['rows']['monthly'])
    alternative=json.loads((OUT/'diagnostic-alternative-clickhouse.json').read_text(encoding='utf-8')) if (OUT/'diagnostic-alternative-clickhouse.json').exists() else {}
    for r in alternative.get('variants',[]):
        if r.get('rows',{}).get('monthly') is not None:add_monthly('SQL CH: альтернативная сборка, не принята',r['name'],r['rows']['monthly'])
    for c in corpus:
        originals=[r for r in engine['results'] if r['id']==c['id']]
        passed=all(r.get('passed') is True for r in originals) and len(originals)==4
        quality.append((c['id'],c['title'],'OpenSAS',int(c['negative']),None,int(passed) if not c['negative'] else None,sum(r.get('passed') is True for r in originals),len(originals),'' if passed else next((r['log_tail'][-350:] for r in originals if r.get('passed') is False),'')))
        for target in ('greenplum','postgresql','clickhouse'):
            r=found.get(c['id'],{});p=r.get('profiles',{}).get(target,{})
            reason='; '.join(x.get('code','') for x in r.get('requirements',[])[:5]) or (json.dumps(r.get('preparation'),ensure_ascii=False)[:350] if r.get('preparation') else r.get('error',''))
            variants=p.get('variants',[])
            quality.append((c['id'],c['title'],'SQL '+target,int(c['negative']),int(p.get('code_passed',False)),
                int(p['all_variants_passed']) if 'all_variants_passed' in p else None,sum(v.get('passed') is True for v in variants),len(variants),reason))
    with owner() as db:
        db.execute('CREATE SCHEMA IF NOT EXISTS bi')
        db.execute('CREATE TABLE IF NOT EXISTS bi.monthly(side TEXT,variant TEXT,month DATE,display_month TEXT,segment TEXT,n NUMERIC,total NUMERIC)')
        db.execute('CREATE TABLE IF NOT EXISTS bi.quality(case_id TEXT,title TEXT,engine TEXT,negative INTEGER,code_passed INTEGER,data_passed INTEGER,variants_passed INTEGER,variants_run INTEGER,reason TEXT)')
        db.execute('TRUNCATE bi.monthly,bi.quality')
        with db.cursor() as cur:
            cur.executemany('INSERT INTO bi.monthly VALUES(%s,%s,%s,%s,%s,%s,%s)',monthly)
            cur.executemany('INSERT INTO bi.quality VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',quality)
    return {'monthly_rows':len(monthly),'quality_rows':len(quality),'arms':sorted({r[0] for r in monthly})}


class BI:
    def __init__(self):
        self.client=httpx.Client(base_url='http://127.0.0.1:18088',trust_env=False,timeout=50)
        login=self.client.post('/api/v1/security/login',json={'username':'lab','password':'synthetic_lab_only','provider':'db','refresh':True});login.raise_for_status()
        self.client.headers['Authorization']='Bearer '+login.json()['access_token']
        csrf=self.client.get('/api/v1/security/csrf_token/');csrf.raise_for_status();self.client.headers['X-CSRFToken']=csrf.json()['result']
    def call(self,method,path,body=None):
        response=self.client.request(method,path,json=body)
        if response.status_code>=400:raise ValueError(path+' '+str(response.status_code)+' '+response.text[:1700])
        return response.json()
    def find(self,resource,field,value):return next((x for x in self.call('GET','/api/v1/'+resource+'/')['result'] if x.get(field)==value),None)
    def ensure(self,resource,field,value,body):
        previous=self.find(resource,field,value)
        if previous:return previous['id']
        return self.call('POST','/api/v1/'+resource+'/',body)['id']


def metric(column,label):
    return {'expressionType':'SIMPLE','column':{'column_name':column},'aggregate':'SUM','label':label,'hasCustomLabel':True,'optionName':'metric_'+column}


def query_context(dataset,columns,metrics=(),filters=()):
    return {'datasource':{'id':dataset,'type':'table'},'force':True,'queries':[{'columns':columns,'metrics':list(metrics),'filters':list(filters),
        'time_range':'No filter','row_limit':10000,'extras':{'having':'','where':''},'orderby':[]}],'result_format':'json','result_type':'full'}


def main():
    summary=seed();bi=BI()
    database=bi.ensure('database','database_name','Synthetic SAS migration lab',{'database_name':'Synthetic SAS migration lab',
        'sqlalchemy_uri':'postgresql+psycopg2://lab_owner:synthetic_lab_only@postgres:5432/synthetic_lab','expose_in_sqllab':False,'allow_dml':False,'allow_run_async':False})
    monthly=bi.ensure('dataset','table_name','monthly',{'database':database,'schema':'bi','table_name':'monthly'})
    quality=bi.ensure('dataset','table_name','quality',{'database':database,'schema':'bi','table_name':'quality'})
    dashboard=bi.ensure('dashboard','slug','sas-testifier-lab',{'dashboard_title':'SAS -> SQL: синтетическая проверка','slug':'sas-testifier-lab','published':True})
    charts=[];specs=[]
    for side in ('Контрольный расчёт','OpenSAS','Ручной SQL PostgreSQL','Ручной SQL ClickHouse','SQL postgresql','SQL clickhouse'):
        filters=[{'col':'side','op':'IN','val':[side]},{'col':'variant','op':'IN','val':['edge']}]
        specs.append((side+' - сумма, граничный набор','big_number_total',monthly,[],[metric('total','Сумма')],filters,{'metric':metric('total','Сумма'),'subheader':'Ручной контроль, не успех конвертера' if side.startswith('Ручной') else 'Не результат SAS Institute; суммы из зафиксированных расчётов'}))
    specs += [
        ('Месяцы и стороны сравнения','echarts_timeseries_bar',monthly,['display_month','side'],[metric('total','Сумма')],[{'col':'variant','op':'IN','val':['edge']}],{'x_axis':'display_month','groupby':['side'],'metrics':[metric('total','Сумма')],'orientation':'vertical','show_legend':True,'show_value':True,'x_axis_sort_asc':True,'time_range':'No filter'}),
        ('Полные месячные результаты','table',monthly,['side','variant','display_month','segment','n','total'],[],[],{'query_mode':'raw','all_columns':['side','variant','display_month','segment','n','total'],'include_search':True}),
        ('Матрица функций, диалектов и проверок','table',quality,['case_id','title','engine','negative','code_passed','data_passed','variants_passed','variants_run','reason'],[],[],{'query_mode':'raw','all_columns':['case_id','title','engine','negative','code_passed','data_passed','variants_passed','variants_run','reason'],'include_search':True})]
    data_checks={}
    for title,viz,dataset,columns,metrics,filters,extra in specs:
        context=query_context(dataset,columns,metrics,filters)
        params={'viz_type':viz,'datasource':str(dataset)+'__table','row_limit':10000,'time_range':'No filter','adhoc_filters':[{'expressionType':'SIMPLE','subject':f['col'],'operator':'IN','comparator':f['val'],'clause':'WHERE','isExtra':False,'sqlExpression':None} for f in filters],**extra}
        payload={'slice_name':title,'viz_type':viz,'datasource_id':dataset,'datasource_type':'table','params':json.dumps(params,ensure_ascii=False),
            'query_context':json.dumps(context,ensure_ascii=False),'dashboards':[dashboard]}
        old=bi.find('chart','slice_name',title)
        if old:bi.call('PUT','/api/v1/chart/'+str(old['id']),payload);ident=old['id']
        else:ident=bi.call('POST','/api/v1/chart/',payload)['id']
        charts.append({'id':ident,'title':title,'dataset':dataset,'query_context':context})
        response=bi.call('POST','/api/v1/chart/data',context)
        data_checks[title]={'status':response.get('result',[{}])[0].get('status'),'data':response.get('result',[{}])[0].get('data')}
    position={'DASHBOARD_VERSION_KEY':'v2','ROOT_ID':{'type':'ROOT','id':'ROOT_ID','children':['GRID_ID']},'GRID_ID':{'type':'GRID','id':'GRID_ID','children':[],'parents':['ROOT_ID']},'HEADER_ID':{'id':'HEADER_ID','type':'HEADER','meta':{'text':'SAS -> SQL: контроль / OpenSAS / SQL'}}}
    for rownum,chunk in enumerate((charts[:4],charts[4:6],charts[6:7],charts[7:8],charts[8:])):
        rid='ROW-'+str(rownum);position['GRID_ID']['children'].append(rid)
        position[rid]={'id':rid,'type':'ROW','children':[],'parents':['ROOT_ID','GRID_ID'],'meta':{'background':'BACKGROUND_TRANSPARENT'}}
        for chart in chunk:
            cid='CHART-'+str(chart['id']);position[rid]['children'].append(cid)
            position[cid]={'id':cid,'type':'CHART','children':[],'parents':['ROOT_ID','GRID_ID',rid],
                'meta':{'chartId':chart['id'],'sliceName':chart['title'],'width':12//len(chunk),'height':36 if len(chunk)>1 else 58}}
    native=[]
    for ident,column,title in [('NATIVE_FILTER-side','side','Сторона расчёта'),('NATIVE_FILTER-month','display_month','Месяц')]:
        native.append({'id':ident,'name':title,'filterType':'filter_select','targets':[{'datasetId':monthly,'column':{'name':column}}],
            'controlValues':{'multiSelect':True,'enableEmptyFilter':False,'searchAllOptions':False,'inverseSelection':False},
            'cascadeParentIds':[],'defaultDataMask':{'extraFormData':{},'filterState':{},'ownState':{}},'scope':{'rootPath':['ROOT_ID'],'excluded':[charts[-1]['id']]},'type':'NATIVE_FILTER','description':'Только вымышленные данные. OpenSAS - независимый интерпретатор.'})
    bi.call('PUT','/api/v1/dashboard/'+str(dashboard),{'position_json':json.dumps(position,ensure_ascii=False),
        'json_metadata':json.dumps({'native_filter_configuration':native,'refresh_frequency':0,'color_scheme':'supersetColors'},ensure_ascii=False),
        'published':True,'dashboard_title':'SAS -> SQL: синтетическая проверка','slug':'sas-testifier-lab'})
    result={'url':'http://127.0.0.1:18088/superset/dashboard/sas-testifier-lab/','synthetic_only':True,'sas_institute_runtime':False,
        'datasets':{'monthly':monthly,'quality':quality},'dashboard':dashboard,'charts':charts,'seed':summary,'chart_data_api':data_checks}
    (OUT/'bi-dashboard.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'url':result['url'],'charts':len(charts),'seed':summary},ensure_ascii=True))


if __name__=='__main__':main()
