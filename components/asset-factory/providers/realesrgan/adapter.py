"""RealESRGAN input upscale. CLI: python3 -B -m providers.realesrgan.adapter."""
from providers.birefnet import offline


def prepare(source, model_root, target, **kwargs):
    return offline.prepare('realesrgan', source, model_root, target, **kwargs)


def main(argv=None):
    return offline.main('realesrgan', argv)


if __name__ == '__main__':
    raise SystemExit(main())
