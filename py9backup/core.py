#! /bin/python3
'''
Dead simple backup functionality.
'''
import glob
import os
import os.path as osp
import re
import string
import sys
import tarfile
import tempfile as tmp

import click


ALLOWABLE_CHARS = set(string.ascii_letters) | set(string.digits) | {'_'}
CONFIG_BASE = osp.join(osp.expanduser('~'), '.config/py9backup_')


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(-1)


def canonicalize_group_name(group):
    if set(group) - ALLOWABLE_CHARS:
        die('Illegal characters in group name.')
    return group


def get_file_list_fn(group, need=True):
    group = canonicalize_group_name(group)
    fn = CONFIG_BASE + f'{group}.txt'
    if not osp.isfile(fn):
        if need:
            die(f'Group "{group}" does not exist.')
        else:
            with open(fn, 'w') as f:
                f.write('')
    return fn


@click.command(name='add')
@click.argument('group', nargs=1)
@click.argument('paths', nargs=-1)
def add_files(group, paths):
    '''
    Adds a path to be tracked under a particular group name.
    '''
    with open(get_file_list_fn(group, need=False), 'a') as f:
        for path in paths:
            print(osp.abspath(path), file=f)


@click.command('show')
@click.argument('group')
def show_files(group):
    '''
    Shows all tracked paths for a particular group.
    '''
    with open(get_file_list_fn(group), 'r') as f:
        for path in f:
            print(path.strip())


@click.command(name='del')
@click.argument('group')
@click.argument('regex')
def del_files(group, regex):
    '''
    Removes files from the registry for the given group based on the regex
    given. Python's `re.match` is used, so the regexp should match from the
    beginning of the path. Use ^.* or similar.
    '''
    path_reg = re.compile(regex)
    fn = get_file_list_fn(group)
    with open(fn, 'r') as f:
        paths = {path.strip() for path in f if not path_reg.match(path)}

    with open(fn, 'w') as f:
        for path in sorted(paths):
            print(path.strip(), file=f)


@click.command()
@click.argument('group')
@click.argument('command')
@click.option('--xz', default=True, is_flag=True, help='use xz compression')
def pull(group, command, *, xz):
    '''
    Pulls all files in the group into a tarball, optionally compressing.
    Subsequently, runs a command on the tarball. {} in the command string is
    expanded to be the name of the created tarball, a la find.

    rsync, scp, git push, etc. are good candidate commands here.
    '''
    paths = set()
    with open(get_file_list_fn(group), 'r') as f:
        for row in f:
            paths.add(row)

    mode = 'w' if not xz else 'w:xz'
    suf = '.tar' if not xz else '.tar.xz'
    with tmp.NamedTemporaryFile(suffix=suf, delete=True) as tf:
        command = re.sub(r'\{\}', tf.name, command)
        with tarfile.open(tf.name, mode) as tar:
            for path in paths:
                tar.add(path.strip())
        os.system(command)


@click.command()
@click.argument('group')
def clean(group):
    '''
    Cleans up the registry for a group by removing duplicates and sorting.
    '''
    del_files(group, '^$')


@click.command('list')
def list_groups():
    '''
    List known groups in the registry.
    '''
    g = CONFIG_BASE + '*.txt'
    fns = glob.glob(g)
    for fn in fns:
        print(fn[len(CONFIG_BASE):].split('.')[0])


@click.command()
@click.argument('group')
def del_group(group):
    '''
    Deletes the registry file for a group. Unless that file itself is
    backed up (as is recommended), this cannot be undone.
    '''
    fn = get_file_list_fn(group, need=True)
    os.remove(fn)


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
main.add_command(clean)
main.add_command(del_files)
main.add_command(del_group)
main.add_command(list_groups)
main.add_command(show_files)
main.add_command(pull)


if __name__ == '__main__':
    main()
