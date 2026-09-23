import json, os, re, sys

def load_texts(files):
    """Return concatenated text of all files under each 'files' entry."""
    out = []
    for f in files:
        if os.path.isdir(f):
            for root,_,fs in os.walk(f):
                for n in fs:
                    p=os.path.join(root,n)
                    try:
                        out.append(open(p,encoding='utf-8',errors='ignore').read())
                    except Exception: pass
        elif os.path.isfile(f):
            try: out.append(open(f,encoding='utf-8',errors='ignore').read())
            except Exception: pass
    return "\n".join(out)

def grade_run(run_dir, assertions):
    texts = load_texts([os.path.join(run_dir,"outputs")])
    hay = texts.lower()
    results=[]
    for a in assertions:
        patterns = [p.lower() for p in a.get("patterns",[])]
        passed = any(p in hay for p in patterns)
        hits = [p for p in patterns if p in hay]
        evidence = ", ".join(hits[:6]) if hits else "none of the expected markers found"
        results.append({"text":a["text"],"passed":passed,"evidence":evidence})
    return results

def main(root, evals_map):
    for eval_dir, meta_path in evals_map.items():
        meta=json.load(open(meta_path,encoding="utf-8"))
        for variant in ["with_skill","without_skill"]:
            run_dir=os.path.join(root,eval_dir,variant)
            if not os.path.isdir(run_dir): continue
            results=grade_run(run_dir, meta.get("assertions",[]))
            gj={"eval_id":meta.get("eval_id"),"eval_name":meta.get("eval_name"),
                "variant":variant,"expectations":results,
                "pass_count":sum(1 for r in results if r["passed"]),
                "total":len(results)}
            with open(os.path.join(run_dir,"grading.json"),"w",encoding="utf-8") as f:
                json.dump(gj,f,indent=2,ensure_ascii=False)
            print(f"{eval_dir}/{variant}: {gj['pass_count']}/{gj['total']}")

if __name__=="__main__":
    root="."
    evals_map={}
    for d in os.listdir(root):
        mp=os.path.join(root,d,"eval_metadata.json")
        if os.path.isfile(mp):
            evals_map[d]=mp
    main(root, evals_map)
