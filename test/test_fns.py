from shutil import rmtree
from pathlib import Path
from tempfile import mkdtemp

from py9backup.core import gather_effective_files, RichPath


def test_gather_effective_files():

    root = Path(mkdtemp())

    a = root.joinpath('a/')
    b = root.joinpath('b/')
    a.mkdir()
    b.mkdir()

    a0 = a.joinpath('0/')
    a1 = a.joinpath('1/')
    b0 = b.joinpath('0/')
    b1 = b.joinpath('1/')
    a0.mkdir()
    b0.mkdir()
    a1.mkdir()
    b1.mkdir()

    a0x = a0.joinpath('x.')
    a0y = a0.joinpath('y.')
    a1x = a1.joinpath('x.')
    a1y = a1.joinpath('y.')
    b0x = b0.joinpath('x.')
    b0y = b0.joinpath('y.')
    b1x = b1.joinpath('x.')
    b1y = b1.joinpath('y.')
    b1z = b1.joinpath('z.')
    a0x.touch()
    b0x.touch()
    a1x.touch()
    b1x.touch()
    a0y.touch()
    b0y.touch()
    a1y.touch()
    b1y.touch()
    b1z.touch()

    rp_ = RichPath(root)
    rp_a = RichPath(a)
    rp_b = RichPath(b, exclude=True)
    rp_b1 = RichPath(b1, exclude=False)
    rp_b1x = RichPath(b1x, exclude=True)

    out = gather_effective_files([rp_, rp_a, rp_b, rp_b1, rp_b1x])
    assert out == [str(a), str(b1y), str(b1z)]

    rmtree(str(root))
