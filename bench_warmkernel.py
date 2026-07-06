"""Time the FIRST run_code after a warm-pool claim (kernel-warmth validation)."""
import sys
import time

from prokube.sandboxv2 import SandboxV2

API_URL = "https://technical-arctotherium.prokube.cloud/pkui"
API_KEY = "pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI"

pool = sys.argv[1] if len(sys.argv) > 1 else "warmkernel-test"

print(f"=== from_pool({pool}) claim + first exec ===")
t_claim = time.perf_counter()
with SandboxV2.from_pool(pool=pool, workspace="developer1",
                         api_url=API_URL, api_key=API_KEY) as sbx:
    claim_ms = (time.perf_counter() - t_claim) * 1000
    print(f"  claim (from_pool): {claim_ms:8.0f} ms  -> {sbx.name}")
    for i in range(5):
        t = time.perf_counter()
        r = sbx.run_code("x = 6*7")
        dt = (time.perf_counter() - t) * 1000
        tag = "  <-- FIRST (kernel warmth)" if i == 0 else ""
        print(f"  exec #{i + 1}: {dt:8.0f} ms{tag}")
