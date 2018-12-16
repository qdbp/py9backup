import os
import os.path as osp
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp, mkstemp

from click.testing import CliRunner

TEST_DIR = osp.dirname(osp.realpath(__file__))

# load the script as a module
bkp = SourceFileLoader(
    'backup',
    Path(TEST_DIR).parent.joinpath('py9backup/backup').as_posix()
).load_module()

# patch the backup directory
RUNNER = CliRunner()

os.chdir(TEST_DIR)


class tempshellfns:

    def __enter__(self):
        self.shellout_fd, self.shellout_fn = mkstemp()
        self.shellerr_fd, self.shellerr_fn = mkstemp()

        return self.shellerr_fn, self.shellerr_fn

    def __exit__(self, *args):
        os.close(self.shellout_fd)
        os.close(self.shellerr_fd)
        os.remove(self.shellerr_fn)
        os.remove(self.shellout_fn)


class clean_configdir:

    def __enter__(self):
        self.dir = mkdtemp()
        self.old_dir_path = bkp.CONFIG_DIR  # type: ignore
        bkp.CONFIG_DIR = Path(self.dir)  # type: ignore
        return self.dir

    def __exit__(self, *args):
        bkp.CONFIG_DIR = self.old_dir_path  # type: ignore
        rmtree(self.dir)


def run(*args, asrt=0, noex=True, **kwargs):

    if len(args) == 1:
        args = args[0].split(' ')

    print('+' + ' '.join(args))
    out = RUNNER.invoke(bkp.main, args, **kwargs)  # type: ignore

    if out.exception:
        print(out.exception)

    print(out.output)

    if asrt is not None:
        assert out.exit_code == asrt
    if noex:
        assert not out.exception
    return out


def test_is_segment_glob():
    assert bkp.is_segment_glob('/**')
    assert bkp.is_segment_glob('/foo/*')
    assert bkp.is_segment_glob('/foo/*.bkp')
    assert bkp.is_segment_glob('/foo/**')
    assert bkp.is_segment_glob('/foo/**/*.bkp')
    assert bkp.is_segment_glob('/foo/**/')
    assert bkp.is_segment_glob(r'/foo/\**/')
    assert not bkp.is_segment_glob(r'/foo/\*.bkp')
    assert not bkp.is_segment_glob('/foo/bar/')

    assert bkp.is_segment_glob('*.bkp')
    assert bkp.is_segment_glob('*')
    assert bkp.is_segment_glob('**')
    assert not bkp.is_segment_glob(r'\*')
    assert bkp.is_segment_glob(r'\**')
    assert bkp.is_segment_glob(r'*\*')


def test_gop_prio():
    assert bkp.gop_prio('/') == 0
    assert bkp.gop_prio('/**/') == 0
    assert bkp.gop_prio('/foo') == 1
    assert bkp.gop_prio('/foo/') == 1
    assert bkp.gop_prio('/foo/**/bar/**/*.bkp') == 2
    assert bkp.gop_prio('/foo/bar/') == 2
    assert bkp.gop_prio('/foo/bar/baz.png') == 3


def test_basic_functionality():
    with clean_configdir() as mock_dir:
        run('add', 'test', './testdir/')
        out = out_add_first = run('show', 'test').output
        assert 'testdir' in out

        run('add', 'test', './testdir/b/b2/', '--exclude')
        out = out_after_exclude = run('show', 'test').output
        assert re.compile('\n +x +[^ ]+b2', re.MULTILINE).search(out)

        out = run('show', 'test', '--full').output
        assert 'b1' in out
        assert 'b2' not in out

        run('del', 'test', 'b2')
        out = run('show', 'test').output
        assert out == out_add_first

        with tempshellfns() as (ofn, efn):
            out = run('pull', 'test', f'tar -tJf {{}} 1>{ofn} 2>{efn}').output
            with open(ofn) as f:
                shell_out = f.read()
            assert '.hidden' in shell_out
            assert 'bar.png' in shell_out
            assert 'foo.txt' in shell_out

        # test accidental delete
        os.remove(osp.join(mock_dir, 'test.txt'))

        # should restore backup
        out = run('show', 'test', input='Y').output
        comp_out = '\n'.join(out.split('\n')[2:])
        assert comp_out == out_after_exclude

        # test forget
        run('forget', 'test', input='foobar')
        assert 'testdir' in run('show', 'test').output

        run('forget', 'test', input='yes')
        assert 'was not found' in run('show', 'test').output
        assert 'test' in run('list').output


def test_prio_example():
    with clean_configdir():
        run('add edgy ./weird/**/wat/ --exclude')
        run('add edgy ./weird/**/wat/wat/')

        with tempshellfns() as (ofn, efn):
            run('pull', 'edgy', f'tar -tJf {{}} 1>{ofn} 2>{efn}')
            with open(ofn) as f:
                shell_out = f.read()

        print(shell_out)

        assert 'weird/wat/wat/some.file' in shell_out


def test_readme_example():
    with clean_configdir():
        run('add mygroup ./stuff/')
        run('add mygroup ./stuff/old/ --exclude')
        run('add mygroup ./stuff/old/important/')
        run('add mygroup ./stuff/**/*.bkp --exclude')
        run('add mygroup ./stuff/archive/**/*.bkp')
        run('add mygroup ./stuff/old/important/special.bkp')
        run('add mygroup ./stuff/**/interesting/')

        with tempshellfns() as (ofn, efn):
            run('pull', 'mygroup', f'tar -tJf {{}} 1>{ofn} 2>{efn}')
            with open(ofn) as f:
                shell_out = f.read()

        assert '/stuff/new/some.file' in shell_out
        assert '/stuff/old/some.file' not in shell_out
        assert '/stuff/old/important/some.file' in shell_out
        assert '/stuff/old/important/some.bkp' not in shell_out
        assert '/stuff/archive/2018/store.bkp' in shell_out
        assert '/stuff/old/important/special.bkp' in shell_out
        assert '/stuff/old/a/b/c/interesting/some.bkp' not in shell_out
        assert '/stuff/old/a/b/c/interesting/some.file' in shell_out


def test_globs():
    with clean_configdir() as mock_dir:
        run('add', 'test', './testdir/**/*.png')
        out = out_png = run('show', 'test').output

        assert re.compile(r' +g +[^ ]+\*\*\/\*.png').search(out_png)

        out_full_png = run('show', 'test', '--full').output

        with tempshellfns() as (ofn, efn):
            run('pull', 'test', f'tar -tJf {{}} 1>{ofn} 2>{efn}').output
            with open(ofn) as f:
                shell_out = f.read()
            assert '.hidden' not in shell_out
            assert 'bar.png' in shell_out
            assert '.txt' not in shell_out

        run('add', 'test', './**/b2/**/*.png', '--exclude')
        out_full_png = run('show', 'test', '--full').output

        print(out_full_png)


def test_empty_handling():

    with clean_configdir():
        run('add mygroup ./stuff')
        run('del mygroup .*/stuff')

        with tempshellfns() as (ofn, _):
            for inp in ['', 'y', 'n']:
                run(
                    'pull', 'mygroup', f'echo -n pulled_{inp} > {ofn}',
                    input=inp)

            with open(ofn) as f:
                assert f.readlines() == ['pulled_y']

        run('forget mygroup')


def test_rename():
    with clean_configdir():
        run('add mygroup ./stuff')
        run('forget mygroup', input='y')

        run('add other ./xxx --allow-nx')

        # should fail since mygroup should have a backup which needs prompt
        run('rename other mygroup', input='')

        # restore backup with this line
        assert 'stuff' in run('show', 'mygroup', input='y').output
        assert 'xxx' not in run('show', 'mygroup').output

        # single confirm should overwrite both restored main file and backup
        run('rename other mygroup', input='y')

        assert 'stuff' not in run('show', 'mygroup').output
        assert 'xxx' in run('show', 'mygroup').output
