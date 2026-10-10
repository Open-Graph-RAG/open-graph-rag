from __future__ import annotations
from collections import defaultdict
from typing import Any

REVIEW_FIELDS=("factual_correctness","citations_semantically_supported","state_correct","no_invented_ownership_or_authorization","uncertainty_correct")
def _review_consensus(reviews: list[dict[str,Any]], response_id: str, required: list[str]):
    group=[x for x in reviews if (x.get("blind_response_id") or x.get("response_id"))==response_id]
    peers=[x for x in group if x.get("reviewer_id") and all(isinstance(x.get(k),bool) for k in required)]
    peer_ids={x["reviewer_id"] for x in peers}
    if len(peer_ids)<2: return None, len(peer_ids), "requires_two_independent_reviewers"
    if len(peer_ids)!=len(peers): raise ValueError(f"duplicate reviewer on response {response_id}")
    values=[{k:x[k] for k in required} for x in peers]
    if all(v==values[0] for v in values[1:]): return values[0],len(peer_ids),"independent_reviewers_agree"
    reviewer_ids={x["reviewer_id"] for x in group if x.get("reviewer_id")}
    adjudicators=[x for x in group if x.get("adjudicator_id") and x["adjudicator_id"] not in reviewer_ids and isinstance(x.get("adjudication"),dict) and all(isinstance(x["adjudication"].get(k),bool) for k in required)]
    if len(adjudicators)!=1: return None,len(peer_ids),"disagreement_requires_single_adjudication"
    return {k:adjudicators[0]["adjudication"][k] for k in required},len(peer_ids),"adjudicated_disagreement"

def _evidence_records(value, visible_source_ids, corpus_by_id):
    """Resolve ranked LightRAG chunks to frozen source-level evidence only."""
    if not isinstance(value,dict) or not isinstance(value.get("chunks"),list):
        return []
    visible=set(visible_source_ids or [])
    records=[]
    for chunk in value["chunks"]:
        if not isinstance(chunk,dict):
            continue
        sid=chunk.get("source_id") or chunk.get("document_id") or chunk.get("doc_id")
        if sid not in visible:
            file_path=chunk.get("file_path")
            if not isinstance(file_path,str):
                continue
            normalized=file_path.replace("\\","/").rstrip("/")
            matches=[source_id for source_id in visible if normalized==source_id or normalized.endswith("/"+source_id)]
            if len(matches)!=1:
                continue
            sid=matches[0]
        source=corpus_by_id.get(sid)
        if source is None:
            continue
        records.append({"source_id":sid,"version":chunk.get("version") or chunk.get("source_version") or source.get("version"),
                        "evidence_id":chunk.get("evidence_id") or source.get("evidence_id")})
    return records

def score_rows(rows: list[dict[str,Any]], gold: dict[str,dict[str,Any]], reviews: list[dict[str,Any]]|None=None, corpus: list[dict[str,Any]]|None=None) -> dict[str,Any]:
    """Mechanical diagnostics plus consensus human judgments; no lexical groundedness."""
    reviews=reviews or []; out=[]
    corpus_by_id={source.get("source_id"):source for source in (corpus or []) if isinstance(source,dict) and source.get("source_id")}
    for r in rows:
        g=gold.get(r.get("case_id"),{}); exp=g.get("expected",{}); answer=r.get("answer") if r.get("status")=="completed" else None
        cited=answer.get("citations",answer.get("supporting_sources",[])) if isinstance(answer,dict) else []
        citations=[x if isinstance(x,str) else x.get("source_id") for x in cited if isinstance(x,(str,dict))] if isinstance(cited,list) else []
        relevant=set(exp.get("supporting_sources",[])); valid=set(r.get("visible_source_ids",[]))
        applicable=list(REVIEW_FIELDS)+(["conflict_recall"] if r.get("category")=="conflicts" else [])
        consensus,reviewers,review_status=_review_consensus(reviews,r.get("response_id"),applicable) if r.get("response_id") else (None,0,"missing_response_id")
        retrieved=_evidence_records(r.get("evidence",{}),r.get("visible_source_ids",[]),corpus_by_id); retrieved_ids=list(dict.fromkeys(x["source_id"] for x in retrieved)); expected_evidence=exp.get("supporting_sources",[]); hit=set(retrieved_ids)&set(expected_evidence)
        gold_pairs={(x.get("source_id"),x.get("version"),x.get("evidence_id")) for x in g.get("evidence",[])}
        got_pairs={(x.get("source_id"),x.get("version"),x.get("evidence_id")) for x in retrieved}
        version_required=len(gold_pairs); version_hits=len(gold_pairs&got_pairs)
        rank=next((i+1 for i,sid in enumerate(retrieved_ids) if sid in set(expected_evidence)),None)
        complete=consensus is not None
        gts=all(consensus.values()) if complete and r.get("status")=="completed" else False if complete else None
        out.append({"response_id":r.get("response_id"),"case_id":r.get("case_id"),"scenario_id":r.get("scenario_id"),"category":r.get("category"),"arm":r.get("arm"),"repeat":r.get("repeat"),"status":r.get("status"),"completed":r.get("status")=="completed","citation_coverage_diagnostic":len(set(citations)&relevant)/len(relevant) if relevant else None,"citation_validity_diagnostic":len(set(citations)&valid)/len(set(citations)) if citations else None,"retrieval_source_ids":retrieved_ids if r.get("evidence_status")!="FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE" else None,"retrieval_evidence_recall":len(hit)/len(set(expected_evidence)) if expected_evidence and r.get("evidence_status")!="FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE" else None,"retrieval_evidence_precision":len(hit)/len(set(retrieved_ids)) if retrieved_ids and r.get("evidence_status")!="FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE" else None,"retrieval_reciprocal_rank":(1/rank if rank and r.get("evidence_status")!="FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE" else None),"source_version_resolution_rate":version_hits/version_required if version_required and r.get("evidence_status")!="FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE" else None,"source_version_resolution":{ "resolved":version_hits,"required":version_required},"human_review":consensus,"human_review_status":review_status,"independent_reviewer_count":reviewers,"human_review_complete":complete,"grounded_task_success":gts,"human_review_fields_required":applicable})
    categories={}
    for cat in sorted({x.get("category") for x in out}):
        group=[x for x in out if x.get("category")==cat]; categories[cat]={"denominator":len(group),"completion_rate":sum(x["completed"] for x in group)/len(group) if group else None,"human_review_complete":sum(x["human_review_complete"] for x in group),"grounded_task_success_rate_conservative_unreviewed_as_failure":sum(bool(x["grounded_task_success"]) for x in group)/len(group) if group else None}
        fields=sorted({k for x in group if x["human_review"] for k in x["human_review"]})
        categories[cat]["human_review_metric_rates"]={k:{"successes":sum(bool(x["human_review"][k]) for x in group if x["human_review"] and k in x["human_review"]),"denominator":sum(1 for x in group if x["human_review"] and k in x["human_review"]),"rate":(sum(bool(x["human_review"][k]) for x in group if x["human_review"] and k in x["human_review"])/sum(1 for x in group if x["human_review"] and k in x["human_review"])) if any(x["human_review"] and k in x["human_review"] for x in group) else None} for k in fields}
    return {"rows":out,"denominator":len(rows),"completed":sum(x["completed"] for x in out),"failed_in_denominator":sum(not x["completed"] for x in out),"human_review_complete":sum(x["human_review_complete"] for x in out),"grounded_task_success_pending_review":any(not x["human_review_complete"] for x in out),"grounded_task_success_rate_conservative_unreviewed_as_failure":sum(bool(x["grounded_task_success"]) for x in out)/len(out) if out else None,"category_metrics":categories,"semantic_groundedness":"human consensus/adjudication only; automated diagnostics are not semantic evidence"}
