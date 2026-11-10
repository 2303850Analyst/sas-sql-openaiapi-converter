"""One deterministic destination policy for planning, generation, linking and export.

Approved logical names never change in storage. Physical overrides/test suffixes
are resolved once; input objects are never renamed by suffix substitution.
"""
from copy import deepcopy
from .contracts import ObjectName, digest


def resolve(plan):
    base=plan.model_dump(mode='json') if hasattr(plan,'model_dump') else deepcopy(plan)
    effective=deepcopy(base);overrides=base.get('output_destinations',{})
    outputs=base.get('outputs',[]);known={'.'.join(o['name']['parts']) for o in outputs}
    if set(overrides)-known:raise ValueError('Destination override refers to an undeclared output')
    mapping={};registry=[];seeds=[]
    for output in outputs:
        key='.'.join(output['name']['parts']);destination=overrides.get(key,output['name'])
        parts=list(destination['parts'])
        if len(parts)==2 and parts[0].casefold()=='work':parts=parts[1:]
        storage='temporary' if len(parts)==1 else 'persistent'
        if base.get('test_mode') and storage=='persistent' and not parts[-1].endswith('_test'):parts[-1]+='_test'
        ObjectName(parts=tuple(parts)) # UTF-8 length and explicit identifier checks.
        target='.'.join(parts)
        if target in mapping.values():raise ValueError('Two outputs resolve to the same physical destination: '+target)
        mapping[key]=target
        original=base.get('output_sources',{}).get(key,key)
        registry.append({'logical':key,'original':original,'destination':{'parts':parts},'storage':storage,
            'work_default':len(output['name']['parts'])==1,'overridden':key in overrides,
            'test_suffix':bool(base.get('test_mode') and storage=='persistent')})
    inputs={'.'.join(b['target']['parts']):b for b in base.get('bindings',[])}
    for output,row in zip(effective.get('outputs',[]),registry):
        key=row['logical'];target=mapping[key]
        output['name']=row['destination'];output['storage']=row['storage']
        effects=base.get('allowed_effects',{}).get(key,['create'])
        if base.get('test_mode') and key in inputs and target!=key and 'create' not in effects:
            source=inputs[key]
            if [c['name'] for c in source['columns']]!=output['columns']:
                raise ValueError('Test copy of an existing destination requires its complete unchanged column contract: '+key)
            seeds.append({'source':source['target'],'target':row['destination'],'columns':output['columns'],
                'source_id':source['source_id'],'original':row['original'],'clickhouse_engine':output.get('clickhouse_engine')})
    # A relocated output must not collide with a different external input.
    for row in registry:
        target='.'.join(row['destination']['parts'])
        if target in inputs and target!=row['logical']:raise ValueError('Output destination collides with another external input: '+target)
    effective['allowed_effects']={mapping[k]:list(v) for k,v in base.get('allowed_effects',{}).items()}
    for seed in seeds:
        key='.'.join(seed['target']['parts']);policy=effective['allowed_effects'].setdefault(key,['create'])
        if 'create' not in policy:policy.insert(0,'create')
    effective['output_sources']={mapping[k]:base.get('output_sources',{}).get(k,k) for k in known}
    effective['scenario_outputs']={s:[mapping[n] for n in names] for s,names in base.get('scenario_outputs',{}).items()}
    # Prevent accidental recursive resolution by callers accepting a materialized view.
    effective['test_mode']=False;effective['output_destinations']={}
    result={'plan':effective,'registry':registry,'seeds':seeds,'test_mode':bool(base.get('test_mode'))}
    result['hash']=digest(result)
    return result


def unit_plan(plan,seeds):
    result=deepcopy(plan)
    targets={s['source_id']:s['target'] for s in seeds}
    for binding in result['bindings']:
        if binding['source_id'] in targets:binding['target']=targets[binding['source_id']]
    return result


def seed_artifacts(seeds,target):
    from sqlglot import exp
    dialect='clickhouse' if target=='clickhouse' else 'postgres'
    result=[]
    def table(name):return exp.Table(this=exp.Identifier(this=name['parts'][-1],quoted=True),db=exp.Identifier(this=name['parts'][0],quoted=True) if len(name['parts'])==2 else None)
    for n,seed in enumerate(seeds):
        query=exp.Select(expressions=[exp.Column(this=exp.Identifier(this=c,quoted=True)) for c in seed['columns']]).from_(table(seed['source']))
        properties=[]
        if len(seed['target']['parts'])==1:properties.append(exp.TemporaryProperty())
        if target=='clickhouse':
            engine = seed.get("clickhouse_engine") or ("Memory" if len(seed["target"]["parts"]) == 1 else "MergeTree")
            if engine == "ReplacingMergeTree":
                raise ValueError("Test copies require an engine that preserves every source row")
            properties.append(exp.EngineProperty(this=exp.Var(this=engine)))
            if engine == "MergeTree":
                properties.append(exp.Order(expressions=[exp.Ordered(this=exp.Anonymous(this="tuple"))]))
        tree=exp.Create(this=table(seed['target']),kind='TABLE',expression=query,properties=exp.Properties(expressions=properties) if properties else None)
        result.append({'name':f'{target}_test_seed_{n+1:03d}.sql','sql':tree.sql(dialect=dialect)+';'})
    return result
