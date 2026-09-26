import json, urllib.request, sys
from urllib.parse import urljoin
def get(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"stac-integrity-gate-research"}),timeout=30) as r: return json.load(r)
    except Exception as e:
        print("FAIL",u,e,file=sys.stderr); return None
root="https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json"
cat=get(root); out=[]
seen=set()
def walk(u,depth):
    if u in seen or depth>3: return
    seen.add(u); d=get(u)
    if not d: return
    for k,a in (d.get("assets") or {}).items():
        t=str(a.get("type","")); h=a.get("href","")
        if "parquet" in t or h.endswith(".parquet") or "table:columns" in a:
            out.append({"doc":u,"key":k,"href":urljoin(u,h),"type":t,"cols":a.get("table:columns"),"rows":a.get("table:row_count"),"pg":a.get("table:primary_geometry"),"proj":{x:a.get(x) for x in ("proj:code","proj:epsg") if x in a}})
    if "table:columns" in d:
        out.append({"doc":u,"key":"<collection-level>","cols":d.get("table:columns"),"rows":d.get("table:row_count"),"pg":d.get("table:primary_geometry")})
    for l in d.get("links",[]):
        if l.get("rel") in ("child","item"): walk(urljoin(u,l["href"]),depth+1)
walk(root,0)
json.dump(out,open("assets.json","w"),indent=1)
print(len(seen),"docs",len(out),"parquet/table assets")
