from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Tuple


class WitnessStatus(Enum):
    """Witness 自身健康状态，只有 HEALTHY 才能投出 quorum 票。"""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    DOWN = "down"


class BlockReason:
    """阻止某个副本（或整轮提升）的原因常量。"""

    OLD_PRIMARY_ALIVE = "old_primary_alive_not_fenced"
    FENCING_TOKEN_NOT_ADVANCED = "fencing_token_not_advanced"
    STALE_FENCING_TOKEN = "old_primary_holds_stale_token"
    NO_QUORUM = "no_majority_quorum"
    DATA_LAGS_CONFIRMED = "log_behind_confirmed_writes"
    TIMELINE_DIVERGED = "timeline_diverged_from_confirmed"
    EPOCH_BEHIND = "new_token_not_ahead_of_candidate_epoch"


@dataclass(frozen=True)
class Node:
    """一个存储数据的节点（旧主或副本）。Witness 不使用该结构。"""

    name: str
    log_pos: int
    timeline_id: str
    epoch: int
    alive: bool = True


@dataclass(frozen=True)
class Witness:
    """Witness 只有 quorum 投票权，没有日志、没有数据位置。"""

    name: str
    status: WitnessStatus = WitnessStatus.HEALTHY


@dataclass(frozen=True)
class Network:
    """
    网络分区视图。

    partitions 中的每个 frozenset 是一个可以互通的节点组，
    不同组之间完全不可达；未登记的节点按孤立处理。
    """

    partitions: Tuple[FrozenSet[str], ...]

    @staticmethod
    def from_groups(*groups: FrozenSet[str]) -> "Network":
        return Network(tuple(frozenset(group) for group in groups))

    def _group_of(self, name: str) -> FrozenSet[str]:
        for group in self.partitions:
            if name in group:
                return group
        return frozenset({name})

    def can_communicate(self, left: str, right: str) -> bool:
        if left == right:
            return True
        return self._group_of(left) == self._group_of(right)


@dataclass
class ClusterState:
    """一次仲裁判断所需的完整集群输入。"""

    primary: Node
    replicas: List[Node]
    witness: Witness
    network: Network
    confirmed_pos: int
    confirmed_timeline: str
    old_fencing_token: int
    new_fencing_token: int

    def voter_names(self) -> List[str]:
        """故障切换期间的投票成员：两个副本 + witness，共 3 票。"""
        return [replica.name for replica in self.replicas] + [self.witness.name]

    def all_node_names(self) -> List[str]:
        return [self.primary.name] + self.voter_names()


@dataclass
class CandidateVerdict:
    """单个副本的仲裁结论。"""

    name: str
    log_pos: int
    timeline_id: str
    epoch: int
    eligible: bool
    reachable_voter_count: int
    quorum_evidence: List[str] = field(default_factory=list)
    block_reasons: List[str] = field(default_factory=list)


@dataclass
class ArbitrationResult:
    """整轮仲裁的输出：候选顺序、quorum 证据、阻止原因。"""

    quorum_threshold: int
    voter_names: List[str]
    global_blockers: List[str]
    candidates: List[CandidateVerdict]
    eligible_order: List[str]
    witness_role: str = "quorum_only_witness_holds_no_data"
