from prokube.sandbox import Sandbox
from prokube.sandboxv2 import SandboxV2
import time


def from_pool(sbx_api, pool):
    t = time.perf_counter()
    with sbx_api.from_pool(
        pool=pool,
        workspace="developer1",
        api_url="https://technical-arctotherium.prokube.cloud/pkui",
        api_key="pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI",
    ) as sbx:
        print(
            sbx.run_code("""
            print("Hello, world!")
            """).stdout
        )

    time_taken = time.perf_counter() - t
    api = "SandboxV2" if sbx_api == SandboxV2 else "Sandbox"
    print(f"Time taken : {time_taken:.6f} seconds for {api} from_pool({pool})")


from_pool(SandboxV2, "hibernated")
from_pool(SandboxV2, "running")
from_pool(Sandbox, "v1pool")