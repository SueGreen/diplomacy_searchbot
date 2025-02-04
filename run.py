# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import os
import json
import torch
import numpy as np

from fairdiplomacy.agents import build_agent_from_cfg
from fairdiplomacy.compare_agents import run_1v6_trial, run_1v6_trial_multiprocess
from fairdiplomacy.models.diplomacy_model import train_sl
from fairdiplomacy.situation_check import run_situation_check

import heyhi
import conf.conf_pb2


TASKS = {}


def _register(f):
    TASKS[f.__name__] = f
    return f


@_register
def compare_agents(cfg):

    # NEED TO SET THIS BEFORE CREATING THE AGENT!
    if cfg.seed >= 0:
        logging.info(f"Set seed to {cfg.seed}")
        torch.manual_seed(cfg.seed)
        np.random.seed(cfg.seed)

    agent_one = build_agent_from_cfg(cfg.agent_one)
    agent_six = build_agent_from_cfg(cfg.agent_six)
    if cfg.cf_agent.WhichOneof("agent") is not None:
        cf_agent = build_agent_from_cfg(cfg.cf_agent)
    else:
        cf_agent = None

    def _power_to_string(power_id):
        power_enum = conf.conf_pb2.CompareAgentsTask.Power
        return dict(zip(power_enum.values(), power_enum.keys()))[power_id]

    power_string = _power_to_string(cfg.power_one)

    kwargs = dict(
        start_game=cfg.start_game,
        start_phase=cfg.start_phase,
        max_turns=cfg.max_turns,
        max_year=cfg.max_year,
    )

    if cfg.num_processes > 0:
        assert cfg.num_trials > 0
        result = run_1v6_trial_multiprocess(
            agent_one,
            agent_six,
            power_string,
            save_path=cfg.out if cfg.out else None,
            seed=cfg.seed,
            cf_agent=cf_agent,
            num_processes=cfg.num_processes,
            num_trials=cfg.num_trials,
            use_shared_agent=cfg.use_shared_agent,
            **kwargs,
        )
    else:
        result = run_1v6_trial(
            agent_one,
            agent_six,
            power_string,
            save_path=cfg.out if cfg.out else None,
            seed=cfg.seed,
            cf_agent=cf_agent,
            use_shared_agent=cfg.use_shared_agent,
            **kwargs,
        )
        logging.warning("Result: {}".format(result))


@_register
def train(cfg):
    train_sl.run_with_cfg(cfg)


@_register
def exploit(cfg):
    # Do not load RL stuff by default.
    import fairdiplomacy.selfplay.exploit

    fairdiplomacy.selfplay.exploit.task(cfg)


@_register
def build_db_cache(cfg):
    from fairdiplomacy.data.build_db_cache import build_db_cache_from_cfg

    build_db_cache_from_cfg(cfg)


@_register
def situation_check(cfg):

    # NEED TO SET THIS BEFORE CREATING THE AGENT!
    if cfg.seed >= 0:
        logging.info(f"Set seed to {cfg.seed}")
        torch.manual_seed(cfg.seed)
        np.random.seed(cfg.seed)

    agent = build_agent_from_cfg(cfg.agent)

    if cfg.single_game:
        # Creating fake meta from a single game.
        meta = {"game": {"game_path": cfg.single_game}}
        if cfg.single_phase:
            meta["game"]["phase"] = cfg.single_phase
        logging.info("Created fake test situation JSON: %s", meta)
    else:
        # If not absolute path, assume relative to project root.
        with open(heyhi.PROJ_ROOT / cfg.situation_json) as f:
            meta = json.load(f)

        selection = None
        if cfg.selection != "":
            selection = cfg.selection.split(",")
            meta = {k: v for k, v in meta.items() if k in selection}

    run_situation_check(meta, agent, extra_plausible_orders_str=cfg.extra_plausible_orders)


@_register
def benchmark_agent(cfg):
    import fairdiplomacy.benchmark_agent

    fairdiplomacy.benchmark_agent.run(cfg)


@_register
def compute_xpower_statistics(cfg):
    from fairdiplomacy.get_xpower_supports import compute_xpower_statistics, get_game_paths

    paths = get_game_paths(
        cfg.game_dir,
        metadata_path=cfg.metadata_path,
        metadata_filter=cfg.metadata_filter,
        dataset_for_eval=cfg.dataset_for_eval,
        max_games=cfg.max_games,
    )

    if cfg.cf_agent.WhichOneof("agent") is not None:
        cf_agent = build_agent_from_cfg(cfg.cf_agent)
    else:
        cf_agent = None

    compute_xpower_statistics(paths, max_year=cfg.max_year, cf_agent=cf_agent)


@_register
def profile_model(cfg):
    from fairdiplomacy.profile_model import profile_model

    profile_model(cfg.model_path)


@heyhi.save_result_in_cwd
def main(task, cfg):
    heyhi.setup_logging()
    logging.info("Cwd: %s", os.getcwd())
    logging.info("Task: %s", task)
    logging.info("Cfg:\n%s", cfg)
    heyhi.log_git_status()
    logging.info("Is on slurm: %s", heyhi.is_on_slurm())
    if heyhi.is_on_slurm():
        logging.info("Slurm job id: %s", heyhi.get_slurm_job_id())
    logging.info("Is master: %s", heyhi.is_master())

    if task not in TASKS:
        raise ValueError("Unknown task: %s. Known tasks: %s" % (task, sorted(TASKS)))
    return TASKS[task](cfg)


if __name__ == "__main__":
    heyhi.parse_args_and_maybe_launch(main)
