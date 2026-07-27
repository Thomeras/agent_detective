"""The shared uuid5 identity derivation.

These properties are load-bearing beyond this package: ingest hashes run keys
into ``agent_runs.run_id`` while the local-mode CLI hashes the same keys into
an in-memory graph. If the two ever disagreed, a blame report produced one way
could not be compared with one produced the other — which is why the
derivation lives here, next to the keys it hashes, rather than in either
consumer.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from otel_mapper import graph_id_from_str, map_spans, run_id_from_key


class TestDetermerminism:
    def test_the_same_run_key_always_hashes_to_the_same_uuid(self):
        key = "a" * 32 + ":" + "b" * 16
        assert run_id_from_key(key) == run_id_from_key(key)

    def test_different_run_keys_hash_differently(self):
        assert run_id_from_key("trace:span1") != run_id_from_key("trace:span2")

    def test_the_same_graph_id_always_hashes_to_the_same_uuid(self):
        assert graph_id_from_str("corr-1") == graph_id_from_str("corr-1")

    def test_run_and_graph_derivations_do_not_collide_on_equal_input(self):
        # Both hash into the same namespace, so equal input MUST give equal
        # output — this pins that they are one derivation, not two that happen
        # to agree today.
        assert run_id_from_key("x") == graph_id_from_str("x")


class TestAlgorithm:
    def test_it_is_uuid5_over_the_url_namespace(self):
        # Pinned explicitly: changing the namespace or version would silently
        # renumber every run in every stored report.
        assert run_id_from_key("trace:span") == uuid5(NAMESPACE_URL, "trace:span")

    def test_the_result_is_a_version_5_uuid(self):
        assert run_id_from_key("trace:span").version == 5

    def test_unicode_keys_are_handled(self):
        assert isinstance(graph_id_from_str("běh-1"), UUID)


class TestAgainstTheMapper:
    def test_mapper_run_keys_hash_without_special_casing(self):
        # The mapper's own output is the only input these functions ever see.
        spans = [
            {
                "traceId": "1" * 32,
                "spanId": "2" * 16,
                "parentSpanId": "",
                "name": "agent.run",
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "2000000000",
                "attributes": [
                    {
                        "key": "openinference.span.kind",
                        "value": {"stringValue": "AGENT"},
                    },
                    {"key": "gen_ai.agent.name", "value": {"stringValue": "writer"}},
                ],
                "status": {"code": 1},
            }
        ]
        result = map_spans(spans)
        run = result.runs[0]
        assert run_id_from_key(run.run_key).version == 5
        assert graph_id_from_str(run.graph_id).version == 5
