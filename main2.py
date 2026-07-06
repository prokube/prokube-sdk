from prokube.sandboxv2 import SandboxV2
from prokube.sandbox import Sandbox
import time


def sandbox_v1(pool):
    print(f"Sandbox V1: {pool}")
    with Sandbox.from_pool(
        pool=pool,
        workspace="developer1",
        api_url="https://technical-arctotherium.prokube.cloud/pkui",
        api_key="pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI",
    ) as sbx:
        sbx.run_code("""
            with open('/workspace/test.txt', 'w') as f:
                f.write('Hello, world!')
        """)

        chart = sbx.files.read("/workspace/test.txt")
        print(f"{pool} (Sandbox V1): {chart.decode("utf-8")}")  # Output: Hello, world!

def sandbox_v2(pool):
    print(f"Sandbox V2: {pool}")
    with SandboxV2.from_pool(
        pool=pool,
        workspace="developer1",
        api_url="https://technical-arctotherium.prokube.cloud/pkui",
        api_key="pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI",
    ) as sbx:
        sbx.run_code("""
            with open('/workspace/test.txt', 'w') as f:
                f.write('Hello, world!')
        """)

        chart = sbx.files.read("/workspace/test.txt")
        print(f"{pool} (Sandbox V2): {chart.decode("utf-8")}")  # Output: Hello, world!

pools_v1 = ["v1"]
pools_v2 = ["no-volume-running-pool", "no-volume-paused-pool", "juicefs-pause-pool", "pvc-juicefs-pause-pool", "pvc-mayastor-pause-pool"]

# for pool in pools_v1:
#     t = time.perf_counter()
#     sandbox_v1(pool)
#     time_taken = time.perf_counter() - t
#     print(f"Time taken for Sandbox V1 with pool '{pool}': {time_taken:.6f} seconds")

for pool in pools_v2:
    t = time.perf_counter()
    sandbox_v2(pool)
    time_taken = time.perf_counter() - t
    print(f"Time taken for Sandbox V2 with pool '{pool}': {time_taken:.6f} seconds")