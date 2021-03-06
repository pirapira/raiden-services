from typing import Any, Dict, List, Tuple

import networkx as nx
import structlog
from eth_utils import is_checksum_address
from networkx import DiGraph

from pathfinding_service.config import (
    DEFAULT_SETTLE_TO_REVEAL_TIMEOUT_RATIO,
    DIVERSITY_PEN_DEFAULT,
    MAX_PATHS_PER_REQUEST,
)
from pathfinding_service.model import ChannelView
from raiden_libs.types import Address, ChannelIdentifier

log = structlog.get_logger(__name__)


class TokenNetwork:
    """ Manages a token network for pathfinding. """

    def __init__(self, token_network_address: Address, token_address: Address):
        """ Initializes a new TokenNetwork. """

        self.address = token_network_address
        self.token_address = token_address
        self.channel_id_to_addresses: Dict[ChannelIdentifier, Tuple[Address, Address]] = dict()
        self.G = DiGraph()
        self.max_relative_fee = 0

    def __repr__(self):
        return f'<TokenNetwork address = {self.address} ' \
            f'num_channels = {len(self.channel_id_to_addresses)}>'
    #
    # Contract event listener functions
    #

    def handle_channel_opened_event(
        self,
        channel_identifier: ChannelIdentifier,
        participant1: Address,
        participant2: Address,
        settle_timeout: int,
    ):
        """ Register the channel in the graph, add participents to graph if necessary.

        Corresponds to the ChannelOpened event. Called by the contract event listener. """

        assert is_checksum_address(participant1)
        assert is_checksum_address(participant2)

        self.channel_id_to_addresses[channel_identifier] = (participant1, participant2)

        view1 = ChannelView(
            channel_id=channel_identifier,
            participant1=participant1,
            participant2=participant2,
            settle_timeout=settle_timeout,
            deposit=0,
        )

        view2 = ChannelView(
            channel_id=channel_identifier,
            participant2=participant2,
            participant1=participant1,
            settle_timeout=settle_timeout,
            deposit=0,
        )

        self.G.add_edge(participant1, participant2, view=view1)
        self.G.add_edge(participant2, participant1, view=view2)

    def handle_channel_new_deposit_event(
        self,
        channel_identifier: ChannelIdentifier,
        receiver: Address,
        total_deposit: int,
    ):
        """ Register a new balance for the beneficiary.

        Corresponds to the ChannelNewDeposit event. Called by the contract event listener. """

        assert is_checksum_address(receiver)

        try:
            participant1, participant2 = self.channel_id_to_addresses[channel_identifier]

            if receiver == participant1:
                self.G[participant1][participant2]['view'].update_capacity(deposit=total_deposit)
            elif receiver == participant2:
                self.G[participant2][participant1]['view'].update_capacity(deposit=total_deposit)
            else:
                log.error(
                    "Receiver in ChannelNewDeposit does not fit the internal channel",
                )
        except KeyError:
            log.error(
                "Received ChannelNewDeposit event for unknown channel",
                channel_identifier=channel_identifier,
            )

    def handle_channel_closed_event(self, channel_identifier: ChannelIdentifier):
        """ Close a channel. This doesn't mean that the channel is settled yet, but it cannot
        transfer any more.

        Corresponds to the ChannelClosed event. Called by the contract event listener. """

        try:
            # we need to unregister the channel_id here
            participant1, participant2 = self.channel_id_to_addresses.pop(channel_identifier)

            self.G.remove_edge(participant1, participant2)
            self.G.remove_edge(participant2, participant1)
        except KeyError:
            log.error(
                "Received ChannelClosed event for unknown channel",
                channel_identifier=channel_identifier,
            )

    def get_channel_views_for_partner(
            self,
            channel_identifier: ChannelIdentifier,
            updating_participant: Address,
            other_participant: Address,
    ) -> Tuple[ChannelView, ChannelView]:

        # Get the channel views from the perspective of the updating participant
        channel_view_to_partner = self.G[updating_participant][other_participant]['view']
        channel_view_from_partner = self.G[other_participant][updating_participant]['view']

        return channel_view_to_partner, channel_view_from_partner

    def handle_channel_balance_update_message(
        self,
        channel_identifier: ChannelIdentifier,
        updating_participant: Address,
        other_participant: Address,
        updating_nonce: int,
        other_nonce: int,
        updating_capacity: int,
        other_capacity: int,
        reveal_timeout: int,
    ):
        """ Sends Capacity Update to PFS including the reveal timeout """
        channel_view_to_partner, channel_view_from_partner = self.get_channel_views_for_partner(
            channel_identifier=channel_identifier,
            updating_participant=updating_participant,
            other_participant=other_participant,
        )
        # FIXME: Add updating only minimum if capacity updates conflict
        channel_view_to_partner.update_capacity(
            nonce=updating_nonce,
            capacity=updating_capacity,
            reveal_timeout=reveal_timeout,
        )
        channel_view_from_partner.update_capacity(
            nonce=other_nonce,
            capacity=other_capacity,
        )

    @staticmethod
    def edge_weight(
        visited: Dict[ChannelIdentifier, float],
        attr: Dict[str, Any],
    ):
        view: ChannelView = attr['view']
        return 1 + visited.get(
            view.channel_id,
            0,
        )

    def check_path_constraints(
        self,
        value: int,
        path: List,
    ) -> bool:
        for node1, node2 in zip(path[:-1], path[1:]):
            channel: ChannelView = self.G[node1][node2]['view']
            # check if available balance > value
            if value > channel.capacity:
                return False
            # check if settle_timeout / reveal_timeout >= default ratio
            ratio = channel.settle_timeout / channel.reveal_timeout
            if ratio < DEFAULT_SETTLE_TO_REVEAL_TIMEOUT_RATIO:
                return False
        return True

    def get_paths(
        self,
        source: Address,
        target: Address,
        value: int,
        max_paths: int,
        diversity_penalty: float = DIVERSITY_PEN_DEFAULT,
        hop_bias: float = 1,
        **kwargs,
    ):
        assert hop_bias == 1, 'Only hop_bias 1 is supported'
        max_paths = min(max_paths, MAX_PATHS_PER_REQUEST)
        visited: Dict[ChannelIdentifier, float] = {}
        paths: List[List[Address]] = []

        for _ in range(max_paths):
            # update edge weights
            for node1, node2 in self.G.edges():
                edge = self.G[node1][node2]
                edge['weight'] = self.edge_weight(visited, edge)

            # find next path
            all_paths = nx.shortest_simple_paths(self.G, source, target, weight='weight')
            try:
                # skip duplicates and invalid paths
                path = next(
                    path for path in all_paths
                    if self.check_path_constraints(value, path) and path not in paths
                )
            except StopIteration:
                break
            # update visited penalty dict
            for node1, node2 in zip(path[:-1], path[1:]):
                channel_id = self.G[node1][node2]['view'].channel_id
                visited[channel_id] = visited.get(channel_id, 0) + diversity_penalty

            paths.append(path)
            if len(paths) >= max_paths:
                break
        result = []

        for path in paths:
            fee = 0
            for node1, node2 in zip(path[:-1], path[1:]):
                fee += self.G[node1][node2]['view'].relative_fee

            result.append(dict(
                path=path,
                estimated_fee=0,
            ))
        return result
