#! /bin/python3
"""
Dead simple backup functionality.
"""
from __future__ import annotations
import configparser as ini
import os
import os.path as osp
import re
import string
import sys
import tarfile
import tempfile as tmp
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from functools import cached_property, lru_cache
from glob import iglob
from heapq import merge
from itertools import groupby
from pathlib import Path
from shutil import copy as fcopy, rmtree
from traceback import format_stack
from typing import (
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    NoReturn,
    Optional,
    Set,
    Tuple,
)

import click
from click import Choice, echo

DIE_CODE = -1


def die(msg) -> NoReturn:
    echo(msg, file=sys.stderr)
    sys.exit(DIE_CODE)


def soft_assert(expr: Any, msg: str) -> None:
    if not bool(expr):
        die(
            "The program has encountered an invalid state. "
            "Please report the following trace as an issue at "
            "https://github.com/qdbp/py9backup\nMessage:\n"
            + msg
            + "\n"
            + format_stack()
        )


ALLOWABLE_CHARS = set(string.ascii_letters) | set(string.digits) | {"_"}
CONFIG_DIR = Path("~/.config/py9backup/").expanduser()


def load_settings() -> ini.ConfigParser:
    settings_fn = CONFIG_DIR.joinpath("settings.ini")
    settings_fn.touch()

    parser = ini.ConfigParser()
    parser.read(str(settings_fn))

    return parser


@lru_cache(maxsize=1 << 10)
def is_glob(segment: str):
    return bool(re.search(r"(?<!\\)\*", segment))


def is_fs_ancestor(candidate: str, path: str):
    """
    Checks whether a candidate is a file system prefix of path.

    A file system prefix is either the file itself, or an
    Args:
        candidate:
        path:

    Returns:
        True iff candidate is an ancestor of 'path' (including if candidate
        == path)

    """

    candidate = candidate.rstrip("/")
    path = path.rstrip("/")

    return osp.commonpath((candidate, path)) == candidate


def calc_raw_path_priority(path: str) -> int:
    """
    Calculates the relative_priority of a glob or str.

    Returns:
         the number of non-glob str segments in the str.

    Args:
        path: str or glob

    """
    if path == "/":
        return 0
    segments = path.strip("/").split("/")
    return sum([1 - int(is_glob(s)) for s in segments])


@dataclass(frozen=True, order=False)
class ReducedPath:
    """
    Class representing simple file-system paths, with additional metadata.

    These cannot be globes (hence 'reduced'). These can, however, be either
    "included" or "exluded", the semantics of which is determined by the path
    enumeration algorithm.

    For the same reason, ReducedPaths also have a relative_priority
    corresponding to their specificity.

    """

    path: str
    # this is a relative priority value -- if this path was expanded from a glob
    # its net priority will be lower than that of a path that was fully
    # specified initially for the purposes of resolving the set of paths from
    # multiple RichPaths.
    _rel_prio: Optional[int]
    excl: bool

    @cached_property
    def depth(self) -> int:
        return self.path.strip("/").count("/")

    @cached_property
    def rel_prio(self) -> int:
        return self._rel_prio if self._rel_prio is not None else self.depth

    @cached_property
    def priority(self) -> Tuple[int, str, int]:
        return self.depth, self.path, self.rel_prio


class RichPath:
    """
    A str or collection of paths with some metadata related to backup
    functionality.
    """

    _FLAGS_LEN = 5
    _FLAGS = [
        (0, "x", "exclude"),
        (1, "?", "sticky"),
        (2, "g", "is_glob"),
    ]

    @classmethod
    def parse(cls, raw_line: str) -> RichPath:
        flags = raw_line[: cls._FLAGS_LEN]
        path = raw_line[cls._FLAGS_LEN + 1 :].strip()

        flag_dict = {}
        for ix, char, kwarg in cls._FLAGS:
            if flags[ix] == char:
                flag_dict[kwarg] = True

        return cls(path, **flag_dict)

    @property
    def str(self) -> str:
        if self.is_glob:
            raise ValueError("A glob-type RichPath has no single str")
        else:
            return self.raw_entry

    def iter_reduced(
        self,
    ) -> Generator[ReducedPath, None, None]:
        """
        Generates the reduced paths of this RP according to their net priority.

        Priority is defined by these two rules:
            - specific beats general
            - files beat directories

        Yields:
            ReducePaths, ordered by their net relative_priority.
            suitable for inclusion in order.
        """

        raw = self.raw_entry

        # if the segment is not a glob, the reduced path is just the raw path
        # with the rp's exclude value.
        if not is_glob(raw):
            yield ReducedPath(raw, None, self.exclude)
            return
        # otherwise, there are multiple reduced paths at play
        else:
            rel_prio = calc_raw_path_priority(raw)
            yield from sorted(
                [
                    ReducedPath(expanded, rel_prio, self.exclude)
                    for expanded in iglob(raw, recursive=True)
                ],
                key=lambda reduced: reduced.priority,
            )

    def __init__(
        self, path_str: str, *, exclude=False, sticky=False, is_glob=False
    ) -> None:

        """
        Args:
            path_str:
                the file path on disk this rp maps to
            exclude:
                mark the file or str as being excluded rather than included.
            sticky:
                mark the file or str to persist in the database even if it
                does not exist.
            is_glob:
                mark the str as a glob to be expanded at gather time.
        """

        self.exclude = exclude
        self.sticky = sticky
        self.is_glob = is_glob

        self.raw_entry = Path(path_str).expanduser().absolute().as_posix()

    def __eq__(self, other: RichPath) -> bool:
        return self.raw_entry == other.raw_entry

    def __lt__(self, other: RichPath) -> bool:
        if self.is_glob ^ other.is_glob:
            return self.is_glob
        else:
            return self.raw_entry < other.raw_entry

    def __str__(self) -> str:
        base = [" "] * (self._FLAGS_LEN + 1)
        for ix, char, kwarg in self._FLAGS:
            if getattr(self, kwarg):
                base[ix] = char

        return "".join(base) + str(self.raw_entry)

    # include only the str in the hash, so that the same str with
    # different flags will intentionally collide in sets/dicts. This
    # makes overwriting the flags easy and enforces a uniqueness invariant.
    def __hash__(self) -> int:
        return hash(self.raw_entry)

    @staticmethod
    def reduce_many(
        rps: Iterable[RichPath],
    ) -> Generator[ReducedPath, None, None]:
        """
        Reduces multiple RichPaths into a sequence of ReducedPaths, ordered
        by priority, defined as
            (depth, raw_path, rel_prio).

        In the case of multiple ReducedPath instances with the same fs path,
        the one with the highest [internal_prio] is taken. This means that,
        for instance, if both
            /home/foo/*/baz
        and /home/foo/important/baz

        are specified independently, then /home/foo/important/baz will appear
        in the output only once with an internal priority of 4
        (i.e. 4 non-glob segments)
        """

        by_prio: Iterable[ReducedPath] = merge(
            *(rp.iter_reduced() for rp in rps),
            key=lambda reduced: reduced.priority,
        )

        for _, group in groupby(by_prio, key=lambda reduced: reduced.path):
            yield sorted(group, key=lambda reduced: reduced.rel_prio)[-1]


def canonicalize_group_name(group: str) -> str:
    group = group.lower()
    if set(group) - ALLOWABLE_CHARS:
        die("Illegal characters in group name.")
    return group


def get_backup_fp(fp: Path) -> Path:
    return Path(fp.parent).joinpath("." + fp.stem + ".bkp")


def get_group_manifest_file(
    group: str, need_exist=True, check_backup=True
) -> Path:
    """
    Get the path of the file storing the backup manifest for the group.

    Arguments:
        group: group for which to get manifest file
        need_exist: if True, dies if manifest does not exist.
        check_backup: if we would die because of need_*s, check backup first.
            If a backup file exists, prompt to restore.
    """

    group = canonicalize_group_name(group)

    fp = CONFIG_DIR.joinpath(f"{group}.txt")
    fp_bkp = get_backup_fp(fp)

    # conditions not met
    if not fp.exists():
        if check_backup and fp_bkp.exists():
            msg = (
                f'The group "{group}" was not found, but a backup file '
                + "was. Would you like to restore the backup file?"
            )

            if click.confirm(msg, default=True):
                fcopy(fp_bkp.as_posix(), fp.as_posix())
                return fp

        elif need_exist:
            die(f'Group "{group}" does not exist.')

    return fp


def get_group_rps(
    group: str,
    need_exist=False,
) -> List[RichPath]:
    """
    Get the list of rich paths corresponding to a group. This is distinct from
    getting the complete list of actual file paths on disk.

    Args:
        group: the name of the group to read
        need_exist: whether it is an error if the group does not exist.

    Returns:
        A list of RichPath objects stored in the group. This is empty if the
        group does not and need not exist.

    """

    fp = get_group_manifest_file(group, need_exist=need_exist)

    if not fp.exists():
        return []
    else:
        with fp.open("r") as f:
            return [RichPath.parse(line) for line in f]


def commit_group_rps(group: str, rps: Iterable[RichPath]) -> None:
    """
    Atomically commit the passed rps as the new contents of the group file.

    Does NOT append or merge, that is the responsibility of the caller.

    Does not write empty files - empty rps is a noop.
    """

    fp = get_group_manifest_file(group, need_exist=False, check_backup=False)
    fp_bkp = get_backup_fp(fp)

    # if the file already exists, back it up
    if fp.exists() and fp.stat().st_size > 0:
        fcopy(str(fp), str(fp_bkp))

    with tmp.NamedTemporaryFile(mode="w") as tf:
        for rp in sorted(set(rps)):
            if rp.is_glob or rp.sticky or osp.exists(rp.str):
                tf.write(str(rp) + "\n")

        tf.flush()
        fcopy(tf.name, str(fp))

    # if a backup does not exist, initialize it to the fresh file contents
    if not fp_bkp.exists() and fp.stat().st_size > 0:
        fcopy(str(fp), str(fp_bkp))


def gather_effective_files(rps: Iterable[RichPath]) -> List[str]:
    """
    Resolves a collection of rich paths paths to a minimal collection
    of include-only paths.
    """

    # sort all paths by depth
    effective_by_depth: Dict[int, Set[str]] = defaultdict(set)

    rdp: ReducedPath
    for rdp in RichPath.reduce_many(rps):

        # exclusions force explicit expansion in each level above them
        if rdp.excl:
            # so for each depth starting from the root, we find the entry at
            # that depth that is a prefix of the path we are excluding and
            # we split it up. Then we recurse.
            for dx in range(rdp.depth):

                for candidate_prefix in effective_by_depth[dx]:

                    # non-prefixes of the excluded path are ignored
                    if not is_fs_ancestor(candidate_prefix, rdp.path):
                        continue

                    # remove higher-level str in favour of fragments
                    effective_by_depth[dx].remove(candidate_prefix)
                    effective_by_depth[dx + 1] |= {
                        str(x) for x in Path(candidate_prefix).iterdir()
                    }
                    break

            # exclude actual exclusion
            effective_by_depth[rdp.depth].discard(rdp.path)

        else:
            # exclude redundant paths
            # this only works because of depth sorting in outer loop!
            for dx in range(rdp.depth):
                if any(
                    [
                        is_fs_ancestor(p, rdp.path)
                        for p in effective_by_depth[dx]
                    ]
                ):
                    break
            else:
                effective_by_depth[rdp.depth].add(rdp.path)

    out: Set[str] = set()
    for fns in effective_by_depth.values():
        out |= fns

    return sorted(out)


# # # COMMANDS SECTION


@click.group()
def main() -> None:
    """
    Tracks files to be backed up, on a per-group basis.

    The first argument is always the group name, which can be an arbitrary well
    behaving string which serves as an identifier for the collection of paths
    added. This allows different backup flow for different files.
    """


@main.command(name="add")
@click.argument("group", nargs=1)
@click.argument("paths", nargs=-1)
@click.option(
    "--allow-nx",
    is_flag=True,
    default=False,
    help=(
        "Allows nonexistent entries and persists them until explicit "
        "deletion. Otherwise, nonexistent entries are dropped."
    ),
)
@click.option(
    "--exclude",
    is_flag=True,
    default=False,
    help=(
        "Excludes the file or str. Overrides more general inclusions. "
        "Overriden by more specific inclusions."
    ),
)
@click.option(
    "--glob/--no-glob",
    is_flag=True,
    default=None,
    help=(
        "If true, the str will be interpreted as a glob. If false, it "
        "will be interpreted literally. If unset, the str will be "
        'treated as a glob iff it contains an unescaped "*".'
    ),
)
def add_files(group: str, paths: Iterable[str], *, exclude, allow_nx, glob):
    """
    Adds a str to be tracked under a group.
    """
    group = canonicalize_group_name(group)
    rps = set(get_group_rps(group))

    new_rps = set()
    for path in paths:

        if glob is None:
            glob = is_glob(path)

        if not (allow_nx or osp.isfile(path) or osp.isdir(path) or glob):
            echo(
                f'Path "{path}" does not exist. Ignoring. '
                "Pass --allow-nx to force persistent inclusion."
            )
            continue

        new_rp = RichPath(path, exclude=exclude, sticky=allow_nx, is_glob=glob)
        new_rps.add(new_rp)

    # order is important, we need to favour the new rps in hash conflicts
    rps = new_rps | rps
    commit_group_rps(group, rps)


@main.command("show")
@click.argument("group")
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="show every included file explicitly",
)
def show_files(group: str, *, full) -> None:
    """
    Shows all tracked paths for group.
    """
    rps = get_group_rps(group, need_exist=True)
    if not full:
        for rp in rps:
            echo(" " + str(rp))
    else:
        for fn in gather_effective_files(rps):
            echo("\t" + fn)


@main.command(name="del")
@click.argument("group")
@click.argument("regex")
def del_files(group: str, regex: str):
    """
    Removes files from group by regex.

    Python's `re.match` is used, so the regexp should match from the
    beginning of the str. Use ^.* or similar.
    """

    if regex == "." and click.confirm(
        '"del ." will delete every file in the group. Did you intend to '
        "delete the current directory instead?",
        default=True,
    ):
        regex = os.getcwd()
    elif regex == ".." and click.confirm(
        '"del .." will likely delete every file in the group. Did you'
        "intend to delete the current directory instead?",
        default=True,
    ):
        regex = str(Path(os.getcwd()).parent)

    regex.rstrip("/")

    path_reg = re.compile(regex)
    rps = get_group_rps(group)

    keep_rps = [rp for rp in rps if not path_reg.search(str(rp))]
    commit_group_rps(group, keep_rps)


@main.command()
@click.argument("group")
@click.argument("commands", nargs=-1)
@click.option(
    "--no-xz",
    default=False,
    is_flag=True,
    help="turn off compression",
)
@click.option(
    "--compalgo",
    default="gz",
    help="compression algorithm to use",
    type=Choice(["xz", "bz2", "gz"], case_sensitive=False),
)
@click.option("--name", default=None, help="name to use for the tarball")
def pull(group, commands, *, no_xz, name, compalgo: str) -> None:
    """
    Pulls files into tarball, runs given commands on it.

    First, a tarball containing all of the files in the group is created
    in a temporary directory. It is given a sensible default name, which
    can be overriden with the --name option.

    This action accepts any number of positional parameters, each of which
    is interpreted as a shell command to run. Within these commands, the
    string "{}" is expanded to the name of the newly-created tar file.

    After the commands have been executed, the tarfile is deleted.
    """

    if name is None:
        name = f"backup_{group}_{date.today().isoformat()}"

    tar_mode = "w" if no_xz else f"w:{compalgo}"
    suf = "tar" if no_xz else f"tar.{compalgo}"

    file_paths = gather_effective_files(get_group_rps(group, need_exist=True))

    if len(file_paths) == 0 and not click.confirm(
        f"Group {group} is empty. Continue?", default=False
    ):
        return

    temp_dir = tmp.mkdtemp()
    tar_fn = osp.join(temp_dir, f"{name}.{suf}")

    with tarfile.open(tar_fn, tar_mode) as tar:
        for path in file_paths:
            try:
                tar.add(path.strip())
            except FileNotFoundError:
                echo(f"File {path} not found, skipping.", file=sys.stderr)
            except PermissionError:
                die(f"File {path} needs elevated permissions. Dying.")

    if not commands:
        settings = load_settings()
        try:
            commands = [settings["py9backup"]["default_pull_command"]]
            click.echo(
                "Running default pull commands:\n\t" + "\n\t".join(commands)
            )
        except KeyError:
            commands = []

    for com in commands:
        com = re.sub(r"{}", tar_fn, com)
        os.system(com)

    rmtree(temp_dir, ignore_errors=True)


@main.command("list")
def list_groups() -> None:
    """
    List known groups.
    """
    for path in sorted(Path(CONFIG_DIR).glob("*.txt")):
        echo(path.stem)


@main.command()
@click.argument("group")
@click.option("--drop-backup", default=False, help="also forgets the backup")
def forget(group: str, drop_backup: bool):
    """
    Deletes the registry file for a group. Unless that file itself is
    backed up (as is recommended), this cannot be undone.
    """

    def render_lines(lines: List[str]) -> str:
        if len(lines) < 3:
            out = "\n".join(lines)
        else:
            out = "\n".join([lines[0], "...", lines[-1]])
        return "\n" + out + "\n"

    group_path = get_group_manifest_file(group, need_exist=True)
    backup_file = get_backup_fp(group_path)

    prompted = False
    for file_in_question in [group_path] + (
        [backup_file] if drop_backup else []
    ):

        if not file_in_question.exists():
            continue

        with file_in_question.open("r") as f:
            lines = f.readlines()

        backup_str = "(backup file)" if file_in_question == backup_file else ""
        if not prompted:
            if lines and not click.confirm(
                f"Confirm deletion of group {group} {backup_str} containing "
                + f"{len(lines)} lines"
                + render_lines(lines),
                default=False,
            ):
                return
            prompted = True

        file_in_question.unlink()


@main.command()
@click.argument("group", type=str)
@click.argument("new_name", type=str)
@click.pass_context
def rename(ctx, group: str, new_name: str):

    group = canonicalize_group_name(group)
    fp = get_group_manifest_file(group, need_exist=False)

    if not fp.exists():
        die('Group "{group}" does not exist')

    new_name = canonicalize_group_name(new_name)
    new_fp = get_group_manifest_file(new_name, need_exist=False)

    if not new_fp.exists() or click.confirm(
        f'Group name "{new_name}" already exists. Overwrite? Warning: '
        f'overwrite will also destroy backup for "{new_name}"',
        default=False,
    ):

        ctx.invoke(forget, group=new_name, drop_backup=True)

        get_backup_fp(fp).rename(get_backup_fp(new_fp))
        fp.rename(new_fp)


if __name__ == "__main__":
    # noinspection PyBroadException
    try:
        CONFIG_DIR.mkdir(exist_ok=True)
    except Exception:
        die(f"Unable to create config directory at {CONFIG_DIR}")

    # noinspection PyBroadException
    try:
        main()
    except Exception:
        from traceback import format_exc

        die(
            "The program has encountered an invalid state.\n"
            "Please report the following trace as an issue at "
            "https://github.com/qdbp/py9backup:\n"
            + "\n"
            + format_exc()
            + "\nI apologize for the inconvenience."
        )
