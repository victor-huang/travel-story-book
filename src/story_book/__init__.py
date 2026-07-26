"""Travel Story Book.

Registers the HEIC/HEIF opener at package import. Pillow cannot open `.heic` without it, and
iPhone libraries are full of them -- the plan calls HEIC "the first real portability trap".

It lives here, as an import side effect, because the alternative failed silently in exactly the
way that matters: the fixture test called `register_heif_opener()` itself before decoding, so the
suite proved *the library* worked while never proving *the application* had registered it. Every
HEIC in a real run failed to open, with a fully green test suite. One registration at package
import covers the CLI, every stage, and any test that imports either.
"""

__version__ = "0.1.0"


def _register_image_formats() -> None:
    try:
        import pillow_heif
    except ImportError:  # pragma: no cover - pillow-heif is a hard dependency
        return
    pillow_heif.register_heif_opener()


_register_image_formats()
