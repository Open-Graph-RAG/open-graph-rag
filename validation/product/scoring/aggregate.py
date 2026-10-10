from __future__ import annotations
import random
from collections import defaultdict
from typing import Any

def aggregate(score: dict[str,Any], arms: list[str], bootstrap_samples: int=5000, seed: int=1) -> dict[str,Any]:
    # Collapse repeats within each case first; sample scenario clusters paired by arm.
    cases=defaultdict(lambda: defaultdict(list))
    for r in score["rows"]:
        cases[(r["scenario_id"],r["case_id"],r.get("category","unknown"))][r["arm"]].append(r)
    scenarios=defaultdict(lambda: defaultdict(list))
    for (sid,cid,cat),by_arm in cases.items():
        for arm,values in by_arm.items():
            completion=sum(float(r["completed"]) for r in values)/len(values)
            gts=sum(float(bool(r["grounded_task_success"])) for r in values)/len(values) # pending review conservatively zero
            scenarios[sid][arm].append((completion,gts,cat))
    metric_by_arm={}
    for arm in arms:
        allv=[x for vals in scenarios.values() for x in vals.get(arm,[])]
        metric_by_arm[arm]={"completion_rate":sum(x[0] for x in allv)/len(allv) if allv else None,"grounded_task_success_rate_conservative_unreviewed_as_failure":sum(x[1] for x in allv)/len(allv) if allv else None,"reviewed_response_count":sum(1 for r in score["rows"] if r["arm"]==arm and r.get("human_review_complete")),"denominator_case_repeats":sum(1 for r in score["rows"] if r["arm"]==arm)}
    complete=sorted(s for s in scenarios if len(arms)>=2 and all(scenarios[s].get(a) for a in arms))
    result={"scenario_count":len(scenarios),"paired_complete_scenario_count":len(complete),"bootstrap_unit":"scenario_id cluster; paired arms retained","repeat_handling":"repeats averaged within case; missing/unreviewed human judgments count as failure only in conservative GTS diagnostic","metrics_by_arm":metric_by_arm,"paired_candidate_minus_baseline":{}}
    if len(arms)<2: return result
    rng=random.Random(seed)
    for metric_index,name in ((0,"completion_rate"),(1,"grounded_task_success_rate_conservative_unreviewed_as_failure")):
        diffs=[]
        for sid in complete:
            left=scenarios[sid][arms[0]]; right=scenarios[sid][arms[1]]
            lv=sum(x[metric_index] for x in left)/len(left); rv=sum(x[metric_index] for x in right)/len(right); diffs.append(rv-lv)
        boot=[]
        for _ in range(max(0,bootstrap_samples)):
            if diffs: boot.append(sum(diffs[rng.randrange(len(diffs))] for _ in diffs)/len(diffs))
        boot.sort(); result["paired_candidate_minus_baseline"][name]={"point_estimate":sum(diffs)/len(diffs) if diffs else None,"cluster_bootstrap_95pct_interval":[boot[int(.025*(len(boot)-1))],boot[int(.975*(len(boot)-1))]] if boot else None}
    return result
