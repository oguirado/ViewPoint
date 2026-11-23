"""Default discovery refuses an unconfigured namespace before loading payloads."""
from pathlib import Path
from asset_factory import proc


def load_tests(loader, standard_tests, pattern):
    try:
        proc.check_test_environment()
    except proc.SignalRefusal as exc:
        raise RuntimeError(f'{exc.code}: {proc.TEST_ENVIRONMENT}') from exc
    root = Path(__file__).resolve().parent
    return loader.discover(str(root), pattern=pattern or 'test*.py', top_level_dir=str(root))
