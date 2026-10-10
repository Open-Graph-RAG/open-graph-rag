# Kev phase 1 feasibility result

**Decision: PASS on the tested host GPU for both phase 1 gates.** The pinned Kev 0.8B model completed the short and near-limit GPU workloads below the 10-second p95 inference limit and stayed within the 6 GiB additional host process-tree RSS limit. This result covers the direct adapter-loaded benchmark on the host GPU. It does not validate Docker GPU access or a later integration phase.

## Environment and method

The run used the host NVIDIA GeForce RTX 3070 Laptop GPU (8 GiB, compute capability 8.6), driver 580.178.04, CUDA runtime 12.8, PyTorch 2.8.0+cu128, Transformers 5.17.0, and Python 3.12.3. It used Kev source commit `5e42a7a03f28134853dd3ff77461457e921e5ec1`, model snapshot `jaredpalmer/kev-0.8b@9a45d25eb2ab761841196625383fa1dff0e56c1e`, and base snapshot `Qwen/Qwen3.5-0.8B-Base@dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68`. The source lock SHA256 is `a9922dbb89acdef78299fd2b4a8c3f7f0fa1b2bc08b55595b6926fa785a9c466`; the JSON report includes the complete installed-package inventory and artifact file hashes.

The benchmark used strict `model.encode`, direct `Checkpoint.load` with merged LoRA, fp32, the exact stored temperature `2.3510958125672174`, and resolved SDPA attention. It did not start Kev's server, apply date preprocessing, or use cross-request prefix caching. Timings use synchronized host-clock measurements around `model.probs`; p95 uses nearest-rank selection. RSS was sampled across the runner's process tree every 50 ms and compared with the measured 45,804 KiB idle bridge baseline.

## Results

| Measurement | Result | Gate |
|---|---:|---:|
| Cold model load | 5.01 s | Informational |
| Warm-up inference | 506 ms, 186 ms, 188 ms | Informational |
| Short inference (30/30) | p95 240 ms | ≤ 10,000 ms — pass |
| Near-limit inference (20/20) | p95 1,544 ms | ≤ 10,000 ms — pass |
| Additional process-tree RSS peak | 5,455,624 KiB (5.20 GiB) | ≤ 6 GiB — pass |
| CUDA allocator overall peak | 4.25 GiB allocated; 4.67 GiB reserved | Informational |

The 30 short cases used 518–520 state tokens and 50–51 branch tokens. The 20 near-limit cases used 3,980–3,982 state tokens and 924–928 branch tokens, with a maximum packed sequence of 4,910 tokens. `nvidia-smi` reported this benchmark's Python process at 138 MiB before checkpoint load and 3,274 MiB after load. The report records its PID/process name, GPU driver, CUDA runtime, free VRAM, and model allocator peaks.

## Scope and reproduction

Docker GPU execution remains untested because the available Desktop engine could not select a GPU driver and the native Docker socket was inaccessible. The GPU pass is a host-GPU feasibility result only.

The CPU diagnostic completed 3 warm-ups and all 30 short cases (short p95 6,617 ms, short-phase additional RSS 5,500,052 KiB). It was interrupted by the GPU steering during near-limit case preparation, before any near-limit CPU inference samples were completed. Its process-tree peak at interruption was 6,823,380 KiB above the idle baseline, exceeding the 6 GiB allowance; therefore there is no CPU pass claim. The GPU workload has its own complete passing result above.

To reproduce on a fresh host, install Python 3.12.3, `uv`, and an NVIDIA driver compatible with CUDA 12.8. Then fetch the exact source and model snapshots, sync the pinned source lock, and invoke the benchmark from the repository root:

```bash
uv --version

git clone https://github.com/jaredpalmer/kev.git /tmp/kev-feasibility/source
git -C /tmp/kev-feasibility/source checkout --detach 5e42a7a03f28134853dd3ff77461457e921e5ec1
uv sync --project /tmp/kev-feasibility/source --frozen --python 3.12.3
uv pip install --python /tmp/kev-feasibility/source/.venv/bin/python \
  --index-url https://download.pytorch.org/whl/cu128 \
  --extra-index-url https://pypi.org/simple 'torch==2.8.0+cu128'

HF_HOME=/tmp/kev-feasibility/hf /tmp/kev-feasibility/source/.venv/bin/hf download \
  jaredpalmer/kev-0.8b --revision 9a45d25eb2ab761841196625383fa1dff0e56c1e
HF_HOME=/tmp/kev-feasibility/hf /tmp/kev-feasibility/source/.venv/bin/hf download \
  Qwen/Qwen3.5-0.8B-Base --revision dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68

KEV_SOURCE=/tmp/kev-feasibility/source \
KEV_CHECKPOINT=/tmp/kev-feasibility/hf/hub/models--jaredpalmer--kev-0.8b/snapshots/9a45d25eb2ab761841196625383fa1dff0e56c1e \
HF_HOME=/tmp/kev-feasibility/hf \
/tmp/kev-feasibility/source/.venv/bin/python scripts/kev_feasibility.py \
  --device cuda --idle-bridge-rss-kib 45804 \
  --output /tmp/kev-feasibility/gpu-benchmark.json
```

The source checkout's `.python-version` requests Python 3.13, so the explicit `--python 3.12.3` selection reproduces this run's interpreter. The pinned `uv.lock` installs the source dependencies; the following explicit CUDA wheel install selects the tested `torch==2.8.0+cu128` build. The JSON records the interpreter, exact torch build, lock hash, and benchmark script SHA256. Compare these before treating another run as equivalent. The full machine-readable result, package inventory, pins, and artifact manifest are in [kev-feasibility-gpu.json](kev-feasibility-gpu.json). No decision-tool integration or phase 2 work is included in this report.
