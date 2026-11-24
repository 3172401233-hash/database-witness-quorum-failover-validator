import unittest

from quorum_failover import (
    BlockReason,
    ClusterState,
    Network,
    Node,
    Witness,
    WitnessStatus,
    arbitrate,
    check_old_primary,
    majority,
)

PRIMARY = "p1"
REPLICA_A = "r1"
REPLICA_B = "r2"
WITNESS = "w1"


def build_state(
    log_a=100,
    log_b=95,
    timeline_a="tl1",
    timeline_b="tl1",
    epoch_a=4,
    epoch_b=4,
    witness_status=WitnessStatus.HEALTHY,
    primary_alive=False,
    confirmed_pos=95,
    confirmed_timeline="tl1",
    old_token=4,
    new_token=5,
    groups=None,
):
    if groups is None:
        groups = [frozenset({PRIMARY, REPLICA_A, REPLICA_B, WITNESS})]
    return ClusterState(
        primary=Node(PRIMARY, log_pos=110, timeline_id="tl1", epoch=4,
                     alive=primary_alive),
        replicas=[
            Node(REPLICA_A, log_pos=log_a, timeline_id=timeline_a, epoch=epoch_a),
            Node(REPLICA_B, log_pos=log_b, timeline_id=timeline_b, epoch=epoch_b),
        ],
        witness=Witness(WITNESS, witness_status),
        network=Network.from_groups(*groups),
        confirmed_pos=confirmed_pos,
        confirmed_timeline=confirmed_timeline,
        old_fencing_token=old_token,
        new_fencing_token=new_token,
    )


def set_partitions(elements):
    """枚举一个集合的全部集合划分（Bell 数），四节点共 15 种。"""
    elements = list(elements)

    def recurse(remaining):
        if not remaining:
            yield ()
            return
        head = remaining[0]
        for tail in recurse(remaining[1:]):
            yield tail + (frozenset({head}),)
            for index, group in enumerate(tail):
                merged = list(tail)
                merged[index] = group | {head}
                yield tuple(merged)

    return list(recurse(elements))


class QuorumBasicsTest(unittest.TestCase):
    def test_majority_of_three_voters_is_two(self):
        self.assertEqual(majority(3), 2)

    def test_healthy_failed_over_cluster_prefers_freshest_replica(self):
        state = build_state()
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [REPLICA_A, REPLICA_B])
        verdict_a, verdict_b = result.candidates
        self.assertTrue(verdict_a.eligible)
        self.assertIn(WITNESS, verdict_a.quorum_evidence)
        self.assertIn(REPLICA_B, verdict_a.quorum_evidence)
        self.assertEqual(result.witness_role, "quorum_only_witness_holds_no_data")


class AllFourNodePartitionCombinationsTest(unittest.TestCase):
    def test_every_partition_topology_respects_majority_and_fencing(self):
        nodes = [PRIMARY, REPLICA_A, REPLICA_B, WITNESS]
        topologies = set_partitions(nodes)
        self.assertEqual(len(topologies), 15)

        for groups in topologies:
            state = build_state(groups=list(groups))
            result = arbitrate(state)
            self.assertEqual(result.quorum_threshold, 2)

            for verdict in result.candidates:
                has_peer = any(
                    voter != verdict.name for voter in verdict.quorum_evidence
                )
                if not has_peer:
                    self.assertIn(BlockReason.NO_QUORUM, verdict.block_reasons)
                    self.assertFalse(verdict.eligible)
                else:
                    self.assertGreaterEqual(
                        verdict.reachable_voter_count, result.quorum_threshold
                    )
                    self.assertNotIn(BlockReason.NO_QUORUM, verdict.block_reasons)

            eligible_names = set(result.eligible_order)
            for verdict in result.candidates:
                self.assertEqual(verdict.eligible, verdict.name in eligible_names)

    def test_reachable_subset_combinations_match_expected_majorities(self):
        scenarios = [
            ([frozenset({REPLICA_A, WITNESS}), frozenset({REPLICA_B}),
              frozenset({PRIMARY})], {REPLICA_A}, {REPLICA_B}),
            ([frozenset({REPLICA_B, WITNESS}), frozenset({REPLICA_A}),
              frozenset({PRIMARY})], {REPLICA_B}, {REPLICA_A}),
            ([frozenset({REPLICA_A, REPLICA_B}), frozenset({WITNESS}),
              frozenset({PRIMARY})], {REPLICA_A, REPLICA_B}, set()),
            ([frozenset({REPLICA_A}), frozenset({REPLICA_B}),
              frozenset({WITNESS, PRIMARY})], set(), {REPLICA_A, REPLICA_B}),
        ]
        for groups, promotable, isolated in scenarios:
            with self.subTest(groups=groups):
                result = arbitrate(build_state(groups=groups))
                self.assertEqual(set(result.eligible_order), promotable)
                for verdict in result.candidates:
                    if verdict.name in isolated:
                        self.assertIn(BlockReason.NO_QUORUM,
                                      verdict.block_reasons)


class WitnessPartitionTest(unittest.TestCase):
    def test_witness_partition_still_allows_replica_pair_quorum(self):
        state = build_state(
            groups=[
                frozenset({REPLICA_A, REPLICA_B}),
                frozenset({WITNESS}),
                frozenset({PRIMARY}),
            ]
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [REPLICA_A, REPLICA_B])
        for verdict in result.candidates:
            self.assertNotIn(WITNESS, verdict.quorum_evidence)
            self.assertGreaterEqual(verdict.reachable_voter_count,
                                    result.quorum_threshold)

    def test_witness_down_breaks_replica_plus_witness_majority(self):
        state = build_state(
            witness_status=WitnessStatus.DOWN,
            groups=[
                frozenset({REPLICA_A, WITNESS}),
                frozenset({REPLICA_B}),
                frozenset({PRIMARY}),
            ],
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [])
        verdict_a = result.candidates[0]
        self.assertEqual(verdict_a.quorum_evidence, [REPLICA_A])
        self.assertIn(BlockReason.NO_QUORUM, verdict_a.block_reasons)


class HeadlineSafetyScenarioTest(unittest.TestCase):
    def test_less_current_but_healthy_link_replica_wins_over_isolated_fresher(self):
        # r1 日志更多但被孤立；r2 与 witness 同分区，只有 r2 能组成多数。
        state = build_state(
            log_a=120,
            log_b=95,
            confirmed_pos=95,
            groups=[
                frozenset({REPLICA_A}),
                frozenset({REPLICA_B, WITNESS}),
                frozenset({PRIMARY}),
            ],
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [REPLICA_B])
        verdict_a, verdict_b = result.candidates
        self.assertIn(BlockReason.NO_QUORUM, verdict_a.block_reasons)
        self.assertFalse(verdict_a.eligible)
        self.assertEqual(verdict_b.quorum_evidence, [REPLICA_B, WITNESS])
        self.assertEqual(verdict_b.block_reasons, [])

    def test_witness_alone_cannot_promote_isolated_replica(self):
        state = build_state(
            groups=[
                frozenset({REPLICA_A, WITNESS}),
                frozenset({REPLICA_B, PRIMARY}),
            ]
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [REPLICA_A])
        verdict_b = result.candidates[1]
        # r2 只能与旧主通信，旧主在故障切换选举中不投票，因此 r2 不足多数。
        self.assertEqual(verdict_b.quorum_evidence, [REPLICA_B])
        self.assertIn(BlockReason.NO_QUORUM, verdict_b.block_reasons)


class OldPrimaryRecoveryTest(unittest.TestCase):
    def test_live_reachable_old_primary_blocks_promotion_until_fenced(self):
        state = build_state(
            primary_alive=True,
            groups=[frozenset({PRIMARY, REPLICA_A, REPLICA_B, WITNESS})],
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [])
        self.assertIn(BlockReason.OLD_PRIMARY_ALIVE, result.global_blockers)
        for verdict in result.candidates:
            self.assertIn(BlockReason.OLD_PRIMARY_ALIVE,
                          verdict.block_reasons)

    def test_token_must_be_advanced_before_any_promotion(self):
        blocked = arbitrate(build_state(new_token=4))
        self.assertEqual(blocked.eligible_order, [])
        self.assertIn(BlockReason.FENCING_TOKEN_NOT_ADVANCED,
                      blocked.global_blockers)

        ok = arbitrate(build_state(new_token=6))
        self.assertEqual(ok.eligible_order, [REPLICA_A, REPLICA_B])

    def test_recovered_old_primary_is_rejected_with_stale_token(self):
        state = build_state(
            primary_alive=True,
            groups=[frozenset({PRIMARY, REPLICA_A, REPLICA_B, WITNESS})],
        )
        decision = check_old_primary(state)
        self.assertFalse(decision["may_serve_writes"])
        self.assertIn(BlockReason.STALE_FENCING_TOKEN, decision["reasons"])
        self.assertEqual(decision["held_token"], 4)
        self.assertEqual(decision["current_token"], 5)


class TimelineForkTest(unittest.TestCase):
    def test_diverged_timeline_blocks_promotion_even_with_quorum(self):
        state = build_state(
            timeline_b="tl-fork",
            log_b=130,
            groups=[
                frozenset({REPLICA_B, WITNESS}),
                frozenset({REPLICA_A}),
                frozenset({PRIMARY}),
            ],
        )
        result = arbitrate(state)
        self.assertEqual(result.eligible_order, [])
        verdict_b = result.candidates[1]
        self.assertIn(BlockReason.TIMELINE_DIVERGED, verdict_b.block_reasons)
        self.assertGreaterEqual(verdict_b.reachable_voter_count,
                                result.quorum_threshold)

    def test_replica_missing_confirmed_writes_is_unsafe(self):
        state = build_state(log_b=80, confirmed_pos=95)
        result = arbitrate(state)
        verdict_b = result.candidates[1]
        self.assertIn(BlockReason.DATA_LAGS_CONFIRMED,
                      verdict_b.block_reasons)
        self.assertNotIn(REPLICA_B, result.eligible_order)

    def test_witness_is_never_a_candidate(self):
        result = arbitrate(build_state())
        self.assertNotIn(WITNESS, result.eligible_order)
        self.assertEqual(result.voter_names, [REPLICA_A, REPLICA_B, WITNESS])


if __name__ == "__main__":
    unittest.main()
