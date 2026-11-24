from typing import List

from .model import (
    ArbitrationResult,
    BlockReason,
    CandidateVerdict,
    ClusterState,
    WitnessStatus,
)


def majority(voter_count: int) -> int:
    """严格多数：3 个投票成员时阈值为 2。"""
    return voter_count // 2 + 1


def _global_fencing_blockers(state: ClusterState) -> List[str]:
    """
    对所有候选都生效的 fencing 前置条件。

    旧主仍然存活且与副本可达时，必须先 fencing 才能提升；
    新一轮 fencing token 必须严格大于旧主持有的 token。
    """
    blockers: List[str] = []
    primary_reachable = any(
        state.network.can_communicate(state.primary.name, replica.name)
        for replica in state.replicas
    )
    if state.primary.alive and primary_reachable:
        blockers.append(BlockReason.OLD_PRIMARY_ALIVE)
    if state.new_fencing_token <= state.old_fencing_token:
        blockers.append(BlockReason.FENCING_TOKEN_NOT_ADVANCED)
    return blockers


def _reachable_voters(state: ClusterState, candidate_name: str) -> List[str]:
    """
    候选在自己的连通分区内能拿到的票。

    候选自己算一票；witness 健康且可达时投一票（仅 quorum 票，不带数据）；
    另一个副本存活且可达时投一票。
    """
    voters = [candidate_name]
    if (
        state.witness.status == WitnessStatus.HEALTHY
        and state.network.can_communicate(candidate_name, state.witness.name)
    ):
        voters.append(state.witness.name)
    for replica in state.replicas:
        if replica.name == candidate_name or not replica.alive:
            continue
        if state.network.can_communicate(candidate_name, replica.name):
            voters.append(replica.name)
    return voters


def _data_safety_blockers(state: ClusterState, candidate) -> List[str]:
    """
    数据安全门槛，witness 不参与这一步：
    时间线必须与已确认写入一致，日志必须覆盖全部已确认写入，
    新 fencing token 必须领先候选当前 epoch。
    """
    blockers: List[str] = []
    if candidate.timeline_id != state.confirmed_timeline:
        blockers.append(BlockReason.TIMELINE_DIVERGED)
    if candidate.log_pos < state.confirmed_pos:
        blockers.append(BlockReason.DATA_LAGS_CONFIRMED)
    if state.new_fencing_token <= candidate.epoch:
        blockers.append(BlockReason.EPOCH_BEHIND)
    return blockers


def arbitrate(state: ClusterState) -> ArbitrationResult:
    """
    判断当前集群状态下哪些副本可以被提升为主。

    返回候选顺序（按日志新旧、epoch、连通票数排序）、
    每个候选的 quorum 证据以及全部阻止原因。
    """
    voter_names = state.voter_names()
    threshold = majority(len(voter_names))
    global_blockers = _global_fencing_blockers(state)

    verdicts: List[CandidateVerdict] = []
    for candidate in state.replicas:
        evidence = _reachable_voters(state, candidate.name)
        reasons = list(global_blockers)
        if len(evidence) < threshold:
            reasons.append(BlockReason.NO_QUORUM)
        reasons.extend(_data_safety_blockers(state, candidate))
        verdicts.append(
            CandidateVerdict(
                name=candidate.name,
                log_pos=candidate.log_pos,
                timeline_id=candidate.timeline_id,
                epoch=candidate.epoch,
                eligible=not reasons,
                reachable_voter_count=len(evidence),
                quorum_evidence=evidence,
                block_reasons=reasons,
            )
        )

    eligible = [verdict for verdict in verdicts if verdict.eligible]
    eligible.sort(
        key=lambda verdict: (
            -verdict.log_pos,
            -verdict.epoch,
            -verdict.reachable_voter_count,
            verdict.name,
        )
    )
    return ArbitrationResult(
        quorum_threshold=threshold,
        voter_names=voter_names,
        global_blockers=global_blockers,
        candidates=verdicts,
        eligible_order=[verdict.name for verdict in eligible],
    )


def check_old_primary(state: ClusterState) -> dict:
    """
    旧主恢复后能否继续对外提供写服务。

    旧主必须先拿到新一轮 fencing token：它手里的旧 token 一旦落后，
    即使重新连通也必须拒绝写入，防止脑裂时期的旧写入生效。
    """
    if not state.primary.alive:
        return {
            "node": state.primary.name,
            "may_serve_writes": False,
            "held_token": state.old_fencing_token,
            "current_token": state.new_fencing_token,
            "reasons": ["old_primary_down"],
        }

    evidence = _reachable_voters(state, state.primary.name)
    reasons: List[str] = []
    if len(evidence) < majority(len(state.voter_names())):
        reasons.append(BlockReason.NO_QUORUM)
    if state.old_fencing_token < state.new_fencing_token:
        reasons.append(BlockReason.STALE_FENCING_TOKEN)
    return {
        "node": state.primary.name,
        "may_serve_writes": not reasons,
        "held_token": state.old_fencing_token,
        "current_token": state.new_fencing_token,
        "quorum_evidence": evidence,
        "reasons": reasons,
    }
