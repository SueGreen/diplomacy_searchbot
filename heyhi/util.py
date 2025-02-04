# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from os.path import exists
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union
import datetime
import enum
import hashlib
import functools
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import socket
import sys
import time

import torch

from . import conf


ModeType = str

MAX_EXP_LEN = 150
MAX_OVERRIDE_LEN = 100
EXP_ID_PATTERN = "%(prefix)s/%(cfg_folder)s/%(cfg)s/%(redefines)s_%(redefines_hash)s"


MODES: Tuple[ModeType, ...] = (
    "gentle_start",
    "start_restart",
    "start_continue",
    "restart",
    "dryrun",
)
JOBFILE_NAME = "heyhi.jobid"
RESULTFILE_NAME = "result.torch"
DELIMETER = "@"
LOCAL_JOB_ID = "local"
_SLURM_CACHE = {}


def reset_slurm_cache():
    global _SLURM_CACHE
    _SLURM_CACHE.clear()


def get_slurm_master():
    hostnames = subprocess.check_output(
        ["scontrol", "show", "hostnames", os.environ["SLURM_JOB_NODELIST"]]
    )
    return hostnames.split()[0].decode("utf-8")


def requeue_myself():
    job_id = get_slurm_job_id()
    logging.warning("Requeuing job %s", job_id)
    os.system("scontrol requeue %s" % job_id)


def term_handler(signum, frame):
    del frame  # Not used.
    logging.warning("Signal handler called with signal %s", signum)
    logging.warning("Bypassing SIGTERM.")


def setup_logging(
    *,
    console_level=logging.INFO,
    fpath: Optional[Union[str, pathlib.Path]] = None,
    file_level=logging.DEBUG,
) -> logging.Logger:
    """Enable pretty logging and sets the level to DEBUG.

    This function kills all previous handlers.

    Args:
        console_level: if not None, will print to console messages of this
            level.
        fpath: if not None, will dump all logs to this file.
    """
    logging.addLevelName(logging.DEBUG, "D")
    logging.addLevelName(logging.INFO, "I")
    logging.addLevelName(logging.WARNING, "W")
    logging.addLevelName(logging.ERROR, "E")
    logging.addLevelName(logging.CRITICAL, "C")

    formatter = logging.Formatter(
        fmt=("%(levelname)s%(asctime)s" " [%(module)s:%(lineno)d] %(message)s"),
        datefmt="%m%d %H:%M:%S",
    )

    logger = logging.getLogger()
    for x in logger.handlers[:]:
        logger.removeHandler(x)
    for x in logger.filters[:]:
        logger.removeFilter(x)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if console_level is not None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if fpath is not None:
        file_handler = logging.FileHandler(fpath)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_git_status():
    git_repo = str(pathlib.Path(__file__).resolve().parent.parent)
    try:
        rev = subprocess.check_output("git rev-parse HEAD".split(), cwd=git_repo)
    except subprocess.CalledProcessError as e:
        logging.error("Attempt to call 'git rev-parse HEAD' failed: %s", e)
    else:
        logging.info("Git revision: %s", rev.decode("utf8").strip())
    try:
        diff = subprocess.check_output("git diff HEAD".split(), cwd=git_repo)
    except subprocess.CalledProcessError as e:
        logging.error("Failed to call 'git diff HEAD': %s", e)
    else:
        if diff:
            diff_path = pathlib.Path("workdir.diff").resolve()
            if is_master():
                logging.info("Found unsubmitited diff. Saving to %s", diff_path)
                with diff_path.open("w") as stream:
                    stream.write(diff.decode("utf8"))
            else:
                logging.info("Found unsubmitited diff. NOT saving as not the master.")
        else:
            logging.info("No diff in the working copy")


def _get_all_runing_job_ids(user_only: bool = False) -> FrozenSet[str]:
    if "CIRCLECI" in os.environ:
        return frozenset([])
    cmd = ["squeue", "-r", "-h", "-o", "%i"]
    if user_only:
        cmd.extend(["-u", os.environ["USER"]])
    output = subprocess.check_output(cmd)
    job_ids = output.decode("utf8").split()
    return frozenset(job_ids)


def get_all_runing_job_ids() -> FrozenSet[str]:
    global _SLURM_CACHE
    if "job_list" not in _SLURM_CACHE:
        _SLURM_CACHE["job_list"] = _get_all_runing_job_ids()
    return _SLURM_CACHE["job_list"]


class Status(enum.IntEnum):
    NOT_STARTED = 1
    DONE = 2
    DEAD = 3
    RUNNING = 4


def is_on_slurm() -> bool:
    return "SLURM_PROCID" in os.environ


def get_slurm_job_id() -> Optional[str]:
    if "SLURM_ARRAY_JOB_ID" in os.environ:
        return "%s_%s" % (os.environ["SLURM_ARRAY_JOB_ID"], os.environ["SLURM_ARRAY_TASK_ID"])
    else:
        return os.environ["SLURM_JOB_ID"]


def is_master() -> bool:
    return os.environ.get("SLURM_PROCID", "0") == "0"


class ExperimentDir:
    """ExperimentDir allows to acess to experiment status via file system

    The assumptions are:
        * User calls save_job_id() with slurm job id corrsponding to the run.
        * Once job is done, user writes something in result_path.

    Given that, the class allows to get the status of the job and the results.
    ."""

    def __init__(self, exp_path: pathlib.Path, exp_id=""):
        self.exp_path = exp_path
        self.exp_id = exp_id

    @property
    def job_id_path(self) -> pathlib.Path:
        return self.exp_path / JOBFILE_NAME

    @property
    def result_path(self) -> pathlib.Path:
        return self.exp_path / RESULTFILE_NAME

    def maybe_get_job_id(self) -> Optional[str]:
        if self.job_id_path.exists():
            with self.job_id_path.open() as stream:
                return stream.read().strip()
        return None

    def save_job_id(self, job_id: str) -> None:
        self.job_id_path.parent.mkdir(exist_ok=True, parents=True)
        with self.job_id_path.open("w") as stream:
            print(job_id, file=stream)

    def get_status(self) -> Status:
        if not self.job_id_path.exists():
            if self.exp_path.exists():
                logging.warning("Experiment folder without job_id file: %s", self.exp_path)
            return Status.NOT_STARTED
        maybe_jobid = self.maybe_get_job_id()
        if (
            maybe_jobid is not None
            and maybe_jobid != LOCAL_JOB_ID
            and maybe_jobid in get_all_runing_job_ids()
        ):
            return Status.RUNNING
        if self.result_path.exists():
            return Status.DONE
        return Status.DEAD

    def is_done(self) -> bool:
        return self.get_status() == Status.DONE

    def is_started(self) -> bool:
        return self.get_status() != Status.NOT_STARTED

    def is_running(self) -> bool:
        return self.get_status() == Status.RUNNING

    def kill_and_prune(self, silent: bool) -> None:
        if not self.exp_path.exists():
            return
        logging.info("Prune+kill for %s", self.exp_path)
        if not silent:
            print("Deleting the folder in 3 seconds", end="", flush=True)
            for _ in range(3):
                print(".", end="", flush=True)
                time.sleep(1)
        maybe_jobid = self.maybe_get_job_id()
        if maybe_jobid is not None and maybe_jobid != LOCAL_JOB_ID:
            if not silent:
                print("killing job", maybe_jobid, "...", end="", flush=True)
            subprocess.check_call(["scancel", str(maybe_jobid)])
        if not silent:
            print(" purging the log dir", "...", end="", flush=True)
        shutil.rmtree(str(self.exp_path))
        if not silent:
            print("done")

    @property
    def slurm_path(self) -> pathlib.Path:
        return self.exp_path / "slurm"


def save_result_in_cwd(f):
    """Save results of the function to a results.torch file in cwd."""

    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        result = f(*args, **kwargs)
        result_path = os.path.join(os.getcwd(), RESULTFILE_NAME)
        if is_master():
            logging.info("Saving result to %s", result_path)
            torch.save(result, result_path)
        return result

    return wrapped


def _get_config_folder_tag(path: pathlib.Path) -> str:
    assert path.exists(), path
    config_path = str(path.parent.absolute())
    if config_path.startswith(str(conf.CONF_ROOT)):
        components = config_path[len(str(conf.CONF_ROOT)) :].strip("/").split("/")
        if components:
            folder = "_".join(components)
        else:
            folder = "root"
    else:
        # Config outside default location. Take a hash of the path.
        folder = hashlib.md5(config_path.encode("utf8")).hexdigest()[:8]
    return folder


def _get_overrides_tags(overrides: Sequence[str]) -> Tuple[str, str]:
    def sort_key(override):
        name = override.split("=")[0]
        depth = len(name.split("."))
        return (depth, name, override)

    overrides = sorted(overrides, key=sort_key)
    hashtag = hashlib.md5(repr(overrides).encode("utf8")).hexdigest()[:8]
    parsed_overrides = []
    for override in overrides:
        key, value = override.split("=", 1)
        if len(key) > 5:
            # Compress key.
            key = ".".join(x[:3] for x in key.split("."))
        if "/" in value:
            # For paths, use the last 2 components.
            value = "_".join(value.split("/")[-2:])
        value = value.replace(" ", "_")
        parsed_overrides.append(f"{key}{DELIMETER}{value}")
    override_tag = DELIMETER.join(parsed_overrides)[:MAX_OVERRIDE_LEN]
    if not override_tag:
        override_tag = "default"
    return override_tag, hashtag


def get_exp_id(
    config_path: pathlib.Path,
    overrides: Sequence[str],
    adhoc: bool,
    exp_id_pattern: Optional[str] = None,
) -> str:
    if exp_id_pattern is None:
        exp_id_pattern = EXP_ID_PATTERN
    folder_tag = _get_config_folder_tag(config_path)
    cfg_tag = config_path.name.rsplit(".", 1)[0]
    date_tag = datetime.datetime.now().isoformat().replace(":", "")
    if adhoc:
        prefix_tag = "adhoc/" + date_tag
    else:
        prefix_tag = "p"

    redefines_tag, redefines_hash_tag = _get_overrides_tags(overrides)
    tags = dict(
        cfg=cfg_tag,
        cfg_folder=folder_tag,
        date=date_tag,
        prefix=prefix_tag,
        redefines=redefines_tag,
        redefines_hash=redefines_hash_tag,
    )
    return exp_id_pattern % tags


def handle_dst(exp_handle, mode: ModeType, force: bool = False) -> bool:
    """Creates/recreates a ExperimentDir and checks whether an action is needed.

    If mode is:
        - gentle_start, set need_run iff the job is NOT_STARTED.
        - start_restart, set need_run iff the job is NOT_STARTED or
          DEAD. If job is dead, wipe the experiment dir.
        - start_continue, set need_run iff the job is NOT_STARTED or
          DEAD.
        - restart, set need_run to True, and kill the job if running.
        - dryrun, set need_run to False.

    If force is True, no warning are issues before wiping.

    Returns a pair (ExperimentDir, need_run).
    """
    need_run = True
    if mode == "gentle_start":
        if exp_handle.is_started():
            logging.info("Alredy started. Status: %s", exp_handle.get_status())
            need_run = False
    elif mode == "start_restart":
        if exp_handle.is_running() or exp_handle.is_done():
            logging.info("Running or done. Status: %s", exp_handle.get_status())
            need_run = False
        elif not exp_handle.is_done():
            exp_handle.kill_and_prune(silent=force)
    elif mode == "start_continue":
        if exp_handle.is_running() or exp_handle.is_done():
            logging.info("Running or done. Status: %s", exp_handle.get_status())
            need_run = False
    elif mode == "restart":
        exp_handle.kill_and_prune(silent=force)
    elif mode == "dryrun":
        logging.info("Dry run, not starting anything")
        need_run = False
    else:
        raise ValueError(f"Unknown mode: {mode}")
    return need_run


def run_with_config(
    task_function: Callable,
    exp_handle: ExperimentDir,
    config_path: pathlib.Path,
    overrides: Sequence[str],
) -> None:
    setup_logging()
    task, meta_cfg = conf.load_cfg(config_path, overrides)
    cfg = getattr(meta_cfg, task)

    # Managed to read config. Cd into exp dir.
    exp_handle.exp_path.mkdir(exist_ok=True, parents=True)
    old_cwd = os.getcwd()
    os.chdir(exp_handle.exp_path)

    if hasattr(cfg, "launcher"):
        if not cfg.launcher.WhichOneof("launcher"):
            cfg.launcher.local.use_local = True
        launcher_type = cfg.launcher.WhichOneof("launcher")
        launcher_cfg = getattr(cfg.launcher, launcher_type)
    else:
        launcher_type = "local"
        launcher_cfg = None
    assert launcher_type in ("local", "slurm"), launcher_type

    conf.save_config(meta_cfg, pathlib.Path("config_meta.prototxt"))
    conf.save_config(cfg, pathlib.Path("config.prototxt"))

    callable = functools.partial(task_function, cfg=cfg, task=task)
    if launcher_type == "slurm" and not is_on_slurm():
        raise NotImplementedError()
    else:
        exp_handle.save_job_id(LOCAL_JOB_ID)
        callable()
    os.chdir(old_cwd)
