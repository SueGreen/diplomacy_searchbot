# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import json
import logging
import os
from itertools import combinations, product
from typing import Any, Dict, Union, List, Optional, Sequence, Tuple

import joblib
import torch
import numpy as np

from fairdiplomacy.data.data_fields import DataFields
from fairdiplomacy.pydipcc import Game
from fairdiplomacy.models.consts import SEASONS, POWERS, MAX_SEQ_LEN, LOCS
from fairdiplomacy.models.diplomacy_model.order_vocabulary import EOS_IDX
from fairdiplomacy.utils.game_scoring import compute_game_scores, compute_game_scores_from_state
from fairdiplomacy.utils.sampling import sample_p_dict
from fairdiplomacy.utils.tensorlist import TensorList
from fairdiplomacy.utils.thread_pool_encoding import FeatureEncoder
from fairdiplomacy.utils.cat_pad_sequences import cat_pad_sequences
from fairdiplomacy.utils.order_idxs import (
    action_strs_to_global_idxs,
    ORDER_VOCABULARY_TO_IDX,
    ORDER_VOCABULARY,
    MAX_VALID_LEN,
)

LOC_IDX = {loc: idx for idx, loc in enumerate(LOCS)}


class Dataset(torch.utils.data.Dataset):
    def __init__(
        self,
        *,
        game_ids: List[Union[int, str]],
        data_dir: str,
        game_metadata: Dict[Union[int, str], Any],
        debug_only_opening_phase=False,
        only_with_min_final_score=7,
        num_dataloader_workers=20,
        value_decay_alpha=1.0,
        cf_agent=None,
        n_cf_agent_samples=1,
        min_rating=None,
        exclude_n_holds=-1,
    ):
        self.game_ids = game_ids
        self.data_dir = data_dir
        self.game_metadata = game_metadata
        self.debug_only_opening_phase = debug_only_opening_phase
        self.only_with_min_final_score = only_with_min_final_score
        self.n_jobs = num_dataloader_workers
        self.value_decay_alpha = value_decay_alpha
        self.cf_agent = cf_agent
        self.n_cf_agent_samples = n_cf_agent_samples
        self.min_rating = min_rating
        self.exclude_n_holds = exclude_n_holds
        # Pre-processing populates these fields
        self.game_idxs = None
        self.phase_idxs = None
        self.power_idxs = None
        self.x_idxs = None
        self.encoded_games = None
        self.num_games = None
        self.num_phases = None
        self.num_elements = None
        self._preprocessed = False

    @property
    def preprocessed(self):
        return self._preprocessed

    def mp_encode_games(self) -> List[Tuple]:
        encoded_game_tuples = joblib.Parallel(n_jobs=self.n_jobs)(
            joblib.delayed(encode_game)(
                game_id=game_id,
                data_dir=self.data_dir,
                only_with_min_final_score=self.only_with_min_final_score,
                cf_agent=self.cf_agent,
                n_cf_agent_samples=self.n_cf_agent_samples,
                value_decay_alpha=self.value_decay_alpha,
                input_valid_power_idxs=self.get_valid_power_idxs(game_id),
                game_metadata=self.game_metadata[game_id],
                exclude_n_holds=self.exclude_n_holds,
            )
            for game_id in self.game_ids
        )

        return encoded_game_tuples

    def preprocess(self):
        """
        Pre-processes dataset
        :return:
        """
        assert not self.debug_only_opening_phase, "FIXME"

        logging.info(
            f"Building dataset from {len(self.game_ids)} games, "
            f"only_with_min_final_score={self.only_with_min_final_score} "
            f"value_decay_alpha={self.value_decay_alpha} cf_agent={self.cf_agent}"
        )

        torch.set_num_threads(1)
        encoder = FeatureEncoder()
        encoded_game_tuples = self.mp_encode_games()

        encoded_games = [
            g for (_, g) in encoded_game_tuples if g is not None
        ]  # remove "empty" games (e.g. json didn't exist)

        logging.info(f"Found data for {len(encoded_games)} / {len(self.game_ids)} games")

        encoded_games = [g for g in encoded_games if g["valid_power_idxs"][0].any()]
        logging.info(f"{len(encoded_games)} games had data for at least one power")

        # Update game_ids
        self.game_ids = [
            g_id
            for (g_id, g) in encoded_game_tuples
            if g_id is not None and g["valid_power_idxs"][0].any()
        ]

        game_idxs, phase_idxs, power_idxs, x_idxs = [], [], [], []
        x_idx = 0
        for game_idx, encoded_game in enumerate(encoded_games):
            for phase_idx, valid_power_idxs in enumerate(encoded_game["valid_power_idxs"]):
                assert valid_power_idxs.nelement() == len(POWERS), (
                    encoded_game["valid_power_idxs"].shape,
                    valid_power_idxs.shape,
                )
                for power_idx in valid_power_idxs.nonzero()[:, 0]:
                    game_idxs.append(game_idx)
                    phase_idxs.append(phase_idx)
                    power_idxs.append(power_idx)
                    x_idxs.append(x_idx)
                x_idx += 1

        self.game_idxs = torch.tensor(game_idxs, dtype=torch.long)
        self.phase_idxs = torch.tensor(phase_idxs, dtype=torch.long)
        self.power_idxs = torch.tensor(power_idxs, dtype=torch.long)
        self.x_idxs = torch.tensor(x_idxs, dtype=torch.long)

        # now collate the data into giant tensors!
        self.encoded_games = DataFields.cat(encoded_games)

        self.num_games = len(encoded_games)
        self.num_phases = len(self.encoded_games["x_board_state"]) if self.encoded_games else 0
        self.num_elements = len(self.x_idxs)

        self.validate_dataset()

        self._preprocessed = True

    def validate_dataset(self):
        for i, e in enumerate(self.encoded_games.values()):
            if isinstance(e, TensorList):
                assert len(e) == self.num_phases * len(POWERS) * MAX_SEQ_LEN
            else:
                assert len(e) == self.num_phases

    def stats_str(self):
        return f"Dataset: {self.num_games} games, {self.num_phases} phases, and {self.num_elements} elements."

    def get_valid_power_idxs(self, game_id):
        return [
            self.game_metadata[game_id][pwr]["logit_rating"] >= self.min_rating for pwr in POWERS
        ]

    def __getitem__(self, idx: Union[int, torch.Tensor]):
        assert self._preprocessed, "Dataset has not been pre-processed."

        if isinstance(idx, int):
            idx = torch.tensor([idx], dtype=torch.long)

        assert isinstance(idx, torch.Tensor) and idx.dtype == torch.long
        assert idx.max() < len(self)

        sample_idxs = idx % self.n_cf_agent_samples
        idx //= self.n_cf_agent_samples

        x_idx = self.x_idxs[idx]
        power_idx = self.power_idxs[idx]

        fields = self.encoded_games.select(x_idx)  # [x[x_idx] for x in self.encoded_games[:-1]]

        # unpack the possible_actions
        possible_actions_idx = ((x_idx * len(POWERS) + power_idx) * MAX_SEQ_LEN).unsqueeze(
            1
        ) + torch.arange(MAX_SEQ_LEN).unsqueeze(0)
        x_possible_actions = self.encoded_games["x_possible_actions"][
            possible_actions_idx.view(-1)
        ]
        x_possible_actions_padded = x_possible_actions.to_padded(
            total_length=MAX_VALID_LEN, padding_value=EOS_IDX
        )
        fields["x_possible_actions"] = x_possible_actions_padded.view(
            len(idx), MAX_SEQ_LEN, MAX_VALID_LEN
        )

        # for these fields we need to select out the correct power
        for f in ("x_power", "x_loc_idxs", "y_actions"):
            fields[f] = fields[f][torch.arange(len(fields[f])), power_idx]

        # for y_actions, select out the correct power
        fields["y_actions"] = (
            fields["y_actions"]
            .gather(1, sample_idxs.view(-1, 1, 1).repeat((1, 1, fields["y_actions"].shape[2])))
            .squeeze(1)
        )

        # cast fields
        for k in fields:
            if isinstance(fields[k], torch.Tensor):
                if k in ("x_possible_actions", "y_actions", "x_prev_orders"):
                    fields[k] = fields[k].to(torch.long)
                elif k != "prev_orders":
                    fields[k] = fields[k].to(torch.float32)

        return fields.from_storage_fmt_()

    def __len__(self):
        return self.num_elements * self.n_cf_agent_samples

    @classmethod
    def from_merge(cls, datasets: Sequence["Dataset"]) -> torch.utils.data.Dataset:
        for d in datasets:
            if d.n_cf_agent_samples != 1:
                raise NotImplementedError()
            if not d._preprocessed:
                raise NotImplementedError()

        merged = Dataset(
            game_ids=[x for d in datasets for x in d.game_ids],
            data_dir=None,
            game_metadata=None,
            only_with_min_final_score=None,
            num_dataloader_workers=None,
            value_decay_alpha=None,
        )

        game_offsets = torch.from_numpy(np.cumsum([0] + [d.num_games for d in datasets[:-1]]))
        phase_offsets = torch.from_numpy(np.cumsum([0] + [d.num_phases for d in datasets[:-1]]))

        merged.game_idxs = torch.cat([d.game_idxs + off for d, off in zip(datasets, game_offsets)])
        merged.phase_idxs = torch.cat([d.phase_idxs for d in datasets])
        merged.power_idxs = torch.cat([d.power_idxs for d in datasets])
        merged.x_idxs = torch.cat([d.x_idxs + off for d, off in zip(datasets, phase_offsets)])
        merged.encoded_games = DataFields.cat([d.encoded_games for d in datasets])
        merged.num_games = sum(d.num_games for d in datasets)
        merged.num_phases = sum(d.num_phases for d in datasets)
        merged.num_elements = sum(d.num_elements for d in datasets)
        merged._preprocessed = True

        return merged


def encode_game(
    game_id: Union[int, str],
    data_dir: str,
    only_with_min_final_score=7,
    *,
    cf_agent=None,
    n_cf_agent_samples=1,
    input_valid_power_idxs,
    value_decay_alpha,
    game_metadata,
    exclude_n_holds,
):
    """
    Arguments:
    - game: Game object
    - only_with_min_final_score: if specified, only encode for powers who
      finish the game with some # of supply centers (i.e. only learn from
      winners). MILA uses 7.
    - input_valid_power_idxs: bool tensor, true if power should a priori be included in
      the dataset based on e.g. player rating)
    Return: game_id, DataFields dict of tensors:
    L is game length, P is # of powers above min_final_score, N is n_cf_agent_samples
    - board_state: shape=(L, 81, 35)
    - prev_state: shape=(L, 81, 35)
    - prev_orders: shape=(L, 2, 100), dtype=long
    - power: shape=(L, 7, 7)
    - season: shape=(L, 3)
    - in_adj_phase: shape=(L, 1)
    - build_numbers: shape=(L, 7)
    - final_scores: shape=(L, 7)
    - possible_actions: TensorList shape=(L x 7, 17 x 469)
    - loc_idxs: shape=(L, 7, 81), int8
    - actions: shape=(L, 7, N, 17) int order idxs, N=n_cf_agent_samples
    - valid_power_idxs: shape=(L, 7) bool mask of valid powers at each phase
    """

    torch.set_num_threads(1)
    encoder = FeatureEncoder()

    if isinstance(game_id, str):
        game_path = game_id
    else:  # Hacky fix to handle game_ids that are paths.
        game_path = os.path.join(f"{data_dir}", f"game_{game_id}.json")

    try:
        with open(game_path) as f:
            game = Game.from_json(f.read())
    except (FileNotFoundError, json.decoder.JSONDecodeError) as e:
        print(f"Error while loading game at {game_path}: {e}")
        return None, None

    num_phases = len(game.get_phase_history())
    logging.info(f"Encoding {game.game_id} with {num_phases} phases")

    phase_encodings = [
        encode_phase(
            encoder,
            game,
            game_id,
            phase_idx,
            only_with_min_final_score=only_with_min_final_score,
            cf_agent=cf_agent,
            n_cf_agent_samples=n_cf_agent_samples,
            value_decay_alpha=value_decay_alpha,
            input_valid_power_idxs=input_valid_power_idxs,
            exclude_n_holds=exclude_n_holds,
        )
        for phase_idx in range(num_phases)
    ]

    stacked_encodings = DataFields.cat(phase_encodings)

    return game_id, stacked_encodings.to_storage_fmt_()


def encode_phase(
    encoder: FeatureEncoder,
    game: Game,
    game_id: str,
    phase_idx: int,
    *,
    only_with_min_final_score: Optional[int],
    cf_agent=None,
    n_cf_agent_samples=1,
    value_decay_alpha,
    input_valid_power_idxs,
    exclude_n_holds,
):
    """
    Arguments:
    - game: Game object
    - game_id: unique id for game
    - phase_idx: int, the index of the phase to encode
    - only_with_min_final_score: if specified, only encode for powers who
      finish the game with some # of supply centers (i.e. only learn from
      winners). MILA uses 7.

    Returns: DataFields, including y_actions and y_final_score
    """
    phase = game.get_phase_history()[phase_idx]
    rolled_back_game = game.rolled_back_to_phase_start(phase.name)
    data_fields = encoder.encode_inputs([rolled_back_game])

    # encode final scores
    y_final_scores = encode_weighted_sos_scores(game, phase_idx, value_decay_alpha)

    # encode actions
    valid_power_idxs = torch.tensor(input_valid_power_idxs, dtype=torch.bool)
    # print('valid_power_idxs', valid_power_idxs)
    y_actions_lst = []
    power_orders_samples = (
        {power: [phase.orders.get(power, [])] for power in POWERS}
        if cf_agent is None
        else get_cf_agent_order_samples(rolled_back_game, phase.name, cf_agent, n_cf_agent_samples)
    )
    for power_i, power in enumerate(POWERS):
        orders_samples = power_orders_samples[power]
        if len(orders_samples) == 0:
            valid_power_idxs[power_i] = False
            y_actions_lst.append(
                torch.empty(n_cf_agent_samples, MAX_SEQ_LEN, dtype=torch.int32).fill_(EOS_IDX)
            )
            continue
        encoded_power_actions_lst = []
        for orders in orders_samples:
            encoded_power_actions, valid = encode_power_actions(
                orders, data_fields["x_possible_actions"][0, power_i]
            )
            encoded_power_actions_lst.append(encoded_power_actions)
            if 0 <= exclude_n_holds <= len(orders):
                if all(o.endswith(" H") for o in orders):
                    valid = 0
            valid_power_idxs[power_i] &= valid
        y_actions_lst.append(torch.stack(encoded_power_actions_lst, dim=0))  # [N, 17]

    y_actions = torch.stack(y_actions_lst, dim=0)  # [7, N, 17]

    # filter away powers that have no orders
    valid_power_idxs &= (y_actions != EOS_IDX).any(dim=2).all(dim=1)
    assert valid_power_idxs.ndimension() == 1

    # Maybe filter away powers that don't finish with enough SC.
    # If all players finish with fewer SC, include everybody.
    # cf. get_top_victors() in mila's state_space.py
    if only_with_min_final_score is not None:
        final_score = {k: len(v) for k, v in game.get_state()["centers"].items()}
        if max(final_score.values()) >= only_with_min_final_score:
            for i, power in enumerate(POWERS):
                if final_score.get(power, 0) < only_with_min_final_score:
                    valid_power_idxs[i] = 0

    data_fields["y_final_scores"] = y_final_scores.unsqueeze(0)
    data_fields["y_actions"] = y_actions.unsqueeze(0)
    data_fields["valid_power_idxs"] = valid_power_idxs.unsqueeze(0)
    data_fields["x_possible_actions"] = TensorList.from_padded(
        data_fields["x_possible_actions"].view(len(POWERS) * MAX_SEQ_LEN, MAX_VALID_LEN),
        padding_value=EOS_IDX,
    )

    return data_fields


def get_valid_orders_impl(power, all_possible_orders, all_orderable_locations, game_state):
    """Return a list of valid orders

    Returns:
    - a [1, 17, 469] int tensor of valid move indexes (padded with EOS_IDX)
    - a [1, 81] int8 tensor of orderable locs, described below
    - the actual length of the sequence == the number of orders to submit, <= 17

    loc_idxs:
    - not adj phase: X[i] = s if LOCS[i] is orderable at step s (0 <= s < 17), -1 otherwise
    - in adj phase: X[i] = -2 if LOCS[i] is orderable this phase, -1 otherwise
    """
    all_order_idxs = torch.empty(1, MAX_SEQ_LEN, MAX_VALID_LEN, dtype=torch.int32).fill_(EOS_IDX)
    loc_idxs = torch.empty(1, len(LOCS), dtype=torch.int8).fill_(-1)

    if power not in all_orderable_locations:
        return all_order_idxs, loc_idxs, 0

    # strip "WAIVE" from possible orders
    all_possible_orders = {
        k: [x for x in v if x != "WAIVE"] for k, v in all_possible_orders.items()
    }

    # sort by index in LOCS using the right coastal variant!
    # all_orderable_locations may give the root loc even if the possible orders
    # are from a coastal fleet
    orderable_locs = sorted(
        all_orderable_locations[power],
        key=lambda loc: LOCS.index(all_possible_orders[loc][0].split()[1]),
    )

    power_possible_orders = [x for loc in orderable_locs for x in all_possible_orders[loc]]
    n_builds = game_state["builds"][power]["count"]

    if n_builds > 0:
        # build phase: represented as a single ;-separated string combining all
        # units to be built.
        orders = [
            ";".join(sorted(x))
            for c in combinations([all_possible_orders[loc] for loc in orderable_locs], n_builds)
            for x in product(*c)
        ]
        order_idxs = torch.tensor([ORDER_VOCABULARY_TO_IDX[x] for x in orders], dtype=torch.int32)
        all_order_idxs[0, :1, : len(order_idxs)] = order_idxs.sort().values.unsqueeze(0)
        loc_idxs[0, [LOCS.index(l) for l in orderable_locs]] = -2
        return all_order_idxs, loc_idxs, n_builds

    if n_builds < 0:
        # disbands: all possible disband orders, up to the number of required disbands
        n_disbands = -n_builds
        _, order_idxs = filter_orders_in_vocab(power_possible_orders)
        all_order_idxs[0, :n_disbands, : len(order_idxs)] = order_idxs.sort().values.unsqueeze(0)
        loc_idxs[0, [LOCS.index(l) for l in orderable_locs]] = -2
        return all_order_idxs, loc_idxs, n_disbands

    # move phase: iterate through orderable_locs in topo order
    for i, loc in enumerate(orderable_locs):
        orders, order_idxs = filter_orders_in_vocab(all_possible_orders[loc])
        all_order_idxs[0, i, : len(order_idxs)] = order_idxs.sort().values
        loc_idxs[0, LOCS.index(loc)] = i

    return all_order_idxs, loc_idxs, len(orderable_locs)


def get_cf_agent_order_samples(game, phase_name, cf_agent, n_cf_agent_samples):
    assert game.get_state()["name"] == phase_name, f"{game.get_state()['name']} != {phase_name}"

    if hasattr(cf_agent, "get_all_power_prob_distributions"):
        power_action_ps = cf_agent.get_all_power_prob_distributions(game)
        logging.info(f"get_all_power_prob_distributions: {power_action_ps}")
        return {
            power: (
                [sample_p_dict(power_action_ps[power]) for _ in range(n_cf_agent_samples)]
                if power_action_ps[power]
                else []
            )
            for power in POWERS
        }
    else:
        return {
            power: [cf_agent.get_orders(game, power) for _ in range(n_cf_agent_samples)]
            for power in POWERS
        }


def encode_power_actions(orders: List[str], x_possible_actions) -> Tuple[torch.LongTensor, bool]:
    """
    Arguments:
    - a list of orders, e.g. ["F APU - ION", "A NAP H"]
    - x_possible_actions, a LongTensor of valid actions for this power-phase, shape=[17, 469]

    Returns a tuple:
    - MAX_SEQ_LEN-len 1d-tensor, pad=EOS_IDX
    - True/False is valid
    """
    y_actions = torch.empty(MAX_SEQ_LEN, dtype=torch.int32).fill_(EOS_IDX)
    order_idxs = []

    if any(len(order.split()) < 3 for order in orders):
        # skip over power with unparseably short order
        return y_actions, False
    elif any(order.split()[2] == "B" for order in orders):
        # builds are represented as a single ;-separated order
        assert all(order.split()[2] == "B" for order in orders), orders
        order = ";".join(sorted(orders))
        try:
            order_idx = ORDER_VOCABULARY_TO_IDX[order]
        except KeyError:
            logging.warning(f"Invalid build order: {order}")
            return y_actions, False
        order_idxs.append(order_idx)
    else:
        for order in orders:
            try:
                order_idxs.append(smarter_order_index(order))
            except KeyError:
                # skip over invalid orders; we may fill them in later
                continue

    # sort by topo order
    order_idxs.sort(key=lambda idx: LOCS.index(ORDER_VOCABULARY[idx].split()[1]))
    for i, order_idx in enumerate(order_idxs):
        try:
            cand_idx = (x_possible_actions[i] == order_idx).nonzero()[0, 0]
            y_actions[i] = cand_idx
        except IndexError:
            # filter away powers whose orders are not in valid_orders
            # most common reasons why this happens:
            # - actual garbage orders (e.g. moves between non-adjacent locations)
            # - too many orders (e.g. three build orders with only two allowed builds)
            return y_actions, False

    return y_actions, True


def filter_orders_in_vocab(orders):
    """Return the subset of orders that are found in the vocab, and their idxs"""
    ret, idxs = [], []
    for order in orders:
        try:
            idx = smarter_order_index(order)
            ret.append(order)
            idxs.append(idx)
        except KeyError:
            continue
    return ret, torch.tensor(idxs, dtype=torch.int32)


def smarter_order_index(order):
    try:
        return ORDER_VOCABULARY_TO_IDX[order]
    except KeyError:
        for suffix in ["/NC", "/EC", "/SC", "/WC"]:
            order = order.replace(suffix, "")
        return ORDER_VOCABULARY_TO_IDX[order]


def encode_weighted_sos_scores(game, phase_idx, value_decay_alpha):
    y_final_scores = torch.zeros(1, 7, dtype=torch.float)
    phases = game.get_phase_history()

    if phase_idx == len(phases) - 1:
        # end of game
        phase = phases[phase_idx]
        y_final_scores[0, :] = torch.FloatTensor(
            [
                compute_game_scores_from_state(p, phase.state).square_score
                for p in range(len(POWERS))
            ]
        )
        return y_final_scores

    # only weight scores at end of year, noting that not all years have a
    # winter adjustment phase
    end_of_year_phases = {}
    for phase in phases:
        end_of_year_phases[int(phase.name[1:-1])] = phase.name
    end_of_year_phases = set(end_of_year_phases.values())

    remaining = 1.0
    weight = 1.0 - value_decay_alpha
    for phase in phases[phase_idx + 1 :]:
        if phase.name not in end_of_year_phases:
            continue

        # calculate sos score at this phase
        sq_scores = torch.FloatTensor(
            [
                compute_game_scores_from_state(p, phase.state).square_score
                for p in range(len(POWERS))
            ]
        )

        # accumulate exp. weighted average
        y_final_scores[0, :] += weight * sq_scores
        remaining -= weight
        weight *= value_decay_alpha

    # fill in remaining weight with final score
    final_sq_scores = torch.FloatTensor(game.get_square_scores())
    y_final_scores[0, :] += remaining * final_sq_scores

    return y_final_scores
