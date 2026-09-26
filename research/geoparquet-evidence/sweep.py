import json, collections, concurrent.futures as cf
from stac_integrity.audit import audit_item_dict, load_json
a=json.load(open("assets.json"))
docs=sorted({x["doc"] for x in a if "parquet" in (x.get("type") or "") or (x.get("href") or "").endswith(".parquet")})
def run(u):
    d=load_json(u)
    if d.get("type")=="Collection": d={"id":d["id"],"properties":d,"assets":d.get("assets") or {}}
    r=audit_item_dict(d,source=u)
    return u,[f for f in r.findings if f.code!="NO_RASTER_ASSETS"], r.checked_assets
with cf.ThreadPoolExecutor(8) as ex: res=list(ex.map(run,docs))
c=collections.Counter((f.severity,f.code) for _,fs,_ in res for f in fs)
print(len(docs),"docs; parquet assets checked",sum(x[2] for x in res)); print(c)
for u,fs,_ in res:
    for f in fs:
        if f.code not in("PARQUET_PARTITIONED_UNVERIFIED","TABLE_COLUMN_TYPE_MISMATCH"): print(f.severity,f.code,u.split('/')[-2:],f.asset,f.declared if len(str(f.declared))<60 else str(f.declared)[:60],str(f.actual)[:60])
json.dump([[u,[f.to_dict() for f in fs]] for u,fs,_ in res],open("sweep.json","w"),indent=1,default=str)
