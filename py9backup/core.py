#! /bin/python3
'''
Dead simple backup functionality.
'''
from collections import defaultdict
from datetime import date
import os
import os.path as osp
from pathlib import Path
import re
from shutil import copy as fcopy
import string
import sys
import tarfile
import tempfile as tmp
from typing import List, Optional

import click


class RichPath:
    '''
    A path with some metadata related to backup functionalty.
    '''

    _FLAGS_LEN = 5
    _FLAGS = [
        (0, 'x', 'exclude'),
        (1, '?', 'sticky'),
    ]

    @classmethod
    def parse(cls, raw_line):
        flags = raw_line[:cls._FLAGS_LEN]
        path = raw_line[cls._FLAGS_LEN + 1:].strip()

        fdict = {}
        for ix, char, kwarg in cls._FLAGS:
            if flags[ix] == char:
                fdict[kwarg] = True

        return cls(path, **fdict)

    @property
    def depth(self):
        return str(self.path).rstrip('/').count('/') - 1

    def __init__(self, path_str, exclude=False, sticky=False):
        '''
        Arguments:
            exclude:
                mark the file or path as being excluded rather than included.
            sticky:
                mark the file or path to persist in the database even if it
                does not exist.
        '''
        self.path = Path(path_str).absolute()
        self.exclude = exclude
        self.sticky = sticky

    def __eq__(self, other):
        return self.path == other.path

    def __lt__(self, other):
        return self.path < other.path

    def __str__(self):
        base = [' '] * (self._FLAGS_LEN + 1)
        for ix, char, kwarg in self._FLAGS:
            if getattr(self, kwarg):
                base[ix] = char

        return ''.join(base) + str(self.path)

    # include only the path in the hash, so that the same path with
    # different flags will intentionally collide in sets/dicts. This
    # makes overwriting the flags easy and enforces a uniqueness invariant.
    def __hash__(self):
        return hash(self.path)


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(-1)


ALLOWABLE_CHARS = set(string.ascii_letters) | set(string.digits) | {'_'}
CONFIG_DIR = Path('~/.config/py9backup/').expanduser()

try:
    CONFIG_DIR.mkdir(exist_ok=True)
except Exception:
    die(f'Unable to create config directory at {CONFIG_DIR}')


def canonicalize_group_name(group):
    group = group.lower()
    if set(group) - ALLOWABLE_CHARS:
        die('Illegal characters in group name.')
    return group


def get_file_list_path(group, need=True):
    group = canonicalize_group_name(group)
    fp = CONFIG_DIR.joinpath(f'{group}.txt')

    if not fp.exists():
        if need:
            die(f'Group "{group}" does not exist.')
        else:
            fp.touch()

    return fp


def get_group_rps(group: str, need=False) -> List[RichPath]:
    fp = get_file_list_path(group, need=need)
    rps = []
    with fp.open('r') as f:
        for line in f:
            rps.append(RichPath.parse(line))

    return rps


def commit_group_rps(group, rps):
    '''
    Atomically commit the passed rps as the new contents of the group file.

    Does NOT append or merge, that is the responsibility of the caller.
    '''

    fp = get_file_list_path(group)
    tfd, tfn = tmp.mkstemp(text=True)

    with open(tfd, 'w') as tf:
        for rp in sorted(set(rps)):
            if rp.sticky or osp.exists(rp.path):
                print(rp, file=tf)

    fcopy(tfn, str(fp))


def gather_effective_files(rps):
    '''
    Converts a collection of include/exclude paths to a minimal collection
    of include-only paths.

    Precedence is given to longer paths, irrespective of ordering.
    '''

    # sort by depth
    rps_by_depth = defaultdict(list)
    for rp in rps:
        rps_by_depth[rp.depth].append(rp)

    fns_by_depth = defaultdict(set)

    # need to iterate in order of depth so that
    # + a/
    # - a/1
    # + a/1/x
    # chains resolve correctly
    for cur_depth, rps in sorted(rps_by_depth.items()):
        for rp in rps:
            cur_path = str(rp.path)
            if rp.exclude:
                # exclusions force explicit expansions of all levels above them
                for dx in range(0, cur_depth):
                    prefix = None
                    for cand_prefix in fns_by_depth[dx]:
                        if cur_path.startswith(cand_prefix):
                            prefix = cand_prefix
                            break
                    if prefix is None:
                        continue

                    # remove higher-level path in favour of fragments
                    fns_by_depth[dx].remove(prefix)
                    fns_by_depth[dx + 1] |=\
                        {str(x) for x in Path(prefix).iterdir()}

                # exclude actual exclusion
                fns_by_depth[cur_depth].remove(cur_path)

            else:
                # exclude redundant paths
                # this only works because of depth sorting in outer loop!
                for dx in range(0, cur_depth):
                    if any([cur_path.startswith(p) for p in fns_by_depth[dx]]):
                        break
                else:
                    fns_by_depth[cur_depth].add(cur_path)

    out = set()
    for fns in fns_by_depth.values():
        out |= fns

    return sorted(out)


@click.command(name='add')
@click.argument('group', nargs=1)
@click.argument('paths', nargs=-1)
@click.option(
    '--allow-nx', is_flag=True, default=False,
    help=(
        'Allows nonexistent entries and persists them until explicit '
        'deletion. Otherwise, nonexistent entries are dropped.'
    )
)
@click.option(
    '--exclude', is_flag=True, default=False,
    help=(
        'Excludes the file or path. Overrides more general inclusions. '
        'Overriden by more specific inclusions'
    )
)
def add_files(group, paths, *, exclude, allow_nx):
    '''
    Adds a path to be tracked under a group.
    '''
    group = canonicalize_group_name(group)
    rps = set(get_group_rps(group))

    new_rps = set()
    for path in paths:
        if not (allow_nx or osp.isfile(path) or osp.isdir(path)):
            print(
                f'Path "{path}" does not exist. Ignoring. '
                'Pass --allow-nx to force persistent inclusion.'
            )
            continue

        new_rp = RichPath(path, exclude=exclude, sticky=allow_nx)
        new_rps.add(new_rp)

    # order is important
    rps = new_rps | rps
    commit_group_rps(group, rps)


@click.command('show')
@click.argument('group')
@click.option(
    '--full', is_flag=True, default=False,
    help='show every included file explicitly',
)
def show_files(group, *, full):
    '''
    Shows all tracked paths for group.
    '''
    rps = get_group_rps(group, need=True)
    if not full:
        for rp in rps:
            print(rp)
    else:
        for fn in gather_effective_files(rps):
            print('\t' + fn)


@click.command(name='del')
@click.argument('group')
@click.argument('regex')
def del_files(group, regex):
    '''
    Removes files from group by regex.
    
    Python's `re.match` is used, so the regexp should match from the
    beginning of the path. Use ^.* or similar.
    '''
    path_reg = re.compile(regex)
    rps = get_group_rps(group)

    keep_rps = [rp for rp in rps if not path_reg.search(str(rp.path))]

    commit_group_rps(group, keep_rps)


@click.command()
@click.argument('group')
@click.argument('command')
@click.option(
    '--no-xz', default=False, is_flag=True, help='turn off xz compression',
)
@click.option('--name', default=None, help='name to use for the tarball')
def pull(group, command, *, no_xz, name):
    '''
    Pulls files into tarball, runs given command.

    Subsequently, runs a command on the tarball. "{}" in the command string is
    expanded to be the name of the created tarball, a la find.

    rsync, scp, git push, etc. are good candidate commands here.
    '''

    if name is None:
        name = f'backup_{group}_{date.today().isoformat()}'

    tar_mode = 'w' if no_xz else 'w:xz'
    suf = 'tar' if no_xz else 'tar.xz'

    file_paths = gather_effective_files(get_group_rps(group))
    tar_fn = osp.join(tmp.mkdtemp(), f'{name}.{suf}')
    command = re.sub(r'\{\}', tar_fn, command)

    with tarfile.open(tar_fn, tar_mode) as tar:
        for path in file_paths:
            try:
                tar.add(path.strip())
            except FileNotFoundError:
                print(f'File {path} not found, skipping.', file=sys.stderr)
            except PermissionError:
                die(f'File {path} needs elevated permissions. Dying.')

    os.system(command)
    os.remove(tar_fn)


@click.command('list')
def list_groups():
    '''
    List known groups in the registry.
    '''
    for path in Path(CONFIG_DIR).glob('*.txt'):
        print(path.stem)


@click.command()
@click.argument('group')
def del_group(group):
    '''
    Deletes the registry file for a group. Unless that file itself is
    backed up (as is recommended), this cannot be undone.
    '''

    group_path = get_file_list_path(group, need=True)
    with group_path.open('r') as f:
        lines = f.readlines()

    message =\
        f'Confirm deletion of group {group} containing {len(lines)} lines' +\
        '\n\t' + lines[0] + '\t...\n\t' + lines[-1]

    if click.confirm(message):
        group_path.unlink()


@click.group()
def main():
    '''
    Tracks files to be backed up, on a per-group basis.

    The first argument is always the group name, which can be an arbitrary well
    behaving string which serves as an identifier for the collection of paths
    added. This allows different backup flow for different files.
    '''
    pass


main.add_command(add_files)
main.add_command(del_files)
main.add_command(del_group)
main.add_command(list_groups)
main.add_command(show_files)
main.add_command(pull)


if __name__ == '__main__':
    main()
