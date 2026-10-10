#!/usr/bin/env python3
"""Phase-1, direct benchmark for pinned Kev-0.8B; outputs outside the repo."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

EXPECTED_SOURCE = "5e42a7a03f28134853dd3ff77461457e921e5ec1"
EXPECTED_KEV = "9a45d25eb2ab761841196625383fa1dff0e56c1e"
EXPECTED_BASE = "dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68"
RSS_BUDGET_KIB = 6 * 1024 * 1024


def tree_rss_kib(root_pid: int) -> int:
    parents, rss = {}, {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            pid = int(entry.name)
            parents[pid] = int(fields[1])
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss[pid] = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            continue
    members, changed = {root_pid}, True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in members and pid not in members:
                members.add(pid)
                changed = True
    return sum(rss.get(pid, 0) for pid in members)


class RssSampler:
    def __init__(self):
        self.samples = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stop_event.is_set():
            self.samples.append((time.monotonic(), tree_rss_kib(os.getpid())))
            self.stop_event.wait(0.05)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    @property
    def peak_kib(self):
        return max((value for _, value in self.samples), default=0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(roots):
    result = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and ".cache" not in path.parts:
                result.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
    return result


def p95(values):
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def nvidia_smi(query):
    try:
        completed = subprocess.run(["nvidia-smi", *query], check=True, capture_output=True, text=True, timeout=5)
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"


def pad_to_tokens(text, count, tokenizer, user_tokens):
    while len(user_tokens(tokenizer, text)) < count:
        text += " Additional documented evidence remains in the same product workflow scope."
    return text


def make_request(state, index, short):
    if short:
        return {"state": state, "questions": {"classification": {
            "type": "choice", "instructions": f"Context {index}: classify these claims using evidence only.",
            "criteria": {"conflict": "Incompatible behavior in the same scope and time.",
                         "compatible": "Different qualifiers that can both hold.",
                         "insufficient": "Evidence cannot establish their relation."}}}}
    detail = "Assess explicit source evidence within the same actor, workflow, status, version, and effective time."
    criteria = {f"option_{i}": f"Candidate {i}. {detail}" for i in range(8)}
    return {"state": state, "questions": {
        "decision": {"type": "choice", "instructions": f"Context {index}: select the supported outcome. {detail}", "criteria": criteria},
        "sufficient": {"type": "noul", "instructions": f"Context {index}: does evidence address scope and timing? {detail}", "criteria": {"true": detail, "false": detail}},
        "support": {"type": "score", "instructions": f"Context {index}: rate direct support with ordered levels. {detail}", "criteria": [f"Level {i}: support degree {i}. {detail}" for i in range(8)]},
        "secondary": {"type": "choice", "instructions": f"Context {index}: assess the alternative outcome. {detail}", "criteria": dict(criteria)},
    }}


def encode_request(request, tokenizer, model, SystemOneRequest, to_record):
    record, _ = to_record(SystemOneRequest.model_validate(request))
    started = time.perf_counter()
    encoded = model.encode(tokenizer, record, max_state=4096, max_branch=5120, strict=True)
    return encoded, (time.perf_counter() - started) * 1000


def fill_branch_budget(request, tokenizer, model, SystemOneRequest, to_record):
    # Grow one option description at a time so the aggregate branch payload approaches 920 tokens.
    candidates = []
    for question in request["questions"].values():
        criteria = question.get("criteria")
        if isinstance(criteria, dict):
            candidates.extend((criteria, key) for key, value in criteria.items() if isinstance(value, str))
        elif isinstance(criteria, list):
            candidates.extend((criteria, index) for index, value in enumerate(criteria) if isinstance(value, str))
    filler, cursor = " Scope and timing must match the stated source passage.", 0
    while True:
        encoded, _ = encode_request(request, tokenizer, model, SystemOneRequest, to_record)
        branch_tokens = len(encoded["ids"]) - encoded["state_tokens"]
        if branch_tokens >= 920:
            if branch_tokens > 1024:
                raise ValueError(f"aggregate branch size {branch_tokens} exceeds 1,024")
            return request
        container, key = candidates[cursor % len(candidates)]
        container[key] += filler
        cursor += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--idle-bridge-rss-kib", type=int, required=True,
                        help="full idle lightweight MCP process-tree RSS measured from the same host")
    parser.add_argument("--load-only", action="store_true", help="measure a fresh model load and stop before inference")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    source = Path(os.environ["KEV_SOURCE"]).resolve()
    checkpoint_path = Path(os.environ["KEV_CHECKPOINT"]).resolve()
    hf_home = Path(os.environ["HF_HOME"]).resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != EXPECTED_SOURCE:
        raise RuntimeError(f"Kev source HEAD {head} != required {EXPECTED_SOURCE}")
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip()
    if dirty:
        raise RuntimeError(f"Pinned Kev source checkout has local modifications: {dirty}")
    base_snapshot = hf_home / "hub" / f"models--Qwen--Qwen3.5-0.8B-Base" / "snapshots" / EXPECTED_BASE
    expected_checkpoint_path = hf_home / "hub" / "models--jaredpalmer--kev-0.8b" / "snapshots" / EXPECTED_KEV
    if not base_snapshot.is_dir():
        raise RuntimeError(f"Pinned base cache snapshot missing: {base_snapshot}")
    if checkpoint_path != expected_checkpoint_path.resolve() or not checkpoint_path.is_dir():
        raise RuntimeError(f"Checkpoint must resolve to pinned Hub snapshot {expected_checkpoint_path}, got {checkpoint_path}")

    rss = RssSampler()
    rss.start()
    process_idle_kib = tree_rss_kib(os.getpid())
    sys.path.insert(0, str(source))
    import torch
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but PyTorch cannot access a CUDA device")
    if args.device == "cuda":
        torch.cuda.set_device(0)
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    import transformers
    from kev.api import SystemOneRequest, to_record
    from kev.checkpoint import Checkpoint, LoadOptions
    from kev.model import user_tokens

    checkpoint = Checkpoint(str(checkpoint_path))
    if checkpoint.meta.base_revision != EXPECTED_BASE:
        raise RuntimeError(f"Checkpoint base pin {checkpoint.meta.base_revision} != required {EXPECTED_BASE}")
    if round(checkpoint.meta.temperature, 2) != 2.35:
        raise RuntimeError(f"Checkpoint temperature {checkpoint.meta.temperature} != documented 2.35 (rounded)")
    lock_path = source / "uv.lock"
    packages = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in importlib.metadata.distributions()
                      if dist.metadata.get("Name"))
    files = manifest([checkpoint_path, base_snapshot])
    report = {
        "schema_version": 1,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "benchmark_script": {"path": str(Path(__file__).resolve()),
                             "sha256": sha256(Path(__file__).resolve())},
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "cpu": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                              if line.startswith("model name")), platform.processor()), "logical_cpus": os.cpu_count()},
        "pins": {"kev_model": f"jaredpalmer/kev-0.8b@{EXPECTED_KEV}",
                 "qwen_base": f"Qwen/Qwen3.5-0.8B-Base@{EXPECTED_BASE}",
                 "kev_source_commit": head, "source_path": str(source),
                 "uv_lock_sha256": sha256(lock_path), "uv_lock_path": str(lock_path),
                 "checkpoint_path": str(checkpoint_path), "hf_home": str(hf_home)},
        "artifact_files": files,
        "installed_packages": packages,
        "runtime": {"device": args.device, "torch": torch.__version__, "torch_cuda_runtime": torch.version.cuda,
                    "transformers": transformers.__version__,
                    "intraop_threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
                    "dtype": "fp32", "temperature": checkpoint.meta.temperature,
                    "attention_setting": "attn=None; Transformers automatic attention selection",
                    "adapter_loading": "direct Checkpoint.load; LoRA merged into base (merge=True)",
                    "date_preprocessing": False, "cross_request_prefix_cache": False,
                    "inference": "DecisionModel.probs after strict model.encode; no kev.serve.Server"},
        "baseline": {"idle_lightweight_bridge_process_tree_rss_kib": args.idle_bridge_rss_kib,
                     "benchmark_process_idle_rss_kib": process_idle_kib,
                     "rss_sampling_interval_ms": 50, "budget_additional_kib": RSS_BUDGET_KIB,
                     "budget_additional_gib": 6},
        "cold_load": {"status": "pending"}, "warmups": {}, "workloads": {}, "result": "pending",
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    gpu_preload = None
    if args.device == "cuda":
        free_vram, total_vram = torch.cuda.mem_get_info(0)
        props = torch.cuda.get_device_properties(0)
        gpu_preload = {"name": torch.cuda.get_device_name(0),
                       "compute_capability": f"{props.major}.{props.minor}",
                       "total_vram_bytes": total_vram, "free_vram_before_load_bytes": free_vram,
                       "nvidia_smi_gpu": nvidia_smi(["--query-gpu=name,driver_version,memory.total,memory.free,uuid", "--format=csv,noheader"]),
                       "nvidia_smi_processes_before_load": nvidia_smi(["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])}
    started = time.perf_counter()
    try:
        tokenizer, model = checkpoint.load(args.device, LoadOptions(dtype=torch.float32, backend="torch", temperature=None,
            merge=True, attn=None, cuda_graphs=False, fused=False))
        if args.device == "cuda":
            torch.cuda.synchronize()
    except BaseException as exc:
        rss.stop()
        report["cold_load"] = {"status": "failed", "seconds": time.perf_counter() - started,
                               "error": f"{type(exc).__name__}: {exc}"}
        report["rss"] = {"process_tree_peak_kib": rss.peak_kib,
                         "additional_over_idle_bridge_kib": rss.peak_kib - args.idle_bridge_rss_kib}
        report["result"] = "BLOCKED: model loading failed"
        save()
        raise
    cold_load_s = time.perf_counter() - started
    model.eval()
    report["cold_load"] = {"status": "complete", "seconds": cold_load_s,
                            "adapter_merge": True, "temperature": float(model.head.temperature)}
    report["runtime"].update({"resolved_attention": getattr(model.lm.config, "_attn_implementation", None),
                              "tokenizer_class": type(tokenizer).__name__, "tokenizer_name": tokenizer.name_or_path,
                              "hybrid_base": bool(model.hybrid)})
    if args.device == "cuda":
        free_vram, _ = torch.cuda.mem_get_info(0)
        report["gpu_memory"] = {**gpu_preload, "free_vram_after_load_bytes": free_vram,
                                "nvidia_smi_processes_after_load": nvidia_smi(["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"]),
                                "load_peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
                                "load_peak_reserved_bytes": torch.cuda.max_memory_reserved(0)}
        torch.cuda.reset_peak_memory_stats(0)
    load_extra = rss.peak_kib - args.idle_bridge_rss_kib
    report["rss_during_load"] = {"process_tree_peak_kib": rss.peak_kib,
                                  "additional_over_idle_bridge_kib": load_extra}
    if load_extra > RSS_BUDGET_KIB:
        rss.stop()
        report["result"] = "FAIL: model loading exceeded the 6 GiB RSS budget; warm workloads stopped"
        save()
        print(json.dumps(report, indent=2))
        return 2
    if args.load_only:
        rss.stop()
        report["rss"] = {"process_tree_peak_kib": rss.peak_kib,
                         "idle_bridge_process_tree_rss_kib": args.idle_bridge_rss_kib,
                         "additional_peak_over_idle_bridge_kib": rss.peak_kib - args.idle_bridge_rss_kib,
                         "sampling_interval_ms": 50}
        report["result"] = "LOAD_GATE_PASS; inference workloads not run"
        save()
        print(json.dumps(report, indent=2))
        return 0
    save()

    def score(request):
        encoded, preparation_ms = encode_request(request, tokenizer, model, SystemOneRequest, to_record)
        branches = len(encoded["ids"]) - encoded["state_tokens"]
        packed = len(encoded["ids"])
        if branches > 1024:
            raise ValueError(f"aggregate question branches {branches} > 1,024")
        if packed > 8192:
            raise ValueError(f"final encoded sequence {packed} > Kev validated context 8,192")
        if args.device == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.inference_mode():
            distributions = model.probs(encoded)
        if args.device == "cuda":
            torch.cuda.synchronize()
        inference_ms = (time.perf_counter() - start) * 1000
        for distribution in distributions:
            values = distribution.tolist()
            if not all(math.isfinite(x) and 0 <= x <= 1 for x in values) or abs(sum(values) - 1) > 1e-5:
                raise RuntimeError("Kev returned an invalid probability distribution")
        return {"inference_ms": inference_ms, "prepare_ms": preparation_ms,
                "state_tokens": encoded["state_tokens"], "branch_tokens": branches, "packed_tokens": packed}

    try:
        warmups = []
        for i in range(3):
            state = pad_to_tokens(f"Warmup {i}. Passage A: A customer plan changes after approval. Passage B: A draft flow applies changes immediately.", 500, tokenizer, user_tokens)
            warmups.append(score(make_request(state, i, True)))
        report["warmups"] = {"count": len(warmups), "inference_ms": [row["inference_ms"] for row in warmups]}
        save()
        short = []
        for i in range(30):
            a = pad_to_tokens(f"Short context {i}. Passage A: A customer change applies after approval.", 250, tokenizer, user_tokens)
            b = pad_to_tokens(f"Short context {i}. Passage B: A draft flow applies the change on confirmation.", 250, tokenizer, user_tokens)
            short.append(score(make_request(a + "\n\n" + b, i, True)))
        short_p95 = p95([row["inference_ms"] for row in short])
        short_peak_extra = rss.peak_kib - args.idle_bridge_rss_kib
        report["workloads"] = {"short": {"count": len(short), "p95_inference_ms": short_p95,
                                           "p95_prepare_ms": p95([row["prepare_ms"] for row in short]),
                                           "state_tokens": {"min": min(row["state_tokens"] for row in short), "max": max(row["state_tokens"] for row in short)},
                                           "branch_tokens": {"min": min(row["branch_tokens"] for row in short), "max": max(row["branch_tokens"] for row in short)}}}
        report["rss_after_short"] = {"process_tree_peak_kib": rss.peak_kib,
                                      "additional_over_idle_bridge_kib": short_peak_extra}
        save()
        if short_p95 > 10000 or short_peak_extra > RSS_BUDGET_KIB:
            report["result"] = "FAIL: short workload exceeded latency or RSS budget; near-limit workload stopped"
            save()
            rss.stop()
            print(json.dumps(report, indent=2))
            return 2
        near = []
        for i in range(20):
            a = pad_to_tokens(f"Near-limit context {i}. Passage A: An approved customer plan change becomes effective after signoff.", 1980, tokenizer, user_tokens)
            b = pad_to_tokens(f"Near-limit context {i}. Passage B: A draft customer workflow records when each change takes effect.", 1980, tokenizer, user_tokens)
            request = make_request(a + "\n\n" + b, i, False)
            request = fill_branch_budget(request, tokenizer, model, SystemOneRequest, to_record)
            near.append(score(request))
    except BaseException as exc:
        rss.stop()
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["rss"] = {"process_tree_peak_kib": rss.peak_kib,
                         "additional_over_idle_bridge_kib": rss.peak_kib - args.idle_bridge_rss_kib}
        report["result"] = "BLOCKED: workload failed"
        save()
        raise
    rss.stop()

    def stats(rows):
        latency = [row["inference_ms"] for row in rows]
        return {"count": len(rows), "p95_inference_ms": p95(latency), "median_inference_ms": sorted(latency)[len(latency)//2],
                "max_inference_ms": max(latency), "p95_prepare_ms": p95([row["prepare_ms"] for row in rows]),
                "state_tokens": {"min": min(row["state_tokens"] for row in rows), "max": max(row["state_tokens"] for row in rows)},
                "branch_tokens": {"min": min(row["branch_tokens"] for row in rows), "max": max(row["branch_tokens"] for row in rows)},
                "max_packed_tokens": max(row["packed_tokens"] for row in rows)}

    peak = rss.peak_kib
    extra = peak - args.idle_bridge_rss_kib
    report["rss"] = {"process_tree_peak_kib": peak, "idle_bridge_process_tree_rss_kib": args.idle_bridge_rss_kib,
                     "additional_peak_over_idle_bridge_kib": extra, "sampling_interval_ms": 50}
    report["warmups"] = {"count": len(warmups), "inference_ms": [row["inference_ms"] for row in warmups]}
    report["workloads"] = {"short": stats(short), "near_limit": stats(near)}
    if args.device == "cuda":
        inference_peak_allocated = torch.cuda.max_memory_allocated(0)
        inference_peak_reserved = torch.cuda.max_memory_reserved(0)
        report["gpu_memory"].update({"inference_peak_allocated_bytes": inference_peak_allocated,
                                     "inference_peak_reserved_bytes": inference_peak_reserved,
                                     "overall_peak_allocated_bytes": max(report["gpu_memory"]["load_peak_allocated_bytes"], inference_peak_allocated),
                                     "overall_peak_reserved_bytes": max(report["gpu_memory"]["load_peak_reserved_bytes"], inference_peak_reserved)})
    latency_pass = all(report["workloads"][name]["p95_inference_ms"] <= 10000 for name in ("short", "near_limit"))
    rss_pass = extra <= RSS_BUDGET_KIB
    report["result"] = {"latency_pass": latency_pass, "rss_pass_including_loading": rss_pass,
                        "phase1_pass": latency_pass and rss_pass}
    save()
    print(json.dumps(report, indent=2))
    return 0 if report["result"]["phase1_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
