from __future__ import annotations
import argparse, copy, hashlib, json, os, random, sys, tempfile, time, uuid
from collections import Counter
from pathlib import Path
from typing import Any
from .adapters.fixture import FixtureAdapter
from .environments.prepare import prepare_snapshot
from .scoring.score import score_rows
from .scoring.aggregate import aggregate

ROOT=Path(__file__).resolve().parent
DEFAULT_CONFIG=ROOT/"config.yaml"
HELDOUT_CATEGORY_COUNTS={"multi_hop":30,"conflicts":30,"missing_evidence":30,"freshness":30}
def digest(path: Path): return hashlib.sha256(path.read_bytes()).hexdigest()
def _read_jsonl(path: Path):
    rows=[]
    with path.open(encoding="utf-8") as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: rows.append(json.loads(line))
                except json.JSONDecodeError as e: raise ValueError(f"{path}:{n}: invalid JSONL") from e
    return rows

def _digest(path: Path): return digest(path)
def _private_directory(path: Path, *, secure_contents=False) -> None:
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.chmod(0o700)
    if secure_contents:
        for child in path.rglob("*"):
            if child.is_symlink(): raise ValueError(f"refusing symlink in private artifact directory: {child}")
            if child.is_dir(): child.chmod(0o700)
            elif child.is_file(): child.chmod(0o600)

def _atomic_private_write(path: Path, text: str, *, private_parent=False) -> None:
    if private_parent: _private_directory(path.parent)
    else: path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp_name=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    temporary=Path(temp_name)
    try:
        os.fchmod(fd,0o600)
        stream=os.fdopen(fd,"w",encoding="utf-8"); fd=None
        with stream:
            stream.write(text)
        temporary.replace(path)
        path.chmod(0o600)
    except Exception:
        try:
            if fd is not None: os.close(fd)
            temporary.unlink(missing_ok=True)
        finally: raise

def _paths(cfg: dict[str,Any], config_path: Path):
    base=config_path.resolve().parent
    data={k:(base/v).resolve() for k,v in cfg["dataset"].items()}
    paths={"config.yaml":config_path.resolve(), **data,"prompt":(base/cfg["prompt"]).resolve(),"ontology":(base/cfg["ontology"]).resolve()}
    live=cfg.get("live",{})
    if isinstance(live,dict) and live.get("equivalence_manifest"):
        paths["environment_equivalence"]=(base/live["equivalence_manifest"]).resolve()
    return paths

def _validate_case_population(dev: list[dict[str,Any]], held: list[dict[str,Any]]) -> None:
    if len(dev)!=24 or len(held)!=120:
        raise ValueError(f"expected 24 development and 120 held-out cases; found {len(dev)} and {len(held)}")
    dev_scenarios={c.get("scenario_id") for c in dev}
    held_scenarios={c.get("scenario_id") for c in held}
    shared=dev_scenarios & held_scenarios
    if shared:
        raise ValueError("development and held-out scenarios overlap")
    if len(dev_scenarios)!=6 or len(held_scenarios)!=30:
        raise ValueError(f"expected 6 development and 30 held-out scenarios; found {len(dev_scenarios)} and {len(held_scenarios)}")
    counts=Counter(c.get("category") for c in held)
    if dict(counts)!=HELDOUT_CATEGORY_COUNTS:
        raise ValueError(f"held-out category counts differ from protocol: {dict(counts)}")
    for name,cases in (("development",dev),("held-out",held)):
        for scenario in (dev_scenarios if name=="development" else held_scenarios):
            scenario_counts=Counter(c.get("category") for c in cases if c.get("scenario_id")==scenario)
            if dict(scenario_counts)!={category:1 for category in HELDOUT_CATEGORY_COUNTS}:
                raise ValueError(f"{name} scenario {scenario!r} must contain one case per category")

def preflight(cfg, config_path: Path=DEFAULT_CONFIG, manifest_override: Path|None=None, freeze=False):
    files=_paths(cfg,config_path); manifest=(manifest_override or (config_path.resolve().parent/cfg["manifest"])).resolve()
    for name,p in files.items():
        if not p.is_file(): raise ValueError(f"required frozen input missing: {name} ({p})")
    dev=_read_jsonl(files["development_cases"]); held=_read_jsonl(files["heldout_cases"]); gold=_read_jsonl(files["gold"]); corpus=_read_jsonl(files["corpus"])
    source_root=(config_path.resolve().parent/"dataset/sources").resolve(); source_files={}
    source_by_id={}
    for rec in corpus:
        sid=rec.get("source_id")
        if not isinstance(sid,str) or sid in source_by_id: raise ValueError("corpus source_id missing or duplicated")
        rel=Path(sid)
        if rel.is_absolute() or ".." in rel.parts: raise ValueError(f"unsafe corpus source path: {sid}")
        src=(source_root/rel).resolve()
        if source_root not in src.parents or not src.is_file(): raise ValueError(f"source document missing: {sid}")
        raw=src.read_text(encoding="utf-8")
        if raw!=rec.get("text"): raise ValueError(f"corpus text differs from source document: {sid}")
        source_files["source:"+sid]=src; source_by_id[sid]=rec
        if not all(rec.get(k) for k in ("scenario_id","version","evidence_id")): raise ValueError(f"corpus metadata incomplete: {sid}")
    harness_root=config_path.resolve().parent
    source_code={}
    product_root=harness_root
    for src in product_root.rglob("*.py"):
        if "__pycache__" in src.parts: continue
        if "tests" in src.relative_to(product_root).parts: continue
        source_code["harness_source:"+src.relative_to(harness_root).as_posix()]=src
    for rel in ("charter.md","review/pilot-protocol.md","review/review-template.json"):
        candidate=product_root/rel
        if candidate.is_file(): source_code["protocol:"+rel]=candidate
    actual={name:_digest(path) for name,path in {**files,**source_files,**source_code}.items()}
    if freeze: manifest.write_text(json.dumps({"schema_version":1,"sha256":actual},indent=2)+"\n",encoding="utf-8")
    elif not manifest.is_file(): raise ValueError("frozen manifest missing; run preflight --freeze after review")
    else:
        lock=json.loads(manifest.read_text(encoding="utf-8")).get("sha256",{}); bad=[k for k,v in actual.items() if lock.get(k)!=v]
        extra=set(lock)-set(actual)
        if bad or extra: raise ValueError("frozen hash mismatch: "+", ".join(bad+sorted(extra)))
    _validate_case_population(dev,held)
    allcases=dev+held; ids=[c.get("id") for c in allcases]; goldids=[g.get("case_id") for g in gold]
    if None in ids or len(set(ids))!=len(ids) or len(set(goldids))!=len(goldids) or set(ids)!=set(goldids): raise ValueError("cases/gold identifiers mismatch or duplicate")
    case_by_id={c["id"]:c for c in allcases}; cats={}
    for c in allcases:
        if not all(k in c for k in ("id","scenario_id","category","question","tags","source_ids")): raise ValueError(f"case contract invalid: {c.get('id')}")
        if not c["source_ids"] or any(s not in source_by_id for s in c["source_ids"]): raise ValueError(f"case source references invalid: {c['id']}")
        if any(source_by_id[s]["scenario_id"]!=c["scenario_id"] for s in c["source_ids"]): raise ValueError(f"cross-scenario source reference: {c['id']}")
        cats[c["category"]]=cats.get(c["category"],0)+1
    for g in gold:
        exp=g.get("expected",{})
        if set(exp)!={"required_claims","supporting_sources","must_not_claim","known_unknowns"}: raise ValueError(f"gold expected fields invalid: {g.get('case_id')}")
        c=case_by_id[g["case_id"]]
        if any(not isinstance(exp[k],list) for k in exp): raise ValueError(f"gold expected fields must be lists: {g['case_id']}")
        if not set(exp["supporting_sources"])<=set(c["source_ids"]): raise ValueError(f"gold source outside case evidence: {g['case_id']}")
        if not isinstance(g.get("evidence"),list) or any(e.get("source_id") not in c["source_ids"] or e.get("version")!=source_by_id[e["source_id"]]["version"] or e.get("evidence_id")!=source_by_id[e["source_id"]]["evidence_id"] for e in g["evidence"]): raise ValueError(f"gold evidence references invalid: {g['case_id']}")
    heldids={c["id"] for c in held}; review={s:sum(g.get("review_status")==s for g in gold if g["case_id"] in heldids) for s in sorted({g.get("review_status","missing") for g in gold if g["case_id"] in heldids})}
    http_calls=(len(allcases)*len(cfg["arms"])*int(cfg["repeats"])*2)
    return {"status":"ok","development_cases":len(dev),"heldout_cases":len(held),"gold_cases":len(gold),"corpus_sources":len(corpus),"category_counts":cats,"hashes":actual,"manifest":str(manifest),"frozen":True,"heldout_review_status":review,"planned_live_http_calls_all_cases":http_calls,"max_live_http_calls":cfg["budget"].get("max_requests"),"live_cost_pricing_configured":bool(cfg["budget"].get("input_usd_per_million",0) and cfg["budget"].get("output_usd_per_million",0))}

def _load(cfg, config_path):
    paths=_paths(cfg,config_path)
    return paths,_read_jsonl(paths["gold"]),_read_jsonl(paths["corpus"]),_read_jsonl(paths["development_cases"]),_read_jsonl(paths["heldout_cases"])
def _new_journal(cfg,config_path,split,adapter,cases,pf,corpus):
    outdir=(config_path.resolve().parent/cfg["runs_dir"]).resolve(); _private_directory(outdir,secure_contents=True)
    run_id=uuid.uuid4().hex; path=outdir/f"{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}-{run_id[:8]}-{split}-{adapter}.jsonl"
    hashes=pf["hashes"]; source_hash={k.removeprefix("source:"):v for k,v in hashes.items() if k.startswith("source:")}; by_source={x["source_id"]:x for x in corpus}
    slots={}; total=len(cases)*len(cfg["arms"])*int(cfg["repeats"])
    for repeat in range(1,int(cfg["repeats"])+1):
        for case in cases:
            visible=[by_source[s] for s in case["source_ids"]]
            for arm in cfg["arms"]:
                key=(case["id"],arm,repeat); rid=hashlib.sha256(f"{run_id}:{key}".encode()).hexdigest()[:16]
                slots[key]={"case_id":case["id"],"scenario_id":case["scenario_id"],"category":case["category"],"arm":arm,"repeat":repeat,"run_id":run_id,"run_split":split,"expected_slots":total,"response_id":rid,"visible_source_ids":[x["source_id"] for x in visible],"input_hashes":hashes,"frozen_corpus_source_hashes":{x["source_id"]:source_hash[x["source_id"]] for x in visible},"status":"not_started","adapter":adapter,"error_type":"NotStarted","error_message":"Execution not yet attempted"}
    return path,run_id,slots

def _flush_journal(path,slots):
    _atomic_private_write(path,"".join(json.dumps(row,ensure_ascii=False)+"\n" for row in slots.values()),private_parent=True)

def _write_run(cfg,config_path,split,adapter,rows):
    outdir=(config_path.resolve().parent/cfg["runs_dir"]).resolve(); _private_directory(outdir,secure_contents=True)
    path=outdir/f"{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}-{uuid.uuid4().hex[:8]}-{split}-{adapter}.jsonl"
    _atomic_private_write(path,"".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),private_parent=True)
    return path

def _schedule(cases,arms,repeats,seed):
    rng=random.Random(seed); schedule=[]
    for repeat in range(1,int(repeats)+1):
        order=list(cases); rng.shuffle(order)
        for case in order:
            arm_order=list(arms); rng.shuffle(arm_order)
            schedule.extend((case,arm,repeat) for arm in arm_order)
    return schedule

def run_fixture(args,cfg,config_path,manifest):
    pf=preflight(cfg,config_path,manifest); paths,_goldrows,corpus,dev,held=_load(cfg,config_path)
    cases=dev if args.split=="development" else held if args.split=="heldout" else dev+held
    fixture_path=Path(args.fixtures); fixture_path=fixture_path if fixture_path.is_absolute() else config_path.parent/fixture_path; fixture=FixtureAdapter(fixture_path.resolve())
    out,run_id,slots=_new_journal(cfg,config_path,args.split,"offline_fixture",cases,pf,corpus); _flush_journal(out,slots)
    for case,arm,repeat in _schedule(cases,cfg["arms"],cfg["repeats"],int(cfg["bootstrap"]["seed"])):
        row=slots[(case["id"],arm,repeat)]; row.update({"evidence_status":"FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE","tool_results":[],"timing":{"elapsed_seconds":0.0},"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":0.0}})
        try: row.update({"status":"completed","answer":fixture.answer(case,[x for x in corpus if x["source_id"] in set(case["source_ids"])],arm)["answer"]}); row.pop("error_type",None); row.pop("error_message",None)
        except Exception as e: row.update({"status":"failed","error_type":type(e).__name__,"error_message":str(e)[:240]})
        _flush_journal(out,slots)
    print(out); return 0

def run_live(args,cfg,config_path,manifest):
    pf=preflight(cfg,config_path,manifest); paths,goldrows,corpus,dev,held=_load(cfg,config_path)
    cases=dev if args.split=="development" else held if args.split=="heldout" else dev+held
    if args.split in ("heldout","all"):
        heldids={c["id"] for c in cases}; pending=[g["case_id"] for g in goldrows if g["case_id"] in heldids and g.get("review_status")!="approved"]
        if pending: raise ValueError(f"live heldout blocked: {len(pending)} gold labels lack independent approval")
    adapter=None; init_error=None
    try:
        if not args.approve_paid: raise RuntimeError("live execution requires --approve-paid")
        from .adapters.live import LiveAdapter
        live_cfg=copy.deepcopy(cfg); live_cfg["dataset"]["corpus"]=str(paths["corpus"])
        if live_cfg.get("live",{}).get("equivalence_manifest"): live_cfg["live"]["equivalence_manifest"]=str(paths["environment_equivalence"])
        adapter=LiveAdapter(live_cfg,paths["prompt"].read_text(encoding="utf-8"),approve_paid=True)
    except Exception as e: init_error=e
    out,run_id,slots=_new_journal(cfg,config_path,args.split,"live_lightrag",cases,pf,corpus); _flush_journal(out,slots)
    for case,arm,repeat in _schedule(cases,cfg["arms"],cfg["repeats"],int(cfg["bootstrap"]["seed"])):
        row=slots[(case["id"],arm,repeat)]; row.update({"evidence_status":"LIVE_NOT_YET_REVIEWED","tool_results":[],"timing":{},"evidence":[],"usage":{}})
        try:
            if init_error: raise init_error
            result=adapter.answer(case,[x for x in corpus if x["source_id"] in set(case["source_ids"])],arm)
            evidence=result.get("evidence",[])
            row.update({"status":"completed","answer":result.get("answer"),"evidence":evidence,"retrieved_evidence_sha256":hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest(),"tool_results":result.get("tool_calls",result.get("tool_results",[])),"usage":result.get("usage",{}),"timing":{"elapsed_seconds":result.get("elapsed_seconds"),"retrieval_elapsed_seconds":result.get("retrieval_elapsed_seconds")},"evidence_status":result.get("evidence_status","LIVE_NOT_YET_REVIEWED")})
            for key in ("budget_consumed",):
                if key in result: row[key]=result[key]
            row.pop("error_type",None); row.pop("error_message",None)
        except Exception as e:
            row.update({"status":"failed","error_type":type(e).__name__,"error_message":str(e)[:240]})
            trace=getattr(adapter,"last_attempt",None) if adapter else None
            if isinstance(trace,dict): row["attempt_trace"]=trace
        if adapter and hasattr(adapter,"budget_snapshot"): row["budget_consumed"]=adapter.budget_snapshot()
        _flush_journal(out,slots)
    print(out); return 0

def replay(args,cfg,config_path,manifest):
    pf=preflight(cfg,config_path,manifest); paths,goldrows,corpus,dev,held=_load(cfg,config_path)
    cases=dev if args.split=="development" else held if args.split=="heldout" else dev+held; case_by={x["id"]:x for x in cases}
    source_hash={k.removeprefix("source:"):v for k,v in pf["hashes"].items() if k.startswith("source:")}; raw=_read_jsonl(args.responses)
    expected={(c["id"],a,r) for c in cases for a in cfg["arms"] for r in range(1,int(cfg["repeats"])+1)}; indexed={}
    for x in raw:
        key=(x.get("case_id"),x.get("arm"),x.get("repeat"))
        if key not in expected or key in indexed: raise ValueError(f"unexpected or duplicate replay row: {key}")
        if x.get("status") not in {"completed","failed","timeout","aborted"}: raise ValueError(f"invalid replay status: {key}")
        indexed[key]=x
    outputs=[]; corpus_by={x["source_id"]:x for x in corpus}
    for case in cases:
        visible=[corpus_by[s] for s in case["source_ids"]]
        for rep in range(1,int(cfg["repeats"])+1):
            for arm in cfg["arms"]:
                key=(case["id"],arm,rep); x=indexed.get(key,{}); status=x.get("status","missing_response")
                response_id=x.get("response_id") or hashlib.sha256(f"replay:{key}".encode()).hexdigest()[:16]
                row={"case_id":case["id"],"scenario_id":case["scenario_id"],"category":case["category"],"arm":arm,"repeat":rep,"response_id":response_id,"visible_source_ids":[s["source_id"] for s in visible],"input_hashes":pf["hashes"],"frozen_corpus_source_hashes":{s["source_id"]:source_hash[s["source_id"]] for s in visible},"status":status,"adapter":"replay","evidence_status":"RECORDED_OUTPUT; retrieval/generation provenance must be attested separately","tool_results":x.get("tool_results",[]),"timing":x.get("timing",{}),"evidence":x.get("evidence",[]),"usage":x.get("usage",{})}
                if status=="completed": row["answer"]=x.get("answer")
                if status!="completed": row["error_type"]=x.get("error_type","MissingOrFailedReplay"); row["error_message"]=x.get("error_message","No completed response recorded")[:240]
                outputs.append(row)
    out=_write_run(cfg,config_path,args.split,"replay",outputs); print(out); return 0

def prepare_facts(output: Path,cfg,config_path,manifest):
    pf=preflight(cfg,config_path,manifest); paths,goldrows,corpus,dev,held=_load(cfg,config_path)
    # Refuse public/tracked destinations; preparation only writes an ignored private artifact.
    base=config_path.resolve().parent; out=output if output.is_absolute() else base/output; out=out.resolve()
    allowed={(base/".runs").resolve(),(base/"private").resolve()}
    if not any(root==out or root in out.parents for root in allowed): raise ValueError("prepare-facts output must be under ignored .runs/ or private/")
    if out.exists(): raise FileExistsError("refusing to overwrite prepared facts")
    from .canonical import prepare_candidates, validate_provenance
    facts=prepare_candidates(corpus)
    validate_provenance(facts,corpus)
    artifact={"schema_version":1,"source_hashes":{k:v for k,v in pf["hashes"].items() if k.startswith("source:")},"candidate_fact_count":len(facts),"acceptance_required":True,"written_to_lightrag":False,"facts":facts}
    _private_directory(out.parent,secure_contents=True)
    _atomic_private_write(out,json.dumps(artifact,ensure_ascii=False,indent=2)+"\n",private_parent=True)
    print(json.dumps({"output":str(out),"candidate_fact_count":len(facts),"written_to_lightrag":False,"acceptance_required":True})); return 0

def _scores(results, goldrows, reviews=None):
    gold={x["case_id"]:x for x in goldrows}; return score_rows(results,gold,reviews or [])
def main(argv=None):
    p=argparse.ArgumentParser(prog="ogr-product-validation"); p.add_argument("--config",type=Path,default=DEFAULT_CONFIG); p.add_argument("--manifest",type=Path,dest="manifest_global")
    sub=p.add_subparsers(dest="cmd",required=True)
    pr=sub.add_parser("preflight"); pr.add_argument("--freeze",action="store_true"); pr.add_argument("--manifest",type=Path,dest="manifest_cmd")
    pp=sub.add_parser("prepare"); pp.add_argument("source",type=Path); pp.add_argument("destination",type=Path); pp.add_argument("--manifest",type=Path,dest="manifest_cmd")
    pfacts=sub.add_parser("prepare-facts"); pfacts.add_argument("--output",type=Path,required=True); pfacts.add_argument("--manifest",type=Path,dest="manifest_cmd")
    rn=sub.add_parser("run"); rn.add_argument("--adapter",choices=["fixture","live"],default="fixture"); rn.add_argument("--approve-paid",action="store_true"); rn.add_argument("--fixtures",default=str(DEFAULT_CONFIG.parent/"fixtures.jsonl")); rn.add_argument("--split",choices=["development","heldout","all"],default="development"); rn.add_argument("--manifest",type=Path,dest="manifest_cmd")
    rp=sub.add_parser("replay"); rp.add_argument("responses",type=Path); rp.add_argument("--split",choices=["development","heldout","all"],default="development"); rp.add_argument("--manifest",type=Path,dest="manifest_cmd")
    for name in ("score","report"):
        sc=sub.add_parser(name); sc.add_argument("results",type=Path); sc.add_argument("--reviews",type=Path); sc.add_argument("--output",type=Path); sc.add_argument("--manifest",type=Path,dest="manifest_cmd")
    a=p.parse_args(argv); config_path=a.config.resolve(); manifest=getattr(a,"manifest_cmd",None) or getattr(a,"manifest_global",None); cfg=json.loads(config_path.read_text(encoding="utf-8"))
    try:
        if a.cmd=="preflight": print(json.dumps(preflight(cfg,config_path,manifest,a.freeze),indent=2)); return 0
        if a.cmd=="prepare":
            preflight(cfg,config_path,manifest); print(json.dumps(prepare_snapshot(a.source,a.destination),indent=2)); return 0
        if a.cmd=="prepare-facts": return prepare_facts(a.output,cfg,config_path,manifest)
        if a.cmd=="run":
            if a.adapter=="fixture": return run_fixture(a,cfg,config_path,manifest)
            return run_live(a,cfg,config_path,manifest)
        if a.cmd=="replay": return replay(a,cfg,config_path,manifest)
        preflight(cfg,config_path,manifest); results=_read_jsonl(a.results); reviews=_read_jsonl(a.reviews) if a.reviews else []
        scored=_scores(results,_read_jsonl(_paths(cfg,config_path)["gold"]),reviews)
        if a.cmd=="score": output=scored
        else:
            agg=aggregate(scored,cfg["arms"],cfg["bootstrap"]["samples"],cfg["bootstrap"]["seed"])
            output={"scoring":scored,"aggregation":agg,"human_review_required":True,"offline_fixture_warning":"Fixture rows are implementation smoke only, explicitly not LightRAG evidence.","decision":"GTS remains pending until independent semantic reviews cover all rows; mechanical diagnostics do not establish groundedness."}
        text=json.dumps(output,indent=2)
        if a.output: _atomic_private_write(a.output,text+"\n")
        else: print(text)
        return 0
    except Exception as e: print(f"error: {e}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
