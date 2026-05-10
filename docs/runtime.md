
# Development, Test, and Validation Runtime Environment

## Hardware and Software

Teams are encouraged to develop as much as possible on their own hardware and environments. However, to provide a fair playing field for testing and validating team submissions, contest organizers have selected the following platform upon which team submissions are evaluated:

### AWS Instance m7a.2xlarge
- **8 vCPUs**: 4th Gen AMD EPYC Processors
- **32 GB RAM**

### AWS Vivado ML 2025.1 Developer AMI
- **Ubuntu 22.02 Operating System**
- **Pre-installed and licensed Vivado 2025.1**

This platform should comfortably run a sequential workflow without any memory pressure issues. A larger platform with more memory and more CPU cores was avoided to enable teams to focus on optimization innovation rather than brute force parallelized exploration.

At various stages of the contest, teams may be provided with AWS credit to enable them to validate with little or no cost on the contest validation platform. Successful alpha and beta submissions may enable teams to earn additional credits. More details to follow.

## LLM Access

If teams will be using LLMs for their workflow (most will), they will be required to use **OpenRouter** as demonstrated in the example `dcp_optimizer.py` agent. OpenRouter is a unified API interface for a wide range of LLMs. It provides a unified way to switch between many different LLMs using a single API.

Teams may also be provided with OpenRouter credit ahead of submission checkpoints to help develop and validate their solutions. More details to follow.

**Submissions must read from the environment variable `OPENROUTER_API_KEY` for the access key and use the OpenRouter API to access the models. No other LLM service will be supported.**

# Alpha Submission Guidelines

## Overview

To ensure the contest environment can support all entries ahead of the beta and final submission deadlines, an early **"alpha" release** is a mandatory step for continued participation. 

The performance of this alpha submission will have **zero effect** on the final submission score. Instead, the organizers will work with contestants to ensure the runtime environment functions as desired. Contestants will receive **private feedback** assessing their submission's performance on both the released benchmark suite and a hidden benchmark, executed within the official contest runtime environment.

---

## Key Details

- **Mandatory Participation:** Alpha submission is required to remain in the contest, but no teams will be eliminated for submitting.
- **Private Scoring:** Performance results are shared privately and will not impact the final score.
- **Environment:** Evaluations are performed on the official contest runtime environment.

---

## Submission Format

Contestants must submit an archive containing a clone of the contest repository, modified to run their submission. 

- **Preferred Format:** Zip (`.tar.gz` also accepted)
- **Size Limit:** Maximum 4 GB (2^32 bytes)

The following commands will be executed on the verification instance to evaluate a submission:

```bash
unzip submission.zip   # or: tar -xzf submission.tar.gz
cd fpl26_optimization_contest
make setup
make run_optimizer DCP=benchmark1.dcp
make run_optimizer DCP=benchmark2.dcp
make run_optimizer DCP=...
```

- **`make setup`**: This target can be updated by teams to install additional packages or perform any one-time preparation required before the submission runs.
- **`make run_optimizer`**: This target is invoked once per benchmark DCP in the evaluation suite.

---

## Output DCP Location

For each `make run_optimizer DCP=<input>.dcp` invocation, the evaluation harness searches for the optimized output in the same directory as the input DCP, using the filename pattern:

```
<input_stem>_optimized*.dcp
```

This matches the default location produced by the example `dcp_optimizer.py`. For instance, given `fpl26_contest_benchmarks/benchmark1.dcp`, the default output would be something like `fpl26_contest_benchmarks/benchmark1_optimized-<YYYYMMDD_HHMMSS>.dcp`. A fixed filename without a timestamp (e.g., `benchmark1_optimized.dcp`) is also accepted.

If multiple files matching this pattern exist at the end of the run, the **most recently modified file** (by `mtime`) will be validated and scored; all others are ignored. 

> **Note:** Since per-benchmark wall-clock runtime is capped, teams are encouraged to overwrite or refresh this output file each time their agent finds an improved solution — the last best result on disk is what will be scored.

---

## OpenRouter API Key

When `make run_optimizer` is invoked by the organizers, the `OPENROUTER_API_KEY` environment variable will be set to a key provisioned by the contest organizers with a **$1.00 (USD) spending limit per benchmark**.

- Submissions **must** read this environment variable to access OpenRouter.
- Submissions must **not** bundle, hard-code, or otherwise rely on a different API key.

---

## Upload Instructions

The exact instructions for uploading the submission archive will be emailed directly to teams.

---

## Closed-Source Submissions

While contestants are strongly encouraged to open-source their solutions at the conclusion of the contest, there is no requirement to do so. In such cases, it is still necessary to follow the flow described above to produce a binary-only submission.
