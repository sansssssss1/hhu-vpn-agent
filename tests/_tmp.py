"""测试用临时目录。

注意：不用 tempfile.mkdtemp（随机目录名）——在受限沙箱里新建的随机目录会被拒绝写入。
这里用固定名字的 tests/.tmp/work，用完清空，效果一样且到处都能跑。
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP_ROOT = ROOT / ".tmp" / "work"


class TempDir:
    def __enter__(self) -> Path:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        return TMP_ROOT

    def __exit__(self, *exc):
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        return False
