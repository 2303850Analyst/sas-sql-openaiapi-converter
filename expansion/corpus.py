"""New synthetic SAS projects with independent Python references and stable EGP bytes."""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import random
import zipfile
import io

HERE=Path(__file__).resolve().parent
FROZEN=HERE/"corpus"
D=Decimal


def col(name,type_="INTEGER",nullable=True):
    return {"name":name,"data_type":type_,"nullable":nullable}


ID=col("id",nullable=False)
KEY=col("sector")
AMOUNT=col("reading","NUMERIC(24,12)")
AVG=col("mean_reading","DECIMAL(30,9)")
TOTAL=col("total_reading","NUMERIC(28,12)")
COUNT=col("present",nullable=False)


def binding(columns,unique_keys=()):
    return {"columns":columns,"unique_keys":list(unique_keys)}


def make(name,family,sources,inputs,outputs,oracle,examples,*,negative=False,intermediate=()):
    if isinstance(sources,str):
        sources={"P1":sources}
    variants=[]
    for label,rows in examples:
        variants.append({"name":label,"inputs":rows,"expected":None if negative else oracle(rows)})
    return {"id":name,"title":family,"sources":sources,"source":"\n".join(sources.values()),"inputs":inputs,
        "outputs":outputs,"variants":variants,"negative":negative,"finals":[k for k in outputs if k not in intermediate],
        "output_sources":{k:"work."+k if k in intermediate else "mart."+k for k in outputs}}


def aggregate(inputs,*,sql):
    groups={}
    for _,key,text in inputs["readings"]:
        vals=groups.setdefault(key,[])
        vals.append(D(text) if text is not None else None)
    rows=[];before=[]
    for key,values in groups.items():
        good=[v for v in values if v is not None]
        total=sum(good,D(0)) if good else None
        mean=total/len(good) if good else None
        before.append([key,str(mean) if mean is not None else None])
        stored=mean.quantize(D("0.000000001"),rounding=ROUND_HALF_UP) if mean is not None else None
        row=[key]
        if sql:
            row.extend([len(values),len(set(good))])
        rows.append([*row,str(total) if total is not None else None,str(stored) if stored is not None else None,len(good)])
    return rows,before


def projects():
    edge=[[1,None,None],[2,None,"0"],[3,0,None],[4,1,"0.000499999975"],
          [5,2,"90071992547.409250000000"],[6,2,"90071992547.409260000000"],[7,3,"-0.000500000000"]]
    r=random.Random(8237)
    seeded=[[i,r.choice([None,1,2,3]),None if i%5==0 else str(D(r.randint(-100000,100000))/1000)]
            for i in range(1,49)]
    readings=[("edge",{"readings":edge}),("seed8237",{"readings":seeded}),
              ("permutation",{"readings":seeded[::-1]}),("empty",{"readings":[]})]
    inputs={"readings":binding([ID,KEY,AMOUNT],[["id"]])}
    common=[TOTAL,AVG,COUNT]
    sql=make("ex_observatory","PROC SQL GROUP BY, AVG and scale-9 checkpoint",
        "proc sql; create table mart.sky_summary as select sector, count(*) as n, count(distinct reading) as uniq, sum(reading) as total_reading, avg(reading) as mean_reading, count(reading) as present from source.readings group by sector having count(*)>0; quit;",
        inputs,{"sky_summary":[KEY,col("n",nullable=False),col("uniq",nullable=False),*common]},
        lambda x:{"sky_summary":aggregate(x,sql=True)[0]},readings)
    summary=make("ex_acoustic","PROC SUMMARY NWAY MISSING, named statistics",
        "proc summary data=source.readings nway missing; class sector; var reading; output out=mart.echo_summary(drop=_type_ _freq_) sum=total_reading mean=mean_reading n=present; run;",
        inputs,{"echo_summary":[KEY,*common]},lambda x:{"echo_summary":aggregate(x,sql=False)[0]},readings)
    for c,is_sql in ((sql,True),(summary,False)):
        for v in c["variants"]:
            v["before_storage_average"]=aggregate(v["inputs"],sql=is_sql)[1]
        c["arithmetic_contract"]="Exact decimal after transport; round final storage to scale 9 half away from zero, then QA scale 3; average of all missing is NULL."
    append=make("ex_library","Two programs: APPEND FORCE by name, duplicate IDs",
        {"P1":"data mart.catalogue; set source.editions; run;",
         "P2":"proc append base=mart.catalogue data=source.accessions force; run;"},
        {"editions":binding([ID,col("pages")]),"accessions":binding([ID,col("shelf")])},
        {"catalogue":[ID,col("pages")]},lambda x:{"catalogue":x["editions"]+[[r[0],None] for r in x["accessions"]]},
        [("duplicates",{"editions":[[1,10],[1,10],[9,None]],"accessions":[[1,2],[1,2],[3,None]]}),
         ("no_base",{"editions":[],"accessions":[[2,5]]}),("no_append",{"editions":[[2,5]],"accessions":[]}),
         ("empty",{"editions":[],"accessions":[]})])
    packet_inputs={"parcels":binding([ID,col("weight"),col("zone")],[["id"]]),
                   "zones":binding([col("zone",nullable=False),col("label","VARCHAR(16)")],[["zone"]])}
    def dispatch(x):
        allowed=[];rejected=[]
        for id_,weight,zone in x["parcels"]:
            (allowed if weight is not None and weight>=3 else rejected).append([id_,weight,zone])
        labels=dict(x["zones"])
        return {"light_parcels":rejected,"ready_parcels":allowed,
                "dispatch_board":[[*row,labels.get(row[2])] for row in allowed]}
    flow=make("ex_dispatch","Two programs: local LET, DATA routing, temporary outputs and LEFT JOIN",
        {"P1":"%let threshold=3; data work.ready_parcels work.light_parcels; set source.parcels; if missing(weight) or weight<&threshold. then output work.light_parcels; else output work.ready_parcels; run;",
         "P2":"proc sql; create table mart.dispatch_board as select p.id,p.weight,p.zone,z.label from work.ready_parcels p left join source.zones z on p.zone=z.zone; quit;"},
        packet_inputs,{"ready_parcels":[ID,col("weight"),col("zone")],"light_parcels":[ID,col("weight"),col("zone")],
                       "dispatch_board":[ID,col("weight"),col("zone"),col("label","VARCHAR(16)")]},dispatch,
        [("edge",{"parcels":[[1,2,2],[2,3,None],[3,None,1],[4,12,1],[5,14,3]],"zones":[[1,""],[2,"east"]]}),
         ("empty_right",{"parcels":[[1,6,6]],"zones":[]}),("empty",{"parcels":[],"zones":[]})],
        intermediate=("ready_parcels","light_parcels"))
    pulse_cols=[ID,col("seq",nullable=False),col("enabled"),col("signal")]
    def pulses(x):
        ordered=sorted(x["samples"],key=lambda r:r[1]); prior=None; out=[]
        for id_,_,enabled,signal in ordered:
            out.append([id_,prior if enabled==1 else None])
            if enabled==1:
                prior=signal
        return {"queued_pulses":ordered,"pulse_delta":out}
    lag=make("ex_pulses","Two programs: SORT, conditional LAG with null queue values",
        {"P1":"proc sort data=source.samples out=work.queued_pulses; by seq; run;",
         "P2":"data mart.pulse_delta(keep=id previous); set work.queued_pulses; if enabled=1 then previous=lag(signal); else previous=.; run;"},
        {"samples":binding(pulse_cols,[["id"],["seq"]])},{"queued_pulses":pulse_cols,"pulse_delta":[ID,col("previous")]},pulses,
        [("queue",{"samples":[[90,1,1,10],[5,2,0,99],[12,3,1,None],[2,4,1,45]]}),
         ("permuted",{"samples":[[2,4,1,45],[12,3,1,None],[5,2,0,99],[90,1,1,10]]}),("empty",{"samples":[]})],
        intermediate=("queued_pulses",))
    negative=make("ex_unknown","External environment parameter requires HITL",
        "%let min_pages=%sysget(SYNTHETIC_UNSUPPLIED_PAGES); data mart.selected_books; set source.books; if pages>&min_pages.; run;",
        {"books":binding([ID,col("pages")])},{"selected_books":[ID,col("pages")]},None,
        [("negative",{"books":[[1,2],[2,9]]})],negative=True)
    return [sql,summary,append,flow,lag,negative]



def additional_projects():
    """Independent examples covering seven distinct SAS language families."""
    def examples(source, edge, other=None):
        return [("edge", {source: edge}), ("second", {source: other if other is not None else edge[::-1]}),
                ("empty", {source: []})]

    projected = make("trial_options", "Input KEEP/WHERE/RENAME and output KEEP",
        "data mart.selected(keep=id result); set source.items(keep=id raw active where=(active=1) rename=(raw=points)); result=points+2; run;",
        {"items": binding([ID,col("raw"),col("active")])}, {"selected":[ID,col("result")]},
        lambda x: {"selected":[[i,None if value is None else value+2] for i,value,a in x["items"] if a==1]},
        examples("items",[[1,0,1],[2,None,1],[3,9,0],[4,8,None]]))
    ordered_columns=[ID,col("bucket",nullable=False),col("value")]
    def retained(x):
        running={}
        result=[]
        ordered=sorted(x["events"],key=lambda r:(r[1],r[0]))
        for i,bucket,value in ordered:
            running[bucket]=running.get(bucket,0)+(value or 0)
            result.append([i,bucket,running[bucket]])
        return {"ordered_events":ordered,"running_totals":result}
    retain = make("trial_retain", "Two programs: RETAIN, BY boundaries and sum statement",
        {"P1":"proc sort data=source.events out=work.ordered_events; by bucket id; run;",
         "P2":"data mart.running_totals(keep=id bucket running); set work.ordered_events; by bucket id; retain running 0; if first.bucket then running=0; running + value; run;"},
        {"events":binding(ordered_columns,[["id"]])}, {"ordered_events":ordered_columns,"running_totals":[ID,col("bucket"),col("running",nullable=False)]},
        retained,examples("events",[[3,2,9],[4,1,None],[2,1,-4],[1,1,4],[5,2,None]]),intermediate=("ordered_events",))
    array = make("trial_array", "Fixed arrays and bounded DO loop",
        "data mart.samples(keep=id total); set source.values_in; array v[2] a b; total=0; do j=1 to dim(v); if not missing(v[j]) then total=total+v[j]*j; end; run;",
        {"values_in":binding([ID,col("a"),col("b")])},{"samples":[ID,col("total",nullable=False)]},
        lambda x:{"samples":[[i,(a or 0)+2*(b or 0)] for i,a,b in x["values_in"]]},
        examples("values_in",[[1,None,None],[2,3,-3],[3,0,5],[4,None,2]]))
    def select_into(x):
        limit=min(x["parameters"])[1] if x["parameters"] else 4
        return {"selected_scores":[row for row in x["scores"] if row[1] is not None and row[1]>=limit]}
    into = make("trial_select_into", "SELECT INTO scalar macro for zero, one and many result rows",
        {"P1":"%let cutoff=4; proc sql noprint; select points into :cutoff trimmed from source.parameters order by seq; quit;",
         "P2":"data mart.selected_scores; set source.scores; if score>=&cutoff.; run;"},
        {"parameters":binding([col("seq",nullable=False),col("points",nullable=False)],[["seq"]]),
         "scores":binding([ID,col("score")])},{"selected_scores":[ID,col("score")]},select_into,
        [(label,{"parameters":rows,"scores":[[1,None],[2,3],[3,4],[4,10]]}) for label,rows in
         (("zero",[]),("one",[[9,10]]),("many",[[9,10],[2,3],[3,8]]))])
    def transpose(x):
        groups={}
        for station,band,reading in x["readings"]:
            groups.setdefault(station,{})[band]=reading
        return {"wide_readings":[[station,values.get("low"),values.get("high")] for station,values in groups.items()]}
    wide = make("trial_transpose", "TRANSPOSE fixed global ID domain, sparse groups",
        "proc transpose data=source.readings out=mart.wide_readings(drop=_name_) prefix=band_; by station; id band; var reading; run;",
        {"readings":binding([col("station",nullable=False),col("band","VARCHAR(4)",False),col("reading")],[["station","band"]])},
        {"wide_readings":[col("station"),col("band_low"),col("band_high")]},transpose,
        [("sparse",{"readings":[[1,"low",4],[1,"high",7],[2,"high",None],[3,"low",0]]}),
         ("one_group",{"readings":[[7,"low",None],[7,"high",9]]})])
    format_case = make("trial_format", "User-defined FORMAT and character storage",
        {"P1":"proc format; value badge 0='zero' 1-3='small' other='other'; run;",
         "P2":"data mart.badges(keep=id label); length label $5; set source.levels; label=put(level,badge.); run;"},
        {"levels":binding([ID,col("level")])},{"badges":[ID,col("label","VARCHAR(5)")]},
        lambda x:{"badges":[[i,"zero" if v==0 else "small" if v is not None and 1<=v<=3 else "other"] for i,v in x["levels"]]},
        examples("levels",[[1,None],[2,-1],[3,0],[4,1],[5,3],[6,4]]))
    all_sets=make("trial_multiset", "UNION ALL preserves duplicates and NULL",
        "proc sql; create table mart.combined as select id, units from source.batch_a union all select id, units from source.batch_b; quit;",
        {"batch_a":binding([ID,col("units")]),"batch_b":binding([ID,col("units")])},{"combined":[ID,col("units")]},
        lambda x:{"combined":x["batch_a"]+x["batch_b"]},
        [("duplicate",{"batch_a":[[1,None],[1,None],[2,4]],"batch_b":[[1,None],[2,4],[3,0]]}),
         ("one_side",{"batch_a":[],"batch_b":[[1,None],[1,None]]}),
         ("empty",{"batch_a":[],"batch_b":[]})])
    unsupported = make("trial_unknown_array", "External array bound must be resolved",
        "%let n=%sysget(SYNTHETIC_ARRAY_BOUND); data mart.output; set source.values_in; array x[&n.] a b; run;",
        {"values_in":binding([ID,col("a"),col("b")])},{"output":[ID,col("a"),col("b")]},None,
        [("unresolved",{"values_in":[[1,2,3]]})],negative=True)
    return [projected,retain,array,into,wide,format_case,all_sets,unsupported]



def stable_archive(sources):
    out=io.BytesIO()
    xml="<ProjectCollection><Elements>"+"".join(
        f'<Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>{key}</ID><Label>{key}</Label></Element></Element>'
        for key in sources)+"</Elements></ProjectCollection>"
    with zipfile.ZipFile(out,"w") as z:
        z.writestr(zipfile.ZipInfo("project.xml"),xml)
        for name,code in sources.items():
            z.writestr(zipfile.ZipInfo(name+"/code.sas"),"/* Synthetic fixture */\n"+code)
    return out.getvalue()


def freeze():
    FROZEN.mkdir(exist_ok=True)
    items=projects()+additional_projects()
    for c in items:
        raw=stable_archive(c["sources"])
        c["archive_sha256"]=hashlib.sha256(raw).hexdigest()
        (FROZEN/(c["id"]+".egp")).write_bytes(raw)
        (FROZEN/(c["id"]+".sas")).write_text(c["source"],encoding="utf-8")
    raw=json.dumps(items,ensure_ascii=False,indent=2).encode("utf-8")
    (FROZEN/"cases.json").write_bytes(raw)
    result={"synthetic_only":True,"cases":len(items),"datasets":sum(len(c["variants"]) for c in items),
        "seed":8237,"archives":{c["id"]:c["archive_sha256"] for c in items},
        "oracle_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases_sha256":hashlib.sha256(raw).hexdigest(),"native_enterprise_guide_verified":False}
    (HERE/"synthetic_corpus_manifest.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result


if __name__=="__main__":
    print(freeze())
