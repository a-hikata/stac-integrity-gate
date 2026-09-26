import json, fsspec, pyarrow.parquet as pq, concurrent.futures as cf
a=json.load(open("assets.json"))
targets=[x for x in a if x.get("cols") and x.get("href") and "*" not in x["href"] and "parquet" in (x.get("type") or "")+x["href"]]
# dedupe by href
uniq={}
for x in targets: uniq.setdefault(x["href"],x)
print("single-file parquet with cols:",len(uniq))
fs=fsspec.filesystem("https", block_size=1<<16)
def one(x):
    try:
        with fs.open(x["href"],"rb", cache_type="readahead") as f:
            md=pq.ParquetFile(f).metadata
            names=[md.schema.column(i).path.split(".")[0] for i in range(md.num_columns)]
            aschema=md.schema.to_arrow_schema()
            kv=md.metadata or {}
            geo=json.loads(kv[b"geo"]) if b"geo" in kv else None
            return {**x,"ok":True,"actual_cols":aschema.names,"actual_types":[str(t) for t in aschema.types],"num_rows":md.num_rows,"geo":geo}
    except Exception as e:
        return {**x,"ok":False,"err":f"{type(e).__name__}: {e}"[:200]}
with cf.ThreadPoolExecutor(12) as ex: res=list(ex.map(one,uniq.values()))
json.dump(res,open("footers.json","w"),indent=1)
print("read ok",sum(r["ok"] for r in res),"fail",sum(not r["ok"] for r in res))
