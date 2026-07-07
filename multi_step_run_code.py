from prokube.sandboxv2 import SandboxV2
from prokube.sandbox import Sandbox

API_URL = "https://technical-arctotherium.prokube.cloud/pkui"
API_KEY = "pk_live_boGZR4RnpTwuDQWJedQtCu0zR9QXR-YIWhFwYfB7PFI"


def multi_step_run_code(sandbox_api: Sandbox | SandboxV2, name: str, workspace: str, ):
    sbx = sandbox_api.get(name=name, workspace=workspace, api_url=API_URL, api_key=API_KEY)
    assert(sbx.status == "Running")
    api_type = isinstance(sbx, SandboxV2) and "SandboxV2" or "Sandbox"
    print(f"({api_type}) {sbx.name} at {sbx.workspace} workspace")
    print("Running python code in the sandbox...")
    sbx.run_code(language="python", code=
    """
        x = x if 'x' in globals() else 3
        x *= 2
    """)
    result = sbx.run_code(language="python", code=
    """
        print(x)
    """)
    print(f"python result: {result.stdout}")  # Output: 15, 30, 45... increments by 15 each time the code is run

    print("Running bash code in the sandbox...")
    sbx.run_code(language="bash", code=
    """
        x=${x:-5}
        x=$((x * 2))
    """)
    result = sbx.run_code(language="bash", code=
    """
        echo $x
    """)
    print(f"bash result: {result.stdout}")  # Output: 15, 30, 45... increments by 15 each time the code is run

    print("Running node code in the sandbox...")
    sbx.run_code(language="node", code=
    """
        if (typeof x === 'undefined') {
            let x = 7
        }
        x *= 2;
    """)
    result = sbx.run_code(language="node", code=
    """
        console.log(x);
    """)
    print(f"node result: {result.stdout}")  # Output: 15, 30, 45... increments by 15 each time the code is run


v1_sandbox = "python-pool-f6pg8"
v2_sandbox = "agent-pool-be36bb"
multi_step_run_code(Sandbox, name=v1_sandbox, workspace="developer1")
print("================================")
multi_step_run_code(SandboxV2, name=v2_sandbox, workspace="developer1")
