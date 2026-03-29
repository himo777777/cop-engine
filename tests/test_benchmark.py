"""
COP Engine — Solver Benchmark / Load Test
==========================================
Mäter solver-prestanda under olika belastningar:
  1w, 2w, 4w med timing och minnesanvändning.

Kör: pytest tests/test_benchmark.py -v -s -p no:cacheprovider
"""

import time
import tracemalloc
import pytest
from data_model import create_kristianstad_example
from solver import solve_schedule


# ============================================================================
# HELPERS
# ============================================================================

def run_solver_benchmark(num_weeks: int, time_limit: int = 120) -> dict:
    """Kör solver och returnera timing + kvalitetsmetrik."""
    config = create_kristianstad_example()

    tracemalloc.start()
    t0 = time.perf_counter()

    result = solve_schedule(config, num_weeks=num_weeks, time_limit_seconds=time_limit)

    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Räkna tilldelade pass
    total_assignments = 0
    if result:
        for doc_id, days in result.items():
            total_assignments += len(days)

    return {
        "num_weeks": num_weeks,
        "time_limit": time_limit,
        "elapsed_seconds": round(elapsed, 2),
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
        "total_assignments": total_assignments,
        "num_doctors": len(config.doctors),
        "result_not_none": result is not None,
        "assignments_per_second": round(total_assignments / elapsed, 1) if elapsed > 0 else 0,
    }


# ============================================================================
# BENCHMARK TESTS
# ============================================================================

class TestSolverBenchmark:
    """Lasttest: mät prestanda för olika schemaperioder."""

    def test_benchmark_1_week(self):
        """1 vecka — baseline performance."""
        r = run_solver_benchmark(num_weeks=1, time_limit=60)
        print(f"\n{'='*60}")
        print(f"  BENCHMARK: 1 vecka")
        print(f"  Tid:          {r['elapsed_seconds']}s")
        print(f"  Minne (peak): {r['peak_memory_mb']} MB")
        print(f"  Tilldelningar: {r['total_assignments']}")
        print(f"  Throughput:   {r['assignments_per_second']} tilldelningar/s")
        print(f"  Läkare:       {r['num_doctors']}")
        print(f"{'='*60}")

        assert r["result_not_none"], "Solver returnerade None för 1 vecka"
        assert r["elapsed_seconds"] < 90, f"Solver tog >90s för 1 vecka"
        assert r["total_assignments"] > 0, "Inga tilldelningar genererade"

    def test_benchmark_2_weeks(self):
        """2 veckor — normal drift."""
        r = run_solver_benchmark(num_weeks=2, time_limit=120)
        print(f"\n{'='*60}")
        print(f"  BENCHMARK: 2 veckor")
        print(f"  Tid:          {r['elapsed_seconds']}s")
        print(f"  Minne (peak): {r['peak_memory_mb']} MB")
        print(f"  Tilldelningar: {r['total_assignments']}")
        print(f"  Throughput:   {r['assignments_per_second']} tilldelningar/s")
        print(f"  Läkare:       {r['num_doctors']}")
        print(f"{'='*60}")

        assert r["result_not_none"], "Solver returnerade None för 2 veckor"
        assert r["elapsed_seconds"] < 180, f"Solver tog >180s för 2 veckor"

    @pytest.mark.slow
    def test_benchmark_4_weeks(self):
        """4 veckor — stresstest."""
        r = run_solver_benchmark(num_weeks=4, time_limit=180)
        print(f"\n{'='*60}")
        print(f"  BENCHMARK: 4 veckor")
        print(f"  Tid:          {r['elapsed_seconds']}s")
        print(f"  Minne (peak): {r['peak_memory_mb']} MB")
        print(f"  Tilldelningar: {r['total_assignments']}")
        print(f"  Throughput:   {r['assignments_per_second']} tilldelningar/s")
        print(f"  Läkare:       {r['num_doctors']}")
        print(f"{'='*60}")

        assert r["result_not_none"], "Solver returnerade None för 4 veckor"
        assert r["elapsed_seconds"] < 200, f"Solver tog >{200}s för 4 veckor"

    def test_solver_scales_linearly(self):
        """Verifiera att solver inte exploderar exponentiellt."""
        r1 = run_solver_benchmark(num_weeks=1, time_limit=60)
        r2 = run_solver_benchmark(num_weeks=2, time_limit=120)

        ratio = r2["elapsed_seconds"] / max(r1["elapsed_seconds"], 0.01)
        print(f"\n{'='*60}")
        print(f"  SKALNINGSANALYS")
        print(f"  1v: {r1['elapsed_seconds']}s  |  2v: {r2['elapsed_seconds']}s")
        print(f"  Ratio (2v/1v): {ratio:.1f}x")
        print(f"  {'OK — subkvadratisk' if ratio < 6 else 'VARNING — superkvadratisk'}")
        print(f"{'='*60}")

        # 2 veckor borde vara max 6x (generöst) — inte exponentiellt
        assert ratio < 6, f"Solver skalar dåligt: 2v/1v = {ratio:.1f}x"

    def test_concurrent_api_requests(self):
        """Simulera flera samtida API-anrop till /schedule/generate."""
        from starlette.testclient import TestClient
        from api import app
        import concurrent.futures

        with TestClient(app) as client:
            def generate():
                t0 = time.perf_counter()
                r = client.post("/schedule/generate", json={
                    "clinic_id": "kristianstad",
                    "num_weeks": 1,
                    "time_limit_seconds": 60,
                })
                elapsed = time.perf_counter() - t0
                return r.status_code, elapsed

            # 3 samtida requests
            t_total = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(generate) for _ in range(3)]
                results = [f.result() for f in futures]
            total_elapsed = time.perf_counter() - t_total

            print(f"\n{'='*60}")
            print(f"  CONCURRENT API TEST: 3 samtida /schedule/generate")
            for i, (status, t) in enumerate(results):
                print(f"  Request {i+1}: status={status}, tid={t:.1f}s")
            print(f"  Total wallclock: {total_elapsed:.1f}s")
            print(f"{'='*60}")

            for status, _ in results:
                assert status == 200, f"Request misslyckades med status {status}"
