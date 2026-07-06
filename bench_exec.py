import time
from prokube.sandbox import Sandbox
from prokube.sandboxv2 import SandboxV2

API_URL = "https://technical-arctotherium.prokube.cloud/pkui"
API_KEY = "pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI"

# print("=== V2 (Firecracker) from hybernate pool ===")
# with SandboxV2.from_pool(pool="python-pool", workspace="developer1", api_url=API_URL, api_key=API_KEY) as sbx:
#     for i in range(4):
#         t = time.perf_counter()
#         sbx.run_code("x = 6*7")
#         print(f"  exec #{i+1}: {(time.perf_counter()-t)*1000:7.0f} ms")

# print("\n=== V1 (gVisor pod) from warm pool ===")
# with Sandbox.from_pool(pool="python-pool", workspace="developer1", api_url=API_URL, api_key=API_KEY) as sbx:
#     for i in range(4):
#         t = time.perf_counter()
#         sbx.run_code("x = 6*7")
#         print(f"  exec #{i+1}: {(time.perf_counter()-t)*1000:7.0f} ms")

vms = ["python-pool-322094"]

print("\n=== V2 (Firecracker) from manually hybernate VM ===")
sbx = SandboxV2.get(name=vms[0], workspace="developer1", api_url=API_URL, api_key=API_KEY)
# t = time.perf_counter()
# sbx.resume()
# sbx.wait_until_ready()
# print(f"  resume: {(time.perf_counter()-t)*1000:7.0f} ms")
# for i in range(4):
#     t = time.perf_counter()
#     sbx.run_code("x = 6*7")
#     print(f"  exec #{i+1}: {(time.perf_counter()-t)*1000:7.0f} ms")
# sbx.pause()

for i in range(4):
    t1 = time.perf_counter()
    sbx.resume()
    print(f"  resume           #{i+1}: {(time.perf_counter()-t1)*1000:7.0f} ms")
    t2 = time.perf_counter()
    sbx.wait_until_ready()
    print(f"  wait_until_ready #{i+1}: {(time.perf_counter()-t2)*1000:7.0f} ms")
    t3 = time.perf_counter()
    sbx.run_code("x = 6*7")
    print(f"  exec             #{i+1}: {(time.perf_counter()-t3)*1000:7.0f} ms")
    t4 = time.perf_counter()
    sbx.pause()
    print(f"  pause            #{i+1}: {(time.perf_counter()-t4)*1000:7.0f} ms")
    print(f"  total            #{i+1}: {(time.perf_counter()-t1)*1000:7.0f} ms")



print("=== V2 (Firecracker) from hybernate pool ===")
for i in range(4):
    t1 = time.perf_counter()
    sbx2 = SandboxV2.from_pool(pool="python-pool", workspace="developer1", api_url=API_URL, api_key=API_KEY)
    print(f"  from_pool        #{i+1}: {(time.perf_counter()-t1)*1000:7.0f} ms")
    t2 = time.perf_counter()
    sbx2.wait_until_ready()
    print(f"  wait_until_ready #{i+1}: {(time.perf_counter()-t2)*1000:7.0f} ms")
    t3 = time.perf_counter()
    sbx2.run_code("x = 6*7")
    print(f"  exec             #{i+1}: {(time.perf_counter()-t3)*1000:7.0f} ms")
    t4 = time.perf_counter()
    sbx2.kill()
    print(f"  kill             #{i+1}: {(time.perf_counter()-t4)*1000:7.0f} ms")
    print(f"  total            #{i+1}: {(time.perf_counter()-t1)*1000:7.0f} ms")
