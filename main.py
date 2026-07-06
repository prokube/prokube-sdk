from prokube.sandbox import Sandbox
from prokube.sandboxv2 import SandboxV2
import time

pool = "python-pool"
t = time.perf_counter()
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
    print(f"{pool}: {chart.decode("utf-8")}")  # Output: Hello, world!

time_taken = time.perf_counter() - t
print(f"Time taken : {time_taken:.6f} seconds")
