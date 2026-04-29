#!/usr/bin/env python3
"""FPL'26 Contest Submission Runner.

Runs the optimizer on all benchmark DCPs in a directory with a 1-hour
per-benchmark timeout, recording Fmax improvement, cost, and runtime.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path


async def run_benchmark(dcp_path: Path, output_dir: Path, api_key: str) -> dict:
    """Run optimizer on a single benchmark DCP with 1-hour timeout.

    Returns dict with keys: benchmark, initial_fmax_mhz, best_fmax_mhz,
    fmax_improvement_mhz, runtime_s, cost_usd, score, status, error.
    """
    from dcp_optimizer import DCPOptimizer

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_dcp = output_dir / f"{dcp_path.stem}_optimized-{timestamp}.dcp"
    run_dir = output_dir / f"submission_run_{dcp_path.stem}_{timestamp}"
    output_dcp.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "benchmark": dcp_path.name,
        "dcp_path": str(dcp_path),
        "output_dcp": str(output_dcp),
        "initial_fmax_mhz": None,
        "best_fmax_mhz": None,
        "fmax_improvement_mhz": 0.0,
        "runtime_s": 0.0,
        "cost_usd": 0.0,
        "score": 0.0,
        "status": "unknown",
        "error": None,
    }

    optimizer = DCPOptimizer(
        api_key=api_key,
        debug=False,
        run_dir=run_dir,
    )

    start = time.time()
    try:
        await optimizer.start_servers()
        success = await asyncio.wait_for(
            optimizer.optimize(dcp_path, output_dcp),
            timeout=3500,  # 58:20, leave 100s for cleanup
        )
        result["status"] = "completed" if success else "failed"
    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["error"] = "1-hour wall clock limit reached"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
    finally:
        result["runtime_s"] = time.time() - start
        await optimizer.cleanup()

    # Extract metrics from optimizer state
    if optimizer.initial_wns is not None and optimizer.clock_period:
        result["initial_fmax_mhz"] = round(optimizer.calculate_fmax(
            optimizer.initial_wns, optimizer.clock_period) or 0, 2)
    if optimizer.best_wns > float("-inf") and optimizer.clock_period:
        result["best_fmax_mhz"] = round(optimizer.calculate_fmax(
            optimizer.best_wns, optimizer.clock_period) or 0, 2)
    if result["initial_fmax_mhz"] and result["best_fmax_mhz"]:
        result["fmax_improvement_mhz"] = round(
            result["best_fmax_mhz"] - result["initial_fmax_mhz"], 2)
    result["cost_usd"] = round(optimizer.total_cost, 6)

    # Compute contest score
    alpha = result["fmax_improvement_mhz"]
    beta = result["cost_usd"]
    gamma = result["runtime_s"] / 3600.0
    result["score"] = round(max(0.0, alpha - 0.1 * alpha * beta - 0.1 * alpha * gamma), 4)

    return result


async def main():
    if len(sys.argv) < 2:
        print("Usage: python run_submission.py <dcp_dir> [output_dir]", file=sys.stderr)
        sys.exit(1)

    dcp_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else dcp_dir
    api_key = os.environ.get("OPENROUTER_API_KEY", "")

    if not dcp_dir.is_dir():
        print(f"Error: DCP directory not found: {dcp_dir}", file=sys.stderr)
        sys.exit(1)

    if not api_key:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    dcps = sorted(dcp_dir.glob("*.dcp"))
    if not dcps:
        print(f"No .dcp files found in {dcp_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Submission runner starting...")
    print(f"  DCP directory: {dcp_dir}")
    print(f"  Benchmark count: {len(dcps)}")
    print(f"  Time limit per benchmark: 1 hour")
    print(f"  Cost limit per benchmark: $1.00")
    print()

    results = []
    overall_start = time.time()
    failures = 0

    for i, dcp in enumerate(dcps):
        print(f"[{i+1}/{len(dcps)}] Running {dcp.name}...")
        bench_start = time.time()
        result = await run_benchmark(dcp, output_dir, api_key)
        bench_elapsed = time.time() - bench_start
        results.append(result)
        print(f"  Status: {result['status']}, "
              f"Fmax: {result['initial_fmax_mhz']} → {result['best_fmax_mhz']} MHz "
              f"(+{result['fmax_improvement_mhz']} MHz), "
              f"Score: {result['score']}, "
              f"Runtime: {bench_elapsed:.0f}s, Cost: ${result['cost_usd']:.4f}")
        if result["status"] not in ("completed",):
            failures += 1

    overall_elapsed = time.time() - overall_start

    # Write results
    report_path = output_dir / "submission_results.json"
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overall_runtime_s": round(overall_elapsed, 2),
        "total_benchmarks": len(dcps),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": failures,
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "average_score": round(sum(r["score"] for r in results) / len(results), 4) if results else 0,
        "results": results,
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nResults saved to {report_path}")
    print(f"Average score: {report['average_score']}")

    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
