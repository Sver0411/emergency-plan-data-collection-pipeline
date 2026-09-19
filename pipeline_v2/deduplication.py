from __future__ import annotations
from datasketch import MinHash, MinHashLSH
from rapidfuzz.fuzz import ratio
from .normalization import devariable_text

def shingles(text: str, width: int=5): return {text[i:i+width] for i in range(max(0,len(text)-width+1))}
def minhash(text: str) -> MinHash:
    m=MinHash(num_perm=128)
    for gram in shingles(devariable_text(text)): m.update(gram.encode("utf-8"))
    return m
def near_duplicate_groups(records: list[dict]) -> list[dict]:
    lsh=MinHashLSH(threshold=.80,num_perm=128); signatures={}
    for record in records:
        signature=minhash(record["normalized_text"]); signatures[record["document_id"]]=signature; lsh.insert(record["document_id"],signature)
    groups=[]; seen=set()
    for record in records:
        ident=record["document_id"]; candidates=sorted(x for x in lsh.query(signatures[ident]) if x!=ident)
        for other in candidates:
            pair=tuple(sorted((ident,other)))
            if pair in seen: continue
            seen.add(pair); left=next(r for r in records if r["document_id"]==ident); right=next(r for r in records if r["document_id"]==other)
            sim=ratio(left["normalized_text"],right["normalized_text"])/100
            groups.append({"group_id":"dup-"+"-".join(pair),"document_ids":list(pair),"classification":"manual_review" if sim<.92 else "near_duplicate","metrics":{"rapidfuzz_ratio":round(sim,4),"minhash_jaccard":round(signatures[ident].jaccard(signatures[other]),4)},"review_status":"pending"})
    return groups
