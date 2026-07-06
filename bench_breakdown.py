import time
from prokube.sandboxv2 import SandboxV2
from prokube.sandbox import Sandbox

API_URL = "https://technical-arctotherium.prokube.cloud/pkui"
API_KEY = "pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI"
CODE = "open('/workspace/test.txt','w').write('Hello, world!')"


def bench(cls, label):
    print(f"=== {label} ===")
    total = time.perf_counter()
    t = time.perf_counter()
    cm = cls.from_pool(pool="python-pool", workspace="developer1", api_url=API_URL, api_key=API_KEY)
    sbx = cm.__enter__()
    print(f"  claim (from_pool)      : {(time.perf_counter()-t)*1000:7.0f} ms")
    try:
        t = time.perf_counter()
        sbx.run_code(CODE)
        print(f"  run_code (exec)        : {(time.perf_counter()-t)*1000:7.0f} ms")
        t = time.perf_counter()
        sbx.files.read("/workspace/test.txt")
        print(f"  files.read             : {(time.perf_counter()-t)*1000:7.0f} ms")
    finally:
        t = time.perf_counter()
        cm.__exit__(None, None, None)
        print(f"  exit (delete/release)  : {(time.perf_counter()-t)*1000:7.0f} ms")
    print(f"  --------------------------------")
    print(f"  TOTAL                  : {(time.perf_counter()-total)*1000:7.0f} ms\n")


bench(Sandbox, "V1 (gVisor pod)")
bench(SandboxV2, "V2 (Firecracker)")
# second FC run to show warm-cache behaviour
bench(SandboxV2, "V2 (Firecracker) 2nd")
