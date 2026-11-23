"""サンドボックスの入れ物（Agent Engine）を1つ作り、名前を表示する。LATTICE_SANDBOX_ENGINE に入れて使う。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.agent import sandbox  # noqa: E402

e = sandbox.client().runtimes.create(config={"display_name": "lattice-sandbox-holder"})
print(e.api_resource.name)
