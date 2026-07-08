from prokube.sandbox import Sandbox
from prokube.sandboxv2 import SandboxV2
import time


def from_pool(sbx_api, pool):
    print("Testing from_pool() with", sbx_api.__name__, "and pool:", pool)
    t1 = time.perf_counter()
    with sbx_api.from_pool(
        pool=pool,
        workspace="developer1",
        api_url="https://technical-arctotherium.prokube.cloud/pkui",
        api_key="pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI",
    ) as sbx:
        time_taken = time.perf_counter() - t1
        print(f"Acquire **ready** sandbox: {time_taken:.6f}s")
        t2 = time.perf_counter()
        sbx.run_code("""
            print("Hello, world!")
            """).stdout
        print(f"Run code time: {time.perf_counter() - t2:.6f}s")
        print(f"Total time to run code: {time.perf_counter() - t1:.6f}s")
    time_taken = time.perf_counter() - t1
    print(f"Total time: {time_taken:.6f}s")

for i in range(20):
    print("==============================")
    print(f"Iteration {i+1}/20")
    from_pool(SandboxV2, "gpool")
# from_pool(SandboxV2, "running")
# from_pool(Sandbox, "v1pool")