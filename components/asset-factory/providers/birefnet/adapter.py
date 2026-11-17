"""BiRefNet input preparation. CLI: python3 -B -m providers.birefnet.adapter."""
from providers.birefnet import offline


def prepare(source, model_root, target, **kwargs):
    return offline.prepare('birefnet', source, model_root, target, **kwargs)


def main(argv=None):
    return offline.main('birefnet', argv)


if __name__ == '__main__':
    raise SystemExit(main())
