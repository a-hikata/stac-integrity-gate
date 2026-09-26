import json, collections
r=json.load(open("footers.json"))
ok=[x for x in r if x["ok"]]
print([x for x in r if not x["ok"]][0]["err"], [x for x in r if not x["ok"]][0]["href"])
miss=[];extra=0;rowm=[];rowok=0;pgm=[];geon=0;typepairs=collections.Counter();crs=collections.Counter()
for x in ok:
    decl=[c.get("name") for c in x["cols"] if isinstance(c,dict)]
    act=set(x["actual_cols"])
    m=[d for d in decl if d not in act]
    if m: miss.append((x["href"],m,len(decl),len(act)))
    if act-set(decl): extra+=1
    if x.get("rows") is not None:
        (rowok:=rowok+1) if x["rows"]==x["num_rows"] else rowm.append((x["href"],x["rows"],x["num_rows"]))
    if x["geo"]: geon+=1; crs[json.dumps((x["geo"]["columns"].get(x["geo"]["primary_column"],{}).get("crs") or {}).get("id","<absent=OGC:CRS84>") if isinstance(x["geo"]["columns"].get(x["geo"]["primary_column"],{}).get("crs",{}),dict) else "str")]+=1
    if x.get("pg"):
        pgm.append((x["href"],x["pg"],x["geo"]["primary_column"] if x["geo"] else None, x["pg"] in act))
    at=dict(zip(x["actual_cols"],x["actual_types"]))
    for c in x["cols"]:
        if isinstance(c,dict) and c.get("name") in at and c.get("type"): typepairs[(c["type"],at[c["name"]])]+=1
print("ok",len(ok),"assets w/ missing declared cols",len(miss),"assets with extra undeclared",extra)
for m in miss[:40]: print("  MISS",m)
print("row_count declared",rowok+len(rowm),"match",rowok,"mismatch",rowm)
print("primary_geometry",pgm)
print("with geo metadata",geon, crs)
print("type pairs",typepairs.most_common(60))
